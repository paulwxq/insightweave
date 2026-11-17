"""遗留 pending/running 运行的收尾核心逻辑（doc 8.1 §5.4 → doc 11 §4.1）。

CLI 脚本 scripts/resolve_orphaned_run.py 与 Web 终止端点共用本模块，
避免两份实现漂移。锁序（源 → 工作区 → 作业 → 运行 → 全部步骤）、
复查幂等、状态迁移（running→failed、pending→blocked）与汇总裁决
全部与 doc 8.1 §5.4 保持一致；``origin`` 参数仅区分 note 前缀来源
（CLI「离线收尾」/ Web「Web 终止」，doc 11 §4.1）。
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

_UNFINISHED = ("pending", "running")
_STEP_TERMINAL = ("success", "failed", "blocked")

# 运行不存在的错误文案单一来源：preview/apply 返回与 Web 端点的 404 判定
# 共用同一常量，避免文案调整时 404 静默退化为 400（doc 11 §4.3）
RUN_NOT_FOUND_MESSAGE = "运行不存在"


def _kind_of(spec) -> str:
    """预览输出的 kind 展示字段：内置 spec 走身份判定，自定义 spec 按
    run_id_column 推导（如 "publish_run_id" → "publish"），不抛错。"""
    from backend.jobs.run_repository import COLLECTION_RUN, PUBLISH_RUN

    if spec is COLLECTION_RUN:
        return "collection"
    if spec is PUBLISH_RUN:
        return "publish"
    return getattr(spec, "run_id_column", "").removesuffix("_run_id") or "unknown"


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


def preview_resolution(session, spec, run_id: UUID, *, finalize_only: bool = False) -> dict:
    """只读预览：运行与步骤现状、计划变更、前置条件提醒。"""
    from backend.jobs.run_repository import (
        get_run,
        job_of_run,
        list_run_steps,
    )

    kind = _kind_of(spec)
    run = get_run(spec, session, run_id)
    if run is None:
        return {"error": RUN_NOT_FOUND_MESSAGE}
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
    session,
    spec,
    run_id: UUID,
    reason: str,
    *,
    finalize_only: bool = False,
    origin: str = "离线收尾",
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

    run = get_run(spec, session, run_id)
    if run is None:
        return {"error": RUN_NOT_FOUND_MESSAGE}
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
        f"{'仅汇总修复' if finalize_only else origin}：原状态 {run.status}；"
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
