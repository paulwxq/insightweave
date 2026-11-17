"""resource_deletion_task 持久化与删除准入查询（doc 8.1 §5.1/§7/§7.0）。

实体查询一律 ``session.scalars``（返回 ORM 对象）；过滤条件写进 SQL，
不在 Python 侧加载全表再筛。
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from backend.persistence.models import (
    CollectionJobInfo,
    CollectionRunInfo,
    PublishJobInfo,
    PublishRunInfo,
    ResourceDeletionTask,
    WorkspaceInfo,
)

UNRESOLVED_STATUSES = ("pending", "running", "failed")
ACTIVE_STATUSES = ("pending", "running")
UNFINISHED_RUN_STATUSES = ("pending", "running")

_SUMMARY_FIELDS = (
    "task_id",
    "action",
    "status",
    "source_id",
    "workspace_id",
    "target_id",
    "error_message",
)


def db_now(session: Session) -> datetime:
    """数据库当前时间（doc 8.1 §7.2：保护期判断不用实例本地时钟）。"""
    return session.scalar(select(func.now()))


def insert_task(
    session: Session,
    *,
    action: str,
    target_id: str,
    source_id: str,
    workspace_id: str | None = None,
) -> ResourceDeletionTask:
    task = ResourceDeletionTask(
        task_id=uuid4(),
        action=action,
        target_id=target_id,
        source_id=source_id,
        workspace_id=workspace_id,
        status="pending",
    )
    session.add(task)
    session.commit()
    return task


def get_task(session: Session, task_id: UUID) -> ResourceDeletionTask | None:
    return session.get(ResourceDeletionTask, task_id)


def claim_task(session: Session, task_id: UUID) -> bool:
    """pending → running 原子认领；仅条件更新返回行的一个执行者生效。"""
    stmt = (
        update(ResourceDeletionTask)
        .where(
            ResourceDeletionTask.task_id == task_id,
            ResourceDeletionTask.status == "pending",
        )
        .values(status="running", started_at=func.now())
        .execution_options(synchronize_session=False)
    )
    return bool(session.execute(stmt).rowcount)


def retry_transition(session: Session, task_id: UUID) -> bool:
    """failed → pending；同事务显式刷新 queued_at，重置 started/finished。"""
    stmt = (
        update(ResourceDeletionTask)
        .where(
            ResourceDeletionTask.task_id == task_id,
            ResourceDeletionTask.status == "failed",
        )
        .values(
            status="pending",
            queued_at=func.now(),
            started_at=None,
            finished_at=None,
            error_message=None,
        )
        .execution_options(synchronize_session=False)
    )
    return bool(session.execute(stmt).rowcount)


def interrupt_task(session: Session, task_id: UUID, *, reason: str) -> bool:
    """running → failed（启动扫描/重试确认中断）；保留步骤进度。"""
    stmt = (
        update(ResourceDeletionTask)
        .where(
            ResourceDeletionTask.task_id == task_id,
            ResourceDeletionTask.status == "running",
        )
        .values(status="failed", finished_at=func.now(), error_message=reason)
        .execution_options(synchronize_session=False)
    )
    return bool(session.execute(stmt).rowcount)


def interrupt_pending_task(session: Session, task_id: UUID, *, reason: str) -> bool:
    """pending → failed（认领等待超时）；保留步骤进度。"""
    stmt = (
        update(ResourceDeletionTask)
        .where(
            ResourceDeletionTask.task_id == task_id,
            ResourceDeletionTask.status == "pending",
        )
        .values(status="failed", finished_at=func.now(), error_message=reason)
        .execution_options(synchronize_session=False)
    )
    return bool(session.execute(stmt).rowcount)


def mark_task_failed(session: Session, task_id: UUID, *, error_message: str) -> bool:
    stmt = (
        update(ResourceDeletionTask)
        .where(
            ResourceDeletionTask.task_id == task_id,
            ResourceDeletionTask.status == "running",
        )
        .values(status="failed", finished_at=func.now(), error_message=error_message)
        .execution_options(synchronize_session=False)
    )
    return bool(session.execute(stmt).rowcount)


def mark_task_success(session: Session, task_id: UUID) -> bool:
    stmt = (
        update(ResourceDeletionTask)
        .where(
            ResourceDeletionTask.task_id == task_id,
            ResourceDeletionTask.status == "running",
        )
        .values(status="success", finished_at=func.now(), error_message=None)
        .execution_options(synchronize_session=False)
    )
    return bool(session.execute(stmt).rowcount)


def record_step(session: Session, task_id: UUID, step: str, payload: dict) -> None:
    task = get_task(session, task_id)
    if task is None:
        return
    merged = dict(task.step_results or {})
    merged[step] = payload
    task.step_results = merged
    session.commit()


def _unresolved_tasks(
    session: Session,
    *,
    source_id: str | None = None,
    action: str | None = None,
) -> list:
    stmt = select(ResourceDeletionTask).where(
        ResourceDeletionTask.status.in_(UNRESOLVED_STATUSES)
    )
    if source_id is not None:
        stmt = stmt.where(ResourceDeletionTask.source_id == source_id)
    if action is not None:
        stmt = stmt.where(ResourceDeletionTask.action == action)
    return list(session.scalars(stmt))


def unresolved_task_for_source(
    session: Session, source_id: str, *, action: str | None = None
):
    """活动任务优先，其次最近失败；无未解决任务返回 None（§7.0/§7.3）。

    ``action`` 过滤用于 §7.3 规则 2：只有 clear_source_knowledge 任务
    冻结整源写入，不能只按 source_id 与状态匹配。
    """
    candidates = _unresolved_tasks(session, source_id=source_id, action=action)
    active = [t for t in candidates if t.status in ACTIVE_STATUSES]
    if active:
        return active[0]
    failed = [t for t in candidates if t.status == "failed"]
    if failed:
        return max(failed, key=lambda t: t.created_at)
    return None


def task_summary(session: Session, *, source_id: str, workspace_id: str | None = None):
    """§7.0 摘要：源视图覆盖三种 action；工作区视图只附本工作区相关任务。"""
    task = unresolved_task_for_source(session, source_id)
    if task is None:
        return None
    if workspace_id is not None and task.workspace_id is not None:
        if task.workspace_id != workspace_id:
            return None
    return {field: getattr(task, field) for field in _SUMMARY_FIELDS}


def unfinished_runs_in_scope(
    session: Session, *, source_id: str | None = None, workspace_id: str | None = None
) -> list[dict]:
    """范围内 pending/running 运行清单（§5.1：删除预览与 409 载荷）。

    按源或工作区在 SQL 内逐级过滤：工作区 ID → 作业 ID → 未结束运行。
    """
    if workspace_id is not None:
        ws_stmt = select(WorkspaceInfo.workspace_id).where(
            WorkspaceInfo.workspace_id == workspace_id
        )
    elif source_id is not None:
        ws_stmt = select(WorkspaceInfo.workspace_id).where(
            WorkspaceInfo.source_id == source_id
        )
    else:
        return []
    ws_ids = list(session.scalars(ws_stmt))
    if not ws_ids:
        return []

    collection_jobs = list(
        session.scalars(
            select(CollectionJobInfo).where(CollectionJobInfo.workspace_id.in_(ws_ids))
        )
    )
    publish_jobs = list(
        session.scalars(
            select(PublishJobInfo).where(PublishJobInfo.workspace_id.in_(ws_ids))
        )
    )

    blocking: list[dict] = []
    job_specs = (
        (
            "collection",
            collection_jobs,
            CollectionRunInfo,
            CollectionRunInfo.collection_job_id,
            lambda job: job.collection_job_id,
            CollectionRunInfo.collection_run_id,
        ),
        (
            "publish",
            publish_jobs,
            PublishRunInfo,
            PublishRunInfo.publish_job_id,
            lambda job: job.publish_job_id,
            PublishRunInfo.publish_run_id,
        ),
    )
    for kind, jobs, run_model, run_job_col, job_id_of, run_id_col in job_specs:
        if not jobs:
            continue
        job_ids = [job_id_of(job) for job in jobs]
        runs = list(
            session.scalars(
                select(run_model).where(
                    run_job_col.in_(job_ids),
                    run_model.status.in_(UNFINISHED_RUN_STATUSES),
                )
            )
        )
        for run in runs:
            job = next(
                (j for j in jobs if job_id_of(j) == getattr(run, run_job_col.key)),
                None,
            )
            if job is None:
                continue
            blocking.append(
                {
                    "type": kind,
                    "run_id": getattr(run, run_id_col.key),
                    "status": run.status,
                    "job_id": job_id_of(job),
                    "job_name": job.job_name,
                    "workspace_id": job.workspace_id,
                    "created_at": run.created_at,
                    "started_at": getattr(run, "started_at", None),
                }
            )
    return blocking
