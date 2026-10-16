"""数据库级画像采集；不依赖平台库、Click 或 Web 入口。"""

import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

import psycopg

from metaweave.core.clean_targets import resolve_db_output_dir
from metaweave.db.postgres_options import build_connect_kwargs
from metaweave.utils.file_utils import atomic_write_json, clean_top_level_files
from metaweave.utils.time_utils import utc_isoformat

logger = logging.getLogger("metaweave.db_step")

SOURCE_CONNECT_TIMEOUT_SECONDS = 10
SOURCE_STATEMENT_TIMEOUT_MS = 60000
MIN_POSTGRESQL_VERSION_NUM = 150000
SYSTEM_SCHEMA_NAMES = frozenset({"pg_catalog", "information_schema", "pg_toast"})
TEMP_SCHEMA_PREFIXES = ("pg_temp_", "pg_toast_temp_")
_COUNT_KEYS = (
    "ordinary_table_count",
    "partitioned_table_count",
    "partition_child_count",
    "view_count",
    "materialized_view_count",
    "object_count",
)


class DbProfileValidationError(ValueError):
    """由本模块产生的、可安全显示的业务校验错误。"""


class UnsupportedServerVersionError(DbProfileValidationError):
    """源库版本低于最低版本，或版本号无法解析。"""


class SchemaMissingError(DbProfileValidationError):
    """明确选中的 schema 不存在。"""


class SchemaNoUsageError(DbProfileValidationError):
    """对明确选中的 schema 没有 USAGE 权限。"""


class SchemaNotSelectableError(DbProfileValidationError):
    """系统或临时 schema 不能登记为业务采集范围。"""


@dataclass(frozen=True)
class DbExecutionRequest:
    source_id: UUID
    job_id: UUID
    db_type: str
    host: str
    port: int
    db_name: str
    username: str
    password: str = field(repr=False)
    sslmode: str | None = None
    selected_schemas: tuple[str, ...] = ()
    clean: bool = False
    # 阶段一只在准备链加载并预检此快照；db 采集不从中读取连接或输出路径。
    main_config: dict[str, Any] = field(default_factory=dict, repr=False)


def validate_schema_names(names: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    """只验证，不裁剪 PostgreSQL 合法的首尾空格。"""
    if not isinstance(names, (tuple, list)) or not names:
        raise DbProfileValidationError("selected_schemas 必须是非空列表")
    if any(not isinstance(name, str) or not name.strip() for name in names):
        raise DbProfileValidationError("selected_schemas 含空串或纯空白名称")
    if len(set(names)) != len(names):
        raise DbProfileValidationError("selected_schemas 含重复名称")
    return tuple(names)


def validate_selected_schemas(conn: psycopg.Connection, names) -> tuple[str, ...]:
    """严格区分不存在、无 USAGE 权限和确实为空的 schema。"""
    names = validate_schema_names(names)
    with conn.cursor() as cur:
        for name in names:
            if _is_system_schema(name):
                raise SchemaNotSelectableError(f"系统或临时 schema 不可选: {name!r}")
            cur.execute(
                "SELECT has_schema_privilege(oid, 'USAGE') "
                "FROM pg_namespace WHERE nspname = %s",
                (name,),
            )
            row = cur.fetchone()
            if row is None:
                raise SchemaMissingError(f"所选 schema 不存在: {name!r}")
            if not row[0]:
                raise SchemaNoUsageError(f"对所选 schema 无 USAGE 权限: {name!r}")
    return names


def _is_system_schema(name: str) -> bool:
    return name in SYSTEM_SCHEMA_NAMES or name.startswith(TEMP_SCHEMA_PREFIXES)


def discover_selectable_schemas(conn: psycopg.Connection) -> list[str]:
    """列出当前账号有 USAGE 权限的业务 schema，按名称排序。"""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT nspname, has_schema_privilege(oid, 'USAGE') " "FROM pg_namespace"
        )
        rows = cur.fetchall()
    return sorted(
        name for name, has_usage in rows if has_usage and not _is_system_schema(name)
    )


