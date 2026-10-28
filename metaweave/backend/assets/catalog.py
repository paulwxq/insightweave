"""数据资产首页：按每个数据源当前 db_profile 行展示（doc 6.1 §14）。

新模型下 db_profile 每源一行即是当前内容；发布来源取行上的
`publish_run_id`（该行最后一次成功写入的发布运行），不再有 current
标记或一致性核对（doc 6.1 §10.1）。
"""

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from metaweave.utils.time_utils import utc_isoformat

from backend.data_sources.registration_service import (
    PlatformUnavailableError,
    SourceNotFoundError,
)
from backend.data_sources.repository import get_db_source
from backend.persistence.models import DbProfile, DbSourceInfo, PublishRunInfo
from backend.persistence.platform_db import PlatformSettings, platform_session
from metaweave.utils.source_id import normalize_source_id

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


def _item(profile: DbProfile, display_name: str, publish_run_id) -> dict:
    return {
        "publish_run_id": str(profile.publish_run_id),
        "source_id": profile.source_id,
        "display_name": display_name or profile.db_name,
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
        "published_at": (
            utc_isoformat(publish_run_id.created_at) if publish_run_id else None
        ),
    }


def assemble_asset_catalog(profiles, names, runs_by_id) -> dict:
    """每个有当前画像的源一行；发布来源取画像行的 publish_run_id。"""
    items = [
        _item(
            profile,
            names.get(profile.source_id, ""),
            runs_by_id.get(profile.publish_run_id),
        )
        for profile in profiles
    ]
    items.sort(key=lambda item: item["collected_at"], reverse=True)
    return {
        "source_count": len(items),
        "object_count": sum(item["object_count"] for item in items),
        "project_count": 0,
        "items": items,
    }


def list_asset_catalog(settings: PlatformSettings) -> dict:
    try:
        with platform_session(settings) as session:
            profiles = list(session.scalars(select(DbProfile)))
            source_ids = sorted({profile.source_id for profile in profiles})
            names = (
                {
                    row.source_id: row.display_name
                    for row in session.scalars(
                        select(DbSourceInfo).where(
                            DbSourceInfo.source_id.in_(source_ids)
                        )
                    )
                }
                if source_ids
                else {}
            )
            run_ids = sorted({profile.publish_run_id for profile in profiles})
            runs_by_id = (
                {
                    row.publish_run_id: row
                    for row in session.scalars(
                        select(PublishRunInfo).where(
                            PublishRunInfo.publish_run_id.in_(run_ids)
                        )
                    )
                }
                if run_ids
                else {}
            )
            return assemble_asset_catalog(profiles, names, runs_by_id)
    except SQLAlchemyError:
        raise PlatformUnavailableError("平台库暂不可用") from None


def _source_block(source, profile) -> dict:
    display_name = source.display_name
    if not display_name and profile is not None:
        display_name = profile.db_name
    return {
        "source_id": source.source_id,
        "display_name": display_name or "",
        "username": source.username or "",
    }


def _publish_block(run) -> dict:
    return {
        "publish_run_id": str(run.publish_run_id),
        "status": run.status,
        "published_at": utc_isoformat(run.created_at),
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


def assemble_asset_profile(source, profile, run) -> dict:
    """画像页：无 db_profile 行 → not_published；有则 ready（doc 6.1 §14）。"""
    if profile is None:
        return {
            "state": "not_published",
            "source": _source_block(source, None),
            "publish": None,
            "profile": None,
        }
    return {
        "state": "ready",
        "source": _source_block(source, profile),
        "publish": _publish_block(run) if run is not None else None,
        "profile": _profile_block(profile),
    }


def get_asset_profile(settings: PlatformSettings, source_id: str) -> dict:
    source_id = normalize_source_id(source_id)
    try:
        with platform_session(settings) as session:
            source = get_db_source(session, source_id)
            if source is None:
                raise SourceNotFoundError("数据源不存在")
            profile = session.get(DbProfile, source_id)
            run = (
                session.get(PublishRunInfo, profile.publish_run_id)
                if profile is not None
                else None
            )
            return assemble_asset_profile(source, profile, run)
    except SQLAlchemyError:
        raise PlatformUnavailableError("平台库暂不可用") from None
