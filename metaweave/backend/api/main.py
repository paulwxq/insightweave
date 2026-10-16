"""平台 FastAPI 应用工厂。"""

import os
from collections.abc import Sequence

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from backend.api.cors import parse_cors_origins, validate_cors_origin
from backend.api.errors import register_exception_handlers
from backend.api.routers.assets import router as assets_router
from backend.api.routers.asset_objects import router as asset_objects_router
from backend.api.routers.data_sources import router as data_sources_router
from backend.api.routers.publishing import router as publishing_router
from backend.persistence.platform_db import (
    PlatformSettings,
    load_platform_settings_from_env,
    load_project_env,
)


def create_app(
    settings: PlatformSettings | None = None,
    cors_origins: Sequence[str] | None = None,
) -> FastAPI:
    if settings is None or cors_origins is None:
        load_project_env()
    if settings is None:
        settings = load_platform_settings_from_env()
    origins = (
        parse_cors_origins(os.getenv("PLATFORM_CORS_ORIGINS"))
        if cors_origins is None
        else tuple(validate_cors_origin(value) for value in cors_origins)
    )
    app = FastAPI(title="MetaWeave Platform API")
    app.state.platform_settings = settings
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(origins),
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        allow_headers=["Content-Type"],
    )
    register_exception_handlers(app)
    app.include_router(data_sources_router)
    app.include_router(publishing_router)
    app.include_router(assets_router)
    app.include_router(asset_objects_router)
    return app
