"""作业步骤状态表的读写（doc 9 §2）。

一个 `job_id`（工作区）下每个步骤一行，主键为 `(job_id, step_name)`；
状态只允许 `running` / `success` / `failed`，错误详情只写日志、不入库。
本模块不读环境变量、不建连接，事务边界由调用方给出的 Session 决定。
"""

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

# 与发布提交、删源共用同一把源行锁（doc 16 §6）；publishing.repository 只依赖
# persistence.models，反向无依赖，不构成导入环。
from backend.publishing.repository import lock_db_source_row

from backend.persistence.models import JobInfo
from backend.publishing.errors import JobNotPublishableError

STATUS_RUNNING = "running"
STATUS_SUCCESS = "success"
STATUS_FAILED = "failed"

# 哨兵：区分「不传 db_desc」与「显式写入 NULL」（doc 21.1 §4）
_UNSET: object = object()


class WorkspaceMissingError(LookupError):
    """指定的 `job_id` 在 `job_info` 中不存在。"""


class SourceRegistrationMissingError(LookupError):
    """源行锁下重查发现数据源登记已不存在（doc 16 §6）。"""


class WorkspaceSourceMismatchError(ValueError):
    """指定的 `job_id` 已有步骤行属于另一个数据源。"""


class StepAlreadyRunningError(RuntimeError):
    """该工作区的该步骤最近一次执行仍标记为 `running`。"""


class StepRowNotFoundError(LookupError):
    """写回状态时步骤行已不存在。"""


def list_step_rows(session: Session, job_id: UUID) -> list[JobInfo]:
    stmt = select(JobInfo).where(JobInfo.job_id == job_id)
    return list(session.scalars(stmt))


def list_source_step_rows(session: Session, source_id: UUID) -> list[JobInfo]:
    """读取某个数据源下的全部步骤行，按登记时间从早到晚。"""
    stmt = (
        select(JobInfo)
        .where(JobInfo.source_id == source_id)
        .order_by(JobInfo.created_at, JobInfo.step_name)
    )
    return list(session.scalars(stmt))


def get_step_row(session: Session, job_id: UUID, step_name: str) -> JobInfo | None:
    return session.get(JobInfo, {"job_id": job_id, "step_name": step_name})


def assert_workspace_owner(session: Session, job_id: UUID, source_id: UUID) -> None:
    """校验工作区存在，且其全部步骤行都属于该数据源。"""
    rows = list_step_rows(session, job_id)
    if not rows:
        raise WorkspaceMissingError(f"指定的 job_id 不存在: {job_id}")
    if any(row.source_id != source_id for row in rows):
        raise WorkspaceSourceMismatchError(f"指定的 job_id 不属于该数据源: {job_id}")


def start_step(
    session: Session,
    job_id: UUID,
    source_id: UUID,
    step_name: str,
    db_desc: str | None | object = _UNSET,
) -> None:
    """新建步骤行，或把终态行改回 `running`；仍在 `running` 时拒绝。

    写 running 前先取得源行锁并确认登记仍存在（doc 16 §6）：与删源、发布
    共用同一把锁串行化，避免与删除竞争写出孤儿步骤行。

    `db_desc` 哨兵语义（doc 21.1 §4）：不传（既有全部调用方）不触碰该列；
    传入字符串或 `None`（写入 NULL）时 INSERT 分支写入新行、UPDATE 分支
    覆盖既有行。末尾这一次 commit 同时是 domain 调用方提交数据源描述更新
    的唯一提交点——commit 之后写入的修改会随会话关闭回滚丢弃。
    """
    if lock_db_source_row(session, source_id) is None:
        raise SourceRegistrationMissingError(f"数据源登记已不存在: {source_id}")
    row = get_step_row(session, job_id, step_name)
    if row is None:
        new_row = JobInfo(
            job_id=job_id,
            source_id=source_id,
            step_name=step_name,
            last_status=STATUS_RUNNING,
        )
        if db_desc is not _UNSET:
            new_row.db_desc = db_desc
        session.add(new_row)
    elif row.last_status == STATUS_RUNNING:
        raise StepAlreadyRunningError("当前步骤可能已有作业正在执行")
    else:
        row.last_status = STATUS_RUNNING
        row.updated_at = func.now()
        if db_desc is not _UNSET:
            row.db_desc = db_desc
    session.commit()


def finish_step(session: Session, job_id: UUID, step_name: str, status: str) -> None:
    """按联合主键写回最近一次执行结果。"""
    if status not in (STATUS_SUCCESS, STATUS_FAILED):
        raise ValueError(f"未知的步骤状态: {status!r}")
    row = get_step_row(session, job_id, step_name)
    if row is None:
        raise StepRowNotFoundError(f"步骤行已不存在: {job_id}/{step_name}")
    row.last_status = status
    row.updated_at = func.now()
    session.commit()


def rollup_job_status(statuses: list[str]) -> str:
    """同一 Job 全部步骤行的汇总状态：running 优先，其次 failed，否则 success。

    采集首页、作业列表与发布准入共用同一口径（doc 20.1 §11.2），不落库。
    """
    if STATUS_RUNNING in statuses:
        return STATUS_RUNNING
    if STATUS_FAILED in statuses:
        return STATUS_FAILED
    return STATUS_SUCCESS


def get_job_status(session: Session, job_id: UUID) -> str | None:
    """按 job_info 现有全部步骤行计算汇总状态；没有任何步骤行返回 None。"""
    rows = list_step_rows(session, job_id)
    if not rows:
        return None
    return rollup_job_status([row.last_status for row in rows])


def assert_job_publishable(session: Session, job_id: UUID) -> None:
    """现有全部步骤行均为 success 才允许正式发布；不存在步骤行由工作区校验处理。"""
    status = get_job_status(session, job_id)
    if status is not None and status != STATUS_SUCCESS:
        raise JobNotPublishableError("采集作业存在失败或运行中的步骤，不能发布")
