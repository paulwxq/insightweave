"""工作区、作业、版本与运行路由（doc 6.2 §6.1）。

运行创建采用同进程后台执行（FastAPI BackgroundTasks + 线程）：
POST 返回 202 与新运行 UUID；后台任务只执行本次请求新建的那一个运行
（doc 6.2 §6.3）。发布运行创建前先做集中预检，不合格整单 400。
"""

import asyncio
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Response

from backend.api.errors import RunResolveError
from backend.api.routers.data_sources import get_platform_settings
from backend.api.schemas import (
    CollectionRunDetail,
    DeletionConfirmRequest,
    DeletionRetryRequest,
    DeletionTaskDetail,
    DeletionTaskItem,
    DomainConfigResponse,
    DomainConfigTablesResponse,
    DomainConfigUpdateRequest,
    DimValueColumnsResponse,
    DimValueConfigGetResponse,
    DimValueConfigUpdateRequest,
    DimValueConfigUpdateResponse,
    DimValueObjectsResponse,
    DimValueRecommendationsResponse,
    JobCreateRequest,
    JobItem,
    JobListResponse,
    GlobalJobListResponse,
    JobPatchRequest,
    PublishCheckResponse,
    PublishJobPrecheckRequest,
    PublishJobPrecheckResponse,
    PublishRunDetail,
    RelConfigColumnsResponse,
    RelConfigGetResponse,
    RelConfigUpdateRequest,
    RelConfigUpdateResponse,
    RevisionItem,
    RevisionListResponse,
    RunCreateRequest,
    RunListResponse,
    RunResolveRequest,
    RunResolveResponse,
    WorkspaceCreateRequest,
    WorkspaceItem,
    WorkspaceListResponse,
    WorkspacePageResponse,
    WorkspacePatchRequest,
)
from backend.jobs import active_runs
from backend.jobs.collection_run import (
    create_collection_run,
    execute_collection_run,
)
from backend.jobs.run_repository import COLLECTION_RUN, PUBLISH_RUN, job_of_run, new_run_id
from backend.jobs.run_resolution import RUN_NOT_FOUND_MESSAGE, apply_resolution
from backend.jobs.job_service import (
    JobConfigPayload as JobConfigPayloadData,
    add_revision,
    create_job,
    get_job_detail,
    get_revision_detail,
    list_jobs,
    list_jobs_global_page,
    list_revisions_endpoint,
    patch_job,
)
from backend.jobs.run_detail import (
    get_collection_run_detail,
    get_publish_run_detail,
    list_collection_job_runs,
    list_publish_job_runs,
)
from backend.jobs.listing import (
    list_workspace_collection_runs,
    list_workspace_publish_runs,
)
from backend.persistence.platform_db import PlatformSettings, platform_session
from backend.profile_loading.dim_value_config_api import (
    list_dim_value_objects,
    read_dim_value_config,
    read_dim_value_object_columns,
    read_dim_value_recommendations,
    write_dim_value_config,
)
from backend.profile_loading.domain_config_api import (
    list_domain_tables,
    read_domain_config,
    write_domain_config,
)
from backend.profile_loading.rel_config_api import (
    read_rel_config,
    read_rel_object_columns,
    write_rel_config,
)
from backend.publishing.publish_service import (
    check_publication,
    create_publish_run,
    execute_publish_run,
    precheck_publish_job,
)
from backend.workspaces.service import (
    create_workspace,
    get_workspace_detail,
    list_workspaces,
    page_workspaces,
    patch_workspace,
)

router = APIRouter(prefix="/api", tags=["workspaces"])


# ---- 工作区 ----


@router.get(
    "/data-sources/{source_id}/workspaces", response_model=WorkspaceListResponse
)
def list_source_workspaces(
    source_id: str, settings: PlatformSettings = Depends(get_platform_settings)
):
    return list_workspaces(settings, source_id)


@router.get("/workspaces", response_model=WorkspacePageResponse)
def list_all_workspaces_route(
    source_id: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    settings: PlatformSettings = Depends(get_platform_settings),
):
    """跨源工作区列表（doc 6.2 §6.1）：服务端分页，供全局工作区管理页使用。"""
    return page_workspaces(settings, source_id=source_id, limit=limit, offset=offset)


