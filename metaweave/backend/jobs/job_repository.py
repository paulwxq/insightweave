"""两类作业主表与不可变版本表的访问（doc 6.1 §2–§3、§6–§7）。

首次保存：同一事务写作业主表与 `revision_no = 1` 的版本行。
新增版本：锁定作业行 → 读取最大版本号加一 → 插入新版本 → 更新主表
`updated_at`；旧版本不可原地更新。事务边界由调用方的 Session 决定，
本模块不提交（`create_*` 系列由服务层在同一会话内 commit）。
"""

from dataclasses import dataclass

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from backend.persistence.models import (
    CollectionJobInfo,
    CollectionJobRevision,
    DbSourceInfo,
    PublishJobInfo,
    PublishJobRevision,
    WorkspaceInfo,
)


@dataclass(frozen=True)
class JobSpec:
    """一种作业（采集/发布）的表与列规格。"""

    info_table: type
    revision_table: type
    id_column: str


COLLECTION_JOB = JobSpec(
    info_table=CollectionJobInfo,
    revision_table=CollectionJobRevision,
    id_column="collection_job_id",
)
PUBLISH_JOB = JobSpec(
    info_table=PublishJobInfo,
    revision_table=PublishJobRevision,
    id_column="publish_job_id",
)


def get_job(spec: JobSpec, session: Session, job_id: str):
    return session.get(spec.info_table, job_id)


def get_revision(spec: JobSpec, session: Session, job_id: str, revision_no: int):
    column = getattr(spec.revision_table, spec.id_column)
    return session.execute(
        select(spec.revision_table).where(
            column == job_id,
            spec.revision_table.revision_no == revision_no,
        )
    ).scalar_one_or_none()


def latest_revision_no(spec: JobSpec, session: Session, job_id: str) -> int | None:
    column = getattr(spec.revision_table, spec.id_column)
    value = session.execute(
        select(func.max(spec.revision_table.revision_no)).where(column == job_id)
    ).scalar_one_or_none()
    return int(value) if value is not None else None


def list_revisions(spec: JobSpec, session: Session, job_id: str) -> list:
    column = getattr(spec.revision_table, spec.id_column)
    stmt = (
        select(spec.revision_table)
        .where(column == job_id)
        .order_by(spec.revision_table.revision_no)
    )
    return list(session.scalars(stmt))


def list_jobs_by_workspace(spec: JobSpec, session: Session, workspace_id: str) -> list:
    stmt = (
        select(spec.info_table)
        .where(spec.info_table.workspace_id == workspace_id)
        .order_by(spec.info_table.created_at, spec.info_table.job_name)
    )
    return list(session.scalars(stmt))


def latest_revisions_for_jobs(spec: JobSpec, session: Session, job_ids: list[str]) -> dict:
    """批量取各作业最新版本的 ``(revision_no, steps)``；无版本的作业不在结果中。"""
    if not job_ids:
        return {}
    column = getattr(spec.revision_table, spec.id_column)
    latest = (
        select(
            column.label("job_id"),
            func.max(spec.revision_table.revision_no).label("revision_no"),
        )
        .where(column.in_(job_ids))
        .group_by(column)
        .subquery()
    )
    stmt = select(
        latest.c.job_id,
        spec.revision_table.revision_no,
        spec.revision_table.steps,
    ).join(
        spec.revision_table,
        (column == latest.c.job_id)
        & (spec.revision_table.revision_no == latest.c.revision_no),
    )
    return {
        row.job_id: (int(row.revision_no), list(row.steps))
        for row in session.execute(stmt)
    }


def lock_job(spec: JobSpec, session: Session, job_id: str):
    """取得作业行锁（新版本并发保存串行化，doc 6.1 §3）。"""
    column = getattr(spec.info_table, spec.id_column)
    stmt = select(spec.info_table).where(column == job_id).with_for_update()
    return next(iter(session.scalars(stmt)), None)


def create_job_with_first_revision(
    spec: JobSpec,
    session: Session,
    *,
    workspace_id: str,
    job_name: str,
    job_desc: str | None,
    steps: list[str],
    step_config: dict | None,
    change_note: str | None,
):
    """同一事务创建作业主表与 revision_no = 1（doc 6.2 §4.3.1）。"""
    job = spec.info_table(
        workspace_id=workspace_id,
        job_name=job_name,
        job_desc=job_desc,
        created_by_user_id=None,
    )
    session.add(job)
    session.flush()  # 取回序列生成的短 ID
    # psycopg3 会把 None 序列化为 JSON null 而非 SQL NULL；省略未设置的
    # jsonb 列，让数据库保持 SQL NULL（doc 6.1 §step_config 统一约定）。
    revision_fields = {
        spec.id_column: getattr(job, spec.id_column),
        "revision_no": 1,
        "steps": steps,
        "change_note": change_note,
    }
    if step_config is not None:
        revision_fields["step_config"] = step_config
    revision = spec.revision_table(**revision_fields)
    session.add(revision)
    session.flush()
    return job, revision


