"""数据源连接检查；与 Web 路由和平台库访问解耦。"""

from contextlib import contextmanager
from dataclasses import dataclass, field

import psycopg

from metaweave.core.metadata.db_step import (
    SOURCE_CONNECT_TIMEOUT_SECONDS,
    SOURCE_STATEMENT_TIMEOUT_MS,
    DbProfileValidationError,
    discover_selectable_schemas,
    format_server_version,
    read_and_validate_server_version,
)
from metaweave.db.postgres_options import build_connect_kwargs


class SourceConnectionFailedError(RuntimeError):
    """源库连接或驱动操作失败；不携带原始异常中的凭据。"""


@dataclass(frozen=True)
class SourceConnectionInput:
    db_type: str
    host: str
    port: int
    db_name: str
    username: str
    password: str = field(repr=False)
    sslmode: str | None = None


@contextmanager
def open_source_connection(fields: SourceConnectionInput):
    """连接、检查版本并在退出时关闭；校验异常保持原类型。"""
    if fields.db_type != "postgresql":
        raise DbProfileValidationError("当前仅支持 PostgreSQL 数据源")
    if not fields.password:
        raise DbProfileValidationError("源库密码不能为空")
    kwargs = build_connect_kwargs(
        host=fields.host,
        port=fields.port,
        dbname=fields.db_name,
        user=fields.username,
        password=fields.password,
        sslmode=fields.sslmode,
        connect_timeout_seconds=SOURCE_CONNECT_TIMEOUT_SECONDS,
        statement_timeout_ms=SOURCE_STATEMENT_TIMEOUT_MS,
    )
    try:
        with psycopg.connect(**kwargs) as conn:
            conn.autocommit = True
            version = read_and_validate_server_version(conn)
            yield conn, version
    except DbProfileValidationError:
        raise
    except psycopg.Error:
        raise SourceConnectionFailedError("源库连接或查询失败") from None


def check_connection(fields: SourceConnectionInput) -> dict:
    with open_source_connection(fields) as (conn, version):
        schemas = discover_selectable_schemas(conn)
    return {"server_version": format_server_version(version), "schemas": schemas}
