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


def rel_publish_in_progress(session: Session, workspace_id: str):
    """该工作区是否有 running 的 rel 发布步骤（doc 8.1 §5.1.1）。

    `_publish_rel` 拆分事务后不再跨 Neo4j 调用持工作区行锁；同工作区的
    Rel 配置保存与重新采集改用本检查互斥，替代原行锁提供的保护。
    """
    from sqlalchemy import select

    from backend.persistence.models import (
        PublishJobInfo,
        PublishRunInfo,
        PublishRunStep,
    )

    return session.execute(
        select(PublishRunInfo.publish_run_id)
        .join(
            PublishRunStep,
            PublishRunStep.publish_run_id == PublishRunInfo.publish_run_id,
        )
        .join(
            PublishJobInfo,
            PublishJobInfo.publish_job_id == PublishRunInfo.publish_job_id,
        )
        .where(
            PublishJobInfo.workspace_id == workspace_id,
            PublishRunStep.step_name == "rel",
            PublishRunStep.status == "running",
            PublishRunInfo.status == "running",
        )
        .limit(1)
    ).scalar_one_or_none()


class RelPublishInProgressError(Exception):
    """409 rel_publish_in_progress：同工作区有 running 的 Rel 发布（doc 8.1 §5.1.1）。

    `_publish_rel` 拆分事务后不再跨 Neo4j 调用持工作区行锁；Rel 发布
    输入（JSON/Rel/Domain 产物与配置）的写入方改用运行中检查互斥。
    """

    def __init__(self, message: str = "Rel 发布正在执行，请等待完成后再修改其输入"):
        super().__init__(message)
        self.message = message


# ---- doc 10.2 §5.1–§5.3：活动运行查询与准入/保留 409 ----


