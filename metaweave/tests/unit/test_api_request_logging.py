"""doc 15 统一日志：外层 ASGI 请求日志包装器的测试。

包装器直接以纯 ASGI / TestClient 方式驱动，不重配 pytest 进程的全局
logging；涉及 Uvicorn 协议层行为的验收（stderr traceback、--log-level）
在真实 Uvicorn 子进程中执行（doc 15 §8.2）。
"""

import asyncio
import logging
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# 测试辅助
# ---------------------------------------------------------------------------


class _RecordSink(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@pytest.fixture
def access_records():
    sink = _RecordSink()
    access_logger = logging.getLogger("platform.http.access")
    old_propagate = access_logger.propagate
    old_level = access_logger.level
    access_logger.addHandler(sink)
    access_logger.setLevel(logging.DEBUG)
    access_logger.propagate = False
    try:
        yield sink
    finally:
        access_logger.removeHandler(sink)
        access_logger.propagate = old_propagate
        access_logger.setLevel(old_level)


def _registry():
    from services.logging_manager import ModuleRegistration, ModuleRegistry

    return ModuleRegistry(
        (
            ModuleRegistration(
                module_id="platform", backend_file=Path("/unused/platform.log")
            ),
            ModuleRegistration(
                module_id="metaweave",
                backend_file=Path("/unused/metaweave.log"),
                api_route_prefixes=("/api/data-sources", "/api/data-assets"),
                logger_prefixes=("metaweave",),
            ),
        )
    )


def _make_app(inner):
    from backend.api.request_logging import RequestLoggingApp

    return RequestLoggingApp(inner, _registry())


def _scope(path="/x", method="GET", root_path=""):
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("ascii"),
        "root_path": root_path,
        "query_string": b"",
        "headers": [(b"host", b"testserver")],
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
    }


class _SendCollector:
    def __init__(self):
        self.messages = []

    async def __call__(self, message):
        self.messages.append(message)


async def _receive_nothing():
    return {"type": "http.request", "body": b"", "more_body": False}


def _invoke(app, scope, send=None):
    """同步驱动一次 ASGI 调用，返回 (发往服务器的消息, 抛出的异常)。"""
    sent = send if send is not None else _SendCollector()

    async def run():
        await app(scope, _receive_nothing, sent)

    try:
        asyncio.run(run())
        error = None
    except BaseException as exc:  # noqa: BLE001 - 测试需要收集所有异常
        error = exc
    return getattr(sent, "messages", []), error


class _FailingSend:
    """第 fail_on 次调用抛连接断开，模拟服务器向客户端发送失败。"""

    def __init__(self, fail_on: int):
        self.messages = []
        self.calls = 0
        self.fail_on = fail_on

    async def __call__(self, message):
        self.calls += 1
        if self.calls >= self.fail_on:
            raise ConnectionResetError("peer closed")
        self.messages.append(message)


def _ok_asgi_app():
    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    return app


# ---------------------------------------------------------------------------
# 摘要字段与级别（doc 15 §4）
# ---------------------------------------------------------------------------


def test_completed_success_writes_single_info_summary(access_records):
    app = _make_app(_ok_asgi_app())
    messages, error = _invoke(app, _scope(path="/api/data-sources/abc", method="POST"))
    assert error is None
    assert len(messages) == 2

    assert len(access_records.records) == 1
    record = access_records.records[0]
    assert record.levelname == "INFO"
    assert record.request_id
    assert record.app_module == "metaweave"
    assert record.method == "POST"
    # 纯 ASGI 场景没有 FastAPI 路由匹配，route 写固定标识
    assert record.route == "-"
    assert record.status_code == "200"
    assert record.outcome == "completed"
    assert isinstance(record.duration_ms, float)
    assert record.duration_ms >= 0.0
    # 毫秒保留一位小数（doc 15 §4）：全精度浮点在日志里是噪音
    assert round(record.duration_ms, 1) == record.duration_ms


def test_response_without_route_template_uses_fixed_marker(access_records):
    """未匹配路由时 route 写固定标识，不把任意用户输入当路由模板。"""
    app = _make_app(_ok_asgi_app())
    _invoke(app, _scope(path="/totally/unknown;drop?secret=1"))
    record = access_records.records[0]
    assert record.route == "-"
    assert record.app_module == "platform"
    assert "secret" not in record.getMessage()