def validate_server_version_num(value) -> int:
    """源库 PostgreSQL 15.0 起可执行 db 采集与数据源登记。"""
    try:
        version = int(value)
    except (TypeError, ValueError) as exc:
        raise UnsupportedServerVersionError(
            "源库版本号不可解析；要求源库 PostgreSQL 15.0 及以上"
        ) from exc
    if version < MIN_POSTGRESQL_VERSION_NUM:
        raise UnsupportedServerVersionError("要求源库 PostgreSQL 15.0 及以上")
    return version


def read_and_validate_server_version(conn: psycopg.Connection) -> int:
    with conn.cursor() as cur:
        cur.execute("SHOW server_version_num")
        row = cur.fetchone()
    return validate_server_version_num(row[0] if row else None)


def format_server_version(server_version_num: int) -> str:
    """与 database.json 共用的 PostgreSQL 主版本.小版本展示格式。"""
    return f"{server_version_num // 10000}.{server_version_num % 10000}"


_OS_FAMILIES = (
    ("windows", "Windows"),
    ("mingw", "Windows"),
    ("cygwin", "Windows"),
    ("darwin", "macOS"),
    ("linux", "Linux"),
    ("freebsd", "FreeBSD"),
    ("openbsd", "OpenBSD"),
    ("netbsd", "NetBSD"),
    ("solaris", "Solaris"),
    ("sunos", "Solaris"),
    ("aix", "AIX"),
)
_OS_ARCHES = frozenset(
    {
        "x86_64",
        "amd64",
        "i386",
        "i486",
        "i586",
        "i686",
        "aarch64",
        "arm64",
        "armv6l",
        "armv7l",
        "armv8l",
        "riscv64",
        "s390x",
        "ppc64",
        "ppc64le",
        "powerpc64",
        "powerpc64le",
        "sparc",
        "sparc64",
        "mips",
        "mipsel",
        "mips64",
        "mips64el",
        "loongarch64",
        "rs6000",
        "ia64",
        "alpha",
        "hppa",
    }
)
_OS_PLATFORM_RE = re.compile(r"\bon ([^\s,]+)", re.IGNORECASE)


def read_server_version_string(conn: psycopg.Connection) -> str | None:
    """读取 SELECT version() 原串；空结果返回 None。异常由调用方决定是否降级。"""
    with conn.cursor() as cur:
        cur.execute("SELECT version()")
        row = cur.fetchone()
    return row[0] if row and row[0] else None


def normalize_os_platform(version_string: str | None) -> str | None:
    """从 version() 原串解析安装平台展示串；族名解析不出返回 None。"""
    if not version_string:
        return None
    matched = _OS_PLATFORM_RE.search(version_string)
    if matched is None:
        return None
    token = matched.group(1).casefold()
    family = next((name for key, name in _OS_FAMILIES if key in token), None)
    if family is None:
        return None
    arch = token.split("-")[0]
    return f"{family} {arch}" if arch in _OS_ARCHES else family


