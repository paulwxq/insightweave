"""开发期交互式登记 PostgreSQL 数据源；正式 Web 入口后复用下层函数。"""

import sys
from getpass import getpass
from pathlib import Path

import psycopg

# 兼容 `python scripts/register_db_source.py`：直接执行时 sys.path[0] 是 scripts/。
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.data_sources.repository import (
    find_db_sources_by_connection,
    insert_db_source,
)
from backend.persistence.platform_db import (
    load_platform_settings_from_env,
    load_project_env,
    platform_session,
)
from backend.security.source_password import encrypt_password
from metaweave.core.metadata.db_step import (
    DbProfileValidationError,
    SOURCE_CONNECT_TIMEOUT_SECONDS,
    SOURCE_STATEMENT_TIMEOUT_MS,
    format_server_version,
    read_and_validate_server_version,
    validate_schema_names,
    validate_selected_schemas,
)
from metaweave.db.postgres_options import build_connect_kwargs, normalize_sslmode


def _required(prompt: str) -> str:
    value = input(prompt)
    if not value.strip():
        raise ValueError(f"{prompt.rstrip(': ')} 不能为空")
    return value


def _prompt_schema_names() -> tuple[str, ...]:
    while True:
        names = tuple(
            _required("schema 名称（逗号分隔，如 public,sales；空格会保留）: ").split(
                ","
            )
        )
        validate_schema_names(names)
        if any(name != name.strip() for name in names):
            print(
                "注意：以下 schema 名称含首尾空格，将按原样查询: "
                + ", ".join(repr(name) for name in names)
            )
            if input("确认按原样使用？输入 yes 确认，否则重新输入 schema: ") != "yes":
                continue
        return names


def main() -> None:
    load_project_env()
    settings = load_platform_settings_from_env()
    display_name = _required("显示名称: ")
    host = _required("源库 host: ")
    try:
        port = int(_required("源库 port: "))
    except ValueError as exc:
        raise ValueError("源库 port 必须是 1–65535 的整数") from exc
    if not 1 <= port <= 65535:
        raise ValueError("源库 port 必须是 1–65535 的整数")
    db_name = _required("源库数据库名: ")
    username = _required("源库用户名: ")
    password = getpass("源库密码: ")
    # 手工登记工具存规范化后的值（doc 18.1 §4.1）；非法模式在连接前失败。
    sslmode = normalize_sslmode(input("sslmode（留空则不传）: ") or None)
    names = _prompt_schema_names()

    with platform_session(settings) as session:
        duplicates = find_db_sources_by_connection(
            session, host, port, db_name, username
        )
        if duplicates:
            for row in duplicates:
                print(
                    f"已存在: {row.source_id} | {row.display_name} | {row.selected_schemas}"
                )
            if input("仍新建独立数据源？输入 yes 确认: ") != "yes":
                print("已取消登记")
                return

    kwargs = build_connect_kwargs(
        host=host,
        port=port,
        dbname=db_name,
        user=username,
        password=password,
        sslmode=sslmode,
        connect_timeout_seconds=SOURCE_CONNECT_TIMEOUT_SECONDS,
        statement_timeout_ms=SOURCE_STATEMENT_TIMEOUT_MS,
    )
    try:
        with psycopg.connect(**kwargs) as conn:
            conn.autocommit = True
            server_version_num = read_and_validate_server_version(conn)
            validate_selected_schemas(conn, names)
    except DbProfileValidationError:
        raise
    except Exception as exc:
        raise RuntimeError(
            f"源库连接或 schema 校验失败（{type(exc).__name__}）"
        ) from None

    # source_id 由数据库序列生成（doc 6.1 §13.1），插入时不指定
    with platform_session(settings) as session:
        row = insert_db_source(
            session,
            {
                "display_name": display_name,
                "db_type": "postgresql",
                "host": host,
                "port": port,
                "db_name": db_name,
                "username": username,
                "password_ciphertext": encrypt_password(password),
                "sslmode": sslmode,
                "db_version": format_server_version(server_version_num),
                "selected_schemas": list(names),
                "description": None,
                "created_by_user_id": None,
            },
        )
    print(f"数据源已登记: {row.source_id}")


if __name__ == "__main__":
    try:
        main()
    except ValueError as exc:
        raise SystemExit(f"登记失败: {exc}") from None
    except Exception as exc:
        # SQLAlchemy/psycopg 原始异常可能含连接参数或密文，不能打印 traceback。
        raise SystemExit(f"登记失败（{type(exc).__name__}；凭据已隐藏）") from None
