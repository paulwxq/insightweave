"""两类作业的保存、版本与查询用例（doc 6.2 §4.3、§6.1）。

首次保存主表与 revision 1 同一事务；修改执行配置新增不可变版本；仅修改
名称/说明不动版本。作业所属工作区创建后不可更换；本期不提供删除入口。
"""

from dataclasses import dataclass

from sqlalchemy.exc import SQLAlchemyError

from backend.data_sources.registration_service import (
    PlatformUnavailableError,
)
from backend.jobs.job_params import (
    COLLECTION_STEP_PARAMS,
    COLLECTION_STEPS,
    PUBLISH_STEP_PARAMS,
    PUBLISH_STEPS,
    validate_step_config,
    validate_steps,
)
from backend.jobs.job_repository import (
    COLLECTION_JOB,
    PUBLISH_JOB,
    JobSpec,
    create_job_with_first_revision,
    get_job,
    get_revision,
    insert_next_revision,
    last_run_times,
    latest_revision_no,
    latest_revisions_for_jobs,
    list_jobs_by_workspace,
    list_jobs_global,
    list_revisions,
    lock_job,
    touch_job_updated_at,
    workspace_source_names,
)
from backend.jobs.run_repository import (
    COLLECTION_RUN,
    PUBLISH_RUN,
    count_runs_for_jobs,
)
from backend.persistence.platform_db import PlatformSettings, platform_session
from backend.workspaces.repository import get_workspace
from backend.workspaces.service import WorkspaceNotFoundError
from metaweave.utils.source_id import (
    normalize_collection_job_id,
    normalize_publish_job_id,
    normalize_source_id,
    normalize_workspace_id,
)
from metaweave.utils.time_utils import utc_isoformat


class JobNotFoundError(LookupError):
    """指定的作业 ID 不存在。"""


class JobRevisionNotFoundError(LookupError):
    """指定的作业版本不存在。"""


class JobNameExistsError(ValueError):
    """同一工作区内已存在同名作业。"""


class JobValidationError(ValueError):
    """作业名称、说明或请求参数不合法。"""


_SPEC_BY_KIND = {"collection": COLLECTION_JOB, "publish": PUBLISH_JOB}
_RUN_SPEC_BY_KIND = {"collection": COLLECTION_RUN, "publish": PUBLISH_RUN}
_STEPS_BY_KIND = {"collection": COLLECTION_STEPS, "publish": PUBLISH_STEPS}
_PARAMS_BY_KIND = {
    "collection": COLLECTION_STEP_PARAMS,
    "publish": PUBLISH_STEP_PARAMS,
}
_NAME_FIELD = {"collection": "collection_job_id", "publish": "publish_job_id"}
_NAME_MAX = 200
_DESC_MAX = 1000


@dataclass(frozen=True)
class JobConfigPayload:
    steps: list[str]
    step_config: dict | None = None
    change_note: str | None = None


def _validate_job_name(name: object) -> str:
    if not isinstance(name, str):
        raise JobValidationError("作业名称必须是字符串")
    name = name.strip()
    if not name:
        raise JobValidationError("作业名称不能为空")
    if len(name) > _NAME_MAX:
        raise JobValidationError(f"作业名称不能超过 {_NAME_MAX} 个字符")
    return name


def _validate_job_desc(desc: object) -> str | None:
    if desc is None:
        return None
    if not isinstance(desc, str):
        raise JobValidationError("作业说明必须是字符串")
    desc = desc.strip()
    if len(desc) > _DESC_MAX:
        raise JobValidationError(f"作业说明不能超过 {_DESC_MAX} 个字符")
    return desc or None


def _validate_change_note(note: object) -> str | None:
    if note is None:
        return None
    if not isinstance(note, str):
        raise JobValidationError("变更说明必须是字符串")
    note = note.strip()
    if len(note) > _DESC_MAX:
        raise JobValidationError(f"变更说明不能超过 {_DESC_MAX} 个字符")
    return note or None


def _validate_config(
    kind: str, payload: JobConfigPayload
) -> tuple[list[str], dict | None]:
    steps = validate_steps(payload.steps, _STEPS_BY_KIND[kind])
    step_config = validate_step_config(
        payload.step_config, steps, _PARAMS_BY_KIND[kind]
    )
    return steps, step_config


def _job_item(kind: str, job) -> dict:
    return {
        "job_id": getattr(job, _NAME_FIELD[kind]),
        "workspace_id": job.workspace_id,
        "job_name": job.job_name,
        "job_desc": job.job_desc,
        "created_at": utc_isoformat(job.created_at),
        "updated_at": utc_isoformat(job.updated_at),
        "job_type": kind,
    }


def _revision_item(kind: str, revision) -> dict:
    return {
        "job_id": getattr(revision, _NAME_FIELD[kind]),
        "revision_no": revision.revision_no,
        "steps": list(revision.steps),
        "step_config": revision.step_config,
        "change_note": revision.change_note,
        "created_at": utc_isoformat(revision.created_at),
    }


