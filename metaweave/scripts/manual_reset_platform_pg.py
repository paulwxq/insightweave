"""仅供手工执行：删除平台 PG 库、创建同名空库并执行现有建表 SQL。

默认预览且不连接数据库。真正执行必须同时指定 --execute 和
--confirm-database <实际库名>。本脚本不接入服务启动或业务流程。
"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

from dotenv import load_dotenv
import psycopg
from psycopg import sql

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if __package__ in (None, ""):
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.persistence.platform_db import (  # noqa: E402
    PlatformSettings,
    load_platform_settings_from_env,
)

ENV_FILE = PROJECT_ROOT / ".env"
SQL_FILE = PROJECT_ROOT / "db" / "rebuild_platform_schema.sql"


def validate_target(settings: PlatformSettings) -> None:
    name = settings.name
    if (
        not isinstance(name, str)
        or not name.strip()
        or name != name.strip()
        or any(ord(char) < 32 or ord(char) == 127 for char in name)
    ):
        raise ValueError("平台 PG 库名必须非空，且不能包含首尾空白或控制字符")
    if name.casefold() in {"postgres", "template0", "template1"}:
        raise ValueError("拒绝重置 PostgreSQL 系统库或模板库")
    if len(name.encode("utf-8")) > 63:
        raise ValueError("平台 PG 库名不得超过 63 字节，避免创建时被截断")


def load_target() -> PlatformSettings:
    # 固定读取本项目 .env，不随调用时的工作目录改变；环境变量仍优先。
    load_dotenv(ENV_FILE, override=False)
    settings = load_platform_settings_from_env()
    validate_target(settings)
    return settings


def reset_database(settings: PlatformSettings, schema_sql: str) -> None:
    validate_target(settings)
    if not schema_sql.strip():
        raise ValueError("建表 SQL 为空，拒绝删除数据库")
    kwargs = dict(
        host=settings.host,
        port=settings.port,
        user=settings.user,
        password=settings.password,
        connect_timeout=10,
    )
    if settings.sslmode is not None:
        kwargs["sslmode"] = settings.sslmode

    # DROP/CREATE DATABASE 不能在事务内执行；不强制终止现有连接。
    with psycopg.connect(dbname="postgres", autocommit=True, **kwargs) as admin:
        print("[检查] 确认当前账号有建库权限")
        role = admin.execute(
            "SELECT rolcreatedb OR rolsuper FROM pg_roles WHERE rolname = current_user"
        ).fetchone()
        if role is None or not role[0]:
            raise PermissionError("当前账号没有建库权限，原库未删除")
        print("[1/3] 删除平台 PG 库（有活动连接时将失败，不强制断开）")
        admin.execute(
            sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(settings.name))
        )
        print("[2/3] 创建同名空库")
        admin.execute(
            sql.SQL("CREATE DATABASE {}").format(sql.Identifier(settings.name))
        )

    print("[3/3] 执行现有建表 SQL（建表阶段使用事务）")
    with psycopg.connect(dbname=settings.name, **kwargs) as conn:
        with conn.transaction():
            # 无绑定参数、禁用 prepare，使用 simple query 执行完整静态 DDL。
            conn.execute(schema_sql, prepare=False)


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
        settings = load_target()
        # 在任何删除之前读好 SQL；文件缺失或为空时保持原库不动。
        schema_sql = SQL_FILE.read_text(encoding="utf-8")
        if not schema_sql.strip():
            raise ValueError("建表 SQL 为空")
    except (ValueError, OSError) as exc:
        parser.error(str(exc))

    print(f"PG 目标：{settings.host}:{settings.port}/{settings.name}")
    print("范围：删除整个平台库（所有 schema 和数据），创建同名空库后初始化平台结构。")
    print(f"建表文件：{SQL_FILE}")
    if args.execute:
        if args.confirm_database != settings.name:
            parser.error("执行重置必须通过 --confirm-database 填写上方显示的完整库名")
    elif args.confirm_database is not None:
        parser.error("--confirm-database 必须与 --execute 一起使用")
    else:
        print("预览结束：未连接数据库，未执行任何删除或初始化。")
        return 0

    try:
        reset_database(settings, schema_sql)
    except Exception as exc:
        # 不输出驱动原始异常，避免连接凭据进入终端日志。
        print(
            f"PG 重置失败（{type(exc).__name__}），请核对上方执行阶段。",
            file=sys.stderr,
        )
        print("删库、建库无法整体回滚；若初始化失败，新库可能为空。", file=sys.stderr)
        return 1
    print("PG 重置完成：同名空库及平台表结构已创建。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
