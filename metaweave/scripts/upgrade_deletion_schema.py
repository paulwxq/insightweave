"""为 metaweave_rehearsal_final 补齐删除功能表结构，保留现有数据。

在 metaweave/ 目录执行：
    .venv/bin/python scripts/upgrade_deletion_schema.py          # 仅预览 SQL
    .venv/bin/python scripts/upgrade_deletion_schema.py --check  # 只读检查
    .venv/bin/python scripts/upgrade_deletion_schema.py --apply  # 事务内更新

连接地址、账号、密码沿用 PLATFORM_DB_*；数据库名固定，不使用 .env 中
可能仍为 metaweave 的 PLATFORM_DB_NAME。不修改数据行、序列值或已有版本。
"""

from __future__ import annotations

import argparse
from dataclasses import replace
from pathlib import Path
import sys

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import psycopg

from backend.persistence.platform_db import (
    load_platform_settings_from_env,
    load_project_env,
)

TARGET_DATABASE = "metaweave_rehearsal_final"
SQL_FILE = Path(__file__).resolve().parents[1] / "db" / "upgrade_deletion_schema.sql"
REQUIRED_COLUMNS = {
    "db_source_info": {"deleted_at", "deletion_state"},
    "workspace_info": {"deleted_at", "deletion_state"},
    "collection_job_info": {"deleted_at"},
    "publish_job_info": {"deleted_at"},
    "resource_deletion_task": {
        "task_id", "action", "target_id", "source_id", "workspace_id", "status",
        "step_results", "error_message", "created_at", "queued_at", "started_at",
        "finished_at", "requested_by_user_id",
    },
}
REQUIRED_CHECKS = {
    "db_source_info_deletion_state_check",
    "db_source_info_deletion_state_time_check",
    "db_source_info_connection_presence_check",
    "workspace_info_deletion_state_check",
    "workspace_info_deletion_state_time_check",
    "resource_deletion_task_action_check",
    "resource_deletion_task_status_check",
    "resource_deletion_task_workspace_required_check",
}
REQUIRED_INDEXES = {
    "workspace_info_active_name_idx",
    "collection_job_info_active_name_idx",
    "publish_job_info_active_name_idx",
    "resource_deletion_task_one_active_source_idx",
}
OLD_NAME_CONSTRAINTS = {
    "workspace_info_source_name_key",
    "collection_job_info_workspace_name_key",
    "publish_job_info_workspace_name_key",
}
NULLABLE_CONNECTION_COLUMNS = {
    "host", "port", "db_name", "username", "password_ciphertext", "selected_schemas",
}


def require_target(conn) -> None:
    actual = conn.execute("SELECT current_database()").fetchone()[0]
    if actual != TARGET_DATABASE:
        raise ValueError(f"拒绝操作数据库 {actual!r}，目标必须为 {TARGET_DATABASE}")


def schema_problems(conn) -> list[str]:
    """只读核验本次结构变更；不读取连接凭据或业务数据。"""
    require_target(conn)
    columns = conn.execute(
        "SELECT table_name, column_name, is_nullable FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = ANY(%s)",
        (list(REQUIRED_COLUMNS),),
    ).fetchall()
    by_table = {table: {} for table in REQUIRED_COLUMNS}
    for table, name, nullable in columns:
        by_table[table][name] = nullable
    problems = []
    for table, required in REQUIRED_COLUMNS.items():
        for name in sorted(required - by_table[table].keys()):
            problems.append(f"缺少 public.{table}.{name}")
    for name in sorted(NULLABLE_CONNECTION_COLUMNS):
        if by_table["db_source_info"].get(name) != "YES":
            problems.append(f"db_source_info.{name} 尚未允许 NULL")
    for table in ("db_source_info", "workspace_info"):
        if by_table[table].get("deletion_state") != "NO":
            problems.append(f"{table}.deletion_state 必须为 NOT NULL")
    for name in ("created_at", "queued_at"):
        if by_table["resource_deletion_task"].get(name) != "NO":
            problems.append(f"resource_deletion_task.{name} 必须为 NOT NULL")
    checks = {
        row[0] for row in conn.execute(
            "SELECT conname FROM pg_constraint c "
            "JOIN pg_namespace n ON n.oid = c.connamespace "
            "WHERE n.nspname = 'public' AND c.contype = 'c'"
        )
    }
    for name in sorted(REQUIRED_CHECKS - checks):
        problems.append(f"缺少 CHECK {name}")
    indexes = {
        row[0] for row in conn.execute(
            "SELECT indexname FROM pg_indexes WHERE schemaname = 'public'"
        )
    }
    for name in sorted(REQUIRED_INDEXES - indexes):
        problems.append(f"缺少索引 {name}")
    old = {
        row[0] for row in conn.execute(
            "SELECT conname FROM pg_constraint WHERE conrelid IN "
            "(SELECT oid FROM pg_class WHERE relnamespace = 'public'::regnamespace) "
            "AND contype = 'u' AND conname = ANY(%s)",
            (list(OLD_NAME_CONSTRAINTS),),
        )
    }
    for name in sorted(old):
        problems.append(f"旧全量名称唯一约束尚未移除：{name}")
    return problems


def apply_update(conn) -> None:
    """DDL 与完成核验处于同一事务，失败不留下部分变更。"""
    with conn.transaction():
        require_target(conn)
        conn.execute("SET LOCAL lock_timeout = '5s'")
        conn.execute("SET LOCAL statement_timeout = '60s'")
        conn.execute(SQL_FILE.read_text(encoding="utf-8"), prepare=False)
        problems = schema_problems(conn)
        if problems:
            raise ValueError("更新后核验未通过：\n" + "\n".join(problems))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="实际执行结构更新")
    mode.add_argument("--check", action="store_true", help="只读检查结构是否齐全")
    args = parser.parse_args(argv)
    if not args.apply and not args.check:
        print(f"仅预览，目标库：{TARGET_DATABASE}；加 --apply 才会执行。")
        print(SQL_FILE.read_text(encoding="utf-8"))
        return 0

    load_project_env()
    settings = replace(load_platform_settings_from_env(), name=TARGET_DATABASE)
    kwargs = dict(
        host=settings.host, port=settings.port, dbname=settings.name,
        user=settings.user, password=settings.password, connect_timeout=10,
        autocommit=True,
    )
    if settings.sslmode is not None:
        kwargs["sslmode"] = settings.sslmode
    if args.check:
        kwargs["options"] = "-c default_transaction_read_only=on"
    try:
        with psycopg.connect(**kwargs) as conn:
            if args.apply:
                apply_update(conn)
                print(f"{TARGET_DATABASE} 结构更新并核验成功，现有数据保留。")
                return 0
            problems = schema_problems(conn)
            if problems:
                print("结构尚未齐全：\n" + "\n".join(problems))
                return 2
            print(f"{TARGET_DATABASE} 删除功能所需结构检查通过。")
            return 0
    except (psycopg.Error, ValueError) as exc:
        # 不输出连接串、密码或整份配置。
        detail = exc.diag.message_primary if isinstance(exc, psycopg.Error) else str(exc)
        print(f"操作失败：{detail or type(exc).__name__}", file=sys.stderr)
        if args.apply:
            print("未完成的结构更新已回滚。", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
