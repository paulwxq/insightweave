"""平台库一次性重建：旧表统一改名留存 → 创建十六张新表与视图。

doc 6.1 §10.0 / doc 6.2 §4.2、§12 的显式一次性入口；应用启动与普通运行
不调用本脚本。

执行流程（单个事务）：
1. 生成同批次时间戳 `<yyyyMMddHHmmss>`；
2. 将八张旧表（db_source_info、db_profile、table_definition、table_profile、
   table_column_profile、table_domain、job_info、publish_info）改名留存为
   `<原表名>_old_<ts>`，并同步改名其主键/唯一约束支撑索引、普通索引、
   拥有序列与依赖视图，避免与新 DDL 同名冲突；旧外键继续关联改名后的旧表；
3. 按依赖顺序执行 `db/rebuild_platform_schema.sql` 的静态 DDL（序列、
   十六张表（含删除任务表）、索引、视图）；
4. 提交。任一步失败整体回滚，旧对象名称不变。

安全约定：
- 不删除、不清空任何旧表，不复制旧行，不迁移旧 UUID；
- 若任一新表/视图已存在则拒绝执行（先人工处理），不覆盖已有对象；
- 重名长度超过 63 字节时截断基名后附加唯一后缀，不依赖 PostgreSQL 截断。

用法：
    uv run python scripts/rebuild_platform_schema.py --dry-run   # 仅打印
    uv run python scripts/rebuild_platform_schema.py             # 实际执行
可选 --database 覆盖 PLATFORM_DB_NAME（指向隔离测试库演练时使用）。
"""

import argparse
import sys
from datetime import datetime
from pathlib import Path

import psycopg

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.persistence.platform_db import (  # noqa: E402
    load_platform_settings_from_env,
    load_project_env,
)

SQL_FILE = Path(__file__).resolve().parents[1] / "db" / "rebuild_platform_schema.sql"

OLD_TABLES = (
    "db_source_info",
    "db_profile",
    "table_definition",
    "table_profile",
    "table_column_profile",
    "table_domain",
    "job_info",
    "publish_info",
)

NEW_TABLES = (
    "db_source_info",
    "workspace_info",
    "collection_job_info",
    "collection_job_revision",
    "collection_run_info",
    "collection_run_step",
    "publish_job_info",
    "publish_job_revision",
    "publish_run_info",
    "publish_run_step",
    "db_profile",
    "table_definition",
    "table_profile",
    "table_column_profile",
    "table_domain",
    "resource_deletion_task",
)

NEW_SEQUENCES = (
    "db_source_info_id_seq",
    "workspace_info_id_seq",
    "collection_job_info_id_seq",
    "publish_job_info_id_seq",
)

NEW_VIEW = "vw_workspace_latest_successful_publish"

MAX_IDENT = 63


def _qualified(name: str) -> str:
    return f'public."{name}"'


def _renamed(base: str, ts: str) -> str:
    """`<base>_old_<ts>`；超过 63 字节时截断基名（不依赖 PG 截断）。"""
    suffix = f"_old_{ts}"
    if len(base) + len(suffix) <= MAX_IDENT:
        return base + suffix
    return base[: MAX_IDENT - len(suffix)] + suffix


def _existing_tables(cur) -> set[str]:
    cur.execute(
        "SELECT relname FROM pg_class c JOIN pg_namespace n "
        "ON n.oid = c.relnamespace "
        "WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p')"
    )
    return {row[0] for row in cur.fetchall()}


def _existing_other_objects(cur) -> set[str]:
    """序列与视图名称（重名检查用）。"""
    cur.execute(
        "SELECT relname FROM pg_class c JOIN pg_namespace n "
        "ON n.oid = c.relnamespace "
        "WHERE n.nspname = 'public' AND c.relkind IN ('S', 'v', 'm')"
    )
    return {row[0] for row in cur.fetchall()}


def _table_constraints(cur, table: str) -> list[str]:
    cur.execute(
        "SELECT conname FROM pg_constraint con "
        "JOIN pg_class c ON c.oid = con.conrelid "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = 'public' AND c.relname = %s "
        "AND con.contype IN ('p', 'u', 'x')",
        (table,),
    )
    return [row[0] for row in cur.fetchall()]


