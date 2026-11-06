"""为发布并行安全前置补齐 schema（doc 10.2 §8），保留现有数据。

在 metaweave/ 目录执行：
    .venv/bin/python scripts/upgrade_publish_parallel_schema.py          # 仅预览 SQL
    .venv/bin/python scripts/upgrade_publish_parallel_schema.py --check  # 只读检查
    .venv/bin/python scripts/upgrade_publish_parallel_schema.py --apply  # 事务内更新

连接地址、账号、密码沿用 PLATFORM_DB_*；数据库名固定为演练库，不使用
.env 中可能仍为 metaweave 的 PLATFORM_DB_NAME（与 upgrade_deletion_schema
同一约定）。全部语句幂等，可重复执行；不修改数据行。
"""

from __future__ import annotations

import argparse
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
SQL_FILE = Path(__file__).resolve().parents[1] / "db" / "upgrade_publish_parallel_schema.sql"
REQUIRED_COLUMNS = {
    "publish_run_info": {"execution_plan"},
}
REQUIRED_TABLES = {"publish_target_guard"}


def _check(conn) -> list[str]:
    problems: list[str] = []
    with conn.cursor() as cur:
        for table, columns in REQUIRED_COLUMNS.items():
            cur.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = %s",
                (table,),
            )
            present = {row[0] for row in cur.fetchall()}
            for column in sorted(columns):
                if column not in present:
                    problems.append(f"缺少列 public.{table}.{column}")
        for table in sorted(REQUIRED_TABLES):
            cur.execute(
                "SELECT to_regclass(%s)",
                (f"public.{table}",),
            )
            if cur.fetchone()[0] is None:
                problems.append(f"缺少表 public.{table}")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--check", action="store_true", help="只读检查")
    group.add_argument("--apply", action="store_true", help="事务内执行更新")
    parser.add_argument(
        "--database",
        help="显式指定目标平台库，覆盖 PLATFORM_DB_NAME"
        "（默认仅允许演练库；开发库须显式指定）",
    )
    args = parser.parse_args()

    load_project_env()
    settings = load_platform_settings_from_env()
    if args.database is not None:
        if not args.database.strip():
            print("--database 不能为空")
            return 2
        from dataclasses import replace as _replace

        settings = _replace(settings, name=args.database.strip())
    elif settings.name != TARGET_DATABASE:
        print(
            f"拒绝执行：PLATFORM_DB_NAME={settings.name!r}，"
            f"本脚本默认只针对演练库 {TARGET_DATABASE!r}；"
            "更新其他开发库请显式传 --database"
        )
        return 2
    statements = [
        stmt.strip()
        for stmt in (
            "\n".join(
                line for line in chunk.splitlines() if not line.lstrip().startswith("--")
            )
            for chunk in SQL_FILE.read_text().split(";")
        )
        if stmt.strip()
    ]
    print(f"目标库：{settings.host}:{settings.port}/{settings.name}")
    for stmt in statements:
        preview = " ".join(stmt.split())[:100]
        print(f"  {preview}...")

    conn = psycopg.connect(
        host=settings.host,
        port=settings.port,
        dbname=settings.name,
        user=settings.user,
        password=settings.password,
    )
    try:
        if args.check:
            problems = _check(conn)
            for item in problems:
                print(f"[缺失] {item}")
            print("检查完成" if not problems else "存在缺失，需 --apply")
            return 0 if not problems else 1
        if not args.apply:
            print("预览模式，未执行；--apply 执行，--check 只读检查")
            return 0
        with conn.transaction():
            for stmt in statements:
                with conn.cursor() as cur:
                    cur.execute(stmt)
        problems = _check(conn)
        if problems:
            for item in problems:
                print(f"[仍缺失] {item}")
            return 1
        print("应用完成：publish_run_info.execution_plan 与 publish_target_guard 已就绪")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
