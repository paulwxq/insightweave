"""工作区运行列表与作业列表（doc 6.2 §6.1、A26）。

运行列表在后端通过运行与作业主表关联限定工作区，不由前端逐作业聚合；
与作业运行历史统一 `limit`/`offset` 分页（默认 20，范围 1–100），
按运行 `created_at DESC`、运行 UUID `DESC` 排序。
"""

from sqlalchemy.exc import SQLAlchemyError

from backend.data_sources.registration_service import (
    PlatformUnavailableError,
)
from backend.jobs.job_repository import (
    COLLECTION_JOB,
    PUBLISH_JOB,
    JobSpec,
)
from backend.jobs.run_repository import (
    COLLECTION_RUN,
    PUBLISH_RUN,
    RunSpec,
    list_runs_for_workspace,
)
from backend.paging import DEFAULT_LIMIT, validate_page
from backend.persistence.platform_db import PlatformSettings, platform_session
from backend.workspaces.repository import get_workspace
from backend.workspaces.service import WorkspaceNotFoundError
from metaweave.utils.time_utils import utc_isoformat


def _run_item(run, job, kind: str) -> dict:
    run_id = (
        str(run.collection_run_id) if kind == "collection" else str(run.publish_run_id)
    )
    job_id = job.collection_job_id if kind == "collection" else job.publish_job_id
    item = {
        "run_id": run_id,
        "job_id": job_id,
        "job_name": job.job_name,
        "revision_no": run.revision_no,
        "status": run.status,
        "trigger_type": run.trigger_type,
        "created_at": utc_isoformat(run.created_at),
        "started_at": utc_isoformat(run.started_at) if run.started_at else None,
        "finished_at": utc_isoformat(run.finished_at) if run.finished_at else None,
        "run_type": kind,
    }
    return item


def _workspace_run_list(
    settings: PlatformSettings,
    spec: RunSpec,
    job_spec: JobSpec,
    kind: str,
    workspace_id: str,
    limit: int,
    offset: int,
) -> dict:
    page = validate_page(limit, offset)
    try:
        with platform_session(settings) as session:
            if get_workspace(session, workspace_id) is None:
                raise WorkspaceNotFoundError("工作区不存在")
            runs, total = list_runs_for_workspace(
                spec, session, workspace_id, limit=page.limit, offset=page.offset
            )
            items = []
            for run in runs:
                job = session.get(job_spec.info_table, getattr(run, spec.job_id_column))
                items.append(_run_item(run, job, kind))
            return {
                "workspace_id": workspace_id,
                "run_type": kind,
                "total": total,
                "limit": page.limit,
                "offset": page.offset,
                "items": items,
            }
    except SQLAlchemyError:
        raise PlatformUnavailableError("平台库暂不可用") from None


def list_workspace_collection_runs(
    settings: PlatformSettings,
    workspace_id: str,
    *,
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
) -> dict:
    from metaweave.utils.source_id import normalize_workspace_id

    return _workspace_run_list(
        settings,
        COLLECTION_RUN,
        COLLECTION_JOB,
        "collection",
        normalize_workspace_id(workspace_id),
        limit,
        offset,
    )


def list_workspace_publish_runs(
    settings: PlatformSettings,
    workspace_id: str,
    *,
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
) -> dict:
    from metaweave.utils.source_id import normalize_workspace_id

    return _workspace_run_list(
        settings,
        PUBLISH_RUN,
        PUBLISH_JOB,
        "publish",
        normalize_workspace_id(workspace_id),
        limit,
        offset,
    )
