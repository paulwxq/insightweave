"""BusinessDbService：orders 只读连接池、结构查询、EXPLAIN、只读执行（docs/v4/5 §6.1）

业务结果一律按列位置处理：连接池 row_factory=tuple_row（psycopg dict_row 的
dict(zip()) 会让同名输出列互相覆盖）。服务固定 SQL 可在各自游标上单独指定
dict_row。所有业务 SQL 的唯一出口是 run_readonly：只读事务 + SET LOCAL
statement_timeout + 命名游标 + fetchmany(max_rows + 1)。
"""

from __future__ import annotations

import hashlib
import time
import uuid
from dataclasses import dataclass
from typing import Any

import psycopg
from psycopg_pool import ConnectionPool, PoolTimeout
from psycopg.rows import dict_row, tuple_row

from src.modules.db_deepagent.config import BusinessDbConfig
from src.modules.db_deepagent.errors import (
    TOOL_ERROR_DB_DATA,
    TOOL_ERROR_DB_SEMANTIC,
    TOOL_ERROR_DATASOURCE_UNAVAILABLE,
    TOOL_ERROR_INVALID_ARGUMENT,
    TOOL_ERROR_READONLY_VIOLATION,
    TOOL_ERROR_TIMEOUT,
    DbAgentError,
    DbExecutionError,
    DatasourceMismatchError,
    DuplicateColumnNamesError,
)
from src.utils.logger import get_module_logger

logger = get_module_logger("nl2sql.db_deepagent.business_db")


@dataclass(frozen=True)
class ReadonlyResult:
    """run_readonly 的返回：按列位置排列的行"""

    columns: list[str]
    column_types: list[str]  # cursor.description 的 type_code 经 pg_type 映射后的类型名
    rows: list[tuple]  # 最多 max_rows 行，按位置排列
    source_truncated: bool  # 取到行数 > max_rows
    elapsed_ms: int


@dataclass(frozen=True)
class ColumnSchema:
    """get_columns 的返回（information_schema.columns 摘要）"""

    column_name: str
    data_type: str
    is_nullable: bool
    ordinal_position: int


def _quote_pg_identifier(name: str) -> str:
    """按 PG 标识符规则加双引号（内部双引号翻倍），保留实际大小写。"""
    return '"' + name.replace('"', '""') + '"'


def _make_business_conninfo(cfg: BusinessDbConfig) -> str:
    """构建业务库 conninfo；只含连接目标与只读/时区 options（§6.1）。

    allowed_schemas 非空时追加 search_path（doc 4 §4.7：登记范围落到连接
    默认解析；配合 normalize/校验入口的显式检查，search_path 只是其中一
    层，不单独承担范围约束）。schema 名按 PG 标识符规则加引号——未加引号
    会被折叠为小写，与大小写敏感的登记名（如 "Sales"）不一致。
    """
    from psycopg.conninfo import make_conninfo

    options = f"-c default_transaction_read_only=on -c TimeZone={cfg.timezone}"
    if cfg.allowed_schemas:
        quoted = ",".join(_quote_pg_identifier(s) for s in cfg.allowed_schemas)
        options += f" -c search_path={quoted}"
    return make_conninfo(
        host=cfg.host,
        port=cfg.port,
        dbname=cfg.database,
        user=cfg.user,
        password=cfg.password,
        sslmode=cfg.sslmode,
        options=options,
    )


def _map_db_error(exc: Exception) -> DbExecutionError:
    """§6.1 错误映射表：SQLSTATE／异常类型 → error_type + retryable。"""
    sqlstate = getattr(exc, "sqlstate", None)
    if sqlstate == "57014":
        return DbExecutionError(
            error_type=TOOL_ERROR_TIMEOUT,
            message="语句被 statement_timeout 取消",
            retryable=True,
            hint="缩小查询范围或改为下推聚合",
            sqlstate=sqlstate,
        )
    if sqlstate == "25006":
        return DbExecutionError(
            error_type=TOOL_ERROR_READONLY_VIOLATION,
            message="只读事务内不能执行写操作",
            retryable=False,
            sqlstate=sqlstate,
        )
    if sqlstate and sqlstate.startswith("42"):
        return DbExecutionError(
            error_type=TOOL_ERROR_DB_SEMANTIC,
            message=str(exc).splitlines()[0] if str(exc) else "SQL 语义错误",
            retryable=True,
            sqlstate=sqlstate,
        )
    if sqlstate and sqlstate.startswith("22"):
        return DbExecutionError(
            error_type=TOOL_ERROR_DB_DATA,
            message=str(exc).splitlines()[0] if str(exc) else "数据异常",
            retryable=True,
            sqlstate=sqlstate,
        )
    if isinstance(exc, (psycopg.OperationalError, PoolTimeout, ConnectionError)):
        return DbExecutionError(
            error_type=TOOL_ERROR_DATASOURCE_UNAVAILABLE,
            message="业务库连接不可用",
            retryable=False,
            sqlstate=sqlstate,
        )
    # 未知异常：不吞掉、不降级，按不可重试的语义错误上报（消息不含连接信息）
    return DbExecutionError(
        error_type=TOOL_ERROR_DB_SEMANTIC,
        message=f"业务库执行异常: {type(exc).__name__}",
        retryable=False,
        sqlstate=sqlstate,
    )


