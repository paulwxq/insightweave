"""仅供手工执行：删除配置的 Milvus 库，创建同名空库。

默认预览且不连接数据库。真正执行必须同时指定 --execute 和
--confirm-database <实际库名>。集合与索引由后续发布步骤创建。
"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys
from uuid import uuid4

from dotenv import load_dotenv
import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if __package__ in (None, ""):
    sys.path.insert(0, str(PROJECT_ROOT))

from services.config_loader import ConfigLoader  # noqa: E402
from services.vector_db.milvus_client import _lazy_import_milvus  # noqa: E402

ENV_FILE = PROJECT_ROOT / ".env"
CONFIG_FILE = PROJECT_ROOT / "configs" / "metadata_config.yaml"


def validate_target(config: dict) -> None:
    name = config.get("database")
    if (
        not isinstance(name, str)
        or not name.strip()
        or name != name.strip()
        or any(ord(char) < 32 or ord(char) == 127 for char in name)
    ):
        raise ValueError("Milvus 库名必须非空，且不能包含首尾空白或控制字符")
    if name.casefold() == "default":
        raise ValueError("不能删除 Milvus default 系统库，请配置独立的平台库")
    host = config.get("host")
    if (
        not isinstance(host, str)
        or not host.strip()
        or any(char.isspace() or char in "/@?#\\" for char in host)
    ):
        raise ValueError("Milvus host 必须是非空主机地址，不能包含协议或凭据")
    port = config.get("port")
    try:
        if (
            isinstance(port, bool)
            or not str(port).isdigit()
            or not 1 <= int(port) <= 65535
        ):
            raise ValueError
    except (ValueError, TypeError):
        raise ValueError("Milvus port 必须是 1–65535 的整数") from None


def load_target() -> dict:
    load_dotenv(ENV_FILE, override=False)
    raw = yaml.safe_load(CONFIG_FILE.read_text(encoding="utf-8"))
    try:
        vector = raw["vector_database"]
        selected = {"active": vector["active"], "milvus": vector["providers"]["milvus"]}
    except (KeyError, TypeError):
        raise ValueError("缺少 vector_database.providers.milvus 配置") from None
    config = ConfigLoader(str(CONFIG_FILE))._replace_env_vars(selected)
    if config["active"] != "milvus":
        raise ValueError("当前激活的向量存储不是 Milvus，拒绝重置")
    config = config["milvus"]
    if not isinstance(config, dict) or not config:
        raise ValueError("vector_database.providers.milvus 必须为非空配置对象")
    for key in ("host", "database"):
        if isinstance(config.get(key), str):
            config[key] = config[key].strip()
    if isinstance(config.get("host"), str):
        config["host"] = config["host"].lower()
    validate_target(config)
    return config


def reset_database(config: dict) -> None:
    validate_target(config)
    connections, db, _, _, Collection, _, utility = _lazy_import_milvus()
    # 独立 alias，不切换或关闭项目的 default/发布连接。
    alias = f"manual_reset_{uuid4().hex}"
    connections.connect(
        alias=alias,
        host=config["host"],
        port=str(config["port"]),
        user=config.get("user"),
        password=config.get("password"),
        timeout=config.get("timeout", 30),
    )
    try:
        database = config["database"]
        if database in db.list_database(using=alias):
            db.using_database(database, using=alias)
            # Milvus 只允许删除空库：先删除目标库的所有集合，包括非受管集合。
            for name in utility.list_collections(using=alias):
                Collection(name, using=alias).drop()
            db.using_database("default", using=alias)
            print("[1/2] 删除 Milvus 目标库")
            db.drop_database(database, using=alias)
        else:
            print("[1/2] Milvus 目标库不存在，跳过删除")
        print("[2/2] 创建同名空库（不创建集合）")
        db.create_database(database, using=alias)
        db.using_database(database, using=alias)
        if utility.list_collections(using=alias):
            raise RuntimeError("新库并非空库，核验未通过")
    finally:
        connections.disconnect(alias)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run", action="store_true", help="仅预览，不连接数据库（默认）"
    )
    mode.add_argument("--execute", action="store_true", help="真正执行重置")
    parser.add_argument("--confirm-database", help="执行时必须填写与配置完全一致的库名")
    args = parser.parse_args(argv)
    try:
        config = load_target()
    except (ValueError, OSError, yaml.YAMLError) as exc:
        if isinstance(exc, yaml.YAMLError):
            parser.error("metadata_config.yaml 格式不合法")
        parser.error(str(exc))

    print(f"Milvus 目标：{config['host']}:{config['port']}/{config['database']}")
    print("范围：删除该库全部集合和数据库，再创建同名空库；集合与索引由后续发布创建。")
    if args.execute:
        if args.confirm_database != config["database"]:
            parser.error("执行重置必须通过 --confirm-database 填写上方显示的完整库名")
    elif args.confirm_database is not None:
        parser.error("--confirm-database 必须与 --execute 一起使用")
    else:
        print("预览结束：未连接数据库，未执行任何删除或初始化。")
        return 0

    try:
        reset_database(config)
    except Exception as exc:
        print(
            f"Milvus 重置失败（{type(exc).__name__}），请核对目标库状态。",
            file=sys.stderr,
        )
        print("集合与数据库删除无法整体回滚，可能已完成部分删除。", file=sys.stderr)
        return 1
    print("Milvus 重置完成：同名空库已创建，集合和索引等待后续发布创建。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
