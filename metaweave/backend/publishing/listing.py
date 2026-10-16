"""把同一数据源下的 publish_info 步骤行按 publish_id 收成发布作业列表。"""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy.exc import SQLAlchemyError

from backend.data_sources.registration_service import (
    PlatformUnavailableError,
    SourceNotFoundError,
)
from backend.data_sources.repository import get_db_source
from backend.persistence.models import PublishInfo
from backend.persistence.platform_db import PlatformSettings, platform_session
from backend.publishing.repository import list_publish_id_rows, list_source_publish_rows
from metaweave.utils.time_utils import utc_isoformat


class PublishNotFoundError(LookupError):
    """指定的 publish_id 不存在，或不属于该数据源。"""


PROFILE_STEPS = ("db", "ddl", "json", "domain")
# doc 23.2 §1.5：cql 为从未兑现的占位成员（发布白名单从未含 cql，
# publish_info 不可能有 step_name=cql 行），删之换 rel
RAG_STEPS = ("sql", "table_schema", "dim_value", "rel")


@dataclass(frozen=True)
class PublishStepRecord:
    publish_id: UUID
    job_id: UUID
    step_name: str
    created_at: datetime
    created_by_user_id: UUID | None


def _split_steps(step_names: set[str]) -> tuple[list[str], list[str]]:
    profile = [name for name in PROFILE_STEPS if name in step_names]
    rag = [name for name in RAG_STEPS if name in step_names]
    extra = sorted(step_names.difference(PROFILE_STEPS, RAG_STEPS))
    return profile + extra, rag


def assemble_publish_jobs(rows: list[PublishStepRecord]) -> list[dict]:
    """同一 publish_id 收成一条发布作业。

    发布时间取该次发布全部步骤行 created_at 的最大值。
    作业 ID 与执行者取最早一条步骤行；执行者为空则不返回该字段值。
    发布内容按画像与 RAG 两组展示，只包含实际写入的步骤。
    作业按发布时间倒序。
    """
    grouped: dict[UUID, list[PublishStepRecord]] = {}
    for row in rows:
        grouped.setdefault(row.publish_id, []).append(row)

    items = []
    for publish_id, steps in grouped.items():
        ordered = sorted(steps, key=lambda item: (item.created_at, item.step_name))
        earliest = ordered[0]
        profile_steps, rag_steps = _split_steps({item.step_name for item in ordered})
        items.append(
            {
                "publish_id": str(publish_id),
                "job_id": str(earliest.job_id),
                "published_at": utc_isoformat(max(item.created_at for item in ordered)),
                "profile_steps": profile_steps,
                "rag_steps": rag_steps,
                "created_by_user_id": (
                    str(earliest.created_by_user_id)
                    if earliest.created_by_user_id
                    else None
                ),
            }
        )
    items.sort(key=lambda item: item["published_at"], reverse=True)
    return items


def _record(row: PublishInfo) -> PublishStepRecord:
    return PublishStepRecord(
        publish_id=row.publish_id,
        job_id=row.job_id,
        step_name=row.step_name,
        created_at=row.created_at,
        created_by_user_id=row.created_by_user_id,
    )


def get_publish_definition(
    settings: PlatformSettings, source_id: UUID, publish_id: UUID
) -> dict:
    """读取某一次发布在 publish_info 中实际写入的步骤。"""
    try:
        with platform_session(settings) as session:
            source = get_db_source(session, source_id)
            if source is None:
                raise SourceNotFoundError("数据源不存在")
            rows = list_publish_id_rows(session, publish_id)
            if not rows or any(row.source_id != source_id for row in rows):
                raise PublishNotFoundError("发布不存在或不属于该数据源")
            ordered = sorted(rows, key=lambda item: (item.created_at, item.step_name))
            return {
                "publish_id": str(publish_id),
                "job_id": str(ordered[0].job_id),
                "steps": [item.step_name for item in ordered],
            }
    except (SourceNotFoundError, PublishNotFoundError):
        raise
    except SQLAlchemyError:
        raise PlatformUnavailableError("平台库暂不可用") from None


def list_publish_jobs(settings: PlatformSettings, source_id: UUID) -> dict:
    try:
        with platform_session(settings) as session:
            source = get_db_source(session, source_id)
            if source is None:
                raise SourceNotFoundError("数据源不存在")
            jobs = assemble_publish_jobs(
                [_record(row) for row in list_source_publish_rows(session, source_id)]
            )
            version = source.db_version or ""
            db_type_version = (
                source.db_type if not version else f"{source.db_type} {version}"
            )
            return {
                "source": {
                    "source_id": str(source.source_id),
                    "display_name": source.display_name,
                    "host": source.host,
                    "port": source.port,
                    "db_name": source.db_name,
                    "db_type_version": db_type_version,
                    "description": source.description,
                    "is_active": source.is_active,
                },
                "job_count": len(jobs),
                "items": jobs,
            }
    except SQLAlchemyError:
        raise PlatformUnavailableError("平台库暂不可用") from None