class BusinessDbService:
    """orders 业务库只读访问服务（§6.1）"""

    @property
    def allowed_schemas(self) -> list[str] | None:
        """登记 selected_schemas（doc 4 §4.7）；None 表示不限制。"""
        return self._cfg.allowed_schemas

    def __init__(self, config: BusinessDbConfig):
        self._cfg = config
        self._pool = ConnectionPool(
            _make_business_conninfo(config),
            min_size=config.pool_min_size,
            max_size=config.pool_max_size,
            kwargs={"row_factory": tuple_row, "autocommit": False},
            configure=self._configure,
            open=False,
        )
        self.datasource_id: str | None = None
        self._pg_type_names: dict[int, str] = {}  # oid → typname 映射缓存

    # ---------------------------------------------------------------- 生命周期

    def _configure(self, conn: psycopg.Connection[tuple]) -> None:
        """连接池 configure 回调：核对业务库名；必须在显式事务内检查（C11，返回 IDLE）。

        连接级 row_factory 为 tuple_row，按位置解包。
        """
        with conn.transaction():  # 退出时提交，连接回到 IDLE
            row = conn.execute(
                "SELECT current_database(), current_user, current_setting('search_path')"
            ).fetchone()
        db, role, _search_path = row
        if db != self._cfg.expected_database:
            raise DatasourceMismatchError(
                f"业务库应为 {self._cfg.expected_database}，实际为 {db}"
            )
        logger.debug("业务库连接就绪: database=%s role=%s", db, role)

    def open(self) -> None:
        """打开连接池并计算 datasource_id（§6.1 数据源指纹）。"""
        if self.datasource_id is not None:
            return
        self._pool.open()
        with self._pool.connection() as conn:
            with conn.transaction():
                row = conn.execute(
                    "SELECT current_database(), current_user, current_setting('search_path'), "
                    "inet_server_addr()::text, inet_server_port()"
                ).fetchone()
        db, role, search_path, server_addr, server_port = row
        canonical = "\x1f".join(
            [db, role, search_path, str(server_addr), str(server_port)]
        )
        self.datasource_id = (
            "pg:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
        )
        # 日志只输出数据库名、角色名和指纹，不输出密码（§16.2）
        logger.info(
            "业务库数据源指纹: database=%s role=%s datasource_id=%s",
            db,
            role,
            self.datasource_id,
        )

    def close(self) -> None:
        if not self._pool.closed:
            self._pool.close()

    # ---------------------------------------------------------------- 只读执行

    def run_readonly(
        self, sql: str, *, timeout_s: float, max_rows: int
    ) -> ReadonlyResult:
        """在只读事务和服务端游标内执行，读取 max_rows + 1 行以判定 source_truncated。

        Raises:
            DuplicateColumnNamesError: 输出列名重复（含 ?column?），不读取行
            DbExecutionError: §6.1 错误映射
        """
        started = time.monotonic()
        try:
            with self._pool.connection() as conn, conn.transaction():
                conn.execute("SET TRANSACTION READ ONLY")  # 事务内第一条语句
                conn.execute(f"SET LOCAL statement_timeout = {int(timeout_s * 1000)}")
                stmt = sql.rstrip()
                if stmt.endswith(";"):
                    stmt = stmt[:-1]  # 仅用于游标包装，不影响授权文本
                with conn.cursor(name=f"dbda_{uuid.uuid4().hex}") as cur:
                    cur.execute(stmt)
                    desc = cur.description or []
                    names = [d.name for d in desc]
                    duplicated = sorted({n for n in names if names.count(n) > 1})
                    if duplicated:
                        # 不读取行、不保存结果（§6.1 第 4 步）
                        raise DuplicateColumnNamesError(duplicated)
                    raw_rows = cur.fetchmany(max_rows + 1)
                    type_names = self._resolve_type_names([d.type_code for d in desc])
        except DbAgentError:
            raise
        except Exception as exc:
            raise _map_db_error(exc) from exc
        rows = list(raw_rows[:max_rows])
        elapsed_ms = int((time.monotonic() - started) * 1000)
        return ReadonlyResult(
            columns=names,
            column_types=type_names,
            rows=rows,
            source_truncated=len(raw_rows) > max_rows,
            elapsed_ms=elapsed_ms,
        )

    def explain(self, sql: str, *, timeout_s: float) -> list[tuple]:
        """只读事务内执行 EXPLAIN <sql>（从不使用 ANALYZE）。"""
        try:
            with self._pool.connection() as conn, conn.transaction():
                conn.execute("SET TRANSACTION READ ONLY")
                conn.execute(f"SET LOCAL statement_timeout = {int(timeout_s * 1000)}")
                rows = conn.execute(f"EXPLAIN {sql}").fetchall()
        except DbAgentError:
            raise
        except Exception as exc:
            raise _map_db_error(exc) from exc
        return [tuple(r) for r in rows]

    # ---------------------------------------------------------------- 结构查询

    def locate_table_schemas(
        self, table_names: list[str], schemas: list[str]
    ) -> dict[str, list[str]]:
        """按精确表名核对其实际所属 schema（doc 4 §4.7 裸名解析核对）。

        查 information_schema.tables（只读、参数化），返回
        ``表名 -> [命中 schema, ...]``；名字大小写按 PG 规则由调用方归一
        （未加引号折叠小写，加引号保留原样后精确匹配目录）。
        """
        from collections import defaultdict

        if not table_names:
            return {}
        with self._pool.connection() as conn:
            rows = conn.execute(
                """
                SELECT table_name, table_schema FROM information_schema.tables
                WHERE table_name = ANY(%s) AND table_schema = ANY(%s)
                """,
                (table_names, schemas),
            ).fetchall()
        located: dict[str, list[str]] = defaultdict(list)
        for name, schema in rows:
            located[name].append(schema)
        return dict(located)

    def get_columns(self, schema: str, table: str) -> list[ColumnSchema]:
        """information_schema.columns：列名、类型、是否可空、序号（固定 SQL，不经校验器）。

        allowed_schemas（登记 selected_schemas）非空时，范围外 schema 直接
        拒绝——不返回其他 schema 的实时结构（doc 4 §4.7）。
        """
        allowed = self._cfg.allowed_schemas
        if allowed and schema not in allowed:
            raise DbExecutionError(
                error_type=TOOL_ERROR_INVALID_ARGUMENT,
                message=(
                    f"schema {schema!r} 不在本源登记的 selected_schemas "
                    f"（{', '.join(allowed)}）内，已拒绝读取实时结构"
                ),
                retryable=False,
            )
        with self._pool.connection() as conn:
            with conn.transaction():
                with conn.cursor(
                    row_factory=dict_row
                ) as cur:  # 结构查询固定 SQL 按列名取值（§6.1）
                    cur.execute(
                        """
                        SELECT column_name, data_type, is_nullable, ordinal_position
                        FROM information_schema.columns
                        WHERE table_schema = %s AND table_name = %s
                        ORDER BY ordinal_position
                        """,
                        (schema, table),
                    )
                    rows = cur.fetchall()
        return [
            ColumnSchema(
                column_name=r["column_name"],
                data_type=r["data_type"],
                is_nullable=r["is_nullable"] == "YES",
                ordinal_position=r["ordinal_position"],
            )
            for r in rows
        ]

    def table_exists(self, schema: str, table: str) -> bool:
        with self._pool.connection() as conn:
            with conn.transaction():
                row = conn.execute(
                    """
                    SELECT 1 FROM information_schema.tables
                    WHERE table_schema = %s AND table_name = %s
                    """,
                    (schema, table),
                ).fetchone()
        return row is not None

    def column_type(self, schema: str, table: str, column: str) -> str | None:
        with self._pool.connection() as conn:
            with conn.transaction():
                row = conn.execute(
                    """
                    SELECT data_type FROM information_schema.columns
                    WHERE table_schema = %s AND table_name = %s AND column_name = %s
                    """,
                    (schema, table, column),
                ).fetchone()
        return row[0] if row else None

    # ---------------------------------------------------------------- 内部

    def _resolve_type_names(self, oids: list[Any]) -> list[str]:
        """cursor.description 的 type_code（OID）经 pg_type 映射为类型名（带缓存）。"""
        missing = sorted(
            {int(oid) for oid in oids if int(oid) not in self._pg_type_names}
        )
        if missing:
            with self._pool.connection() as conn:
                with conn.transaction():
                    with conn.cursor(row_factory=dict_row) as cur:
                        cur.execute(
                            "SELECT oid, typname FROM pg_type WHERE oid = ANY(%s)",
                            (missing,),
                        )
                        rows = cur.fetchall()
            for r in rows:
                self._pg_type_names[int(r["oid"])] = r["typname"]
        return [self._pg_type_names.get(int(oid), "unknown") for oid in oids]
