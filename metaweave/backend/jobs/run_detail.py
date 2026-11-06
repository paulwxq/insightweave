"""运行详情与作业运行历史查询（doc 6.2 §6.1）。

详情返回整体状态、逐步骤状态/时间/参数与 manifest；作业运行历史按该
作业列出运行。采集与发布共用。
"""

from uuid import UUID

from sqlalchemy.exc import SQLAlchemyError

from backend.data_sources.registration_service import PlatformUnavailableError
from backend.jobs.active_runs import is_active as _is_executor_active
from backend.jobs.job_repository import COLLECTION_JOB, PUBLISH_JOB, JobSpec
from backend.paging import DEFAULT_LIMIT, validate_page
from backend.jobs.run_repository import (
    COLLECTION_RUN,
    PUBLISH_RUN,
    RunSpec,
    get_run,
    list_run_steps,
    list_runs_for_job,
)
from backend.persistence.platform_db import PlatformSettings, platform_session
from metaweave.utils.time_utils import utc_isoformat


class RunNotFoundError(LookupError):
    """运行不存在。"""


def _iso(value) -> str | None:
    return utc_isoformat(value) if value is not None else None


def _step_item(step, kind: str) -> dict:
    item = {
        "step_name": step.step_name,
        "status": step.status,
        "message": step.message,
        "input_manifest": step.input_manifest,
        "started_at": _iso(step.started_at),
        "finished_at": _iso(step.finished_at),
    }
    if kind == "collection":
        item["output_manifest"] = step.output_manifest
    else:
        item["target_manifest"] = getattr(step, "target_manifest", None)
    return item


def _run_detail(
    settings: PlatformSettings,
    spec: RunSpec,
    job_spec: JobSpec,
    kind: str,
    run_id: UUID,
) -> dict:
    with platform_session(settings) as session:
        run = get_run(spec, session, run_id)
        if run is None:
            raise RunNotFoundError("运行不存在")
        job = session.get(job_spec.info_table, getattr(run, spec.job_id_column))
        workspace_id = job.workspace_id if job else None
        run_id_field = "collection_run_id" if kind == "collection" else "publish_run_id"
        steps = list_run_steps(spec, session, run_id)
        # 步骤按启动时间排列（list_run_steps：started_at、step_name，
        # doc 10.1 §4.6）；同一启动时间按步骤名排序，未启动或被阻断且无
        # 启动时间的步骤沿用该查询排列，不代表执行计划。
        detail = {
            run_id_field: str(getattr(run, run_id_field)),
            "job_id": getattr(run, spec.job_id_column),
            "job_name": job.job_name if job else None,
            "workspace_id": workspace_id,
            "revision_no": run.revision_no,
            "trigger_type": run.trigger_type,
            "status": run.status,
            "message": run.message,
            "created_at": _iso(run.created_at),
            "started_at": _iso(run.started_at),
            "finished_at": _iso(run.finished_at),
            "run_type": kind,
            # doc 11 §4.7：本进程执行者状态（纯注册表读取，不加数据库锁）
            "executor_active": _is_executor_active(kind, run_id),
            # doc 10.2 §8：只读可选字段——创建时冻结的执行计划审计快照；
            # 旧记录无该列值返回 None（legacy serial），旧客户端兼容。
            "execution_plan": getattr(run, "execution_plan", None),
            "steps": [_step_item(step, kind) for step in steps],
        }
        # 步骤参数一并返回（运行详情可看实际生效参数）
        for item, step in zip(detail["steps"], steps):
            item["step_config"] = step.step_config
        return detail


def get_collection_run_detail(
    settings: PlatformSettings, collection_run_id: UUID
) -> dict:
    return _run_detail(
        settings, COLLECTION_RUN, COLLECTION_JOB, "collection", collection_run_id
    )


def get_publish_run_detail(settings: PlatformSettings, publish_run_id: UUID) -> dict:
    return _run_detail(settings, PUBLISH_RUN, PUBLISH_JOB, "publish", publish_run_id)


def _job_runs(
    settings: PlatformSettings,
    spec: RunSpec,
    job_spec: JobSpec,
    kind: str,
    job_id: str,
    limit: int,
    offset: int,
) -> dict:
    page = validate_page(limit, offset)
    try:
        with platform_session(settings) as session:
            job = session.get(job_spec.info_table, job_id)
            if job is None:
                raise RunNotFoundError("作业不存在")
            runs, total = list_runs_for_job(
                spec, session, job_id, limit=page.limit, offset=page.offset
            )
            items = []
            for run in runs:
                run_id_attr = (
                    "collection_run_id" if kind == "collection" else "publish_run_id"
                )
                items.append(
                    {
                        "run_id": str(getattr(run, run_id_attr)),
                        "job_id": job_id,
                        "job_name": job.job_name,
                        "revision_no": run.revision_no,
                        "status": run.status,
                        "trigger_type": run.trigger_type,
                        "created_at": _iso(run.created_at),
                        "started_at": _iso(run.started_at),
                        "finished_at": _iso(run.finished_at),
                        "run_type": kind,
                    }
                )
            return {
                "job_id": job_id,
                "run_type": kind,
                "total": total,
                "limit": page.limit,
                "offset": page.offset,
                "items": items,
            }
    except SQLAlchemyError:
        raise PlatformUnavailableError("平台库暂不可用") from None


def list_collection_job_runs(
    settings: PlatformSettings,
    collection_job_id: str,
    *,
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
) -> dict:
    from metaweave.utils.source_id import normalize_collection_job_id

    return _job_runs(
        settings,
        COLLECTION_RUN,
        COLLECTION_JOB,
        "collection",
        normalize_collection_job_id(collection_job_id),
        limit,
        offset,
    )


def list_publish_job_runs(
    settings: PlatformSettings,
    publish_job_id: str,
    *,
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
) -> dict:
    from metaweave.utils.source_id import normalize_publish_job_id

    return _job_runs(
        settings,
        PUBLISH_RUN,
        PUBLISH_JOB,
        "publish",
        normalize_publish_job_id(publish_job_id),
        limit,
        offset,
    )