class _RunConflictError(Exception):
    """带阻塞运行定位信息的 409 基类（doc 10.2 §5.1–§5.3）。"""

    def __init__(self, code: str, message: str, blocking: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.blocking = blocking or {}


class SourcePublishActiveError(_RunConflictError):
    """409 source_publish_active：同源已有 pending/running 发布运行。

    遗留检查只认同工作区同名 running 步骤；并行后同源任何工作区的
    活动发布都会交错清理 Milvus/Neo4j 同目标内容，创建时按源拒绝。
    """

    def __init__(self, blocking: dict):
        super().__init__(
            "source_publish_active",
            "该数据源已有进行中的发布运行，请等待完成或收尾后重试",
            blocking,
        )


class WorkspacePublishActiveError(_RunConflictError):
    """409 workspace_publish_active：同工作区有 pending/running 发布运行。

    doc 10.2 §5.2 双向保留：发布期间拒绝采集创建/认领与文件配置保存。
    """

    def __init__(self, blocking: dict):
        super().__init__(
            "workspace_publish_active",
            "该工作区存在进行中的发布，请完成或收尾后重试",
            blocking,
        )


class WorkspaceCollectionActiveError(_RunConflictError):
    """409 workspace_collection_active：同工作区有 pending/running 采集运行。"""

    def __init__(self, blocking: dict):
        super().__init__(
            "workspace_collection_active",
            "该工作区存在进行中的采集，请完成或收尾后重试",
            blocking,
        )


class PublishCapacityExhaustedError(_RunConflictError):
    """409 publish_capacity_exhausted：全局活动发布运行名额已满。"""

    def __init__(self, limit: int):
        super().__init__(
            "publish_capacity_exhausted",
            f"发布容量已满（活动运行上限 {limit}），请稍后重试",
            {"limit": limit},
        )


def _run_brief(run_id, job_id, status, kind: str) -> dict:
    return {
        "kind": kind,
        "run_id": str(run_id),
        "job_id": str(job_id),
        "status": status,
    }


def active_publish_runs_for_source(session: Session, source_id: str) -> list[dict]:
    """该源所有关联工作区的 pending/running 发布运行（doc 10.2 §5.1）。

    供创建准入使用：返回阻塞运行定位信息（run_id/job_id/status）。
    """
    from sqlalchemy import select

    from backend.persistence.models import PublishJobInfo, PublishRunInfo, WorkspaceInfo

    rows = session.execute(
        select(
            PublishRunInfo.publish_run_id,
            PublishRunInfo.publish_job_id,
            PublishRunInfo.status,
        )
        .join(
            PublishJobInfo,
            PublishJobInfo.publish_job_id == PublishRunInfo.publish_job_id,
        )
        .join(WorkspaceInfo, WorkspaceInfo.workspace_id == PublishJobInfo.workspace_id)
        .where(
            WorkspaceInfo.source_id == source_id,
            PublishRunInfo.status.in_(("pending", "running")),
        )
        .order_by(PublishRunInfo.created_at)
    ).all()
    return [
        _run_brief(row[0], row[1], row[2], "publish") for row in rows
    ]


def active_publish_run_for_workspace(session: Session, workspace_id: str) -> dict | None:
    """该工作区是否有 pending/running 发布运行（doc 10.2 §5.2）。

    pending 也占用：发布创建后、后台启动前同样要保护输入不被改写。
    """
    from sqlalchemy import select

    from backend.persistence.models import PublishJobInfo, PublishRunInfo

    row = session.execute(
        select(
            PublishRunInfo.publish_run_id,
            PublishRunInfo.publish_job_id,
            PublishRunInfo.status,
        )
        .join(
            PublishJobInfo,
            PublishJobInfo.publish_job_id == PublishRunInfo.publish_job_id,
        )
        .where(
            PublishJobInfo.workspace_id == workspace_id,
            PublishRunInfo.status.in_(("pending", "running")),
        )
        .order_by(PublishRunInfo.created_at)
        .limit(1)
    ).first()
    if row is None:
        return None
    return _run_brief(row[0], row[1], row[2], "publish")


def active_collection_run_for_workspace(
    session: Session, workspace_id: str
) -> dict | None:
    """该工作区是否有 pending/running 采集运行（doc 10.2 §5.2）。"""
    from sqlalchemy import select

    from backend.persistence.models import CollectionJobInfo, CollectionRunInfo

    row = session.execute(
        select(
            CollectionRunInfo.collection_run_id,
            CollectionRunInfo.collection_job_id,
            CollectionRunInfo.status,
        )
        .join(
            CollectionJobInfo,
            CollectionJobInfo.collection_job_id == CollectionRunInfo.collection_job_id,
        )
        .where(
            CollectionJobInfo.workspace_id == workspace_id,
            CollectionRunInfo.status.in_(("pending", "running")),
        )
        .order_by(CollectionRunInfo.created_at)
        .limit(1)
    ).first()
    if row is None:
        return None
    return _run_brief(row[0], row[1], row[2], "collection")


def count_active_publish_runs(session: Session) -> int:
    """全部 pending/running 发布运行数（doc 10.2 §5.3 全局名额计数）。

    pending 行即占用名额；全部 step 终态但 run 未汇总仍占用。
    """
    from sqlalchemy import func, select

    from backend.persistence.models import PublishRunInfo

    return int(
        session.execute(
            select(func.count())
            .select_from(PublishRunInfo)
            .where(PublishRunInfo.status.in_(("pending", "running")))
        ).scalar_one()
    )


# Rel 发布生成器实际读取的采集输入目录步骤（json/domain/rel）
REL_PUBLISH_INPUT_STEPS = ("json", "domain", "rel")


def collection_inputs_in_progress(session: Session, workspace_id: str):
    """该工作区是否有 running 的 json/domain/rel 采集步骤（doc 8.1 §5.1.1）。

    Rel 发布认领前的反向检查：采集先开始时，发布不得读取正在改写的输入。
    """
    from sqlalchemy import select

    from backend.persistence.models import (
        CollectionJobInfo,
        CollectionRunInfo,
        CollectionRunStep,
    )

    return session.execute(
        select(CollectionRunInfo.collection_run_id)
        .join(
            CollectionRunStep,
            CollectionRunStep.collection_run_id == CollectionRunInfo.collection_run_id,
        )
        .join(
            CollectionJobInfo,
            CollectionJobInfo.collection_job_id == CollectionRunInfo.collection_job_id,
        )
        .where(
            CollectionJobInfo.workspace_id == workspace_id,
            CollectionRunStep.step_name.in_(REL_PUBLISH_INPUT_STEPS),
            CollectionRunStep.status == "running",
            CollectionRunInfo.status == "running",
        )
        .limit(1)
    ).scalar_one_or_none()