def collect_database_profile(
    conn: psycopg.Connection,
    request: DbExecutionRequest,
    server_version_num: int,
    server_version_string: str | None,
) -> dict:
    """只返回纯数据，不写盘；所有统计均基于同一次源库连接。"""
    names = tuple(sorted(validate_selected_schemas(conn, request.selected_schemas)))
    schema_rows = []
    totals = dict.fromkeys(_COUNT_KEYS, 0)
    counts_sql = """
        SELECT
            count(*) FILTER (WHERE c.relkind = 'r' AND NOT c.relispartition),
            count(*) FILTER (WHERE c.relkind = 'p' AND NOT c.relispartition),
            count(*) FILTER (WHERE c.relispartition AND c.relkind IN ('r', 'p')),
            count(*) FILTER (WHERE c.relkind = 'v'),
            count(*) FILTER (WHERE c.relkind = 'm')
        FROM pg_class AS c
        JOIN pg_namespace AS n ON n.oid = c.relnamespace
        WHERE n.nspname = %s
    """
    with conn.cursor() as cur:
        for name in names:
            cur.execute(counts_sql, (name,))
            row = cur.fetchone()
            if row is None:
                raise RuntimeError("对象统计未返回结果")
            counts = {key: int(value) for key, value in zip(_COUNT_KEYS[:5], row)}
            counts["object_count"] = sum(
                counts[key] for key in _COUNT_KEYS[:5] if key != "partition_child_count"
            )
            schema_rows.append({"name": name, **counts})
            for key in _COUNT_KEYS:
                totals[key] += counts[key]
        cur.execute("SELECT pg_database_size(current_database())")
        size_row = cur.fetchone()
        if size_row is None or size_row[0] is None:
            raise RuntimeError("数据库大小查询未返回结果")
    return {
        "schema_version": 1,
        "source_id": str(request.source_id),
        "collected_at": utc_isoformat(datetime.now(timezone.utc)),
        "database": {
            "type": request.db_type,
            "host": request.host,
            "port": request.port,
            "name": request.db_name,
            "server_version": format_server_version(server_version_num),
            "server_version_full": server_version_string,
            "os_platform": normalize_os_platform(server_version_string),
            "size_bytes": int(size_row[0]),
            "size_scope": "whole_database",
            "record_count": None,
        },
        "selected_schemas": list(names),
        "schemas": schema_rows,
        "totals": totals,
    }


def execute_db(request: DbExecutionRequest) -> dict:
    """连接、版本检查、可选清理、统计与原子写入的唯一入口。"""
    start = time.perf_counter()
    phase = "参数校验"
    try:
        if request.db_type != "postgresql":
            raise DbProfileValidationError("仅支持 PostgreSQL 数据源")
        validate_schema_names(request.selected_schemas)
        target_dir = resolve_db_output_dir(request.source_id, request.job_id)
        kwargs = build_connect_kwargs(
            host=request.host,
            port=request.port,
            dbname=request.db_name,
            user=request.username,
            password=request.password,
            sslmode=request.sslmode,
            connect_timeout_seconds=SOURCE_CONNECT_TIMEOUT_SECONDS,
            statement_timeout_ms=SOURCE_STATEMENT_TIMEOUT_MS,
        )
        phase = "源库连接"
        with psycopg.connect(**kwargs) as conn:
            conn.autocommit = True
            phase = "源库版本检查"
            server_version_num = read_and_validate_server_version(conn)
            try:
                server_version_string = read_server_version_string(conn)
            except Exception as exc:
                server_version_string = None
                logger.warning(
                    "job_id=%s source_id=%s 读取 version() 失败，平台字段记为 NULL（%s）",
                    request.job_id,
                    request.source_id,
                    type(exc).__name__,
                )
            if request.clean:
                phase = "目录清理"
                clean_top_level_files(target_dir)
            phase = "schema 校验或对象统计"
            profile = collect_database_profile(
                conn, request, server_version_num, server_version_string
            )
        phase = "画像写入"
        target_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_json(profile, target_dir / "database.json")
        logger.info(
            "数据库概览已写入: %s (job_id=%s source_id=%s)",
            target_dir / "database.json",
            request.job_id,
            request.source_id,
        )
        return {
            "success": True,
            "execution_time": time.perf_counter() - start,
            "message": "数据库概览采集完成",
        }
    except DbProfileValidationError as exc:
        # 自身校验消息不含源库凭据；外部驱动异常在下方按类型报告。
        logger.error(
            "job_id=%s source_id=%s %s失败: %s",
            request.job_id,
            request.source_id,
            phase,
            exc,
        )
        return {
            "success": False,
            "execution_time": time.perf_counter() - start,
            "message": f"{phase}失败: {exc}",
        }
    except Exception as exc:
        logger.error(
            "job_id=%s source_id=%s %s失败（%s；凭据已隐藏）",
            request.job_id,
            request.source_id,
            phase,
            type(exc).__name__,
        )
        return {
            "success": False,
            "execution_time": time.perf_counter() - start,
            "message": f"{phase}失败（{type(exc).__name__}）",
        }