@router.post(
    "/data-sources/{source_id}/workspaces",
    response_model=WorkspaceItem,
    status_code=201,
)
def create_source_workspace(
    source_id: str,
    payload: WorkspaceCreateRequest,
    settings: PlatformSettings = Depends(get_platform_settings),
):
    return create_workspace(
        settings, source_id, payload.workspace_name, payload.workspace_desc
    )


@router.get("/workspaces/{workspace_id}", response_model=WorkspaceItem)
def get_workspace_route(
    workspace_id: str, settings: PlatformSettings = Depends(get_platform_settings)
):
    return get_workspace_detail(settings, workspace_id)


@router.patch("/workspaces/{workspace_id}", response_model=WorkspaceItem)
def patch_workspace_route(
    workspace_id: str,
    payload: WorkspacePatchRequest,
    settings: PlatformSettings = Depends(get_platform_settings),
):
    return patch_workspace(
        settings,
        workspace_id,
        name=payload.workspace_name,
        desc=payload.workspace_desc,
        has_name="workspace_name" in payload.model_fields_set,
        has_desc="workspace_desc" in payload.model_fields_set,
    )


# ---- 作业与版本 ----


def _config_payload(payload: JobCreateRequest) -> JobConfigPayloadData:
    return JobConfigPayloadData(
        steps=payload.config.steps,
        step_config=payload.config.step_config,
        change_note=payload.config.change_note,
    )


@router.post(
    "/workspaces/{workspace_id}/collection-jobs",
    response_model=JobItem,
    status_code=201,
)
def create_collection_job_route(
    workspace_id: str,
    payload: JobCreateRequest,
    settings: PlatformSettings = Depends(get_platform_settings),
):
    return create_job(
        settings,
        "collection",
        workspace_id,
        payload.job_name,
        payload.job_desc,
        _config_payload(payload),
    )


@router.get(
    "/workspaces/{workspace_id}/collection-jobs", response_model=JobListResponse
)
def list_collection_jobs_route(
    workspace_id: str, settings: PlatformSettings = Depends(get_platform_settings)
):
    return list_jobs(settings, "collection", workspace_id)


@router.post(
    "/workspaces/{workspace_id}/publish-jobs",
    response_model=JobItem,
    status_code=201,
)
def create_publish_job_route(
    workspace_id: str,
    payload: JobCreateRequest,
    settings: PlatformSettings = Depends(get_platform_settings),
):
    return create_job(
        settings,
        "publish",
        workspace_id,
        payload.job_name,
        payload.job_desc,
        _config_payload(payload),
    )


@router.get("/workspaces/{workspace_id}/publish-jobs", response_model=JobListResponse)
def list_publish_jobs_route(
    workspace_id: str, settings: PlatformSettings = Depends(get_platform_settings)
):
    return list_jobs(settings, "publish", workspace_id)


