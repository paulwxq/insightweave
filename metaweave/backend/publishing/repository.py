"""发布侧平台库访问：源行锁、最后成功发布视图查询（doc 6.1 §12）。

`vw_workspace_latest_successful_publish` 由建表脚本创建；本模块提供
工作区口径与源级口径（NL2SQL 就绪等使用，doc 6.2 §10.2）的查询。
事务边界由调用方的 Session 决定，本模块不提交。
"""

from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from backend.persistence.models import DbSourceInfo


def lock_db_source_row(session: Session, source_id: str) -> DbSourceInfo | None:
    """取得数据源行锁并返回登记行；不存在返回 None（doc 16 §5/§6）。"""
    stmt = (
        select(DbSourceInfo)
        .where(DbSourceInfo.source_id == source_id)
        .with_for_update()
    )
    return next(iter(session.scalars(stmt)), None)


def workspace_latest_successful_publish(
    session: Session, workspace_id: str
) -> list[dict]:
    """各步骤最后一次成功发布（工作区口径，doc 6.1 §12）。

    返回 [{step_name, publish_run_id, published_at, source_collection_runs}]。
    """
    rows = session.execute(
        text(
            "SELECT step_name, publish_run_id, published_at, source_collection_runs "
            "FROM public.vw_workspace_latest_successful_publish "
            "WHERE workspace_id = :ws ORDER BY step_name"
        ).bindparams(ws=workspace_id)
    ).mappings()
    items = []
    for row in rows:
        items.append(
            {
                "step_name": row["step_name"],
                "publish_run_id": str(row["publish_run_id"]),
                "published_at": (
                    row["published_at"].isoformat()
                    if row["published_at"] is not None
                    else None
                ),
                "source_collection_runs": row["source_collection_runs"],
            }
        )
    return items


def source_latest_successful_publish(session: Session, source_id: str) -> list[dict]:
    """各步骤最近一次成功发布（源级口径，doc 6.2 §10.2）。

    将 6.1 §12 的工作区视图关联 `workspace_info` 得到来源，再按
    `source_id + step_name` 以 `published_at DESC NULLS LAST,
    publish_run_id DESC` 取一条；不同步骤可以来自不同工作区，但同一
    步骤只取最近一次成功结果，后续失败不替代之前成功。
    """
    rows = session.execute(
        text(
            """
            SELECT DISTINCT ON (latest.step_name)
                latest.step_name,
                latest.publish_run_id,
                latest.published_at,
                latest.workspace_id,
                latest.source_collection_runs
            FROM public.vw_workspace_latest_successful_publish AS latest
            JOIN public.workspace_info AS ws ON ws.workspace_id = latest.workspace_id
            WHERE ws.source_id = :sid
            ORDER BY latest.step_name,
                     latest.published_at DESC NULLS LAST,
                     latest.publish_run_id DESC
            """
        ).bindparams(sid=source_id)
    ).mappings()
    items = []
    for row in rows:
        items.append(
            {
                "step_name": row["step_name"],
                "publish_run_id": str(row["publish_run_id"]),
                "published_at": (
                    row["published_at"].isoformat()
                    if row["published_at"] is not None
                    else None
                ),
                "workspace_id": row["workspace_id"],
                "source_collection_runs": row["source_collection_runs"],
            }
        )
    return items


def get_publish_run_context(session: Session, publish_run_id: UUID):
    """发布运行 → 作业 → 工作区 → 数据源的上下文（doc 6.2 §4.1）。"""
    from backend.persistence.models import PublishJobInfo, PublishRunInfo, WorkspaceInfo

    run = session.get(PublishRunInfo, publish_run_id)
    if run is None:
        return None, None, None
    job = session.get(PublishJobInfo, run.publish_job_id)
    workspace = session.get(WorkspaceInfo, job.workspace_id) if job else None
    return run, job, workspace
