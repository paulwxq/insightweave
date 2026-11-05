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
from backend.api.routers.workspaces import router as workspaces_router
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
    app.include_router(workspaces_router)
    app.include_router(assets_router)
    app.include_router(asset_objects_router)

    @app.on_event("startup")
    def _deletion_startup_scan() -> None:
        """doc 8.1 §7.2：启动恢复扫描，只修复状态不自动执行清理。

        METAWEAVE_DELETION_STARTUP_SCAN=0 可关闭（测试环境隔离用）；
        扫描失败仅记日志，不阻断启动，页面仍可显式重试。
        """
        import logging
        import os

        if os.getenv("METAWEAVE_DELETION_STARTUP_SCAN", "1") != "1":
            return
        from backend.deletion.service import startup_scan

        try:
            startup_scan(settings)
        except Exception:  # noqa: BLE001 —— 扫描失败不阻断启动
            logging.getLogger("backend.api").exception("删除任务启动扫描失败")

    return app