def test_no_response_records_error_with_dash_status(access_records):
    async def app(scope, receive, send):
        return  # 未发 http.response.start 就结束

    wrapped = _make_app(app)
    messages, error = _invoke(wrapped, _scope())
    assert error is None
    assert messages == []
    record = access_records.records[0]
    assert record.levelname == "ERROR"
    assert record.status_code == "-"
    assert record.outcome == "no_response"


def test_incomplete_response_records_error_with_sent_status(access_records):
    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        # 响应头已发，但响应体未完整发完就结束
        return

    wrapped = _make_app(app)
    _invoke(wrapped, _scope())
    record = access_records.records[0]
    assert record.levelname == "ERROR"
    assert record.status_code == "200"
    assert record.outcome == "incomplete"


def test_cancelled_after_start_records_incomplete(access_records):
    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 202, "headers": []})
        raise asyncio.CancelledError()

    wrapped = _make_app(app)
    _, error = _invoke(wrapped, _scope())
    assert isinstance(error, asyncio.CancelledError)
    record = access_records.records[0]
    assert record.outcome == "incomplete"
    assert record.status_code == "202"
    assert record.levelname == "ERROR"


def test_pathsend_counts_as_complete_response(access_records):
    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.pathsend", "path": "/tmp/probe.bin"})

    wrapped = _make_app(app)
    _invoke(wrapped, _scope())
    record = access_records.records[0]
    assert record.outcome == "completed"
    assert record.status_code == "200"
    assert record.levelname == "INFO"


def test_complete_response_then_raise_keeps_status_and_escalates(access_records):
    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"done"})
        raise RuntimeError("late failure")

    wrapped = _make_app(app)
    _, error = _invoke(wrapped, _scope())
    assert isinstance(error, RuntimeError)
    record = access_records.records[0]
    assert record.outcome == "completed"
    assert record.status_code == "200"
    assert record.levelname == "ERROR"


def test_more_body_true_then_final_body_counts_complete(access_records):
    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"chunk", "more_body": True})
        # 省略 more_body 时视为 false（doc 15 §4）
        await send({"type": "http.response.body", "body": b"!"})

    wrapped = _make_app(app)
    _invoke(wrapped, _scope())
    record = access_records.records[0]
    assert record.outcome == "completed"


def test_status_500_complete_records_error(access_records):
    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 503, "headers": []})
        await send({"type": "http.response.body", "body": b"unavailable"})

    wrapped = _make_app(app)
    _invoke(wrapped, _scope())
    record = access_records.records[0]
    assert record.status_code == "503"
    assert record.outcome == "completed"
    assert record.levelname == "ERROR"


# ---------------------------------------------------------------------------
# 发送失败：状态只在消息成功交给服务器后更新（doc 15 §4）
# ---------------------------------------------------------------------------


def test_send_failure_on_start_records_no_response(access_records):
    """响应头发送失败：客户端什么都没收到，不得声称已有状态码。"""
    wrapped = _make_app(_ok_asgi_app())
    failing = _FailingSend(fail_on=1)
    messages, error = _invoke(wrapped, _scope(), send=failing)
    assert isinstance(error, ConnectionResetError)
    assert messages == []

    record = access_records.records[0]
    assert record.levelname == "ERROR"
    assert record.status_code == "-"
    assert record.outcome == "no_response"


def test_send_failure_on_final_body_records_incomplete(access_records):
    """最终响应体发送失败：客户端收到截断响应，不得记成 completed。"""
    wrapped = _make_app(_ok_asgi_app())
    failing = _FailingSend(fail_on=2)
    messages, error = _invoke(wrapped, _scope(), send=failing)
    assert isinstance(error, ConnectionResetError)
    # 响应头已成功交给服务器
    assert len(messages) == 1

    record = access_records.records[0]
    assert record.levelname == "ERROR"
    assert record.status_code == "200"
    assert record.outcome == "incomplete"


# ---------------------------------------------------------------------------
# 请求上下文生命周期
# ---------------------------------------------------------------------------


def test_request_context_visible_inside_and_reset_after(access_records):
    from services.logging_manager import get_request_id, get_request_module

    seen = {}

    async def app(scope, receive, send):
        seen["rid"] = get_request_id()
        seen["module"] = get_request_module()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    wrapped = _make_app(app)
    _invoke(wrapped, _scope(path="/api/data-sources/x"))
    assert seen["rid"]
    assert seen["module"] == "metaweave"
    assert get_request_id() is None
    assert get_request_module() is None
    # 摘要记录里的请求 ID 与请求内一致
    assert access_records.records[0].request_id == seen["rid"]


