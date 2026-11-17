"""统一访问守卫（doc 8.1 §8.1）。

普通访问按层级读取：不存在 404，目标或祖先 deleted 返回 410；
写入守卫再查父级 deleting/delete_failed 与源级知识清理冻结（§7.3）。
守卫读取包含已删除行，区分“已删”和“不存在”；路由只做错误映射。
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from backend.deletion import repository as deletion_repo
from backend.deletion.errors import (
    ParentResourceUnavailableError,
    ResourceDeletedError,
    ResourceNotFoundError,
    SourceKnowledgeFrozenError,
)
from backend.persistence.models import (
    CollectionJobInfo,
    DbSourceInfo,
    PublishJobInfo,
    WorkspaceInfo,
)

_BLOCKING_STATES = ("deleting", "delete_failed")


def _is_deleted(row) -> bool:
    return getattr(row, "deleted_at", None) is not None or (
        getattr(row, "deletion_state", "active") == "deleted"
    )


def _deletion_state(row) -> str:
    return getattr(row, "deletion_state", "active") or "active"


def _require_source_checked(
    session: Session, source: DbSourceInfo | None, *, for_write: bool
) -> DbSourceInfo:
    if source is None:
        raise ResourceNotFoundError("数据源不存在")
    if _is_deleted(source):
        raise ResourceDeletedError("数据源已删除")
    if for_write:
        if _deletion_state(source) in _BLOCKING_STATES:
            raise ParentResourceUnavailableError(
                "数据源正在删除或删除失败，管理写入已冻结",
                payload={"parent_type": "source", "source_id": source.source_id},
            )
        clear_task = deletion_repo.unresolved_task_for_source(
            session, source.source_id, action="clear_source_knowledge"
        )
        if clear_task is not None:
            raise SourceKnowledgeFrozenError(
                "该源知识清理进行中或失败待重试，管理写入已冻结",
                payload={"task_id": str(clear_task.task_id)},
            )
    return source


def require_source(
    session: Session, source_id: str, *, for_write: bool = False
) -> DbSourceInfo:
    return _require_source_checked(
        session, session.get(DbSourceInfo, source_id), for_write=for_write
    )


def require_workspace(
    session: Session, workspace_id: str, *, for_write: bool = False
) -> WorkspaceInfo:
    workspace = session.get(WorkspaceInfo, workspace_id)
    if workspace is None:
        raise ResourceNotFoundError("工作区不存在")
    if _is_deleted(workspace):
        raise ResourceDeletedError("工作区已删除")
    _require_source_checked(
        session,
        session.get(DbSourceInfo, workspace.source_id),
        for_write=for_write,
    )
    if for_write and _deletion_state(workspace) in _BLOCKING_STATES:
        raise ParentResourceUnavailableError(
            "工作区正在删除或删除失败，管理写入已冻结",
            payload={
                "parent_type": "workspace",
                "workspace_id": workspace.workspace_id,
            },
        )
    return workspace


_JOB_MODELS = {"collection": CollectionJobInfo, "publish": PublishJobInfo}


def require_job(session: Session, kind: str, job_id: str, *, for_write: bool = False):
    model = _JOB_MODELS.get(kind)
    if model is None:
        raise ValueError(f"未知作业类型: {kind!r}")
    job = session.get(model, job_id)
    if job is None:
        raise ResourceNotFoundError("作业不存在")
    if _is_deleted(job):
        raise ResourceDeletedError("作业已删除")
    require_workspace(session, job.workspace_id, for_write=for_write)
    return job
