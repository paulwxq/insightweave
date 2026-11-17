"""对象列表、级联候选、详情与 DDL 只读接口（doc 19.2 §8.1）。"""

from fastapi import APIRouter, Depends, Query

from backend.api.object_schemas import (
    ObjectDdlResponse,
    ObjectDetailResponse,
    ObjectOptionsResponse,
    ObjectsResponse,
)
from backend.api.routers.data_sources import get_platform_settings
from backend.assets.object_catalog import list_object_options, list_objects
from backend.assets.object_detail import get_object_ddl, get_object_detail
from backend.persistence.platform_db import PlatformSettings

router = APIRouter(prefix="/api/data-assets", tags=["asset-objects"])


@router.get("/{source_id}/objects", response_model=ObjectsResponse)
def list_source_objects(
    source_id: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=100),
    object_type: str | None = None,
    schema_name: str | None = None,
    keyword: str | None = None,
    settings: PlatformSettings = Depends(get_platform_settings),
) -> dict:
    return list_objects(
        settings,
        source_id,
        page=page,
        page_size=page_size,
        object_type=object_type or None,
        schema_name=schema_name or None,
        keyword=keyword or None,
    )


@router.get("/{source_id}/object-options", response_model=ObjectOptionsResponse)
def list_source_object_options(
    source_id: str,
    object_type: str | None = None,
    schema_name: str | None = None,
    keyword: str | None = None,
    settings: PlatformSettings = Depends(get_platform_settings),
) -> dict:
    return list_object_options(
        settings,
        source_id,
        object_type=object_type or None,
        schema_name=schema_name or None,
        keyword=keyword or None,
    )


@router.get("/{source_id}/object-detail", response_model=ObjectDetailResponse)
def get_source_object_detail(
    source_id: str,
    schema_name: str = Query(min_length=1),
    object_name: str = Query(min_length=1),
    settings: PlatformSettings = Depends(get_platform_settings),
) -> dict:
    return get_object_detail(settings, source_id, schema_name, object_name)


@router.get("/{source_id}/object-ddl", response_model=ObjectDdlResponse)
def get_source_object_ddl(
    source_id: str,
    schema_name: str = Query(min_length=1),
    object_name: str = Query(min_length=1),
    settings: PlatformSettings = Depends(get_platform_settings),
) -> dict:
    return get_object_ddl(settings, source_id, schema_name, object_name)
