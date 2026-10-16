"""外层纯 ASGI 请求日志包装器（doc 15 §4）。

由 ``backend/api/logged_app.py`` 包在整个 FastAPI/Starlette 应用之外，
因此位于 Starlette ``ServerErrorMiddleware`` 之外：未预期 500 的异常
处理器写日志时仍保有请求上下文。每个已进入包装器且执行到 ``finally``
的 HTTP 请求在 ``platform.http.access`` 上恰好写一条摘要。
"""

import logging
import sys
import time
import traceback
import uuid

from starlette.routing import get_route_path

from services.logging_manager import (
    ACCESS_LOGGER_NAME,
    PLATFORM_MODULE_ID,
    ModuleRegistry,
    reset_request_id,
    reset_request_module,
    set_request_id,
    set_request_module,
)

# 摘要 logger 名与 YAML/装载器校验共用同一常量（doc 15 §3.1 契约），
# 装载期由 _validate_access_logger 校验其"只写 api_file、propagate: false"
_access_logger = logging.getLogger(ACCESS_LOGGER_NAME)


class RequestLoggingApp:
    """纯 ASGI 日志层；通过只读 ``app`` 属性暴露内层 FastAPI 实例。"""

    def __init__(self, app, registry: ModuleRegistry):
        self._app = app
        self._registry = registry

    @property
    def app(self):
        """内层应用实例（调试用；不做 ``__getattr__`` 隐式转发）。"""
        return self._app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        request_id = uuid.uuid4().hex
        route_path = get_route_path(scope)
        module_id = (
            self._registry.resolve_request_module(route_path) or PLATFORM_MODULE_ID
        )
        started = time.monotonic()
        state = {"status_code": None, "complete": False, "raised": False}

        async def send_wrapper(message):
            # 先交给服务器再更新状态：发送失败（连接断开）时不得把
            # "已尝试发送"记成"已发出"（doc 15 §4 —— no_response/incomplete）
            await send(message)
            message_type = message["type"]
            if message_type == "http.response.start":
                state["status_code"] = message["status"]
            elif message_type == "http.response.body":
                # 省略 more_body 时视为 false（doc 15 §4）
                if not message.get("more_body", False):
                    state["complete"] = True
            elif message_type == "http.response.pathsend":
                state["complete"] = True

        request_id_token = set_request_id(request_id)
        module_token = set_request_module(module_id)
        try:
            await self._app(scope, receive, send_wrapper)
        except BaseException:
            state["raised"] = True
            raise
        finally:
            try:
                self._write_summary(scope, request_id, module_id, started, state)
            except Exception:
                # 摘要写入失败不得顶掉在途异常（doc 15 §4：文件写入失败
                # 不属于"恰好一条"的保证范围）；上下文复位必须始终执行。
                # 能抛到这里的几乎只有代码缺陷（handler I/O 错误已被
                # logging 内部 handleError 吞掉），向 stderr 打完整堆栈
                # 且不含请求内容，避免"api.log 长期 0 行但服务看似正常"
                try:
                    traceback.print_exc(file=sys.stderr)
                except Exception:
                    # 兜底输出自身失败（daemon 关闭 stderr、管道断开）
                    # 只能放弃：不得反过来顶掉在途异常，也不得让已
                    # 完成的响应从包装器抛错
                    pass
            finally:
                reset_request_module(module_token)
                reset_request_id(request_id_token)

    @staticmethod
    def _write_summary(scope, request_id, module_id, started, state) -> None:
        # 毫秒保留一位小数（doc 15 §4）：排查用数量级，全精度浮点是噪音
        duration_ms = round((time.monotonic() - started) * 1000.0, 1)
        status = state["status_code"]
        if status is None:
            outcome, status_display, level = "no_response", "-", logging.ERROR
        elif not state["complete"]:
            outcome, status_display, level = "incomplete", str(status), logging.ERROR
        else:
            outcome, status_display = "completed", str(status)
            level = (
                logging.ERROR if (state["raised"] or status >= 500) else logging.INFO
            )
        # 匹配到路由时从 scope["route"].path 读模板；预检/未匹配写固定标识，
        # 不把任意用户输入当作路由模板（doc 15 §4）
        route_obj = scope.get("route")
        route = getattr(route_obj, "path", None) or "-"
        method = scope.get("method") or "-"
        _access_logger.log(
            level,
            "%s %s",
            method,
            route,
            extra={
                "request_id": request_id,
                "app_module": module_id,
                "method": method,
                "route": route,
                "status_code": status_display,
                "outcome": outcome,
                "duration_ms": duration_ms,
            },
        )
