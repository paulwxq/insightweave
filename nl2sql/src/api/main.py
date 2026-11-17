"""FastAPI 应用入口（doc 2 §4.2/§6.2）：仅挂载 /api/v2 Web 链路。

不再挂载旧 /api/v1 query/history 路由（旧路由文件保留供脚本用途），启动
也不再预编译旧父图。统一封装与全局异常处理经 register_exception_handlers
注册；本机 Origin/Host/媒体类型拒绝在业务前以稳定错误码返回（403/400/415）。
"""

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.datastructures import Headers

from src.api.v2 import codes
from src.api.v2.envelope import (
    TraceIdMiddleware,
    error_response,
    register_exception_handlers,
)
from src.api.v2.router import router as v2_router
from src.api.v2.runtime import get_web_runtime


def _guard_error(scope, error_code: str, message: str):
    """守卫层拒绝响应（带 trace_id；TraceId 中间件在外层先行生成）。"""
    state = scope.get("state") or {}
    return error_response(
        error_code=error_code,
        message=message,
        trace_id=state.get("trace_id") or "",
    )


class LocalOriginGuard:
    """本机 Origin 白名单（doc 2 §6.2）：非白名单 Origin 一律 403。

    放在 CORSMiddleware 外层：被拒请求拿到统一封装的 403/origin_not_allowed
    （无 CORS 头，浏览器侧仍按跨域错误呈现）；白名单 Origin 由内层
    CORSMiddleware 正常补齐响应头。无 Origin 的非浏览器请求直接放行。
    """

    def __init__(self, app, allowed_origins):
        self.app = app
        self.allowed_origins = set(allowed_origins)

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            origin = Headers(scope=scope).get("origin")
            if origin is not None and origin not in self.allowed_origins:
                response = _guard_error(
                    scope, codes.ORIGIN_NOT_ALLOWED, "来源不在允许列表"
                )
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


class HostAllowlistGuard:
    """Host 白名单（doc 2 §6.2）：仅允许本机回环域名，其余 400。"""

    def __init__(self, app, allowed_hosts):
        self.app = app
        self.allowed_hosts = set(allowed_hosts)

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            host = Headers(scope=scope).get("host", "")
            hostname = host.split(":", 1)[0].strip().lower()
            if hostname.startswith("[") and hostname.endswith("]"):
                hostname = hostname[1:-1]
            if hostname not in self.allowed_hosts:
                response = _guard_error(
                    scope, codes.HOST_NOT_ALLOWED, "Host 不在允许列表"
                )
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


class JsonMediaTypeGuard:
    """写接口媒体类型检查（doc 2 §4.2）：/api/v2 写方法仅接受 JSON，415。

    Content-Type 缺失时不在此拒绝（空体/非 JSON 由 FastAPI 解析报 422
    invalid_request）；声明了其他类型才以 415 在业务前拒绝。
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            method = scope.get("method", "")
            path = scope.get("path", "")
            if method in ("POST", "PUT", "PATCH") and path.startswith("/api/v2"):
                content_type = Headers(scope=scope).get("content-type")
                if content_type is not None:
                    media = content_type.split(";", 1)[0].strip().lower()
                    if media != "application/json":
                        response = _guard_error(
                            scope,
                            codes.UNSUPPORTED_MEDIA_TYPE,
                            "写接口仅接受 application/json",
                        )
                        await response(scope, receive, send)
                        return
        await self.app(scope, receive, send)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ---- 启动阶段：只初始化日志与运行时（Store 惰性，health 不连库）----
    from src.api.core.logging import init_api_logging
    from src.api.v2.runtime import get_web_runtime, shutdown_web_runtime

    logger = init_api_logging()
    runtime = get_web_runtime()
    logger.info(
        "NL2SQL /api/v2 Web 运行时已启动（最大并发 %d，提交限流 %d/分钟）",
        runtime.max_concurrent_runs,
        runtime.rate_limit_per_minute,
    )
    yield
    # ---- 关闭阶段：排空执行器后释放平台池（doc 2 §5.5.1）----
    shutdown_web_runtime()
    logger.info("Web 运行时已排空关闭（executor + 平台池）")


app = FastAPI(title="NL2SQL Web API", version="2.0.0", lifespan=lifespan)

register_exception_handlers(app)
app.include_router(v2_router)

_runtime = get_web_runtime()

# 中间件顺序（外 → 内）：TraceId → OriginGuard → CORS → Host → 媒体类型 → 应用
# （add_middleware 后添加者在外层，故按内 → 外的逆序添加）
app.add_middleware(JsonMediaTypeGuard)
app.add_middleware(HostAllowlistGuard, allowed_hosts=_runtime.allowed_hosts)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_runtime.cors_origins,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type"],
)
app.add_middleware(LocalOriginGuard, allowed_origins=_runtime.cors_origins)
app.add_middleware(TraceIdMiddleware)

# ---- 部署门闩接线（doc 13 §7.5、§8）----
# 仅部署开关启用时惰性导入 deploy.runtime.lifecycle（容器内 PYTHONPATH 含 /app）；
# 默认开发启动零额外路径依赖。门闩最后添加、位于最外层：活动计数覆盖整个
# ASGI 调用（含响应发送后的后台任务）；NL2SQL 的 executor future 由
# gate.submit_tracked 追踪（提交前登记、完成释放）。
if os.getenv("DEPLOY_CONTROL_ENABLED") == "1":
    from deploy.runtime.lifecycle import install_deployment_gate

    _deploy_gate = install_deployment_gate(
        app,
        control_prefix="/api/v2/_deploy",
        health_paths=("/api/v2/health",),
    )
    from src.api.v2.runtime import set_future_tracker

    set_future_tracker(_deploy_gate.submit_tracked)
