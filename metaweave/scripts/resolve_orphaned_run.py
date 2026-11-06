"""离线收尾遗留 pending/running 运行（doc 8.1 §5.4）。

只处理显式指定的一条采集或发布运行。默认只预览；实际变更要求
``--apply --confirm-executors-stopped`` 与必填 ``--reason``。确认参数是
运维承诺（已停止全部可能执行该任务的进程），不是脚本自动探测。
``--finalize-only`` 仅汇总已全部结束的步骤，拒绝更改未结束步骤。

核心逻辑在 backend/jobs/run_resolution.py（doc 11 §4.1，与 Web 终止
端点共享）；本脚本只负责参数解析与 JSON 输出。

doc 10.2 §5.1/§8：另提供 Neo4j 目标保留记录（publish_target_guard）
的预览与人工清除入口——保留记录只能人工核验旧执行者与外部请求已结束、
核对目标结果后清除；Web/CLI 运行收尾一律不清除它。

用法：
    uv run python scripts/resolve_orphaned_run.py collection <run_id> \
        --reason "维护窗口收尾" [--apply --confirm-executors-stopped]
    uv run python scripts/resolve_orphaned_run.py --list-target-guards
    uv run python scripts/resolve_orphaned_run.py --clear-target-guard <key> \
        [--confirm-external-verified]
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from pathlib import Path
from uuid import UUID

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.jobs.run_resolution import apply_resolution, preview_resolution

_UNFINISHED = ("pending", "running")


def _spec_for(kind: str):
    if kind == "collection":
        from backend.jobs.run_repository import COLLECTION_RUN

        return COLLECTION_RUN
    if kind == "publish":
        from backend.jobs.run_repository import PUBLISH_RUN

        return PUBLISH_RUN
    raise SystemExit(f"未知 kind: {kind!r}（仅支持 collection / publish）")


def _list_target_guards() -> int:
    """doc 10.2 §8：目标保留记录只读预览。"""
    from sqlalchemy import select

    from backend.persistence.models import PublishTargetGuard
    from backend.persistence.platform_db import (
        load_platform_settings_from_env,
        load_project_env,
        platform_session,
    )

    load_project_env()
    settings = load_platform_settings_from_env()
    with platform_session(settings) as session:
        rows = list(
            session.scalars(
                select(PublishTargetGuard).order_by(
                    PublishTargetGuard.reserved_at.desc()
                )
            )
        )
    payload = {
        "platform_database": settings.name,
        "guards": [
            {
                "target_key": row.target_key,
                "owner_kind": row.owner_kind,
                "owner_id": row.owner_id,
                "reserved_at": (
                    row.reserved_at.isoformat() if row.reserved_at else None
                ),
            }
            for row in rows
        ],
    }
    payload["hint"] = (
        "清除前须人工核验：旧执行者与外部请求已结束、目标结果已核对"
        "（doc 10.2 §5.1）；确认后用 --clear-target-guard <key> "
        "--confirm-external-verified"
        if rows
        else "无保留记录"
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def _clear_target_guard(target_key: str | None, confirmed: bool) -> int:
    """doc 10.2 §5.1：人工核验后按 key 清除单条保留记录。"""
    from backend.persistence.models import PublishTargetGuard
    from backend.persistence.platform_db import (
        load_platform_settings_from_env,
        load_project_env,
        platform_session,
    )

    if not target_key:
        print("--clear-target-guard 需要一个 target_key（先用 --list-target-guards 查看）")
        return 2
    load_project_env()
    settings = load_platform_settings_from_env()
    with platform_session(settings) as session:
        guard = session.get(PublishTargetGuard, target_key)
        if guard is None:
            print(json.dumps({"cleared": False, "target_key": target_key,
                              "message": "保留记录不存在"}, ensure_ascii=False))
            return 1
        if not confirmed:
            print(json.dumps({
                "cleared": False,
                "target_key": guard.target_key,
                "owner_kind": guard.owner_kind,
                "owner_id": guard.owner_id,
                "reserved_at": guard.reserved_at.isoformat() if guard.reserved_at else None,
                "message": "预览：确认旧执行者与外部请求已结束并核对目标结果后，"
                           "加 --confirm-external-verified 执行清除",
            }, ensure_ascii=False, indent=2))
            return 0
        session.delete(guard)
        session.commit()
    print(json.dumps({"cleared": True, "target_key": target_key},
                     ensure_ascii=False))
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # doc 10.2 §8：目标保留记录运维入口（与运行收尾互不混用）
    if "--list-target-guards" in argv:
        return _list_target_guards()
    if "--clear-target-guard" in argv:
        idx = argv.index("--clear-target-guard")
        key = argv[idx + 1] if idx + 1 < len(argv) else None
        return _clear_target_guard(
            key, confirmed="--confirm-external-verified" in argv
        )
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kind", choices=["collection", "publish"])
    parser.add_argument("run_id", type=UUID)
    parser.add_argument("--database", help="显式指定平台库，覆盖 PLATFORM_DB_NAME")
    parser.add_argument(
        "--finalize-only",
        action="store_true",
        help="仅汇总非空且全部已结束的步骤，不修改任何步骤状态",
    )
    parser.add_argument(
        "--reason", required=True, help="必填处理原因，写入运行与步骤 message"
    )
    parser.add_argument("--apply", action="store_true", help="实际变更（默认只预览）")
    parser.add_argument(
        "--confirm-executors-stopped",
        action="store_true",
        help="运维承诺：所有可能执行该任务的进程均已停止",
    )
    args = parser.parse_args(argv)
    if args.apply and not args.confirm_executors_stopped:
        print("--apply 需要 --confirm-executors-stopped（运维承诺执行者已停止）")
        return 2
    if not args.reason.strip():
        parser.error("--reason 不能为空")

    from backend.persistence.platform_db import (
        load_platform_settings_from_env,
        load_project_env,
        platform_session,
    )

    load_project_env()
    settings = load_platform_settings_from_env()
    if args.database is not None:
        if not args.database.strip():
            parser.error("--database 不能为空")
        settings = replace(settings, name=args.database.strip())
    run_id = args.run_id
    spec = _spec_for(args.kind)
    with platform_session(settings) as session:
        if not args.apply:
            result = preview_resolution(
                session, spec, run_id, finalize_only=args.finalize_only
            )
        else:
            result = apply_resolution(
                session,
                spec,
                run_id,
                args.reason,
                finalize_only=args.finalize_only,
            )
    print(
        json.dumps(
            {"platform_database": settings.name, **result}, ensure_ascii=False, indent=2
        )
    )
    if "error" in result:
        return 1
    if (
        not args.apply
        and result.get("refusal_reason")
        and result.get("run_status") in _UNFINISHED
    ):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
