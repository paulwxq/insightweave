"""部署初始化封装（doc 13 §6.1–6.3、§8.1；审核 R1/R2/R3/R4 修复）。

在后端容器内经 ``docker compose exec`` 执行，不新增 init 服务：
- metaweave：读取镜像内 ``metaweave/db/rebuild_platform_schema.sql``。
  全缺→单事务整份 DDL+核验；完整且结构契约通过→仅核验；部分对象或
  结构冲突→拒绝；失败整体回滚。生产连接 autocommit=True，使
  ``conn.transaction()`` 成为真顶层 BEGIN…COMMIT（审核 R1）。
- nl2sql：先做只读兼容检查（迁移版本不得超过所装 SDK 支持上限，
  审核R2），已知兼容的缺失对象交原幂等入口 ``init_platform()``
  补齐；异常一律停止（不猜结构）。
- milvus：initialize 复用 ``connect(create_database=True)``；
  check 只读（缺库清晰失败，绝不建库，审核 R4）。

check 模式只核验；initialize 才允许创建。数据库边界经参数注入，
离线测试用替身。
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from collections.abc import Callable, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

_SEQUENCE_RE = re.compile(
    r"CREATE\s+SEQUENCE\s+(?:IF\s+NOT\s+EXISTS\s+)?public\.([A-Za-z0-9_]+)",
    re.IGNORECASE,
)
_VIEW_RE = re.compile(
    r"CREATE\s+(?:OR\s+REPLACE\s+)?VIEW\s+public\.([A-Za-z0-9_]+)", re.IGNORECASE
)
_INDEX_RE = re.compile(
    r"CREATE\s+(?:UNIQUE\s+)?INDEX\s+(?:IF\s+NOT\s+EXISTS\s+)?(?:public\.)?([A-Za-z0-9_]+)",
    re.IGNORECASE,
)
_TABLE_BLOCK_RE = re.compile(
    r"CREATE\s+TABLE\s+public\.([A-Za-z0-9_]+)\s*\(([^;]*)\)", re.IGNORECASE | re.DOTALL
)
_CONSTRAINT_PREFIXES = (
    "primary",
    "foreign",
    "unique",
    "check",
    "constraint",
    "exclude",
    "like",
)

# DDL 类型基名 → information_schema.columns.data_type（规范化）
_TYPE_MAP = {
    "text": "text",
    "integer": "integer",
    "bigint": "bigint",
    "smallint": "smallint",
    "boolean": "boolean",
    "jsonb": "jsonb",
    "json": "json",
    "uuid": "uuid",
    "date": "date",
    "bytea": "bytea",
    "numeric": "numeric",
    "decimal": "numeric",
    "real": "real",
    "double precision": "double precision",
    "float": "double precision",
    "character varying": "character varying",
    "varchar": "character varying",
    "char": "character",
    "character": "character",
    "timestamptz": "timestamp with time zone",
    "timetz": "time with time zone",
    "timestamp with time zone": "timestamp with time zone",
    "timestamp": "timestamp without time zone",
    "time with time zone": "time with time zone",
    "time": "time without time zone",
}


class DeployInitError(RuntimeError):
    """初始化执行失败（连接/执行/提交错误），调用方按失败处理。"""


class DeployInitConflict(DeployInitError):
    """来源不明的部分对象或结构冲突：停止并说明，不删库不改造。"""


_TYPE_TOKEN_RE = re.compile(
    r"(?:double\s+precision|timestamptz|timetz|timestamp(?:\s+with(?:out)?\s+time\s+zone)?"
    r"|time(?:\s+with(?:out)?\s+time\s+zone)?|character\s+varying"
    r"|char(?:acter)?(?:\s+varying)?|(?:big|small)?int(?:eger)?"
    r"|varchar|boolean|numeric|decimal|real|float|text|jsonb|json|uuid|date|bytea)"
    r"(?:\s*\([^)]*\))?",
    re.IGNORECASE,
)


def _normalize_type(ddl_type: str) -> str:
    base = ddl_type.strip().lower()
    base = re.sub(r"\s*\([^)]*\)\s*$", "", base).strip()  # 去长度/精度
    base = re.sub(r"\s+", " ", base)
    for key in sorted(_TYPE_MAP, key=len, reverse=True):
        if base == key or base.startswith(key + " "):
            return _TYPE_MAP[key]
    return base  # 未知类型：按小写原文比较


def _split_top_level(body: str) -> list[str]:
    parts, depth, current = [], 0, []
    for ch in body:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(ch)
    if current:
        parts.append("".join(current))
    return parts


def build_metaweave_contract(sql_text: str) -> dict[str, Any]:
    """从静态 DDL 解析受控结构契约（表→列→规范化类型、序列、视图、索引）。

    契约同时固化在 deploy/contract.json（供任一后端容器的只读冒烟使用），
    由 test_deployment_contract 防止两处漂移。
    """
    tables: dict[str, dict[str, str]] = {}
    for name, body in _TABLE_BLOCK_RE.findall(sql_text):
        columns: dict[str, str] = {}
        for item in _split_top_level(body):
            entry = item.strip()
            if not entry or entry.lower().startswith(_CONSTRAINT_PREFIXES):
                continue
            match = re.match(r"([A-Za-z_][A-Za-z0-9_]*)\s+(.+)", entry, re.DOTALL)
            if not match:
                continue
            column, rest = match.group(1), match.group(2).strip()
            # 列类型可能跨行（续行已并入 rest 的首段）
            first_line = rest.splitlines()[0].strip()
            type_part = _TYPE_TOKEN_RE.match(first_line)
            if type_part is None:
                continue
            rest_after_type = first_line[type_part.end() :].lstrip()
            if rest_after_type.startswith("[]"):
                # information_schema.data_type 对数组列统一报 ARRAY
                columns[column] = "ARRAY"
            else:
                columns[column] = _normalize_type(type_part.group(0))
        tables[name] = columns
    return {
        "tables": tables,
        "sequences": sorted(set(_SEQUENCE_RE.findall(sql_text))),
        "views": sorted(set(_VIEW_RE.findall(sql_text))),
        "indexes": sorted(set(_INDEX_RE.findall(sql_text))),
    }


def load_deploy_contract() -> dict[str, Any]:
    """读取 deploy/contract.json（镜像内 /app/deploy/contract.json）。"""
    import json

    contract_path = Path(__file__).resolve().parents[1] / "contract.json"
    return json.loads(contract_path.read_text(encoding="utf-8"))


def verify_metaweave_objects(conn: Any, contract: dict[str, Any]) -> list[str]:
    """只读核验 MetaWeave 结构契约，返回问题清单（空列表=完整兼容）。"""
    expected_tables = contract["tables"]
    rows = conn.execute(
        "SELECT table_name, column_name, data_type FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = ANY(%s)",
        (sorted(expected_tables),),
    ).fetchall()
    actual: dict[str, dict[str, str]] = {}
    for table_name, column_name, data_type in rows:
        actual.setdefault(table_name, {})[column_name] = data_type
    problems: list[str] = []
    for table, columns in expected_tables.items():
        got = actual.get(table)
        if got is None:
            problems.append(f"表 {table} 不存在")
            continue
        for column, expected_type in columns.items():
            if column not in got:
                problems.append(f"表 {table} 缺列 {column}")
            elif got[column] != expected_type:
                problems.append(
                    f"表 {table}.{column} 类型不符：期望 {expected_type}，实得 {got[column]}"
                )
    seq_rows = conn.execute(
        "SELECT sequence_name FROM information_schema.sequences WHERE sequence_schema = 'public'"
    ).fetchall()
    got_sequences = {row[0] for row in seq_rows}
    for name in contract["sequences"]:
        if name not in got_sequences:
            problems.append(f"缺序列 {name}")
    view_rows = conn.execute(
        "SELECT table_name FROM information_schema.views WHERE table_schema = 'public'"
    ).fetchall()
    got_views = {row[0] for row in view_rows}
    for name in contract["views"]:
        if name not in got_views:
            problems.append(f"缺视图 {name}")
    index_rows = conn.execute(
        "SELECT indexname FROM pg_indexes WHERE schemaname = 'public'"
    ).fetchall()
    got_indexes = {row[0] for row in index_rows}
    for name in contract["indexes"]:
        if name not in got_indexes:
            problems.append(f"缺索引 {name}")
    return problems


@contextmanager
def _connection(connect: Callable[[], Any]):
    conn = connect()
    try:
        yield conn
    finally:
        close = getattr(conn, "close", None)
        if callable(close):
            close()


def _existing_public_tables(conn: Any) -> set[str]:
    rows = conn.execute(
        "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
    ).fetchall()
    return {row[0] for row in rows}


def ensure_metaweave_schema(connect: Callable[[], Any], sql_text: str) -> str:
    """按 doc 13 §6.1 + 审核 R2：verified / created（单事务）/ 部分或结构冲突拒绝。"""
    contract = build_metaweave_contract(sql_text)
    expected = set(contract["tables"])
    if not expected:
        raise DeployInitError("SQL 中未解析到任何 public 目标表")
    with _connection(connect) as conn:
        existing = _existing_public_tables(conn) & expected
        if existing and existing != expected:
            raise DeployInitConflict(
                f"平台对象仅部分存在（已有 {sorted(existing)}，"
                f"缺失 {sorted(expected - existing)}）：来源不明，停止"
            )
        if existing == expected:
            problems = verify_metaweave_objects(conn, contract)
            if problems:
                raise DeployInitConflict("平台结构契约核验失败：" + "；".join(problems))
            return "verified"
        try:
            with conn.transaction():
                try:
                    conn.execute(sql_text)
                except Exception as exc:
                    raise DeployInitError(f"DDL 执行失败（事务回滚）：{exc}") from exc
                problems = verify_metaweave_objects(conn, contract)
                if problems:
                    raise DeployInitError(
                        "DDL 执行后契约核验失败（事务回滚）：" + "；".join(problems)
                    )
        except DeployInitError:
            raise
        except Exception as exc:
            raise DeployInitError(f"DDL 事务提交失败（已回滚）：{exc}") from exc
        return "created"


def check_metaweave_schema(connect: Callable[[], Any], sql_text: str) -> list[str]:
    """check 模式：只读核验，返回缺失/结构问题清单。"""
    contract = build_metaweave_contract(sql_text)
    expected = set(contract["tables"])
    with _connection(connect) as conn:
        existing = _existing_public_tables(conn) & expected
        problems = [f"表 {name} 不存在" for name in sorted(expected - existing)]
        if existing:
            sub = verify_metaweave_objects(
                conn,
                {
                    "tables": {t: contract["tables"][t] for t in existing},
                    "sequences": contract["sequences"],
                    "views": contract["views"],
                    "indexes": contract["indexes"],
                },
            )
            problems.extend(
                p for p in sub if not p.startswith("表 ") or "不存在" not in p
            )
        return problems


# ---------------------------------------------------------------- NL2SQL


def check_nl2sql_migration_versions(
    connect: Callable[[], Any],
    *,
    saver_max: int | None = None,
    store_max: int | None = None,
) -> None:
    """只读兼容检查（审核 R2）：迁移版本不得超过所装 SDK 支持上限。

    迁移表不存在视为尚未初始化（兼容，交原入口幂等创建）；
    版本超上限 → 明确拒绝，且此时尚未执行任何创建/迁移。
    """
    if saver_max is None or store_max is None:
        from langgraph.checkpoint.postgres import PostgresSaver
        from langgraph.store.postgres import PostgresStore

        saver_max = len(PostgresSaver.MIGRATIONS) - 1
        store_max = len(PostgresStore.MIGRATIONS) - 1
    with _connection(connect) as conn:
        for qualified, max_version in (
            ("db_deepagent.checkpoint_migrations", saver_max),
            ("db_deepagent.store_migrations", store_max),
        ):
            exists = conn.execute(
                "SELECT to_regclass(%s) AS reg", (qualified,)
            ).fetchone()
            if exists is None or not exists[0]:
                continue  # 表不存在：尚未初始化，兼容
            # 迁移表为每版本一行（v 为主键）；取 max 判定是否超出所装 SDK
            row = conn.execute(f"SELECT max(v) FROM {qualified}").fetchone()
            if row is None or row[0] is None:
                continue
            version = row[0]
            if version > max_version:
                raise DeployInitConflict(
                    f"NL2SQL 迁移版本不支持：{qualified} v={version} > 当前 SDK 支持上限 "
                    f"{max_version}。SDK 升级/降级需人工处理，拒绝自动初始化。"
                )


def ensure_nl2sql_persistence(
    config: Any,
    init_platform: Callable[[Any], list[str]],
    *,
    connect: Callable[[], Any] | None = None,
) -> list[str]:
    """按 doc 13 §6.2 + 审核 R2：先只读兼容检查，再交原幂等入口补齐。

    连接/结构失败一律停止，不区分猜测；已知兼容的部分初始化由原入口
    幂等完成，不外包大事务（保持 SDK 自身事务要求）。
    """
    if connect is None:
        connect = _psycopg_connect_from_config(config)
    try:
        check_nl2sql_migration_versions(connect)
        return init_platform(config)
    except DeployInitError:
        raise
    except Exception as exc:
        raise DeployInitConflict(
            f"NL2SQL 持久化初始化失败（未知结构/冲突或连接问题）：{exc}"
        ) from exc


def ensure_milvus_database(client: Any) -> None:
    """按 doc 13 §6.3：复用客户端 create_database 语义，存在则只核对。"""
    client.connect(create_database=True)


def _psycopg_connect_from_config(config: Any) -> Callable[[], Any]:
    import psycopg

    def connect():
        return psycopg.connect(
            host=config.host,
            port=config.port,
            dbname=config.database,
            user=config.user,
            password=config.password or "",
        )

    return connect


# ---------------------------------------------------------------- CLI


def _metaweave_sql_path() -> Path:
    return (
        Path(__file__).resolve().parents[2]
        / "metaweave"
        / "db"
        / "rebuild_platform_schema.sql"
    )


def _run_metaweave(mode: str) -> int:
    import psycopg  # 容器内（/app/metaweave）具备

    sql_text = _metaweave_sql_path().read_text(encoding="utf-8")

    def connect():
        # autocommit=True（审核 R1）：使 conn.transaction() 成为真 BEGIN…COMMIT。
        # psycopg 默认 autocommit=False 时，先行的核验 SELECT 开启隐式事务，
        # 事务块降级为 SAVEPOINT（成功仅 RELEASE 不 COMMIT），连接关闭即回滚。
        return psycopg.connect(
            host=os.environ["PLATFORM_DB_HOST"],
            port=int(os.environ["PLATFORM_DB_PORT"]),
            dbname=os.environ["PLATFORM_DB_NAME"],
            user=os.environ["PLATFORM_DB_USER"],
            password=os.environ.get("PLATFORM_DB_PASSWORD", ""),
            autocommit=True,
        )

    if mode == "check":
        problems = check_metaweave_schema(connect, sql_text)
        if problems:
            print(f"metaweave check: {len(problems)} 项问题：")
            for line in problems:
                print(f"  - {line}")
            return 3
        print("metaweave check: 完整且结构契约通过")
        return 0
    action = ensure_metaweave_schema(connect, sql_text)
    print(f"metaweave initialize: {action}")
    return 0


def _nl2sql_config_from_env() -> Any:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "nl2sql"))
    from src.modules.db_deepagent.config import PlatformDbConfig

    return PlatformDbConfig(
        host=os.environ["LANGGRAPH_DB_HOST"],
        port=int(os.environ["LANGGRAPH_DB_PORT"]),
        database=os.environ["LANGGRAPH_DB_NAME"],
        user=os.environ["LANGGRAPH_DB_USER"],
        password=os.environ.get("LANGGRAPH_DB_PASSWORD", ""),
        expected_database=os.environ["LANGGRAPH_DB_NAME"],
        schema="db_deepagent",
        connect_timeout_seconds=10.0,
        statement_timeout_ms=15000,
        pool_min_size=1,
        pool_max_size=2,
    )


def _run_nl2sql(mode: str) -> int:
    config = _nl2sql_config_from_env()
    connect = _psycopg_connect_from_config(config)
    if mode == "check":
        # 只读核验（审核 R3）：迁移版本兼容 + 目标表齐全，不执行任何 setup/DDL
        check_nl2sql_migration_versions(connect)
        from src.modules.db_deepagent.persistence.platform_pool import (
            PLATFORM_TABLES,
            SNAPSHOT_TABLE,
        )

        expected = list(PLATFORM_TABLES) + [SNAPSHOT_TABLE]
        with _connection(connect) as conn:
            rows = conn.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'db_deepagent'"
            ).fetchall()
        present = {row[0] for row in rows}
        missing = [name for name in expected if name not in present]
        if missing:
            print(f"nl2sql check: 缺表 {missing}")
            return 3
        print("nl2sql check: 迁移版本兼容且持久化表齐全")
        return 0
    from src.modules.db_deepagent.persistence.platform_pool import init_platform

    tables = ensure_nl2sql_persistence(config, init_platform, connect=connect)
    print(f"nl2sql initialize: {len(tables)} 张持久化表就绪（{tables}）")
    return 0


def _run_milvus(mode: str, client_factory=None) -> int:
    if client_factory is None:
        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "metaweave"))
        from services.vector_db.milvus_client import MilvusClient

        client_factory = MilvusClient

    client = client_factory(
        {
            "host": os.environ["MILVUS_HOST"],
            "port": os.environ.get("MILVUS_PORT", "19530"),
            "database": os.environ["MILVUS_DATABASE"],
            "user": os.environ.get("MILVUS_USER", ""),
            "password": os.environ.get("MILVUS_PASSWORD", ""),
        }
    )
    try:
        if mode == "check":
            # check 只核验（doc 13 §6.3/§8.1、审核 R4）：缺库清晰失败，不建库
            client.connect()
        else:
            ensure_milvus_database(client)
    except Exception as exc:  # noqa: BLE001 —— 缺库/不可达均清晰报错
        print(f"milvus {mode} 失败：{exc}", file=sys.stderr)
        return 3
    print(f"milvus {mode}: 数据库 {os.environ['MILVUS_DATABASE']} 可用")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="部署初始化（doc 13 §6）")
    parser.add_argument("target", choices=["metaweave", "nl2sql", "milvus"])
    parser.add_argument("--mode", choices=["initialize", "check"], default="check")
    args = parser.parse_args(argv)
    try:
        if args.target == "metaweave":
            return _run_metaweave(args.mode)
        if args.target == "nl2sql":
            return _run_nl2sql(args.mode)
        return _run_milvus(args.mode)
    except DeployInitConflict as exc:
        print(f"初始化冲突，停止：{exc}", file=sys.stderr)
        return 4
    except DeployInitError as exc:
        print(f"初始化失败：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