@router.get("/collection-jobs", response_model=GlobalJobListResponse)
def list_collection_jobs_global_route(
    workspace_id: str | None = Query(default=None),
    source_id: str | None = Query(default=None),
    q: str | None = Query(default=None),
    limit: int = Query(default=10, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    settings: PlatformSettings = Depends(get_platform_settings),
):
    """全局采集作业分页列表（设计稿 35；跨工作区，可选源/工作区/关键字过滤）。"""
    return list_jobs_global_page(
        settings,
        "collection",
        workspace_id=workspace_id,
        source_id=source_id,
        q=q,
        limit=limit,
        offset=offset,
    )


@router.get("/publish-jobs", response_model=GlobalJobListResponse)
def list_publish_jobs_global_route(
    workspace_id: str | None = Query(default=None),
    source_id: str | None = Query(default=None),
    q: str | None = Query(default=None),
    limit: int = Query(default=10, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    settings: PlatformSettings = Depends(get_platform_settings),
):
    """全局发布作业分页列表（设计稿 37；跨工作区，可选源/工作区/关键字过滤）。"""
    return list_jobs_global_page(
        settings,
        "publish",
        workspace_id=workspace_id,
        source_id=source_id,
        q=q,
        limit=limit,
        offset=offset,
    )


@router.get("/collection-jobs/{job_id}", response_model=JobItem)
def get_collection_job_route(
    job_id: str, settings: PlatformSettings = Depends(get_platform_settings)
):
    return get_job_detail(settings, "collection", job_id)


@router.patch("/collection-jobs/{job_id}", response_model=JobItem)
def patch_collection_job_route(
    job_id: str,
    payload: JobPatchRequest,
    settings: PlatformSettings = Depends(get_platform_settings),
):
    return patch_job(
        settings,
        "collection",
        job_id,
        name=payload.job_name,
        desc=payload.job_desc,
        has_name="job_name" in payload.model_fields_set,
        has_desc="job_desc" in payload.model_fields_set,
    )


@router.get("/publish-jobs/{job_id}", response_model=JobItem)
def get_publish_job_route(
    job_id: str, settings: PlatformSettings = Depends(get_platform_settings)
):
    return get_job_detail(settings, "publish", job_id)


@router.patch("/publish-jobs/{job_id}", response_model=JobItem)
def patch_publish_job_route(
    job_id: str,
    payload: JobPatchRequest,
    settings: PlatformSettings = Depends(get_platform_settings),
):
    return patch_job(
        settings,
        "publish",
        job_id,
        name=payload.job_name,
        desc=payload.job_desc,
        has_name="job_name" in payload.model_fields_set,
        has_desc="job_desc" in payload.model_fields_set,
    )


@router.post(
    "/collection-jobs/{job_id}/revisions", response_model=RevisionItem, status_code=201
)
def add_collection_revision_route(
    job_id: str,
    payload: JobCreateRequest,
    settings: PlatformSettings = Depends(get_platform_settings),
):
    return add_revision(settings, "collection", job_id, _config_payload(payload))


@router.get("/collection-jobs/{job_id}/revisions", response_model=RevisionListResponse)
def list_collection_revisions_route(
    job_id: str, settings: PlatformSettings = Depends(get_platform_settings)
):
    return list_revisions_endpoint(settings, "collection", job_id)


@router.post(
    "/publish-jobs/{job_id}/revisions", response_model=RevisionItem, status_code=201
)
def add_publish_revision_route(
    job_id: str,
    payload: JobCreateRequest,
    settings: PlatformSettings = Depends(get_platform_settings),
):
    return add_revision(settings, "publish", job_id, _config_payload(payload))


@router.get("/publish-jobs/{job_id}/revisions", response_model=RevisionListResponse)
def list_publish_revisions_route(
    job_id: str, settings: PlatformSettings = Depends(get_platform_settings)
):
    return list_revisions_endpoint(settings, "publish", job_id)


@router.get(
    "/collection-jobs/{job_id}/revisions/{revision_no}", response_model=RevisionItem
)
def get_collection_revision_route(
    job_id: str,
    revision_no: int,
    settings: PlatformSettings = Depends(get_platform_settings),
):
    return get_revision_detail(settings, "collection", job_id, revision_no)


@router.get(
    "/publish-jobs/{job_id}/revisions/{revision_no}", response_model=RevisionItem
)
def get_publish_revision_route(
    job_id: str,
    revision_no: int,
    settings: PlatformSettings = Depends(get_platform_settings),
):
    return get_revision_detail(settings, "publish", job_id, revision_no)


# ---- 运行 ----


@router.post("/collection-jobs/{job_id}/runs", status_code=202)
async def create_collection_run_route(
    job_id: str,
    payload: RunCreateRequest,
    background: BackgroundTasks,
    settings: PlatformSettings = Depends(get_platform_settings),
):
    return await _create_and_dispatch_run(
        "collection",
        create_collection_run,
        execute_collection_run,
        settings,
        job_id,
        payload,
        background,
    )


@router.post("/publish-jobs/{job_id}/runs", status_code=202)
async def create_publish_run_route(
    job_id: str,
    payload: RunCreateRequest,
    background: BackgroundTasks,
    settings: PlatformSettings = Depends(get_platform_settings),
):
    return await _create_and_dispatch_run(
        "publish",
        create_publish_run,
        execute_publish_run,
        settings,
        job_id,
        payload,
        background,
    )


async def _create_and_dispatch_run(
    kind: str,
    create_fn,
    execute_fn,
    settings: PlatformSettings,
    job_id: str,
    payload: RunCreateRequest,
    background: BackgroundTasks,
):
    """doc 11 §4.2 规则 1：提交前注册。

    先生成 run_id 并注册本进程执行者，再创建运行行（创建函数接受外部
    run_id），最后交付后台任务；创建/交付失败注销并向上抛错。不变式：
    可能被本进程执行的运行，在其运行行对他人可见（提交）前已注册。
    """
    run_id = new_run_id()
    active_runs.register(kind, run_id)
    try:
        result = await asyncio.to_thread(
            create_fn,
            settings,
            job_id,
            run_id=run_id,
            revision_no=payload.revision_no,
            mode=payload.mode,
            steps=payload.steps,
            step_config=payload.step_config,
        )
        if kind == "collection":
            # 采集执行器需要本次选择的步骤清单（doc 6.2 §6.3）
            task_args = (settings, run_id, result["steps"])
        else:
            task_args = (settings, run_id)
        background.add_task(_run_in_thread, execute_fn, kind, *task_args)
    except BaseException:
        active_runs.unregister(kind, run_id)
        raise
    return result


@router.post(
    "/publish-jobs/{job_id}/precheck", response_model=PublishJobPrecheckResponse
)
async def precheck_publish_job_route(
    job_id: str,
    payload: PublishJobPrecheckRequest,
    settings: PlatformSettings = Depends(get_platform_settings),
):
    """作业级预检（doc 7.2 §5.2）：有效参数校验，不创建运行。"""
    return await asyncio.to_thread(
        precheck_publish_job,
        settings,
        job_id,
        revision_no=payload.revision_no,
        mode=payload.mode,
        steps=payload.steps,
        step_config=payload.step_config,
    )


def _run_in_thread(func, kind, settings, run_id, *args):
    """BackgroundTasks 对同步函数自动使用线程池（doc 6.2 §6.3：同进程线程执行）。

    doc 11 §4.2 规则 2：执行结束（含异常退出）注销本进程执行者注册。
    """
    try:
        func(settings, run_id, *args)
    finally:
        active_runs.unregister(kind, run_id)


@router.get("/collection-runs/{collection_run_id}", response_model=CollectionRunDetail)
def get_collection_run_route(
    collection_run_id: UUID,
    settings: PlatformSettings = Depends(get_platform_settings),
):
    return get_collection_run_detail(settings, collection_run_id)


@router.get("/publish-runs/{publish_run_id}", response_model=PublishRunDetail)
def get_publish_run_route(
    publish_run_id: UUID,
    settings: PlatformSettings = Depends(get_platform_settings),
):
    return get_publish_run_detail(settings, publish_run_id)


@router.post(
    "/collection-runs/{collection_run_id}/resolve",
    response_model=RunResolveResponse,
)
def resolve_collection_run_route(
    collection_run_id: UUID,
    payload: RunResolveRequest,
    settings: PlatformSettings = Depends(get_platform_settings),
):
    """doc 11 §4.3：僵尸运行 Web 收尾（采集）。"""
    return _resolve_run("collection", COLLECTION_RUN, collection_run_id, payload, settings)


@router.post(
    "/publish-runs/{publish_run_id}/resolve", response_model=RunResolveResponse
)
def resolve_publish_run_route(
    publish_run_id: UUID,
    payload: RunResolveRequest,
    settings: PlatformSettings = Depends(get_platform_settings),
):
    """doc 11 §4.3：僵尸运行 Web 收尾（发布）。"""
    return _resolve_run("publish", PUBLISH_RUN, publish_run_id, payload, settings)


def _resolve_run(
    kind: str,
    spec,
    run_id: UUID,
    payload: RunResolveRequest,
    settings: PlatformSettings,
) -> dict:
    """收尾编排：注册表拦截（§4.2 规则 3）→ 参数校验 → 共享收尾模块。

    与 CLI 完全同一份收尾逻辑（run_resolution.apply_resolution），仅 note
    前缀 origin=「Web 终止」；响应字段按 doc 11 §4.3 映射（workspace_id
    取自运行所属作业）。
    """
    if active_runs.is_active(kind, run_id):
        raise RunResolveError(
            409,
            "run_resolve_active",
            "该运行正在本进程执行中，不能收尾；若确认执行已中止，请重启后端后重试",
        )
    reason = (payload.reason or "").strip()
    if not reason:
        raise RunResolveError(400, "invalid_request", "处理原因不能为空")
    if payload.confirm_executors_stopped is not True:
        raise RunResolveError(400, "invalid_request", "需确认执行该运行的进程已停止")

    with platform_session(settings) as session:
        result = apply_resolution(session, spec, run_id, reason, origin="Web 终止")
        if "error" in result:
            if result["error"] == RUN_NOT_FOUND_MESSAGE:
                raise RunResolveError(404, "not_found", result["error"])
            raise RunResolveError(400, "invalid_request", result["error"])
        if not result.get("changed"):
            return {
                "changed": False,
                "message": result.get("message", ""),
                "run_id": str(run_id),
            }
        _info, job = job_of_run(spec, session, run_id)
        return {
            "changed": True,
            "final_status": result["final_status"],
            "steps": result["steps"],
            "note": result["note"],
            "run_id": result["run_id"],
            "job_id": result["job_id"],
            "workspace_id": job.workspace_id if job else None,
        }


@router.get("/collection-jobs/{job_id}/runs", response_model=RunListResponse)
def list_collection_job_runs_route(
    job_id: str,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    settings: PlatformSettings = Depends(get_platform_settings),
):
    return list_collection_job_runs(settings, job_id, limit=limit, offset=offset)


@router.get("/publish-jobs/{job_id}/runs", response_model=RunListResponse)
def list_publish_job_runs_route(
    job_id: str,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    settings: PlatformSettings = Depends(get_platform_settings),
):
    return list_publish_job_runs(settings, job_id, limit=limit, offset=offset)


@router.get(
    "/workspaces/{workspace_id}/collection-runs", response_model=RunListResponse
)
def list_workspace_collection_runs_route(
    workspace_id: str,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    settings: PlatformSettings = Depends(get_platform_settings),
):
    return list_workspace_collection_runs(
        settings, workspace_id, limit=limit, offset=offset
    )


@router.get("/workspaces/{workspace_id}/publish-runs", response_model=RunListResponse)
def list_workspace_publish_runs_route(
    workspace_id: str,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    settings: PlatformSettings = Depends(get_platform_settings),
):
    return list_workspace_publish_runs(
        settings, workspace_id, limit=limit, offset=offset
    )


# ---- 发布预检（固定全部 8 步骤）与配置编辑 ----


@router.get(
    "/workspaces/{workspace_id}/publish-check", response_model=PublishCheckResponse
)
def publish_check_route(
    workspace_id: str, settings: PlatformSettings = Depends(get_platform_settings)
):
    return check_publication(settings, workspace_id)


@router.get(
    "/workspaces/{workspace_id}/domain-config", response_model=DomainConfigResponse
)
def get_domain_config_route(
    workspace_id: str, settings: PlatformSettings = Depends(get_platform_settings)
):
    return read_domain_config(settings, workspace_id)


@router.get(
    "/workspaces/{workspace_id}/domain-config/tables",
    response_model=DomainConfigTablesResponse,
)
def get_domain_tables_route(
    workspace_id: str, settings: PlatformSettings = Depends(get_platform_settings)
):
    return list_domain_tables(settings, workspace_id)


@router.put(
    "/workspaces/{workspace_id}/domain-config", response_model=DomainConfigResponse
)
def put_domain_config_route(
    workspace_id: str,
    payload: DomainConfigUpdateRequest,
    settings: PlatformSettings = Depends(get_platform_settings),
):
    return write_domain_config(settings, workspace_id, payload.model_dump())


@router.get(
    "/workspaces/{workspace_id}/rel-config", response_model=RelConfigGetResponse
)
def get_rel_config_route(
    workspace_id: str, settings: PlatformSettings = Depends(get_platform_settings)
):
    return read_rel_config(settings, workspace_id)


@router.get(
    "/workspaces/{workspace_id}/rel-config/object-columns",
    response_model=RelConfigColumnsResponse,
)
def get_rel_object_columns_route(
    workspace_id: str,
    schema_name: str,
    object_name: str,
    settings: PlatformSettings = Depends(get_platform_settings),
):
    return read_rel_object_columns(settings, workspace_id, schema_name, object_name)


@router.put(
    "/workspaces/{workspace_id}/rel-config", response_model=RelConfigUpdateResponse
)
def put_rel_config_route(
    workspace_id: str,
    payload: RelConfigUpdateRequest,
    settings: PlatformSettings = Depends(get_platform_settings),
):
    return write_rel_config(settings, workspace_id, payload.model_dump())


@router.get(
    "/workspaces/{workspace_id}/dim-config", response_model=DimValueConfigGetResponse
)
def get_dim_value_config_route(
    workspace_id: str, settings: PlatformSettings = Depends(get_platform_settings)
):
    return read_dim_value_config(settings, workspace_id)


@router.get(
    "/workspaces/{workspace_id}/dim-config/objects",
    response_model=DimValueObjectsResponse,
)
def get_dim_value_objects_route(
    workspace_id: str, settings: PlatformSettings = Depends(get_platform_settings)
):
    return list_dim_value_objects(settings, workspace_id)


@router.get(
    "/workspaces/{workspace_id}/dim-config/recommendations",
    response_model=DimValueRecommendationsResponse,
)
def get_dim_value_recommendations_route(
    workspace_id: str, settings: PlatformSettings = Depends(get_platform_settings)
):
    """独立推荐读取（doc 7.2 §4）：与被编辑 YAML 解耦。"""
    return read_dim_value_recommendations(settings, workspace_id)


@router.get(
    "/workspaces/{workspace_id}/dim-config/object-columns",
    response_model=DimValueColumnsResponse,
)
def get_dim_value_object_columns_route(
    workspace_id: str,
    schema_name: str,
    object_name: str,
    settings: PlatformSettings = Depends(get_platform_settings),
):
    return read_dim_value_object_columns(
        settings, workspace_id, schema_name, object_name
    )


@router.put(
    "/workspaces/{workspace_id}/dim-config",
    response_model=DimValueConfigUpdateResponse,
)
def put_dim_value_config_route(
    workspace_id: str,
    payload: DimValueConfigUpdateRequest,
    settings: PlatformSettings = Depends(get_platform_settings),
):
    return write_dim_value_config(settings, workspace_id, payload.model_dump())


# ---- 删除与知识清理（doc 8.1 §7 API） ----


@router.get("/workspaces/{workspace_id}/deletion-preview")
def workspace_deletion_preview(
    workspace_id: str, settings: PlatformSettings = Depends(get_platform_settings)
):
    from backend.deletion.service import preview_workspace_deletion

    return preview_workspace_deletion(settings, workspace_id)


@router.delete(
    "/workspaces/{workspace_id}", status_code=202, response_model=DeletionTaskItem
)
def delete_workspace_route(
    workspace_id: str,
    background: BackgroundTasks,
    payload: DeletionConfirmRequest,
    settings: PlatformSettings = Depends(get_platform_settings),
):
    """提交工作区级联删除任务（doc 8.1 §5.2：目录 + 下属作业逻辑删除）。"""
    from backend.deletion.errors import DeletionInvalidRequestError
    from backend.deletion.service import execute_task, submit_workspace_deletion

    if payload.confirm_id != workspace_id:
        raise DeletionInvalidRequestError("确认 ID 与目标工作区不一致")
    task = submit_workspace_deletion(settings, workspace_id)
    background.add_task(execute_task, settings, task["task_id"])
    return task


@router.delete("/collection-jobs/{job_id}", status_code=204)
def delete_collection_job_route(
    job_id: str,
    settings: PlatformSettings = Depends(get_platform_settings),
):
    """采集作业逻辑删除（doc 8.1 §5.2）：单事务写 deleted_at。"""
    from backend.jobs.job_service import delete_job

    delete_job(settings, "collection", job_id)
    return Response(status_code=204)


@router.delete("/publish-jobs/{job_id}", status_code=204)
def delete_publish_job_route(
    job_id: str,
    settings: PlatformSettings = Depends(get_platform_settings),
):
    """发布作业逻辑删除（doc 8.1 §5.2）：单事务写 deleted_at。"""
    from backend.jobs.job_service import delete_job

    delete_job(settings, "publish", job_id)
    return Response(status_code=204)


@router.get("/deletion-tasks/{task_id}", response_model=DeletionTaskDetail)
def get_deletion_task(
    task_id: str, settings: PlatformSettings = Depends(get_platform_settings)
):
    from backend.deletion.service import task_detail

    return task_detail(settings, task_id)


@router.post(
    "/deletion-tasks/{task_id}/retry", response_model=DeletionTaskItem, status_code=202
)
def retry_deletion_task(
    task_id: str,
    background: BackgroundTasks,
    payload: DeletionRetryRequest | None = None,
    settings: PlatformSettings = Depends(get_platform_settings),
):
    """重试失败/中断任务（doc 8.1 §7.2.4：复查准入后同一任务重新入队）。"""
    from backend.deletion.service import execute_task, retry_task

    confirmation = (
        payload.target_confirmation.model_dump()
        if payload is not None and payload.target_confirmation is not None
        else None
    )
    task = retry_task(settings, task_id, target_confirmation=confirmation)
    background.add_task(execute_task, settings, task["task_id"])
    return task
