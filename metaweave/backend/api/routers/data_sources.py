"""数据源登记路由；不直接连接数据库或执行 SQL。"""

from fastapi import APIRouter, BackgroundTasks, Depends, Request

from backend.api.schemas import (
    ConnectionCheckRequest,
    ConnectionCheckResponse,
    DataSourceActivationRequest,
    DataSourceCreateRequest,
    DataSourceListItem,
    DataSourceListResponse,
    DataSourceUpdateRequest,
    DeletionConfirmRequest,
    DeletionTaskItem,
)
from backend.data_sources.connection_check import (
    SourceConnectionInput,
    check_connection,
)
from backend.data_sources.registration_service import (
    CreateSourceInput,
    UpdateSourceInput,
    check_saved_connection,
    create_data_source,
    delete_registered_source,
    get_registered_source,
    list_registered_sources,
    set_source_activation,
    update_registered_source,
)
from backend.persistence.platform_db import PlatformSettings

router = APIRouter(prefix="/api/data-sources", tags=["data-sources"])


def get_platform_settings(request: Request) -> PlatformSettings:
    return request.app.state.platform_settings


def _connection_input(
    payload: ConnectionCheckRequest | DataSourceCreateRequest,
) -> SourceConnectionInput:
    return SourceConnectionInput(
        db_type=payload.db_type,
        host=payload.host,
        port=payload.port,
        db_name=payload.db_name,
        username=payload.username,
        password=payload.password,
        sslmode=payload.sslmode,
    )


@router.post("/connection-check", response_model=ConnectionCheckResponse)
def connection_check(payload: ConnectionCheckRequest):
    return check_connection(_connection_input(payload))


@router.post("", response_model=DataSourceListItem, status_code=201)
def create_source(
    payload: DataSourceCreateRequest,
    settings: PlatformSettings = Depends(get_platform_settings),
):
    fields = CreateSourceInput(
        connection=_connection_input(payload),
        display_name=payload.display_name,
        selected_schemas=tuple(payload.selected_schemas),
        description=payload.description,
        confirm_duplicate=payload.confirm_duplicate,
    )
    return create_data_source(settings, fields)


@router.get("", response_model=DataSourceListResponse)
def list_sources(settings: PlatformSettings = Depends(get_platform_settings)):
    return {"items": list_registered_sources(settings)}


@router.get("/{source_id}", response_model=DataSourceListItem)
def get_source(
    source_id: str, settings: PlatformSettings = Depends(get_platform_settings)
):
    return get_registered_source(settings, source_id)


@router.post("/{source_id}/connection-check", response_model=ConnectionCheckResponse)
def check_saved_source_connection(
    source_id: str, settings: PlatformSettings = Depends(get_platform_settings)
):
    return check_saved_connection(settings, source_id)


@router.patch("/{source_id}/activation", response_model=DataSourceListItem)
def set_activation(
    source_id: str,
    payload: DataSourceActivationRequest,
    settings: PlatformSettings = Depends(get_platform_settings),
):
    """启停数据源；只改启用状态，不触碰连接信息（doc 11 §5）。"""
    return set_source_activation(settings, source_id, payload.is_active)


@router.patch("/{source_id}", response_model=DataSourceListItem)
def update_source(
    source_id: str,
    payload: DataSourceUpdateRequest,
    settings: PlatformSettings = Depends(get_platform_settings),
):
    fields = UpdateSourceInput(
        db_type=payload.db_type,
        host=payload.host,
        port=payload.port,
        db_name=payload.db_name,
        username=payload.username,
        sslmode=payload.sslmode,
        password=payload.password,
        display_name=payload.display_name,
        selected_schemas=tuple(payload.selected_schemas),
        description=payload.description,
        confirm_duplicate=payload.confirm_duplicate,
    )
    return update_registered_source(settings, source_id, fields)


@router.get("/{source_id}/deletion-preview")
def source_deletion_preview(
    source_id: str, settings: PlatformSettings = Depends(get_platform_settings)
):
    """源删除范围、阻断运行与目标存储（doc 8.1 §7）。"""
    from backend.deletion.service import preview_source_deletion

    return preview_source_deletion(settings, source_id)


@router.delete("/{source_id}", status_code=202, response_model=DeletionTaskItem)
def delete_source(
    source_id: str,
    request: Request,
    background: BackgroundTasks,
    payload: DeletionConfirmRequest,
    settings: PlatformSettings = Depends(get_platform_settings),
):
    """提交数据源删除任务（doc 8.1 §2.4：异步逻辑删除，替换旧 204 契约）。"""
    from backend.deletion.errors import DeletionInvalidRequestError
    from backend.deletion.service import execute_task

    # 废止的 delete_output 查询参数显式拒绝，不由 FastAPI 默默忽略
    if "delete_output" in request.query_params:
        raise DeletionInvalidRequestError("delete_output 参数已废止")
    if payload.confirm_id != source_id:
        raise DeletionInvalidRequestError("确认 ID 与目标数据源不一致")
    task = delete_registered_source(settings, source_id)
    background.add_task(execute_task, settings, task["task_id"])
    return task


@router.post(
    "/{source_id}/published-data/clear",
    status_code=202,
    response_model=DeletionTaskItem,
)
def clear_published_data(
    source_id: str,
    background: BackgroundTasks,
    payload: DeletionConfirmRequest,
    settings: PlatformSettings = Depends(get_platform_settings),
):
    """源级知识清理（doc 8.1 §3.2）：五张内容表 + 三类向量 + 该源图。"""
    from backend.deletion.errors import DeletionInvalidRequestError
    from backend.deletion.service import execute_task, submit_knowledge_clear

    if payload.confirm_id != source_id:
        raise DeletionInvalidRequestError("确认 ID 与目标数据源不一致")
    task = submit_knowledge_clear(settings, source_id)
    background.add_task(execute_task, settings, task["task_id"])
    return task