def _require_workspace(session, workspace_id: str):
    row = get_workspace(session, workspace_id)
    if row is None:
        raise WorkspaceNotFoundError("工作区不存在")
    return row


def _require_job(spec: JobSpec, session, job_id: str):
    row = get_job(spec, session, job_id)
    if row is None:
        raise JobNotFoundError("作业不存在")
    return row


def create_job(
    settings: PlatformSettings,
    kind: str,
    workspace_id: str,
    name: object,
    desc: object,
    payload: JobConfigPayload,
) -> dict:
    """保存作业定义与版本 1；不执行任何步骤（A02）。"""
    spec = _SPEC_BY_KIND[kind]
    workspace_id = normalize_workspace_id(workspace_id)
    job_name = _validate_job_name(name)
    job_desc = _validate_job_desc(desc)
    change_note = _validate_change_note(payload.change_note)
    steps, step_config = _validate_config(kind, payload)
    try:
        with platform_session(settings) as session:
            _require_workspace(session, workspace_id)
            existing = list_jobs_by_workspace(spec, session, workspace_id)
            if any(row.job_name == job_name for row in existing):
                raise JobNameExistsError("同一工作区内已存在同名作业")
            job, revision = create_job_with_first_revision(
                spec,
                session,
                workspace_id=workspace_id,
                job_name=job_name,
                job_desc=job_desc,
                steps=steps,
                step_config=step_config,
                change_note=change_note,
            )
            session.commit()
            return {
                **_job_item(kind, job),
                "revision": _revision_item(kind, revision),
                "default_steps": list(revision.steps),
                "run_count": 0,
            }
    except SQLAlchemyError:
        raise PlatformUnavailableError("平台库暂不可用") from None


def add_revision(
    settings: PlatformSettings,
    kind: str,
    job_id: str,
    payload: JobConfigPayload,
) -> dict:
    """锁作业行 → max+1 → 插入新版本；旧版本不可变（A03）。"""
    spec = _SPEC_BY_KIND[kind]
    job_id = _normalize_job_id(kind, job_id)
    change_note = _validate_change_note(payload.change_note)
    steps, step_config = _validate_config(kind, payload)
    try:
        with platform_session(settings) as session:
            job = lock_job(spec, session, job_id)
            if job is None:
                raise JobNotFoundError("作业不存在")
            revision = insert_next_revision(
                spec,
                session,
                job,
                steps=steps,
                step_config=step_config,
                change_note=change_note,
            )
            session.commit()
            return _revision_item(kind, revision)
    except SQLAlchemyError:
        raise PlatformUnavailableError("平台库暂不可用") from None


def _normalize_job_id(kind: str, job_id: str) -> str:
    if kind == "collection":
        return normalize_collection_job_id(job_id)
    return normalize_publish_job_id(job_id)


def list_jobs(settings: PlatformSettings, kind: str, workspace_id: str) -> dict:
    spec = _SPEC_BY_KIND[kind]
    run_spec = _RUN_SPEC_BY_KIND[kind]
    workspace_id = normalize_workspace_id(workspace_id)
    try:
        with platform_session(settings) as session:
            _require_workspace(session, workspace_id)
            rows = list_jobs_by_workspace(spec, session, workspace_id)
            job_ids = [getattr(row, spec.id_column) for row in rows]
            latest_map = latest_revisions_for_jobs(spec, session, job_ids)
            count_map = count_runs_for_jobs(run_spec, session, job_ids)
            items = []
            for row in rows:
                item = _job_item(kind, row)
                job_id = getattr(row, spec.id_column)
                latest = latest_map.get(job_id)
                item["latest_revision_no"] = latest[0] if latest is not None else None
                item["default_steps"] = latest[1] if latest is not None else []
                item["run_count"] = count_map.get(job_id, 0)
                items.append(item)
            return {"workspace_id": workspace_id, "job_type": kind, "items": items}
    except SQLAlchemyError:
        raise PlatformUnavailableError("平台库暂不可用") from None


