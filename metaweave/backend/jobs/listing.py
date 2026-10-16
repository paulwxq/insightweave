"""把同一数据源下的 job_info 步骤行按 job_id 收成作业列表。"""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy.exc import SQLAlchemyError

from backend.data_sources.domain_step_service import DOMAIN_STEP_NAME
from backend.data_sources.registration_service import (
    PlatformUnavailableError,
    SourceNotFoundError,
)
from backend.data_sources.repository import get_db_source
from backend.jobs.repository import list_source_step_rows, rollup_job_status
from backend.persistence.models import JobInfo
from backend.persistence.platform_db import PlatformSettings, platform_session
from metaweave.utils.time_utils import utc_isoformat


@dataclass(frozen=True)
class JobStepRecord:
    job_id: UUID
    step_name: str
    last_status: str
    created_at: datetime
    updated_at: datetime
    run_by_user_id: UUID | None
    # 仅 domain 步骤行会写该列；供重跑弹窗预填（doc 21.1 §7.2）
    db_desc: str | None = None


def assemble_collection_jobs(rows: list[JobStepRecord]) -> list[dict]:
    """同一 job_id 的步骤按登记时间从早到晚拼接。

    执行时间取该作业全部步骤行 updated_at 的最大值，即最近一次状态写入。
    执行者仍取最早一条步骤行；为空则不返回该字段值。
    作业按执行时间倒序，数量等于不同 job_id 的个数。
    """
    grouped: dict[UUID, list[JobStepRecord]] = {}
    for row in rows:
        grouped.setdefault(row.job_id, []).append(row)

    items = []
    for job_id, steps in grouped.items():
        ordered = sorted(steps, key=lambda item: (item.created_at, item.step_name))
        earliest = ordered[0]
        latest_update = max(item.updated_at for item in ordered)
        items.append(
            {
                "job_id": str(job_id),
                "executed_at": utc_isoformat(latest_update),
                "steps": [item.step_name for item in ordered],
                # 查询时汇总，与采集首页同一口径（doc 20.1 §11.2），不落库
                "job_status": rollup_job_status([item.last_status for item in ordered]),
                "run_by_user_id": (
                    str(earliest.run_by_user_id) if earliest.run_by_user_id else None
                ),
                # 本作业 domain 行的 db_desc；缺行或值为 NULL 时为 None（doc 21.1 §7.2）
                "domain_description": next(
                    (
                        item.db_desc
                        for item in ordered
                        if item.step_name == DOMAIN_STEP_NAME
                    ),
                    None,
                ),
            }
        )
    items.sort(key=lambda item: item["executed_at"], reverse=True)
    return items


def _record(row: JobInfo) -> JobStepRecord:
    return JobStepRecord(
        job_id=row.job_id,
        step_name=row.step_name,
        last_status=row.last_status,
        created_at=row.created_at,
        updated_at=row.updated_at,
        run_by_user_id=row.run_by_user_id,
        db_desc=row.db_desc,
    )


def list_collection_jobs(settings: PlatformSettings, source_id: UUID) -> dict:
    try:
        with platform_session(settings) as session:
            source = get_db_source(session, source_id)
            if source is None:
                raise SourceNotFoundError("数据源不存在")
            jobs = assemble_collection_jobs(
                [_record(row) for row in list_source_step_rows(session, source_id)]
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
