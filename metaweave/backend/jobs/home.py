"""采集首页：把一个数据源的 job_info 行收成条目和三张统计卡。"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy.exc import SQLAlchemyError

from backend.data_sources.registration_service import (
    PlatformUnavailableError,
    SourceNotFoundError,
)
from backend.data_sources.repository import get_db_source
from backend.jobs.repository import list_source_step_rows, rollup_job_status
from backend.persistence.models import JobInfo
from backend.persistence.platform_db import PlatformSettings, platform_session
from metaweave.utils.time_utils import utc_isoformat

_RECENT_DAYS = 30


@dataclass(frozen=True)
class CollectionHomeStep:
    job_id: UUID
    step_name: str
    last_status: str
    created_at: datetime
    updated_at: datetime


def _rollup(statuses: list[str]) -> str:
    # 与作业列表、发布准入共用同一汇总口径（doc 20.1 §11.2）
    return rollup_job_status(statuses)


def assemble_collection_home(rows: list[CollectionHomeStep], *, now: datetime) -> dict:
    """同一 job_id 收成一条。running 优先于 failed，再否则为 success。

    成功卡只计执行时间落在 now 往前 30 天（含边界）内的作业。失败不限窗口。
    """
    moment = now
    cutoff = moment - timedelta(days=_RECENT_DAYS)
    grouped: dict[UUID, list[CollectionHomeStep]] = {}
    for row in rows:
        grouped.setdefault(row.job_id, []).append(row)

    items = []
    counts = {"running": 0, "success_recent": 0, "failed": 0}
    for job_id, steps in grouped.items():
        ordered = sorted(steps, key=lambda item: (item.created_at, item.step_name))
        executed_at = max(item.updated_at for item in ordered)
        status = _rollup([item.last_status for item in ordered])
        if status == "running":
            counts["running"] += 1
        elif status == "failed":
            counts["failed"] += 1
        elif cutoff <= executed_at <= moment:
            counts["success_recent"] += 1
        items.append(
            {
                "job_id": str(job_id),
                "publish_id": None,
                "executed_at": utc_isoformat(executed_at),
                "steps": [item.step_name for item in ordered],
                "status": status,
            }
        )
    return {"counts": counts, "items": items}


def _record(row: JobInfo) -> CollectionHomeStep:
    return CollectionHomeStep(
        job_id=row.job_id,
        step_name=row.step_name,
        last_status=row.last_status,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def get_collection_home(
    settings: PlatformSettings, source_id: UUID, *, now: datetime | None = None
) -> dict:
    moment = now or datetime.now(timezone.utc)
    try:
        with platform_session(settings) as session:
            source = get_db_source(session, source_id)
            if source is None:
                raise SourceNotFoundError("数据源不存在")
            home = assemble_collection_home(
                [_record(row) for row in list_source_step_rows(session, source_id)],
                now=moment,
            )
            return {"is_active": source.is_active, **home}
    except SourceNotFoundError:
        raise
    except SQLAlchemyError:
        raise PlatformUnavailableError("平台库暂不可用") from None