def test_summary_failure_never_masks_original_error(
    access_records, monkeypatch, capsys
):
    """摘要写入失败不得顶掉在途异常，且请求上下文必须复位（doc 15 §4）。

    日志系统已坏时，在途的原始错误（要交给服务器记录）比摘要写失败
    重要；文件写入失败本就不属于"恰好一条"的保证范围。但失败不得
    完全静默：向 stderr 打完整堆栈（不含请求内容），否则代码缺陷
    （extra 键冲突、formatter 缺字段）表现为 api.log 长期 0 行而
    服务看似正常。
    """
    from backend.api import request_logging as rl
    from services.logging_manager import get_request_id, get_request_module

    def broken_summary(*_args):
        raise OSError("log device gone")

    monkeypatch.setattr(
        rl.RequestLoggingApp, "_write_summary", staticmethod(broken_summary)
    )

    async def failing(scope, receive, send):
        raise RuntimeError("original boom")

    # app 抛错在先：最终传播的仍是原异常，不被摘要失败顶掉
    _, error = _invoke(_make_app(failing), _scope())
    assert isinstance(error, RuntimeError)
    assert get_request_id() is None
    assert get_request_module() is None

    # app 正常完成：摘要失败也不应冒泡改写已完成的响应
    messages, error2 = _invoke(_make_app(_ok_asgi_app()), _scope())
    assert error2 is None
    assert len(messages) == 2
    assert get_request_id() is None
    assert get_request_module() is None

    # 两次摘要失败都在 stderr 留下完整堆栈（异常消息可见，可定位）。
    # 不数 Traceback 头：app 异常在途时 finally 里抛出的 OSError 会带
    # "During handling..." 链式上下文，多打印一个头；异常结尾行每次
    # 失败恰好一行
    captured = capsys.readouterr()
    assert "Traceback (most recent call last)" in captured.err
    assert captured.err.count("OSError: log device gone") == 2


def test_summary_failure_with_broken_stderr_still_silent(access_records, monkeypatch):
    """stderr 不可写时，兜底打印自身失败也不得改写请求结果（doc 15 §4）。

    daemon 关闭 stderr / 管道断开时 ``traceback.print_exc`` 写入抛
    ValueError：兜底失败不得顶掉在途异常，也不得让已完成的响应从
    包装器抛错（Uvicorn 会把实际成功的请求记成 ASGI 错误）。
    """

    class ClosedStderr:
        def write(self, *_args):
            raise ValueError("I/O operation on closed file")

        def flush(self):
            raise ValueError("I/O operation on closed file")

    def broken_summary(*_args):
        raise OSError("log device gone")

    from backend.api import request_logging as rl
    from services.logging_manager import get_request_id, get_request_module

    monkeypatch.setattr(
        rl.RequestLoggingApp, "_write_summary", staticmethod(broken_summary)
    )
    monkeypatch.setattr(sys, "stderr", ClosedStderr())

    async def failing(scope, receive, send):
        raise RuntimeError("original boom")

    # 兜底打印失败：传播的仍是原异常，不被 ValueError 顶掉
    _, error = _invoke(_make_app(failing), _scope())
    assert isinstance(error, RuntimeError)
    assert get_request_id() is None
    assert get_request_module() is None

    # 响应已发完：包装器不得再抛错
    messages, error2 = _invoke(_make_app(_ok_asgi_app()), _scope())
    assert error2 is None
    assert len(messages) == 2
    assert get_request_id() is None
    assert get_request_module() is None


def test_exception_still_resets_context(access_records):
    from services.logging_manager import get_request_id

    async def app(scope, receive, send):
        raise RuntimeError("boom")

    wrapped = _make_app(app)
    _, error = _invoke(wrapped, _scope())
    assert isinstance(error, RuntimeError)
    assert get_request_id() is None


