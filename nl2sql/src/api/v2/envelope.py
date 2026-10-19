"""统一响应封装与全局异常处理（doc 2 §4.2）。

覆盖 RequestValidationError、Starlette/FastAPI HTTPException（含 405 与未知
路由 404）、未捕获异常、WebApiError 及本机 Origin/Host/媒体类型拒绝；不能
只包装已定义业务路由。trace_id 是 HTTP 入口服务端生成的独立 UUID hex，
422/未知路由/health 等尚无业务请求标识的响应也有 trace_id。
"""

from __future__ import annotations

import uuid
from typing import Any, Optional

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from src.api.v2 import codes
from src.utils.logger import get_module_logger

logger = get_module_logger("nl2sql.api.v2.envelope")


def new_trace_id() -> str:
    return uuid.uuid4().hex


def trace_id_of(request: Request) -> str:
    """请求级 trace_id（中间件已生成；兜底再生成一个）。"""
    trace_id = getattr(request.state, "trace_id", None)
    if not trace_id:
        trace_id = new_trace_id()
        request.state.trace_id = trace_id
    return trace_id


def envelope(
    *,
    data: Any = None,
    message: str = "success",
    code: int = 200,
    trace_id: str,
) -> dict[str, Any]:
    """统一封装 {code, message, data, trace_id}；code 为 HTTP 状态数字值。"""
    return {"code": code, "message": message, "data": data, "trace_id": trace_id}


def success_response(data: Any, request: Request) -> JSONResponse:
    return JSONResponse(
        status_code=200,
        content=envelope(data=data, trace_id=trace_id_of(request)),
    )


def error_response(
    *,
    error_code: str,
    message: str,
    trace_id: str,
    data: Optional[dict[str, Any]] = None,
    status_code: Optional[int] = None,
    headers: Optional[dict[str, str]] = None,
) -> JSONResponse:
    payload_data = {"error_code": error_code, **(data or {})}
    status = status_code if status_code is not None else codes.HTTP_STATUS[error_code]
    return JSONResponse(
        status_code=status,
        content=envelope(
            code=status, message=message, data=payload_data, trace_id=trace_id
        ),
        headers=headers or None,
    )


def _safe_validation_detail(exc: RequestValidationError) -> str:
    """422 消息只含字段路径与错误类型，不回显含凭据的原始输入。"""
    parts = []
    for error in exc.errors():
        loc = ".".join(str(p) for p in error.get("loc", ()))
        parts.append(f"{loc}: {error.get('type', 'invalid')}")
    return "请求参数非法: " + "; ".join(parts) if parts else "请求参数非法"


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(codes.WebApiError)
    async def _web_api_error(request: Request, exc: codes.WebApiError):
        return error_response(
            error_code=exc.error_code,
            message=exc.message,
            trace_id=trace_id_of(request),
            data=exc.data or None,
            headers=exc.headers or None,
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError):
        return error_response(
            error_code=codes.INVALID_REQUEST,
            message=_safe_validation_detail(exc),
            trace_id=trace_id_of(request),
            status_code=422,
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_exception(request: Request, exc: StarletteHTTPException):
        headers = dict(exc.headers or {})
        if exc.status_code == 404:
            code, message = codes.ROUTE_NOT_FOUND, "接口不存在"
        elif exc.status_code == 405:
            code, message = codes.METHOD_NOT_ALLOWED, "不支持的方法"
        elif exc.status_code == 415:
            code, message = (
                codes.UNSUPPORTED_MEDIA_TYPE,
                "写接口仅接受 application/json",
            )
        elif exc.status_code >= 500:
            code, message = codes.INTERNAL_ERROR, "服务暂时无法处理请求"
        else:
            code, message = (
                codes.INVALID_REQUEST,
                f"请求被拒绝（HTTP {exc.status_code}）",
            )
        # 保留原始 HTTP 状态（405 的 Allow 等响应头原样透传）
        return error_response(
            error_code=code,
            message=message,
            trace_id=trace_id_of(request),
            status_code=exc.status_code,
            headers=headers or None,
        )

    @app.exception_handler(Exception)
    async def _uncaught(request: Request, exc: Exception):
        logger.exception(
            "未捕获异常 trace_id=%s: %s", trace_id_of(request), type(exc).__name__
        )
        return error_response(
            error_code=codes.INTERNAL_ERROR,
            message="服务暂时无法处理请求",
            trace_id=trace_id_of(request),
        )


class TraceIdMiddleware:
    """为每个请求生成独立 trace_id（与业务 request_id 分开）。"""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        scope.setdefault("state", {})
        scope["state"]["trace_id"] = new_trace_id()
        await self.app(scope, receive, send)
