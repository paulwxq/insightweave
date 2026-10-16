"""平台库连接池（docs/v4/5 §13.1）

lg_persist 上的 db_deepagent schema 专用池：search_path 通过 libpq options 在每条
连接建立时设置（只含 db_deepagent，不含 public）；kwargs 与
PostgresSaver.from_conn_string 一致（autocommit、prepare_threshold=0、dict_row）。
常规启动只做核对（连接回调 + 表存在性检查）；建 Schema 与 setup 是
`cli maintenance init-platform` 的显式动作，不在每次启动时隐式执行。
"""

from __future__ import annotations

from psycopg.conninfo import make_conninfo
from psycopg_pool import ConnectionPool
from psycopg.rows import dict_row

from src.modules.db_deepagent.config import PlatformDbConfig
from src.modules.db_deepagent.errors import PlatformSchemaError
from src.utils.logger import get_module_logger

logger = get_module_logger("nl2sql.db_deepagent.platform_pool")

# PostgresSaver/PostgresStore 在当前 schema 下的表（§13.1 初始化核对清单）
PLATFORM_TABLES = (
    "checkpoints",
    "checkpoint_blobs",
    "checkpoint_writes",
    "checkpoint_migrations",
    "store",
    "store_migrations",
)


def make_platform_conninfo(
    cfg: PlatformDbConfig, *, with_search_path: bool = True
) -> str:
    """构建平台库 conninfo；with_search_path=False 用于初始化管理连接（建 Schema 前不能设 search_path）。"""
    options = (
        f"-c search_path={cfg.schema_name} -c statement_timeout={cfg.statement_timeout_ms}"
        if with_search_path
        else f"-c statement_timeout={cfg.statement_timeout_ms}"
    )
    return make_conninfo(
        host=cfg.host,
        port=cfg.port,
        dbname=cfg.database,
        user=cfg.user,
        password=cfg.password,
        sslmode=cfg.sslmode,
        connect_timeout=cfg.connect_timeout_seconds,
        options=options,
    )


def _verify_connection(cfg: PlatformDbConfig):
    """连接回调工厂：核对 current_database 与 current_schema（§13.1）。

    Schema 不存在时 PostgreSQL 的 current_schema() 返回 NULL → 报错，不回退 public。
    平台池 autocommit=True，核对查询不开启事务。
    """

    def verify(conn) -> None:
        # 平台池 row_factory=dict_row（K16），按列名取值
        row = conn.execute(
            "SELECT current_database() AS db, current_schema() AS schema"
        ).fetchone()
        db, schema = row["db"], row["schema"]
        if db != cfg.expected_database:
            raise PlatformSchemaError(
                f"平台库应为 {cfg.expected_database}，实际为 {db}"
            )
        if schema != cfg.schema_name:
            raise PlatformSchemaError(
                f"平台库 schema 应为 {cfg.schema_name}，实际为 {schema!r}"
                f"（NULL 表示 schema 不存在，请先运行 init-platform）"
            )

    return verify


def build_platform_pool(cfg: PlatformDbConfig) -> ConnectionPool:
    """构建平台连接池（open=False；调用方在核对表后 open）。"""
    return ConnectionPool(
        make_platform_conninfo(cfg),
        min_size=cfg.pool_min_size,
        max_size=cfg.pool_max_size,
        kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
        configure=_verify_connection(cfg),
        open=False,
    )


def check_platform_tables(pool: ConnectionPool, schema_name: str) -> list[str]:
    """常规启动核对：六张表必须位于当前 schema；返回缺失表名列表（空列表=齐全）。"""
    with pool.connection() as conn:
        rows = conn.execute("""
            SELECT table_name FROM information_schema.tables
            WHERE table_schema = current_schema()
            """).fetchall()
    present = {r["table_name"] for r in rows}
    missing = [t for t in PLATFORM_TABLES if t not in present]
    if missing:
        logger.warning(
            "平台库 %s 缺少表: %s（请先运行 init-platform）",
            schema_name,
            ", ".join(missing),
        )
    else:
        logger.info("平台库表核对通过: %s", ", ".join(PLATFORM_TABLES))
    return missing


def _snapshot_public_tables(conn) -> dict[str, int]:
    """记录 public 中各表行数（P08：init 前后对比，旧表不得变化）。

    conn 为不设 search_path 的管理连接（默认 tuple_row）。
    """
    rows = conn.execute("""
        SELECT table_name FROM information_schema.tables
        WHERE table_schema = 'public' ORDER BY table_name
        """).fetchall()
    snapshot: dict[str, int] = {}
    for r in rows:
        table = r[0]
        snapshot[table] = conn.execute(
            f'SELECT count(*) FROM "public"."{table}"'
        ).fetchone()[0]
    return snapshot


def init_platform(cfg: PlatformDbConfig) -> list[str]:
    """显式初始化（§13.1，`cli maintenance init-platform`；不在每次启动时隐式执行）。

    1. 不设 search_path 的管理连接：CREATE SCHEMA IF NOT EXISTS，并快照 public 行数
    2. 打开连接池，PostgresSaver(pool).setup()、PostgresStore(pool).setup()
    3. 核对六张表位于 db_deepagent，且 public 旧表行数未变化（P08）

    Returns: db_deepagent 下的表名列表

    Raises:
        PlatformSchemaError: 表缺失或 public 旧表被改动
    """
    import psycopg

    with psycopg.connect(make_platform_conninfo(cfg, with_search_path=False)) as mgmt:
        mgmt.execute(f'CREATE SCHEMA IF NOT EXISTS "{cfg.schema_name}"')
        before = _snapshot_public_tables(mgmt)

    pool = build_platform_pool(cfg)
    try:
        pool.open()
        from langgraph.checkpoint.postgres import PostgresSaver
        from langgraph.store.postgres import PostgresStore

        with pool.connection() as conn:
            PostgresSaver(conn).setup()
        with pool.connection() as conn:
            PostgresStore(conn).setup()
        missing = check_platform_tables(pool, cfg.schema_name)
        if missing:
            raise PlatformSchemaError(f"init-platform 后仍缺表: {', '.join(missing)}")
        with psycopg.connect(
            make_platform_conninfo(cfg, with_search_path=False)
        ) as mgmt:
            after = _snapshot_public_tables(mgmt)
        if before != after:
            changed = [
                t for t in set(before) | set(after) if before.get(t) != after.get(t)
            ]
            raise PlatformSchemaError(
                f"public 旧表被改动（P08）: {', '.join(sorted(changed))}"
            )
        with pool.connection() as conn:
            rows = conn.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema() ORDER BY 1"
            ).fetchall()
        tables = [r["table_name"] for r in rows]
        logger.info(
            "init-platform 完成: schema=%s tables=%s",
            cfg.schema_name,
            ", ".join(tables),
        )
        return tables
    finally:
        pool.close()