def test_context_reset_when_exception_handler_writes_log(access_records):
    """未预期 500：ServerErrorMiddleware 处理器写日志时仍在包装器上下文内。"""
    from fastapi import FastAPI, Request
    from fastapi.testclient import TestClient

    from services.logging_manager import get_request_id, get_request_module

    seen = {}
    backend_api = logging.getLogger("backend.api")
    sink = _RecordSink()
    old_propagate = backend_api.propagate
    backend_api.addHandler(sink)
    backend_api.propagate = False
    try:
        app = FastAPI()

        @app.exception_handler(Exception)
        async def handler(_request: Request, _exc: Exception):
            # 写日志那一刻请求上下文必须仍在（字段注入由 ApiContextFilter
            # 负责，这里直接验证上下文本身）
            seen["rid"] = get_request_id()
            seen["module"] = get_request_module()
            backend_api.error("unhandled")
            return _json_500()

        @app.get("/api/data-sources/boom")
        async def boom():
            raise RuntimeError("boom")

        client = TestClient(_make_app(app), raise_server_exceptions=False)
        response = client.get("/api/data-sources/boom")
        assert response.status_code == 500
    finally:
        backend_api.removeHandler(sink)
        backend_api.propagate = old_propagate

    assert seen["rid"], "未预期 500 的处理器应在包装器上下文内执行"
    assert seen["module"] == "metaweave"
    assert len(sink.records) == 1
    # 摘要为 ERROR completed，且恰好一条
    assert len(access_records.records) == 1
    summary = access_records.records[0]
    assert summary.levelname == "ERROR"
    assert summary.outcome == "completed"
    assert summary.status_code == "500"
    assert summary.request_id == seen["rid"]
    assert get_request_id() is None


def _json_500():
    from fastapi.responses import JSONResponse

    return JSONResponse(status_code=500, content={"code": "internal_error"})


class _ContextCaptureSink(logging.Handler):
    """捕获记录 + 写日志那一刻的请求上下文（doc 15 §5 归属验证）。"""

    def __init__(self):
        super().__init__()
        self.records = []
        self.contexts = []

    def emit(self, record: logging.LogRecord) -> None:
        from services.logging_manager import get_request_id, get_request_module

        self.records.append(record)
        self.contexts.append((get_request_id(), get_request_module()))


