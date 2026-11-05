"""离线收尾遗留 pending/running 运行（doc 8.1 §5.4）。

只处理显式指定的一条采集或发布运行。默认只预览；实际变更要求
``--apply --confirm-executors-stopped`` 与必填 ``--reason``。确认参数是
运维承诺（已停止全部可能执行该任务的进程），不是脚本自动探测。
``--finalize-only`` 仅汇总已全部结束的步骤，拒绝更改未结束步骤。

用法：
    uv run python scripts/resolve_orphaned_run.py collection <run_id> \
        --reason "维护窗口收尾" [--apply --confirm-executors-stopped]
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_UNFINISHED = ("pending", "running")
_STEP_TERMINAL = ("success", "failed", "blocked")


def _spec_for(kind: str):
    if kind == "collection":
        from backend.jobs.run_repository import COLLECTION_RUN

        return COLLECTION_RUN
    if kind == "publish":
        from backend.jobs.run_repository import PUBLISH_RUN

        return PUBLISH_RUN
    raise SystemExit(f"未知 kind: {kind!r}（仅支持 collection / publish）")


def _planned_status(steps, *, finalize_only: bool):
    from backend.jobs.run_repository import rollup_run_status

    if not steps:
        return None, "运行没有步骤，拒绝自动汇总"
    statuses = [step.status for step in steps]
    if any(status not in (*_STEP_TERMINAL, *_UNFINISHED) for status in statuses):
        return None, "存在无法识别的步骤状态，拒绝自动汇总"
    if finalize_only and any(status in _UNFINISHED for status in statuses):
        return None, "仍有未结束步骤，仅汇总模式拒绝处理"
    terminal_for = {"running": "failed", "pending": "blocked"}
    resolved = [terminal_for.get(status, status) for status in statuses]
    return rollup_run_status(resolved), None


def _timestamp(value):
    return value.isoformat() if value is not None else None


def preview(session, kind: str, run_id: UUID, *, finalize_only: bool = False) -> dict:
    """只读预览：运行与步骤现状、计划变更、前置条件提醒。"""
    from backend.jobs.run_repository import (
        get_run,
        job_of_run,
        list_run_steps,
    )

    spec = _spec_for(kind)
    run = get_run(spec, session, run_id)
    if run is None:
        return {"error": "运行不存在"}
    _info, job = job_of_run(spec, session, run_id)
    if job is None:
        return {"error": "运行所属作业不存在"}
    from backend.persistence.models import WorkspaceInfo

    workspace = session.get(WorkspaceInfo, job.workspace_id)
    if workspace is None:
        return {"error": "运行所属工作区不存在"}
    steps = list_run_steps(spec, session, run_id)
    final, error = _planned_status(steps, finalize_only=finalize_only)
    actionable = run.status in _UNFINISHED
    transitions = {"running": "running→failed", "pending": "pending→blocked"}
    planned = [
        {
            "step": step.step_name,
            "status": step.status,
            "started_at": _timestamp(getattr(step, "started_at", None)),
            "finished_at": _timestamp(getattr(step, "finished_at", None)),
            "transition": (
                "保持不变"
                if finalize_only or not actionable or error
                else transitions.get(step.status, "保持不变")
            ),
        }
        for step in steps
    ]
    return {
        "kind": kind,
        "run_id": str(run_id),
        "run_status": run.status,
        "source_id": workspace.source_id,
        "job_id": getattr(job, "collection_job_id", None)
        or getattr(job, "publish_job_id", None),
        "workspace_id": getattr(job, "workspace_id", None),
        "created_at": _timestamp(getattr(run, "created_at", None)),
        "started_at": _timestamp(getattr(run, "started_at", None)),
        "finished_at": _timestamp(getattr(run, "finished_at", None)),
        "steps": planned,
        "mode": "finalize_only" if finalize_only else "resolve_unfinished",
        "planned_final_status": final if actionable else run.status,
        "actionable": actionable and error is None,
        "refusal_reason": error if actionable else "运行已是终态，无需处理",
        "notice": (
            "执行 apply 前必须确认所有可能执行该任务的进程已停止；"
            "本脚本不删除文件、不改 marker、不清理发布内容"
        ),
    }


def apply_resolution(
    session, kind: str, run_id: UUID, reason: str, *, finalize_only: bool = False
) -> dict:
    """确认无执行者后收尾：步骤 running→failed、pending→blocked，汇总终态。"""
    from backend.jobs.run_repository import (
        finish_step,
        finalize_run,
        get_run,
        job_of_run,
    )
    from backend.persistence.models import DbSourceInfo, WorkspaceInfo
    from sqlalchemy import select

    reason = reason.strip()
    if not reason:
        return {"changed": False, "error": "处理原因不能为空"}

    spec = _spec_for(kind)
    run = get_run(spec, session, run_id)
    if run is None:
        return {"error": "运行不存在"}
    _info, job = job_of_run(spec, session, run_id)
    if run.status not in _UNFINISHED:
        return {"changed": False, "message": f"无需处理：运行已是终态 {run.status}"}

    # §5.1.1 锁顺序：源 → 工作区 → 作业 → 运行
    if job is None:
        return {"changed": False, "error": "运行所属作业不存在"}
    job_id = getattr(job, spec.job_id_column)
    workspace_id = job.workspace_id
    workspace = session.get(WorkspaceInfo, workspace_id)
    if workspace is None:
        return {"changed": False, "error": "运行所属工作区不存在"}
    source_id = workspace.source_id
    lock_options = {"with_for_update": True, "populate_existing": True}
    source = session.get(DbSourceInfo, source_id, **lock_options)
    workspace = session.get(WorkspaceInfo, workspace_id, **lock_options)
    job = session.get(spec.job_table, job_id, **lock_options)
    run = session.get(spec.info_table, run_id, **lock_options)
    if (
        source is None
        or workspace is None
        or job is None
        or run is None
        or workspace.source_id != source_id
        or job.workspace_id != workspace_id
        or getattr(run, spec.job_id_column) != job_id
    ):
        return {"changed": False, "error": "运行所属对象已变化，请重新预览"}
    if run.status not in _UNFINISHED:
        return {
            "changed": False,
            "message": f"无需处理：加锁后运行已是终态 {run.status}",
        }

    # 锁定并刷新步骤，不能沿用预览或 Session 缓存中的旧状态。
    steps = list(
        session.scalars(
            select(spec.step_table)
            .where(getattr(spec.step_table, spec.run_id_column) == run_id)
            .order_by(spec.step_table.step_name)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    )
    _final, error = _planned_status(steps, finalize_only=finalize_only)
    if error:
        return {"changed": False, "error": error}
    processed_at = datetime.now(timezone.utc).isoformat()
    note = (
        f"{'仅汇总修复' if finalize_only else '离线收尾'}：原状态 {run.status}；"
        f"处理时间 {processed_at}；执行者已停止（人工确认）；"
        f"处理原因：{reason}；实际外部写入结果未核验"
    )
    changes = []
    for step in steps:
        if step.status == "running":
            finish_step(spec, session, run_id, step.step_name, "failed", note)
            changes.append({"step": step.step_name, "to": "failed"})
        elif step.status == "pending":
            finish_step(spec, session, run_id, step.step_name, "blocked", note)
            changes.append({"step": step.step_name, "to": "blocked"})
    if hasattr(run, "message"):
        run.message = f"{run.message}\n{note}" if run.message else note
    final = finalize_run(spec, session, run_id)
    session.commit()
    return {
        "changed": True,
        "final_status": final,
        "steps": changes,
        "note": note,
        "source_id": source_id,
        "job_id": job_id,
        "run_id": str(run_id),
    }


def main(argv: list[str] | None = None) -> int:
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
    with platform_session(settings) as session:
        if not args.apply:
            result = preview(
                session, args.kind, run_id, finalize_only=args.finalize_only
            )
        else:
            result = apply_resolution(
                session,
                args.kind,
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
