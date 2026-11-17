"""数据源登记表访问。source_id 为 text 短 ID（doc 6.1 §13）。"""

from datetime import datetime

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from backend.persistence.models import (
    CollectionJobInfo,
    CollectionRunInfo,
    DbProfile,
    DbSourceInfo,
    PublishJobInfo,
    PublishRunInfo,
    TableColumnProfile,
    TableDefinition,
    TableDomain,
    TableProfile,
    WorkspaceInfo,
)


def get_db_source(session: Session, source_id: str) -> DbSourceInfo | None:
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
) -> tuple[dict[str, datetime], dict[str, datetime]]:
    """每个数据源最近一次采集运行与发布运行的创建时间。

    采集经 collection_run_info → collection_job_info → workspace_info 归源；
    发布经 publish_run_info → publish_job_info → workspace_info 归源。
    没有对应运行的数据源不会出现在字典里。
    """
    collected_rows = session.execute(
        select(
            WorkspaceInfo.source_id,
            func.max(CollectionRunInfo.created_at),
        )
        .join(
            CollectionJobInfo,
            CollectionJobInfo.collection_job_id == CollectionRunInfo.collection_job_id,
        )
        .join(
            WorkspaceInfo,
            WorkspaceInfo.workspace_id == CollectionJobInfo.workspace_id,
        )
        .group_by(WorkspaceInfo.source_id)
    ).all()
    published_rows = session.execute(
        select(
            WorkspaceInfo.source_id,
            func.max(PublishRunInfo.created_at),
        )
        .join(
            PublishJobInfo,
            PublishJobInfo.publish_job_id == PublishRunInfo.publish_job_id,
        )
        .join(
            WorkspaceInfo,
            WorkspaceInfo.workspace_id == PublishJobInfo.workspace_id,
        )
        .group_by(WorkspaceInfo.source_id)
    ).all()
    collected = {
        source_id: moment for source_id, moment in collected_rows if moment is not None
    }
    published = {
        source_id: moment for source_id, moment in published_rows if moment is not None
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
    if "source_id" in fields:
        # 登记入口不接受客户端指定 source_id（doc 6.1 §13.1）；
        # 短 ID 由数据库序列默认值生成并取回。
        raise ValueError("source_id 由数据库生成，不接受外部指定")
    row = DbSourceInfo(**fields)
    session.add(row)
    session.commit()
    return row


def update_db_source(
    session: Session, source_id: str, fields: dict
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
    session: Session, source_id: str, is_active: bool
) -> DbSourceInfo | None:
    """按主键更新启用状态；找不到返回 None，不触碰其他列（doc 11）。"""
    row = get_db_source(session, source_id)
    if row is None:
        return None
    row.is_active = is_active
    row.updated_at = func.now()
    session.commit()
    return row


def count_workspaces_by_source(session: Session, source_id: str) -> int:
    stmt = (
        select(func.count())
        .select_from(WorkspaceInfo)
        .where(WorkspaceInfo.source_id == source_id)
    )
    return int(session.scalar(stmt) or 0)


def purge_source_profiles(session: Session, source_id: str) -> dict:
    """删除该源当前已发布内容行（doc 6.1 §14.1、doc 8.1 §6.1）。

    依次删除 table_column_profile、table_profile、table_definition、
    table_domain、db_profile 五张内容表；字段画像同时显式删除
    （虽可依赖 CASCADE，显式列出便于统计核验）。返回各表删除计数。
    事务与提交由调用方决定，本模块不提交。
    """
    deleted = {}
    for name, model in (
        ("table_column_profile", TableColumnProfile),
        ("table_profile", TableProfile),
        ("table_definition", TableDefinition),
        ("table_domain", TableDomain),
        ("db_profile", DbProfile),
    ):
        result = session.execute(
            delete(model).where(model.source_id == source_id)
        )
        deleted[name] = int(getattr(result, "rowcount", 0) or 0)
    return {"deleted": deleted}


def remove_db_source(session: Session, source_id: str) -> bool:
    """登记行加入待删除，不提交；不存在返回 False（doc 16 §6）。"""
    row = get_db_source(session, source_id)
    if row is None:
        return False
    session.delete(row)
    return True