@pytest.mark.parametrize(
    ("exc_type", "expected_status", "expected_code"),
    [
        pytest.param(
            "PlatformUnavailableError",
            503,
            "platform_unavailable",
            id="platform-unavailable-503",
        ),
        pytest.param(
            "OutputDeleteFailedError",
            500,
            "output_delete_failed",
            id="output-delete-failed-500",
        ),
        pytest.param(
            "RuntimeError",
            500,
            "internal_error",
            id="unhandled-internal-error-500",
        ),
    ],
)
def test_handled_server_error_logs_backend_diagnostic(
    access_records, exc_type, expected_status, expected_code
):
    """已处理的 503/500 也必须留下受控诊断（doc 15 §5：处理器统一记录点）。

    本测试直接在路由内抛异常，构造**无上游业务日志**的路径（对应
    registration_service、jobs 等真实链路），验证处理器兜底记录；
    有上游业务日志的路径（如发布预检）会额外多一条业务定位记录，
    与这条边界诊断内容不同、各有用途，同属允许的行为。
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from backend.api.errors import register_exception_handlers
    from backend.data_sources import registration_service
    from services.logging_manager import get_request_id

    exception_class = getattr(registration_service, exc_type, RuntimeError)
    seen = {}
    backend_api = logging.getLogger("backend.api")
    sink = _ContextCaptureSink()
    old_propagate = backend_api.propagate
    backend_api.addHandler(sink)
    backend_api.propagate = False
    try:
        app = FastAPI()
        register_exception_handlers(app)

        @app.get("/api/data-sources/{source_id}")
        async def boom(source_id: str):
            seen["rid"] = get_request_id()
            raise exception_class("probe failure")

        client = TestClient(_make_app(app), raise_server_exceptions=False)
        response = client.get("/api/data-sources/leaked-secret-id")
        assert response.status_code == expected_status, response.text
        assert response.json()["code"] == expected_code
    finally:
        backend_api.removeHandler(sink)
        backend_api.propagate = old_propagate

    # 恰好一条受控诊断：异常类型 + 路由模板，不写 str(exc)，
    # 也不把路径参数实际值（请求输入）写进日志（doc 15 §4/§5）
    assert len(sink.records) == 1, [r.getMessage() for r in sink.records]
    record = sink.records[0]
    assert record.levelname == "ERROR"
    assert record.name == "backend.api"
    message = record.getMessage()
    assert f"type={exc_type}" in message
    assert "route=/api/data-sources/{source_id}" in message
    assert "leaked-secret-id" not in message
    # 写日志那一刻仍在包装器上下文内：与摘要同请求 ID、归 metaweave
    assert sink.contexts[0][0] == seen["rid"]
    assert sink.contexts[0][1] == "metaweave"
    # api.log 摘要同步为 ERROR
    assert len(access_records.records) == 1
    summary = access_records.records[0]
    assert summary.levelname == "ERROR"
    assert summary.status_code == str(expected_status)
    assert summary.request_id == seen["rid"]


def test_named_exception_records_info_with_route_template(access_records):
    from fastapi import FastAPI, HTTPException
    from fastapi.testclient import TestClient

    app = FastAPI()

    @app.get("/api/data-sources/{source_id}")
    async def detail(source_id: str):
        raise HTTPException(status_code=409, detail="conflict")

    client = TestClient(_make_app(app))
    response = client.get("/api/data-sources/abc")
    assert response.status_code == 409

    assert len(access_records.records) == 1
    record = access_records.records[0]
    assert record.levelname == "INFO"
    assert record.route == "/api/data-sources/{source_id}"
    assert record.app_module == "metaweave"
    assert record.status_code == "409"


def test_not_found_records_info_platform(access_records):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    client = TestClient(_make_app(app))
    assert client.get("/nope").status_code == 404

    record = access_records.records[0]
    assert record.levelname == "INFO"
    assert record.app_module == "platform"
    assert record.status_code == "404"
    assert record.outcome == "completed"


# ---------------------------------------------------------------------------
# root_path 归属判定（doc 15 §8.8）
# ---------------------------------------------------------------------------


def test_root_path_prefix_stripped_for_module_resolution(access_records):
    app = _make_app(_ok_asgi_app())
    _invoke(
        app,
        _scope(path="/prefix/api/data-sources/x", root_path="/prefix"),
    )
    record = access_records.records[0]
    assert record.app_module == "metaweave"
    # 摘要路由仍写 scope path（无路由模板时为固定标识）
    assert record.route == "-"


def test_root_path_equal_path_maps_to_platform(access_records):
    app = _make_app(_ok_asgi_app())
    _invoke(app, _scope(path="/prefix", root_path="/prefix"))
    record = access_records.records[0]
    assert record.app_module == "platform"


def test_prefix_like_path_not_stripped(access_records):
    app = _make_app(_ok_asgi_app())
    _invoke(app, _scope(path="/prefixes/x", root_path="/prefix"))
    record = access_records.records[0]
    assert record.app_module == "platform"


# ---------------------------------------------------------------------------
# 并发请求不串号
# ---------------------------------------------------------------------------


def test_concurrent_requests_keep_isolated_contexts(access_records):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from services.logging_manager import get_request_id

    app = FastAPI()

    @app.get("/api/data-sources/{source_id}")
    async def slow(source_id: str):
        await asyncio.sleep(0.05 if source_id == "a" else 0.01)
        return {"seen_rid": get_request_id(), "source_id": source_id}

    client = TestClient(_make_app(app))
    ra = client.get("/api/data-sources/a")
    rb = client.get("/api/data-sources/b")
    assert ra.json()["seen_rid"] != rb.json()["seen_rid"]

    assert len(access_records.records) == 2
    by_route_arg = {record.request_id for record in access_records.records}
    assert by_route_arg == {ra.json()["seen_rid"], rb.json()["seen_rid"]}
    for record in access_records.records:
        assert record.app_module == "metaweave"


def test_concurrent_asgi_tasks_no_cross_talk(access_records):
    """两个并发 ASGI 任务交错执行时请求字段不串号。"""
    from services.logging_manager import get_request_id, get_request_module

    seen = {}

    def make_handler(tag: str):
        async def app(scope, receive, send):
            seen[tag] = (get_request_id(), get_request_module())
            await asyncio.sleep(0.05 if tag == "slow" else 0.0)
            seen[tag] = (seen[tag][0], get_request_module())
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": tag.encode()})

        return app

    slow_app = _make_app(make_handler("slow"))
    fast_app = _make_app(make_handler("fast"))
    scope_slow = _scope(path="/api/data-sources/slow")
    scope_fast = _scope(path="/api/data-sources/fast")

    async def run():
        await asyncio.gather(
            slow_app(scope_slow, _receive_nothing, _SendCollector()),
            fast_app(scope_fast, _receive_nothing, _SendCollector()),
        )

    asyncio.run(run())
    slow_rid, slow_mod = seen["slow"]
    fast_rid, fast_mod = seen["fast"]
    assert slow_rid != fast_rid
    assert slow_mod == fast_mod == "metaweave"
    assert len(access_records.records) == 2


# ---------------------------------------------------------------------------
# 包装器形态
# ---------------------------------------------------------------------------


def test_app_property_exposes_inner_fastapi_instance():
    from fastapi import FastAPI

    inner = FastAPI()
    wrapped = _make_app(inner)
    assert wrapped.app is inner


def test_non_http_scope_passes_through_untouched():
    from services.logging_manager import ModuleRegistry

    from backend.api.request_logging import RequestLoggingApp

    called = {}

    async def inner(scope, receive, send):
        called["type"] = scope["type"]

    async def run():
        await RequestLoggingApp(inner, ModuleRegistry(()))(
            {"type": "lifespan"}, _receive_nothing, _SendCollector()
        )

    asyncio.run(run())
    assert called["type"] == "lifespan"


def test_summary_extra_fields_do_not_shadow_logrecord_attrs(access_records):
    """摘要 extra 键不得与 LogRecord 保留属性冲突（doc 15 §4）。"""
    app = _make_app(_ok_asgi_app())
    _invoke(app, _scope())
    record = access_records.records[0]
    # 标准 %(module)s 语义保留：仍是产生日志的源文件名
    assert record.module == "request_logging"
    assert record.name == "platform.http.access"


# ---------------------------------------------------------------------------
# 真实 Uvicorn 子进程：stderr traceback 与 --log-level（doc 15 §8.2）
# ---------------------------------------------------------------------------

_UVICORN_PROBE_APP = """
from pathlib import Path

