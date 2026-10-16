"""数据源登记表访问。"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from backend.persistence.models import (
    DbProfile,
    DbSourceInfo,
    JobInfo,
    PublishInfo,
    TableDefinition,
    TableProfile,
)


def get_db_source(session: Session, source_id: UUID) -> DbSourceInfo | None:
    return session.get(DbSourceInfo, source_id)


def find_db_sources_by_connection(
    session: Session, host: str, port: int, db_name: str, username: str
) -> list[DbSourceInfo]:
    stmt = select(DbSourceInfo).where(
        DbSourceInfo.host == host,
        DbSourceInfo.port == port,
        DbSourceInfo.db_name == db_name,
        DbSourceInfo.username == username,
    )
    return list(session.scalars(stmt))


def latest_source_activity(
    session: Session,
) -> tuple[dict[UUID, datetime], dict[UUID, datetime]]:
    """每个数据源最近一次采集和最近一次已提交发布。

    采集取 job_info.updated_at 的最大值；发布取 publish_info.created_at 的最大值。
    没有对应行的数据源不会出现在字典里。
    """
    collected = {
        source_id: moment
        for source_id, moment in session.execute(
            select(JobInfo.source_id, func.max(JobInfo.updated_at)).group_by(
                JobInfo.source_id
            )
        ).all()
        if moment is not None
    }
    published = {
        source_id: moment
        for source_id, moment in session.execute(
            select(PublishInfo.source_id, func.max(PublishInfo.created_at)).group_by(
                PublishInfo.source_id
            )
        ).all()
        if moment is not None
    }
    return collected, published


def list_db_sources(session: Session) -> list[DbSourceInfo]:
    stmt = select(DbSourceInfo).order_by(
        DbSourceInfo.created_at.desc(), DbSourceInfo.source_id
    )
    return list(session.scalars(stmt))


def insert_db_source(session: Session, fields: dict) -> DbSourceInfo:
    if (
        not isinstance(fields.get("display_name"), str)
        or not fields["display_name"].strip()
    ):
        raise ValueError("display_name 不能为空")
    row = DbSourceInfo(**fields)
    session.add(row)
    session.commit()
    return row


def update_db_source(
    session: Session, source_id: UUID, fields: dict
) -> DbSourceInfo | None:
    if (
        not isinstance(fields.get("display_name"), str)
        or not fields["display_name"].strip()
    ):
        raise ValueError("display_name 不能为空")
    row = get_db_source(session, source_id)
    if row is None:
        return None
    for key, value in fields.items():
        setattr(row, key, value)
    row.updated_at = func.now()
    session.commit()
    return row


def set_db_source_active(
    session: Session, source_id: UUID, is_active: bool
) -> DbSourceInfo | None:
    """按主键更新启用状态；找不到返回 None，不触碰其他列（doc 11）。"""
    row = get_db_source(session, source_id)
    if row is None:
        return None
    row.is_active = is_active
    row.updated_at = func.now()
    session.commit()
    return row


def purge_source_profiles(session: Session, source_id: UUID) -> None:
    """删除该源当前画像并清除全部 current 标记（doc 16 §6）。

    `db_profile` 每源一行、`table_definition` 所有定义类型行一并删除；
    `publish_info` 只翻 current，历史尝试行保留作审计。
    事务与提交由调用方决定，本模块不提交。
    """
    session.execute(delete(DbProfile).where(DbProfile.source_id == source_id))
    session.execute(
        delete(TableDefinition).where(TableDefinition.source_id == source_id)
    )
    # JSON 对象画像行一并删除，字段行由 ON DELETE CASCADE 级联（doc 19.2 §7.4）
    session.execute(delete(TableProfile).where(TableProfile.source_id == source_id))
    for row in session.scalars(
        select(PublishInfo).where(PublishInfo.source_id == source_id)
    ):
        if row.is_current_publish:
            row.is_current_publish = False
            row.updated_at = func.now()


def remove_db_source(session: Session, source_id: UUID) -> bool:
    """登记行加入待删除，不提交；不存在返回 False（doc 16 §6）。"""
    row = get_db_source(session, source_id)
    if row is None:
        return False
    session.delete(row)
    return True
