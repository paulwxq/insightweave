"""数据资产首页：按每个数据源当前 db 发布读取 db_profile。"""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from metaweave.utils.time_utils import utc_isoformat

from backend.data_sources.registration_service import (
    PlatformUnavailableError,
    SourceNotFoundError,
)
from backend.data_sources.repository import get_db_source
from backend.persistence.models import DbProfile, DbSourceInfo, PublishInfo
from backend.persistence.platform_db import PlatformSettings, platform_session

_TYPE_LABELS = {
    "postgresql": "PostgreSQL",
    "mysql": "MySQL",
    "sqlite": "SQLite",
    "csv": "CSV",
    "excel": "Excel",
}
_FILE_TYPES = frozenset({"sqlite", "csv", "excel"})


def _type_label(db_type: str) -> str:
    return _TYPE_LABELS.get(db_type.lower(), db_type)


def _connection(profile: DbProfile) -> str:
    if profile.db_type.lower() in _FILE_TYPES or not profile.host.strip():
        return profile.db_name
    return f"{profile.host}:{profile.port}/{profile.db_name}"


def assemble_asset_catalog(publishes, profiles, sources) -> dict:
    """只展示每个数据源 db 步骤 current 且成功的画像。

    画像每源一行（doc 16 §3.1），按 source_id 对位；画像行的 publish_id
    必须与该源 db current 成功发布一致，否则计为一致性问题，不换用其他版本。
    """
    current: dict = {}
    for row in publishes:
        if row.step_name == "db" and row.is_current_publish:
            current[row.source_id] = row
    profile_by_source = {profile.source_id: profile for profile in profiles}
    names = {source.source_id: source.display_name for source in sources}
    items = []
    inconsistent_count = 0
    for source_id, publish in current.items():
        if publish.status != "success":
            continue
        profile = profile_by_source.get(source_id)
        if profile is None or profile.publish_id != publish.publish_id:
            inconsistent_count += 1
            continue
        items.append(
            {
                "publish_id": str(publish.publish_id),
                "source_id": str(source_id),
                "display_name": names.get(source_id) or profile.db_name,
                "db_type": profile.db_type,
                "db_type_label": _type_label(profile.db_type),
                "server_version": profile.server_version,
                "connection": _connection(profile),
                "table_count": (
                    profile.ordinary_table_count
                    + profile.partitioned_table_count
                    + profile.view_count
                    + profile.materialized_view_count
                ),
                "object_count": profile.object_count,
                "size_bytes": profile.size_bytes,
                "collected_at": utc_isoformat(profile.collected_at),
            }
        )
    items.sort(key=lambda item: item["collected_at"], reverse=True)
    return {
        "source_count": len(items),
        "object_count": sum(item["object_count"] for item in items),
        "project_count": 0,
        "inconsistent_count": inconsistent_count,
        "items": items,
    }


def _source_block(source, profile) -> dict:
    display_name = source.display_name
    if not display_name and profile is not None:
        display_name = profile.db_name
    return {
        "source_id": str(source.source_id),
        "display_name": display_name or "",
        "username": source.username or "",
    }


def _publish_block(publish) -> dict:
    return {
        "publish_id": str(publish.publish_id),
        "status": publish.status,
        "error_summary": publish.error_summary,
        "published_at": utc_isoformat(publish.created_at),
    }


def _profile_block(profile) -> dict:
    return {
        "db_type": profile.db_type,
        "db_type_label": _type_label(profile.db_type),
        "db_name": profile.db_name,
        "host": profile.host,
        "port": profile.port,
        "server_version": profile.server_version,
        "server_version_full": profile.server_version_full,
        "os_platform": profile.os_platform,
        "collected_at": utc_isoformat(profile.collected_at),
        "size_bytes": profile.size_bytes,
        "size_scope": profile.size_scope,
        "record_count": profile.record_count,
        "selected_schemas": list(profile.selected_schemas),
        "schemas": list(profile.schemas),
        "totals": {
            "ordinary_table_count": profile.ordinary_table_count,
            "partitioned_table_count": profile.partitioned_table_count,
            "partition_child_count": profile.partition_child_count,
            "view_count": profile.view_count,
            "materialized_view_count": profile.materialized_view_count,
            "object_count": profile.object_count,
        },
    }


def assemble_asset_profile(source, publishes, profiles) -> dict:
    """按 db 步骤的 current 发布组装画像页。失败的 current 不回退旧画像。

    画像按 source_id 对位（每源一行）；画像行 publish_id 与 current 发布
    不一致时计为 inconsistent，不换用其他版本。
    """
    current = next(
        (
            row
            for row in publishes
            if row.step_name == "db"
            and row.is_current_publish
            and row.source_id == source.source_id
        ),
        None,
    )
    if current is None:
        return {
            "state": "not_published",
            "source": _source_block(source, None),
            "publish": None,
            "profile": None,
        }
    publish_block = _publish_block(current)
    if current.status != "success":
        return {
            "state": "failed",
            "source": _source_block(source, None),
            "publish": publish_block,
            "profile": None,
        }
    profile = next(
        (item for item in profiles if item.source_id == source.source_id), None
    )
    if profile is None or profile.publish_id != current.publish_id:
        return {
            "state": "inconsistent",
            "source": _source_block(source, None),
            "publish": publish_block,
            "profile": None,
        }
    return {
        "state": "ready",
        "source": _source_block(source, profile),
        "publish": publish_block,
        "profile": _profile_block(profile),
    }


def get_asset_profile(settings: PlatformSettings, source_id: UUID) -> dict:
    try:
        with platform_session(settings) as session:
            source = get_db_source(session, source_id)
            if source is None:
                raise SourceNotFoundError("数据源不存在")
            publishes = list(
                session.scalars(
                    select(PublishInfo).where(
                        PublishInfo.source_id == source_id,
                        PublishInfo.step_name == "db",
                        PublishInfo.is_current_publish.is_(True),
                    )
                )
            )
            profiles = (
                list(
                    session.scalars(
                        select(DbProfile).where(DbProfile.source_id == source_id)
                    )
                )
                if any(row.status == "success" for row in publishes)
                else []
            )
            return assemble_asset_profile(source, publishes, profiles)
    except SQLAlchemyError:
        raise PlatformUnavailableError("平台库暂不可用") from None


def list_asset_catalog(settings: PlatformSettings) -> dict:
    try:
        with platform_session(settings) as session:
            publishes = list(
                session.scalars(
                    select(PublishInfo).where(
                        PublishInfo.step_name == "db",
                        PublishInfo.is_current_publish.is_(True),
                    )
                )
            )
            source_ids = [row.source_id for row in publishes if row.status == "success"]
            profiles = (
                list(
                    session.scalars(
                        select(DbProfile).where(DbProfile.source_id.in_(source_ids))
                    )
                )
                if source_ids
                else []
            )
            source_ids = list({profile.source_id for profile in profiles})
            sources = (
                list(
                    session.scalars(
                        select(DbSourceInfo).where(
                            DbSourceInfo.source_id.in_(source_ids)
                        )
                    )
                )
                if source_ids
                else []
            )
            return assemble_asset_catalog(publishes, profiles, sources)
    except SQLAlchemyError:
        raise PlatformUnavailableError("平台库暂不可用") from None