def _plain_indexes(cur, table: str) -> list[str]:
    """非约束支撑的普通索引（约束支撑索引随约束改名）。"""
    cur.execute(
        "SELECT i.relname FROM pg_index idx "
        "JOIN pg_class c ON c.oid = idx.indrelid "
        "JOIN pg_class i ON i.oid = idx.indexrelid "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = 'public' AND c.relname = %s "
        "AND idx.indisprimary = false "
        "AND NOT EXISTS (SELECT 1 FROM pg_constraint con "
        " WHERE con.conindid = idx.indexrelid)",
        (table,),
    )
    return [row[0] for row in cur.fetchall()]


def _owned_sequences(cur, table: str) -> list[str]:
    cur.execute(
        "SELECT s.relname FROM pg_depend d "
        "JOIN pg_class t ON t.oid = d.refobjid "
        "JOIN pg_class s ON s.oid = d.objid "
        "JOIN pg_namespace n ON n.oid = s.relnamespace "
        "WHERE n.nspname = 'public' AND t.relname = %s "
        "AND d.classid = 'pg_class'::regclass "
        "AND d.refclassid = 'pg_class'::regclass "
        "AND s.relkind = 'S'",
        (table,),
    )
    return [row[0] for row in cur.fetchall()]


def _dependent_views(cur, tables: tuple[str, ...]) -> list[tuple[str, str]]:
    """直接或间接依赖指定表的普通/物化视图。"""
    cur.execute(
        """
        WITH RECURSIVE deps AS (
            SELECT DISTINCT dependent.oid AS view_oid
            FROM pg_depend d
            JOIN pg_rewrite r ON r.oid = d.objid
            JOIN pg_class dependent ON dependent.oid = r.ev_class
            JOIN pg_class source ON source.oid = d.refobjid
            JOIN pg_namespace n ON n.oid = source.relnamespace
            WHERE d.classid = 'pg_rewrite'::regclass
              AND d.refclassid = 'pg_class'::regclass
              AND n.nspname = 'public'
              AND source.relname = ANY(%s)
              AND dependent.oid <> source.oid
        UNION
            SELECT DISTINCT r2.ev_class
            FROM deps
            JOIN pg_depend d2 ON d2.refobjid = deps.view_oid
            JOIN pg_rewrite r2 ON r2.oid = d2.objid
            WHERE d2.classid = 'pg_rewrite'::regclass
        )
        SELECT c.relname, c.relkind FROM pg_class c
        JOIN deps ON deps.view_oid = c.oid
        """,
        (list(tables),),
    )
    return [(row[0], row[1]) for row in cur.fetchall()]


def build_rename_statements(cur, ts: str) -> tuple[list[str], set[str]]:
    """生成旧表及其关联对象的改名语句；全新空库返回空列表。

    返回 (语句列表, 将被改名释放的原名称集合)；后者供占用断言把
    「将被改名让出名称的旧对象」与新对象区分开。
    """
    statements: list[str] = []
    taken = _existing_tables(cur) | _existing_other_objects(cur)
    existing = _existing_tables(cur)
    present = [name for name in OLD_TABLES if name in existing]
    renamed_originals: set[str] = set(present)

    def reserve(base: str) -> str:
        candidate = _renamed(base, ts)
        unique = candidate
        serial = 2
        while unique in taken:
            tail = f"_{serial}"
            unique = _renamed(base, ts)[: MAX_IDENT - len(tail)] + tail
            serial += 1
        taken.add(unique)
        return unique

    # 视图先改名（引用旧表名不影响，改表名也不影响视图关联）；
    # 顺序上先视图或先表均可，PG 按对象 OID 维持关联。
    views = _dependent_views(cur, tuple(present))
    for view_name, _kind in sorted(views):
        new_name = reserve(view_name)
        renamed_originals.add(view_name)
        statements.append(
            f'ALTER VIEW {_qualified(view_name)} RENAME TO "{new_name}";'
        )

    for table in present:
        new_table = reserve(table)
        statements.append(
            f'ALTER TABLE {_qualified(table)} RENAME TO "{new_table}";'
        )
        for constraint in _table_constraints(cur, table):
            new_constraint = reserve(constraint)
            renamed_originals.add(constraint)
            # 重命名约束会同步重命名其支撑索引（doc 6.1 §10.0）
            statements.append(
                f'ALTER TABLE {_qualified(new_table)} '
                f'RENAME CONSTRAINT "{constraint}" TO "{new_constraint}";'
            )
        for index in _plain_indexes(cur, table):
            new_index = reserve(index)
            renamed_originals.add(index)
            statements.append(
                f'ALTER INDEX {_qualified(index)} RENAME TO "{new_index}";'
            )
        for sequence in _owned_sequences(cur, table):
            new_sequence = reserve(sequence)
            renamed_originals.add(sequence)
            statements.append(
                f'ALTER SEQUENCE {_qualified(sequence)} RENAME TO "{new_sequence}";'
            )
    return statements, renamed_originals


