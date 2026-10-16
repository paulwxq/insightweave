"""数据资产首页与数据库画像只读接口。"""

from uuid import UUID

from fastapi import APIRouter, Depends

from backend.api.routers.data_sources import get_platform_settings
from backend.api.schemas import AssetCatalogResponse, AssetProfileResponse
from backend.assets.catalog import get_asset_profile, list_asset_catalog
from backend.persistence.platform_db import PlatformSettings

router = APIRouter(prefix="/api/data-assets", tags=["assets"])


@router.get("", response_model=AssetCatalogResponse)
def get_asset_catalog(
    settings: PlatformSettings = Depends(get_platform_settings),
) -> dict:
    return list_asset_catalog(settings)


@router.get("/{source_id}/profile", response_model=AssetProfileResponse)
def get_database_profile(
    source_id: UUID, settings: PlatformSettings = Depends(get_platform_settings)
) -> dict:
    return get_asset_profile(settings, source_id)
