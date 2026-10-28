"""workspace_info 表访问；事务边界由调用方的 Session 决定。"""

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.persistence.models import CollectionJobInfo, PublishJobInfo, WorkspaceInfo


def get_workspace(session: Session, workspace_id: str) -> WorkspaceInfo | None:
    return session.get(WorkspaceInfo, workspace_id)


def list_workspaces_by_source(session: Session, source_id: str) -> list[WorkspaceInfo]:
    stmt = (
        select(WorkspaceInfo)
        .where(WorkspaceInfo.source_id == source_id)
        .order_by(WorkspaceInfo.created_at, WorkspaceInfo.workspace_id)
    )
    return list(session.scalars(stmt))


def page_workspaces_by_filter(
    session: Session,
    *,
    source_id: str | None = None,
    limit: int,
    offset: int,
) -> tuple[list[WorkspaceInfo], int]:
    """跨源工作区分页（doc 6.2 §6.1）：created_at DESC、workspace_id DESC。

    `source_id` 为 None 时列出全部数据源的工作区；传入时不校验来源是否
    存在，按普通过滤处理（无匹配返回空页）。total 为筛选条件下的全部行数。
    """
    conditions = [WorkspaceInfo.source_id == source_id] if source_id else []
    total = int(
        session.execute(
            select(func.count()).select_from(WorkspaceInfo).where(*conditions)
        ).scalar_one()
    )
    stmt = (
        select(WorkspaceInfo)
        .order_by(WorkspaceInfo.created_at.desc(), WorkspaceInfo.workspace_id.desc())
        .limit(limit)
        .offset(offset)
    )
    if conditions:
        stmt = stmt.where(*conditions)
    return list(session.scalars(stmt)), total


def _count_jobs(session: Session, model, workspace_ids: list[str]) -> dict[str, int]:
    stmt = (
        select(model.workspace_id, func.count())
        .where(model.workspace_id.in_(workspace_ids))
        .group_by(model.workspace_id)
    )
    return {workspace_id: int(total) for workspace_id, total in session.execute(stmt)}


def count_workspace_jobs(
    session: Session, workspace_ids: list[str]
) -> tuple[dict[str, int], dict[str, int]]:
    """按工作区统计（采集作业数, 发布作业数）（doc 6.2 §6.1 列表行聚合）。

    只返回有作业记录的工作区，未出现的工作区由调用方按 0 处理；
    `workspace_ids` 为空时不发起查询。
    """
    if not workspace_ids:
        return {}, {}
    return (
        _count_jobs(session, CollectionJobInfo, workspace_ids),
        _count_jobs(session, PublishJobInfo, workspace_ids),
    )


def insert_workspace(session: Session, fields: dict) -> WorkspaceInfo:
    """插入工作区行；workspace_id 由数据库序列默认值生成并取回。"""
    if "workspace_id" in fields:
        raise ValueError("workspace_id 由数据库生成，不接受外部指定")
    row = WorkspaceInfo(**fields)
    session.add(row)
    session.flush()
    return row


def update_workspace(
    session: Session, workspace_id: str, fields: dict
) -> WorkspaceInfo | None:
    """仅修改主表信息（名称、说明）；来源不可更换（doc 6.2 §6.1）。"""
    row = get_workspace(session, workspace_id)
    if row is None:
        return None
    for key, value in fields.items():
        setattr(row, key, value)
    row.updated_at = func.now()
    return row