def _assert_targets_absent(cur, renamed_originals: set[str]) -> None:
    """新对象名已存在且不在本次改名释放范围内时拒绝执行。"""
    existing = _existing_tables(cur) | _existing_other_objects(cur)
    conflicts = [
        name
        for name in (*NEW_TABLES, *NEW_SEQUENCES, NEW_VIEW)
        if name in existing and name not in renamed_originals
    ]
    if conflicts:
        raise SystemExit(
            "目标对象已存在，拒绝执行以防覆盖："
            + "、".join(sorted(conflicts))
            + "。如需重建请先人工处理（本脚本不删除任何对象）。"
        )


def _split_statements(sql_text: str) -> list[str]:
    """按分号切分静态 DDL；文件不含函数体，无嵌套分号问题。"""
    statements = []
    buffer = ""
    for line in sql_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("--") or not stripped:
            if not buffer:
                continue
        buffer += line + "\n"
        if stripped.endswith(";"):
            statements.append(buffer.strip())
            buffer = ""
    if buffer.strip():
        statements.append(buffer.strip())
    return statements


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只打印将执行的语句，不连接执行",
    )
    parser.add_argument(
        "--database",
        default=None,
        help="覆盖 PLATFORM_DB_NAME（用于隔离测试库演练）",
    )
    parser.add_argument(
        "--timestamp",
        default=None,
        help="指定旧表改名批次时间戳（yyyyMMddHHmmss）；默认当前时间",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="跳过交互确认（自动回答 yes）",
    )
    args = parser.parse_args()

    load_project_env()
    settings = load_platform_settings_from_env()
    database = args.database or settings.name
    ts = args.timestamp or datetime.now().strftime("%Y%m%d%H%M%S")
    ddl_statements = _split_statements(SQL_FILE.read_text(encoding="utf-8"))

    with psycopg.connect(
        host=settings.host,
        port=settings.port,
        dbname=database,
        user=settings.user,
        password=settings.password,
    ) as conn:
        # 规划与断言只读查询；dry-run 到此为止
        with conn.cursor() as cur:
            renames, renamed_originals = build_rename_statements(cur, ts)
            _assert_targets_absent(cur, renamed_originals)
        all_statements = renames + ddl_statements

        print(f"# 目标库: {database}@{settings.host}:{settings.port}")
        print(
            f"# 改名批次时间戳: {ts}"
            f"（改名语句 {len(renames)} 条 + 新对象 DDL {len(ddl_statements)} 条）"
        )
        for statement in all_statements:
            print(statement)
            print()

        if args.dry_run:
            print("# dry-run：未执行任何语句")
            return 0

        if not args.yes:
            confirm = input(
                f"确认在库 {database} 执行以上 {len(all_statements)} 条语句？输入 yes 确认: "
            )
            if confirm.strip() != "yes":
                print("已取消，未执行任何语句")
                return 1

        # 规划与执行放同一事务：执行期间若旧对象状态变化，整体回滚
        with conn.transaction():
            with conn.cursor() as cur:
                final_renames, final_originals = build_rename_statements(cur, ts)
                if final_renames != renames:
                    raise SystemExit("规划期间旧对象发生变化，已回滚；请重试")
                _assert_targets_absent(cur, final_originals)
                for statement in all_statements:
                    cur.execute(statement)

    print("重建完成：旧表已按批次改名留存，新表与视图已创建。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
