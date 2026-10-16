"""发布记录的写入。事务由调用方的 Session 决定，本模块不提交。"""

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.persistence.models import DbSourceInfo, PublishInfo


def list_source_publish_rows(session: Session, source_id: UUID) -> list[PublishInfo]:
    """读取某个数据源下的全部发布结果行。"""
    stmt = select(PublishInfo).where(PublishInfo.source_id == source_id)
    return list(session.scalars(stmt))


def list_publish_id_rows(session: Session, publish_id: UUID) -> list[PublishInfo]:
    """读取某一次发布的全部步骤行，按登记时间从早到晚。"""
    stmt = (
        select(PublishInfo)
        .where(PublishInfo.publish_id == publish_id)
        .order_by(PublishInfo.created_at, PublishInfo.step_name)
    )
    return list(session.scalars(stmt))


def clear_current(session: Session, source_id: UUID, step_name: str) -> None:
    """把该数据源该步骤现有 current 行翻成 false，并推进 updated_at。"""
    stmt = select(PublishInfo).where(
        PublishInfo.source_id == source_id,
        PublishInfo.step_name == step_name,
        PublishInfo.is_current_publish.is_(True),
    )
    for row in session.scalars(stmt):
        row.is_current_publish = False
        row.updated_at = func.now()


def insert_publish_row(
    session: Session,
    publish_id: UUID,
    source_id: UUID,
    job_id: UUID,
    step_name: str,
    status: str,
    error_summary: str | None = None,
    *,
    is_current: bool,
) -> PublishInfo:
    """添加本次发布记录。时间戳交给数据库默认值，不修改旧行的业务字段。

    is_current 必须显式传入（doc 16 §1）：只有成功提交当前内容的行才写
    True；失败尝试一律 False，不清除已有成功 current。
    """
    row = PublishInfo(
        publish_id=publish_id,
        step_name=step_name,
        source_id=source_id,
        job_id=job_id,
        status=status,
        is_current_publish=is_current,
        created_by_user_id=None,
        error_summary=error_summary,
    )
    session.add(row)
    return row


def lock_db_source_row(session: Session, source_id: UUID) -> DbSourceInfo | None:
    """取得数据源行锁并返回登记行；不存在返回 None（doc 16 §5/§6）。

    成功提交当前内容、写 running 状态与删除数据源都先取得该行锁，
    并在锁下重新确认登记仍存在且未被并发禁用或删除。
    """
    stmt = (
        select(DbSourceInfo)
        .where(DbSourceInfo.source_id == source_id)
        .with_for_update()
    )
    return next(iter(session.scalars(stmt)), None)
