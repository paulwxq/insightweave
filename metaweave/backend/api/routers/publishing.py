"""画像发布路由。不写 SQL，不拼接产物路径。"""

import asyncio
from uuid import UUID

from fastapi import APIRouter, Depends

from backend.api.routers.data_sources import get_platform_settings
from backend.api.schemas import (
    DimValueColumnsResponse,
    DimValueConfigGetResponse,
    DimValueConfigUpdateRequest,
    DimValueConfigUpdateResponse,
    DimValueObjectsResponse,
    PublicationCreateRequest,
    PublicationCreateResponse,
    PublishCheckResponse,
    PublishDefinitionResponse,
    PublishHomeResponse,
    PublishJobListResponse,
    DomainConfigResponse,
    DomainConfigTablesResponse,
    DomainConfigUpdateRequest,
    RelConfigColumnsResponse,
    RelConfigGetResponse,
    RelConfigUpdateRequest,
    RelConfigUpdateResponse,
)
from backend.persistence.platform_db import PlatformSettings
from backend.profile_loading.domain_config_api import (
    list_domain_tables,
    read_domain_config,
    write_domain_config,
)
from backend.profile_loading.dim_value_config_api import (
    list_dim_value_objects,
    read_dim_value_config,
    read_dim_value_object_columns,
    write_dim_value_config,
)
from backend.profile_loading.rel_config_api import (
    read_rel_config,
    read_rel_object_columns,
    write_rel_config,
)
from backend.publishing.home import get_publish_home
from backend.publishing.listing import get_publish_definition, list_publish_jobs
from backend.publishing.publish_service import check_publication, publish_steps

router = APIRouter(prefix="/api/data-sources", tags=["publishing"])


@router.get(
    "/{source_id}/publish-jobs/{publish_id}",
    response_model=PublishDefinitionResponse,
)
def get_source_publish_definition(
    source_id: UUID,
    publish_id: UUID,
    settings: PlatformSettings = Depends(get_platform_settings),
) -> dict:
    return get_publish_definition(settings, source_id, publish_id)


@router.get("/{source_id}/publish-home", response_model=PublishHomeResponse)
def get_source_publish_home(
    source_id: UUID, settings: PlatformSettings = Depends(get_platform_settings)
) -> dict:
    return get_publish_home(settings, source_id)


@router.get("/{source_id}/publish-jobs", response_model=PublishJobListResponse)
def list_source_publish_jobs(
    source_id: UUID, settings: PlatformSettings = Depends(get_platform_settings)
) -> dict:
    return list_publish_jobs(settings, source_id)


@router.get(
    "/{source_id}/collection-jobs/{job_id}/publish-check",
    response_model=PublishCheckResponse,
)
def publish_check(
    source_id: UUID,
    job_id: UUID,
    settings: PlatformSettings = Depends(get_platform_settings),
) -> dict:
    return check_publication(settings, source_id, job_id)


@router.post(
    "/{source_id}/collection-jobs/{job_id}/publications",
    response_model=PublicationCreateResponse,
)
async def create_publication(
    source_id: UUID,
    job_id: UUID,
    payload: PublicationCreateRequest,
    settings: PlatformSettings = Depends(get_platform_settings),
) -> dict:
    return await asyncio.to_thread(
        publish_steps, settings, source_id, job_id, payload.steps
    )


# ---- dim_value 配置编辑（doc 22.2 §5.1）----


@router.get(
    "/{source_id}/collection-jobs/{job_id}/dim-config",
    response_model=DimValueConfigGetResponse,
)
def get_dim_value_config(
    source_id: UUID,
    job_id: UUID,
    settings: PlatformSettings = Depends(get_platform_settings),
) -> dict:
    return read_dim_value_config(settings, source_id, job_id)


@router.get(
    "/{source_id}/collection-jobs/{job_id}/dim-config/objects",
    response_model=DimValueObjectsResponse,
)
def get_dim_value_objects(
    source_id: UUID,
    job_id: UUID,
    settings: PlatformSettings = Depends(get_platform_settings),
) -> dict:
    return list_dim_value_objects(settings, source_id, job_id)


@router.get(
    "/{source_id}/collection-jobs/{job_id}/dim-config/object-columns",
    response_model=DimValueColumnsResponse,
)
def get_dim_value_object_columns(
    source_id: UUID,
    job_id: UUID,
    schema_name: str,
    object_name: str,
    settings: PlatformSettings = Depends(get_platform_settings),
) -> dict:
    return read_dim_value_object_columns(
        settings, source_id, job_id, schema_name, object_name
    )


@router.put(
    "/{source_id}/collection-jobs/{job_id}/dim-config",
    response_model=DimValueConfigUpdateResponse,
)
def put_dim_value_config(
    source_id: UUID,
    job_id: UUID,
    payload: DimValueConfigUpdateRequest,
    settings: PlatformSettings = Depends(get_platform_settings),
) -> dict:
    return write_dim_value_config(settings, source_id, job_id, payload.model_dump())


# ---- rel 关系编辑（doc 23.2 §4）----


@router.get(
    "/{source_id}/collection-jobs/{job_id}/rel-config",
    response_model=RelConfigGetResponse,
)
def get_rel_config(
    source_id: UUID,
    job_id: UUID,
    settings: PlatformSettings = Depends(get_platform_settings),
) -> dict:
    return read_rel_config(settings, source_id, job_id)


@router.get(
    "/{source_id}/collection-jobs/{job_id}/rel-config/object-columns",
    response_model=RelConfigColumnsResponse,
)
def get_rel_object_columns(
    source_id: UUID,
    job_id: UUID,
    schema_name: str,
    object_name: str,
    settings: PlatformSettings = Depends(get_platform_settings),
) -> dict:
    return read_rel_object_columns(
        settings, source_id, job_id, schema_name, object_name
    )


@router.put(
    "/{source_id}/collection-jobs/{job_id}/rel-config",
    response_model=RelConfigUpdateResponse,
)
def put_rel_config(
    source_id: UUID,
    job_id: UUID,
    payload: RelConfigUpdateRequest,
    settings: PlatformSettings = Depends(get_platform_settings),
) -> dict:
    return write_rel_config(settings, source_id, job_id, payload.model_dump())


# ---- domain 配置编辑（doc 24.1 §5.1）----


@router.get(
    "/{source_id}/collection-jobs/{job_id}/domain-config",
    response_model=DomainConfigResponse,
)
def get_domain_config(
    source_id: UUID,
    job_id: UUID,
    settings: PlatformSettings = Depends(get_platform_settings),
) -> dict:
    return read_domain_config(settings, source_id, job_id)


@router.get(
    "/{source_id}/collection-jobs/{job_id}/domain-config/tables",
    response_model=DomainConfigTablesResponse,
)
def get_domain_tables(
    source_id: UUID,
    job_id: UUID,
    settings: PlatformSettings = Depends(get_platform_settings),
) -> dict:
    return list_domain_tables(settings, source_id, job_id)


@router.put(
    "/{source_id}/collection-jobs/{job_id}/domain-config",
    response_model=DomainConfigResponse,
)
def put_domain_config(
    source_id: UUID,
    job_id: UUID,
    payload: DomainConfigUpdateRequest,
    settings: PlatformSettings = Depends(get_platform_settings),
) -> dict:
    return write_domain_config(settings, source_id, job_id, payload.model_dump())
