"""发布首页：把一个数据源的 publish_info 行收成条目和三张统计卡。"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy.exc import SQLAlchemyError

from backend.data_sources.registration_service import (
    PlatformUnavailableError,
    SourceNotFoundError,
)
from backend.data_sources.repository import get_db_source
from backend.persistence.models import PublishInfo
from backend.persistence.platform_db import PlatformSettings, platform_session
from backend.publishing.listing import PROFILE_STEPS, RAG_STEPS
from backend.publishing.repository import list_source_publish_rows
from metaweave.utils.time_utils import utc_isoformat

_RECENT_DAYS = 30


@dataclass(frozen=True)
class PublishHomeStep:
    publish_id: UUID
    job_id: UUID
    step_name: str
    status: str
    is_current_publish: bool
    created_at: datetime


def assemble_publish_home(rows: list[PublishHomeStep], *, now: datetime) -> dict:
    """同一 publish_id 收成一条。任一步骤失败则整条失败，否则成功。

    current 只数 is_current_publish 且 success 的步骤行，不查 db_profile。
    成功卡只计发布时间落在 now 往前 30 天（含边界）内的发布。失败不限窗口。
    """
    moment = now
    cutoff = moment - timedelta(days=_RECENT_DAYS)
    grouped: dict[UUID, list[PublishHomeStep]] = {}
    for row in rows:
        grouped.setdefault(row.publish_id, []).append(row)

    items = []
    success_recent = 0
    failed = 0
    for publish_id, steps in grouped.items():
        ordered = sorted(steps, key=lambda item: (item.created_at, item.step_name))
        published_at = max(item.created_at for item in ordered)
        status = "failed" if any(item.status == "failed" for item in ordered) else "success"
        if status == "failed":
            failed += 1
        elif cutoff <= published_at <= moment:
            success_recent += 1
        names = {item.step_name for item in ordered}
        profile = [name for name in PROFILE_STEPS if name in names]
        rag = [name for name in RAG_STEPS if name in names]
        extra = sorted(names.difference(PROFILE_STEPS, RAG_STEPS))
        items.append(
            {
                "job_id": str(ordered[0].job_id),
                "publish_id": str(publish_id),
                "executed_at": utc_isoformat(published_at),
                "steps": profile + extra + rag,
                "status": status,
            }
        )
    return {
        "counts": {
            "current": sum(
                1 for row in rows if row.is_current_publish and row.status == "success"
            ),
            "success_recent": success_recent,
            "failed": failed,
        },
        "items": items,
    }


def _record(row: PublishInfo) -> PublishHomeStep:
    return PublishHomeStep(
        publish_id=row.publish_id,
        job_id=row.job_id,
        step_name=row.step_name,
        status=row.status,
        is_current_publish=row.is_current_publish,
        created_at=row.created_at,
    )


def get_publish_home(
    settings: PlatformSettings, source_id: UUID, *, now: datetime | None = None
) -> dict:
    moment = now or datetime.now(timezone.utc)
    try:
        with platform_session(settings) as session:
            source = get_db_source(session, source_id)
            if source is None:
                raise SourceNotFoundError("数据源不存在")
            home = assemble_publish_home(
                [_record(row) for row in list_source_publish_rows(session, source_id)],
                now=moment,
            )
            return {"is_active": source.is_active, **home}
    except SourceNotFoundError:
        raise
    except SQLAlchemyError:
        raise PlatformUnavailableError("平台库暂不可用") from None
