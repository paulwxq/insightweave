"""数据源登记路由；不直接连接数据库或执行 SQL。"""

from fastapi import APIRouter, Depends, Request, Response

from backend.api.schemas import (
    ConnectionCheckRequest,
    ConnectionCheckResponse,
    DataSourceActivationRequest,
    DataSourceCreateRequest,
    DataSourceListItem,
    DataSourceListResponse,
    DataSourceUpdateRequest,
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


@router.delete("/{source_id}", status_code=204)
def delete_source(
    source_id: str,
    delete_output: bool = False,
    settings: PlatformSettings = Depends(get_platform_settings),
):
    """删除数据源；已关联工作区时拒绝（A29，不级联删除工作区）。"""
    delete_registered_source(settings, source_id, delete_output=delete_output)
    return Response(status_code=204)