from metaweave.utils import file_utils

file_utils.get_project_root = lambda: Path({base!r})

import logging

from fastapi import FastAPI, HTTPException

from services.logging_manager import configure_logging
from backend.api.request_logging import RequestLoggingApp


def create_logged():
    setup = configure_logging("api", {config!r})
    app = FastAPI()

    @app.get("/boom")
    async def boom():
        raise RuntimeError("probe-unhandled-boom")

    @app.get("/named")
    async def named():
        raise HTTPException(status_code=409, detail="conflict")

    return RequestLoggingApp(app, setup.modules)
"""


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_port_ready(port: int, proc: subprocess.Popen, timeout: float = 30.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"uvicorn 子进程提前退出: {proc.returncode}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.2)
    raise RuntimeError("uvicorn 子进程在超时内未就绪")


def _run_uvicorn_probe(base: Path, log_level: str) -> str:
    """起真实 Uvicorn 子进程（工厂形态，与部署命令一致），返回 stderr。"""
    import httpx

    from tests.unit.test_logging_profiles import _write_yaml

    config_path = _write_yaml(base)
    probe_app = base / "probe_app.py"
    probe_app.write_text(
        _UVICORN_PROBE_APP.format(base=str(base), config=str(config_path)),
        encoding="utf-8",
    )
    port = _free_port()
    runner = (
        "import sys; "
        f"sys.path.insert(0, {str(base)!r}); "
        "import uvicorn; "
        "uvicorn.run('probe_app:create_logged', factory=True, "
        f"host='127.0.0.1', port={port}, access_log=False, log_level={log_level!r})"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", runner],
        cwd=PROJECT_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        _wait_port_ready(port, proc)
        boom = httpx.get(f"http://127.0.0.1:{port}/boom", trust_env=False, timeout=10)
        named = httpx.get(f"http://127.0.0.1:{port}/named", trust_env=False, timeout=10)
        assert boom.status_code == 500, boom.text
        assert named.status_code == 409, named.text
    finally:
        proc.terminate()
        _out, err = proc.communicate(timeout=15)
    return err


def test_uvicorn_stderr_traceback_once_and_log_level_semantics(tmp_path):
    base = tmp_path / "info"
    base.mkdir()
    err_info = _run_uvicorn_probe(base, "info")

    assert err_info.count("Exception in ASGI application") == 1
    assert "RuntimeError" in err_info
    assert "probe-unhandled-boom" in err_info
    assert "Started server process" in err_info

    api_log = (base / "logs" / "api.log").read_text(encoding="utf-8")
    assert api_log.count("route=/boom") == 1
    assert "status_code=500" in api_log
    assert "outcome=completed" in api_log
    assert "route=/named" in api_log
    assert "status_code=409" in api_log

    base_warn = tmp_path / "warning"
    base_warn.mkdir()
    err_warn = _run_uvicorn_probe(base_warn, "warning")
    assert "Started server process" not in err_warn
    assert err_warn.count("Exception in ASGI application") == 1
    assert "probe-unhandled-boom" in err_warn
