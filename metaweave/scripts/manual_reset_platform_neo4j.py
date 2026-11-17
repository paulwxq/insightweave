"""仅供手工执行：保留配置的 Neo4j 数据库，清空全部节点和关系。

默认预览且不连接数据库。真正执行必须同时指定 --execute 和
--confirm-database <实际库名>。保留已有约束与索引，不删除数据库本身。
"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

from dotenv import load_dotenv
from neo4j import GraphDatabase
import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if __package__ in (None, ""):
    sys.path.insert(0, str(PROJECT_ROOT))

from metaweave.core.clean_targets import normalize_neo4j_uri  # noqa: E402
from services.config_loader import ConfigLoader  # noqa: E402

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
        raise ValueError("Neo4j 库名必须非空，且不能包含首尾空白或控制字符")
    if name.casefold() == "system":
        raise ValueError("拒绝清理 Neo4j system 系统库")
    normalize_neo4j_uri(config.get("uri"))
    if not isinstance(config.get("user"), str) or not isinstance(
        config.get("password"), str
    ):
        raise ValueError("Neo4j user/password 配置必须为字符串")


def load_target() -> dict:
    load_dotenv(ENV_FILE, override=False)
    raw = yaml.safe_load(CONFIG_FILE.read_text(encoding="utf-8"))
    try:
        section = raw["loaders"]["cql_loader"]["neo4j"]
    except (KeyError, TypeError):
        raise ValueError("缺少 loaders.cql_loader.neo4j 配置") from None
    if not isinstance(section, dict) or not section:
        raise ValueError("loaders.cql_loader.neo4j 必须为非空配置对象")
    # 复用项目的环境变量替换规则，仅解析本存储配置，不要求 LLM 等无关密钥。
    config = ConfigLoader(str(CONFIG_FILE))._replace_env_vars(section)
    config["uri"] = normalize_neo4j_uri(config.get("uri"))
    config["database"] = config.get("database", "neo4j")
    if isinstance(config["database"], str):
        config["database"] = config["database"].strip()
    validate_target(config)
    return config


def reset_database(config: dict) -> None:
    validate_target(config)
    driver = GraphDatabase.driver(
        config["uri"],
        auth=(config["user"], config["password"]),
        connection_timeout=10,
    )
    try:
        with driver.session(database=config["database"]) as session:

            def clear_graph(tx):
                tx.run("MATCH (n) DETACH DELETE n").consume()
                for query in (
                    "MATCH (n) RETURN count(n) AS remaining",
                    "MATCH ()-[r]->() RETURN count(r) AS remaining",
                ):
                    row = tx.run(query).single()
                    if row is None or row["remaining"] != 0:
                        raise RuntimeError("图数据清理后核验未通过，回滚本次事务")

            session.execute_write(clear_graph)
    finally:
        driver.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run", action="store_true", help="仅预览，不连接数据库（默认）"
    )
    mode.add_argument("--execute", action="store_true", help="真正执行清理")
    parser.add_argument("--confirm-database", help="执行时必须填写与配置完全一致的库名")
    args = parser.parse_args(argv)
    try:
        config = load_target()
    except (ValueError, OSError, yaml.YAMLError) as exc:
        # YAML 错误可能包含带凭据的原文，不打印原始异常。
        if isinstance(exc, yaml.YAMLError):
            parser.error("metadata_config.yaml 格式不合法")
        parser.error(str(exc))

    print(f"Neo4j 目标：{config['uri']}，数据库 {config['database']}")
    print("范围：清空该库全部节点和关系；保留数据库、约束和索引。")
    if args.execute:
        if args.confirm_database != config["database"]:
            parser.error("执行清理必须通过 --confirm-database 填写上方显示的完整库名")
    elif args.confirm_database is not None:
        parser.error("--confirm-database 必须与 --execute 一起使用")
    else:
        print("预览结束：未连接数据库，未执行任何清理。")
        return 0

    try:
        reset_database(config)
    except Exception as exc:
        print(f"Neo4j 清理失败（{type(exc).__name__}），未确认完成。", file=sys.stderr)
        return 1
    print("Neo4j 清理完成：节点和关系均为零，数据库及约束、索引保留。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