def insert_next_revision(
    spec: JobSpec,
    session: Session,
    job,
    *,
    steps: list[str],
    step_config: dict | None,
    change_note: str | None,
):
    """在已锁定的作业行上追加新版本（doc 6.2 §4.3.2）。

    调用方必须先 `lock_job` 取得行锁；本函数读取最大版本号加一插入，
    并推进主表 `updated_at`。旧版本行不更新。
    """
    job_id = getattr(job, spec.id_column)
    next_no = (latest_revision_no(spec, session, job_id) or 0) + 1
    revision_fields = {
        spec.id_column: job_id,
        "revision_no": next_no,
        "steps": steps,
        "change_note": change_note,
    }
    if step_config is not None:
        revision_fields["step_config"] = step_config
    revision = spec.revision_table(**revision_fields)
    session.add(revision)
    job.updated_at = func.now()
    session.flush()
    return revision


def touch_job_updated_at(session: Session, job) -> None:
    job.updated_at = func.now()


def list_jobs_global(
    spec: JobSpec,
    run_spec,
    session: Session,
    *,
    workspace_id: str | None = None,
    source_id: str | None = None,
    keyword: str | None = None,
    limit: int,
    offset: int,
) -> tuple[list, int]:
    """全局作业分页列表（跨工作区，设计稿 35/37）。

    过滤：workspace_id / source_id（经工作区表联到源）/ 关键字（作业名或
    作业 ID，ILIKE）。排序：最后一次运行时间倒序（无运行的排后，再按
    创建时间倒序）。返回 ``(作业 ORM 行列表, total)``；名称与最后运行
    时间映射由服务层批量补齐。
    """
    job_id_col = getattr(spec.info_table, spec.id_column)
    run_job_column = getattr(run_spec.info_table, run_spec.job_id_column)
    last_run = (
        select(
            run_job_column.label("jid"),
            func.max(run_spec.info_table.created_at).label("last_run_at"),
        )
        .group_by(run_job_column)
        .subquery()
    )
    base = (
        select(spec.info_table)
        .join(WorkspaceInfo, WorkspaceInfo.workspace_id == spec.info_table.workspace_id)
        .outerjoin(last_run, last_run.c.jid == job_id_col)
    )
    conditions = []
    if workspace_id is not None:
        conditions.append(spec.info_table.workspace_id == workspace_id)
    if source_id is not None:
        conditions.append(WorkspaceInfo.source_id == source_id)
    if keyword:
        like = f"%{keyword}%"
        conditions.append(
            or_(spec.info_table.job_name.ilike(like), job_id_col.ilike(like))
        )
    if conditions:
        base = base.where(*conditions)
    total = int(
        session.scalar(select(func.count()).select_from(base.subquery())) or 0
    )
    rows = list(
        session.scalars(
            base.order_by(
                last_run.c.last_run_at.desc().nullslast(),
                spec.info_table.created_at.desc(),
            ).limit(limit).offset(offset)
        )
    )
    return rows, total


def workspace_source_names(
    session: Session, workspace_ids: list[str]
) -> dict[str, tuple[str, str, str]]:
    """工作区 → (工作区名, source_id, 数据源显示名)；作业的工作区必然登记。"""
    if not workspace_ids:
        return {}
    rows = session.execute(
        select(
            WorkspaceInfo.workspace_id,
            WorkspaceInfo.workspace_name,
            DbSourceInfo.source_id,
            DbSourceInfo.display_name,
        )
        .join(DbSourceInfo, DbSourceInfo.source_id == WorkspaceInfo.source_id)
        .where(WorkspaceInfo.workspace_id.in_(workspace_ids))
    ).all()
    return {
        row[0]: (row[1], row[2], row[3])
        for row in rows
    }


def last_run_times(run_spec, session: Session, job_ids: list[str]) -> dict[str, object]:
    """各作业最近一次运行时间（无运行不在结果中）。"""
    if not job_ids:
        return {}
    run_job_column = getattr(run_spec.info_table, run_spec.job_id_column)
    rows = session.execute(
        select(
            run_job_column,
            func.max(run_spec.info_table.created_at),
        )
        .where(run_job_column.in_(job_ids))
        .group_by(run_job_column)
    ).all()
    return {row[0]: row[1] for row in rows}