def list_jobs_global_page(
    settings: PlatformSettings,
    kind: str,
    *,
    workspace_id: str | None = None,
    source_id: str | None = None,
    q: str | None = None,
    limit: int = 10,
    offset: int = 0,
) -> dict:
    """全局作业分页列表（跨工作区，设计稿 35/37）。

    workspace_id / source_id / q 均为可选过滤；排序与字段装配见
    job_repository.list_jobs_global。无运行时间 last_run_at 为 None。
    """
    spec = _SPEC_BY_KIND[kind]
    run_spec = _RUN_SPEC_BY_KIND[kind]
    if workspace_id is not None:
        workspace_id = normalize_workspace_id(workspace_id)
    if source_id is not None:
        source_id = normalize_source_id(source_id)
    keyword = q.strip() if q else None
    if keyword is not None:
        if len(keyword) > _NAME_MAX:
            raise JobValidationError(f"关键字最长 {_NAME_MAX} 字符")
        keyword = keyword or None
    if not 1 <= limit <= 100:
        raise JobValidationError("limit 须在 1–100 之间")
    if offset < 0:
        raise JobValidationError("offset 不能为负")
    try:
        with platform_session(settings) as session:
            rows, total = list_jobs_global(
                spec,
                run_spec,
                session,
                workspace_id=workspace_id,
                source_id=source_id,
                keyword=keyword,
                limit=limit,
                offset=offset,
            )
            job_ids = [getattr(row, spec.id_column) for row in rows]
            latest_map = latest_revisions_for_jobs(spec, session, job_ids)
            count_map = count_runs_for_jobs(run_spec, session, job_ids)
            names = workspace_source_names(session, sorted({row.workspace_id for row in rows}))
            run_map = last_run_times(run_spec, session, job_ids)
            items = []
            for row in rows:
                job_id = getattr(row, spec.id_column)
                latest = latest_map.get(job_id)
                workspace_name, row_source_id, source_name = names.get(
                    row.workspace_id, ("", "", "")
                )
                last_run_at = run_map.get(job_id)
                items.append(
                    {
                        **_job_item(kind, row),
                        "latest_revision_no": latest[0] if latest is not None else None,
                        "default_steps": latest[1] if latest is not None else [],
                        "run_count": count_map.get(job_id, 0),
                        "workspace_name": workspace_name,
                        "source_id": row_source_id,
                        "source_name": source_name,
                        "last_run_at": (
                            utc_isoformat(last_run_at) if last_run_at is not None else None
                        ),
                    }
                )
            return {
                "job_type": kind,
                "workspace_id": workspace_id,
                "source_id": source_id,
                "q": keyword,
                "total": total,
                "limit": limit,
                "offset": offset,
                "items": items,
            }
    except SQLAlchemyError:
        raise PlatformUnavailableError("平台库暂不可用") from None


def get_job_detail(settings: PlatformSettings, kind: str, job_id: str) -> dict:
    spec = _SPEC_BY_KIND[kind]
    job_id = _normalize_job_id(kind, job_id)
    try:
        with platform_session(settings) as session:
            job = _require_job(spec, session, job_id)
            latest = latest_revision_no(spec, session, job_id)
            revision = (
                get_revision(spec, session, job_id, latest)
                if latest is not None
                else None
            )
            item = _job_item(kind, job)
            item["latest_revision_no"] = latest
            item["revision"] = (
                _revision_item(kind, revision) if revision is not None else None
            )
            item["default_steps"] = list(revision.steps) if revision is not None else []
            item["run_count"] = count_runs_for_jobs(
                _RUN_SPEC_BY_KIND[kind], session, [job_id]
            ).get(job_id, 0)
            return item
    except SQLAlchemyError:
        raise PlatformUnavailableError("平台库暂不可用") from None


def patch_job(
    settings: PlatformSettings,
    kind: str,
    job_id: str,
    *,
    name: object = None,
    desc: object = None,
    has_name: bool = False,
    has_desc: bool = False,
) -> dict:
    """仅修改主表名称/说明；不产生新版本（doc 6.2 §4.3.3）。"""
    spec = _SPEC_BY_KIND[kind]
    job_id = _normalize_job_id(kind, job_id)
    if not (has_name or has_desc):
        raise JobValidationError("没有可修改的字段")
    try:
        with platform_session(settings) as session:
            job = lock_job(spec, session, job_id)
            if job is None:
                raise JobNotFoundError("作业不存在")
            if has_name:
                new_name = _validate_job_name(name)
                existing = list_jobs_by_workspace(spec, session, job.workspace_id)
                if any(
                    row.job_name == new_name and getattr(row, spec.id_column) != job_id
                    for row in existing
                ):
                    raise JobNameExistsError("同一工作区内已存在同名作业")
                job.job_name = new_name
            if has_desc:
                job.job_desc = _validate_job_desc(desc)
            touch_job_updated_at(session, job)
            session.commit()
            return _job_item(kind, job)
    except SQLAlchemyError:
        raise PlatformUnavailableError("平台库暂不可用") from None


def list_revisions_endpoint(settings: PlatformSettings, kind: str, job_id: str) -> dict:
    spec = _SPEC_BY_KIND[kind]
    job_id = _normalize_job_id(kind, job_id)
    try:
        with platform_session(settings) as session:
            _require_job(spec, session, job_id)
            rows = list_revisions(spec, session, job_id)
            return {
                "job_id": job_id,
                "items": [_revision_item(kind, row) for row in rows],
            }
    except SQLAlchemyError:
        raise PlatformUnavailableError("平台库暂不可用") from None


def get_revision_detail(
    settings: PlatformSettings, kind: str, job_id: str, revision_no: int
) -> dict:
    spec = _SPEC_BY_KIND[kind]
    job_id = _normalize_job_id(kind, job_id)
    if not isinstance(revision_no, int) or revision_no < 1:
        raise JobRevisionNotFoundError("作业版本不存在")
    try:
        with platform_session(settings) as session:
            _require_job(spec, session, job_id)
            row = get_revision(spec, session, job_id, revision_no)
            if row is None:
                raise JobRevisionNotFoundError("作业版本不存在")
            return _revision_item(kind, row)
    except SQLAlchemyError:
        raise PlatformUnavailableError("平台库暂不可用") from None
