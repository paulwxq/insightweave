"""层级行锁辅助（doc 8.1 §5.1.1）：源 → 工作区 → 作业统一顺序。

只做行锁获取；准入判断由调用方在锁内用 access_guards 复查。
``populate_existing=True`` 确保锁内读到数据库最新状态，不沿用此前
普通定位查询留在 Session 里的旧对象属性。
禁止持有下级行锁再回头取源锁（含仓储函数内部的隐式 FOR UPDATE）。
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from backend.persistence.models import (
    CollectionJobInfo,
    DbSourceInfo,
    PublishJobInfo,
    WorkspaceInfo,
)

_JOB_MODELS = {"collection": CollectionJobInfo, "publish": PublishJobInfo}


def lock_source_row(session: Session, source_id: str) -> DbSourceInfo | None:
    return session.get(
        DbSourceInfo, source_id, with_for_update=True, populate_existing=True
    )


def lock_workspace_row(session: Session, workspace_id: str) -> WorkspaceInfo | None:
    return session.get(
        WorkspaceInfo, workspace_id, with_for_update=True, populate_existing=True
    )


def lock_job_row(session: Session, kind: str, job_id: str):
    model = _JOB_MODELS.get(kind)
    if model is None:
        raise ValueError(f"未知作业类型: {kind!r}")
    return session.get(model, job_id, with_for_update=True, populate_existing=True)
