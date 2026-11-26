"""RunBudgetMiddleware / FinalAnswerGateMiddleware / RequestAuditMiddleware
单元测试（docs/v4/5 §11.1–§11.3）
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from langchain.agents.middleware import ModelRequest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.types import Command
from src.modules.db_deepagent.config import load_db_agent_config
from src.modules.db_deepagent.middleware import run_budget as run_budget_module
from src.modules.db_deepagent.middleware.final_answer_gate import (
    FinalAnswerGateMiddleware,
)
from src.modules.db_deepagent.middleware.request_audit import (
    RequestAuditMiddleware,
    _with_reference_time,
)
from src.modules.db_deepagent.middleware.run_budget import RunBudgetMiddleware
from src.modules.db_deepagent.services import run_budget as run_budget_service_module
from src.modules.db_deepagent.services.run_budget import BudgetLimits, RunBudgetLedger
from src.modules.db_deepagent.tools._envelope import tool_command
from src.tests.db_deepagent.conftest import make_time_evidence

pytestmark = pytest.mark.unit

CFG = load_db_agent_config()
SEP = make_time_evidence("T-sep", "2026-09-01", "2026-10-01")

_REFERENCE_TIME_HEADER = "## 本轮北京时间背景"


def _runtime(ctx):
    return SimpleNamespace(context=ctx)


class _RecordCapture(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


class _AuditCapture:
    def __init__(self, logger_name: str, level: int):
        self._logger = logging.getLogger(logger_name)
        self._level = level

    def __enter__(self):
        self._old_level = self._logger.level
        self._handler = _RecordCapture()
        self._logger.addHandler(self._handler)
        self._logger.setLevel(self._level)
        return self._handler

    def __exit__(self, *exc_info):
        self._logger.removeHandler(self._handler)
        self._logger.setLevel(self._old_level)


def _tool_request(ctx, tool_call_id: str, *, name="validate_sql", args=None):
    return SimpleNamespace(
        tool_call={
            "name": name,
            "id": tool_call_id,
            "args": args if args is not None else {},
        },
        state={"db_run_status": {}},
        runtime=_runtime(ctx),
    )


def _messages(records) -> list[str]:
    return [record.getMessage() for record in records]


def _finished_elapsed(records, tool_call_id: str) -> int:
    for message in _messages(records):
        if f"id={tool_call_id}" in message and "event=finished" in message:
            match = re.search(r"elapsed_ms=(\d+)", message)
            if match:
                return int(match.group(1))
    raise AssertionError(f"缺少 finished：{tool_call_id}")


def _model_request(ctx, system_message=None, tools=()):
    """真实 ModelRequest 替身（doc 20 §7：不用 SimpleNamespace 冒充请求）。"""
    return ModelRequest(
        model=SimpleNamespace(),  # 仅占位，测试不调用模型
        messages=[HumanMessage(content="q")],
        system_message=system_message,
        tools=[SimpleNamespace(name=n) for n in tools],
        runtime=_runtime(ctx),
    )


def _clock_blocks(system_message):
    content = system_message.content
    blocks = content if isinstance(content, list) else [{"text": content}]
    return [
        block
        for block in blocks
        if isinstance(block, dict)
        and isinstance(block.get("text"), str)
        and block["text"].lstrip().startswith(_REFERENCE_TIME_HEADER)
    ]


class TestRunBudgetMiddleware:
    def _ledger(self, model_call=10, tool_call=10, db_execute=10) -> RunBudgetLedger:
        ledger = RunBudgetLedger()
        ledger.open_run(
            "run-1",
            BudgetLimits(model_call, tool_call, db_execute),
            deadline_monotonic=time.monotonic() + 600,
        )
        return ledger

    def test_model_exhausted_jumps_to_end(self, db_agent_context):
        ctx = replace(db_agent_context, run_id="run-1")
        ledger = self._ledger(model_call=0)
        middleware = RunBudgetMiddleware(ledger, CFG.budget)
        update = middleware.before_model(
            {"messages": [], "db_run_status": {}}, _runtime(ctx)
        )
        assert update is not None
        assert update["jump_to"] == "end"
        assert update["db_run_status"]["run-1"]["stop_reason"] == "model_call_limit"
        assert isinstance(update["messages"][0], AIMessage)

    def test_model_ok_no_update(self, db_agent_context):
        db_agent_context = replace(db_agent_context, run_id="run-1")
        middleware = RunBudgetMiddleware(self._ledger(model_call=2), CFG.budget)
        assert (
            middleware.before_model(
                {"messages": [], "db_run_status": {}}, _runtime(db_agent_context)
            )
            is None
        )

    def test_tool_rejected_without_handler(self, db_agent_context):
        """tool_call 预占失败：不调用 handler，返回错误 Command。"""
        db_agent_context = replace(db_agent_context, run_id="run-1")
        ledger = self._ledger(tool_call=0)
        middleware = RunBudgetMiddleware(ledger, CFG.budget)
        request = SimpleNamespace(
            tool_call={"name": "validate_sql", "id": "tc-1", "args": {}},
            state={"db_run_status": {}},
            runtime=_runtime(db_agent_context),
        )
        called = []

        def handler(_request):
            called.append(1)
            return ToolMessage(content="ok", tool_call_id="tc-1")

        result = middleware.wrap_tool_call(request, handler)
        assert not called
        assert isinstance(result, Command)
        message = result.update["messages"][0]
        assert message.tool_call_id == "tc-1"
        assert json.loads(message.content)["error"]["error_type"] == "budget_exhausted"

    def test_tool_reserved_then_handler(self, db_agent_context):
        db_agent_context = replace(db_agent_context, run_id="run-1")
        middleware = RunBudgetMiddleware(self._ledger(), CFG.budget)
        request = SimpleNamespace(
            tool_call={"name": "validate_sql", "id": "tc-2", "args": {}},
            state={"db_run_status": {}},
            runtime=_runtime(db_agent_context),
        )
        outcome = middleware.wrap_tool_call(
            request, lambda _r: ToolMessage(content="ok", tool_call_id="tc-2")
        )
        assert isinstance(outcome, ToolMessage)

    def test_tool_logs_start_end_with_elapsed_and_summary(
        self, db_agent_context, monkeypatch
    ):
        ctx = replace(db_agent_context, run_id="run-1")
        middleware = RunBudgetMiddleware(self._ledger(), CFG.budget)
        content = json.dumps({"status": "ok", "data": {}, "warnings": []})
        expected = ToolMessage(content=content, tool_call_id="tc-log")
        times = iter((100.0, 100.125))
        monkeypatch.setattr(
            run_budget_module,
            "time",
            SimpleNamespace(monotonic=lambda: next(times)),
            raising=False,
        )

        with _AuditCapture(run_budget_module.audit_logger.name, logging.DEBUG) as cap:
            outcome = middleware.wrap_tool_call(
                _tool_request(ctx, "tc-log", args={"query": "orders"}),
                lambda _request: expected,
            )

        assert outcome is expected
        messages = _messages(cap.records)
        started = next(m for m in messages if "event=started" in m)
        finished = next(m for m in messages if "event=finished" in m)
        assert "request_id=req-test" in started and "run_id=run-1" in started
        assert "tool=validate_sql id=tc-log" in started
        assert "elapsed_ms=125" in finished
        assert "result_kind=tool_message" in finished
        assert "status=ok error_type=- error_source=-" in finished
        assert f"output_chars={len(content)}" in finished
        output_index = next(
            i for i, m in enumerate(messages) if "TOOL_OUTPUT_BEGIN" in m
        )
        assert messages.index(finished) < output_index

    def test_tool_payload_only_appears_at_debug(self, db_agent_context, monkeypatch):
        ctx = replace(db_agent_context, run_id="run-1")
        middleware = RunBudgetMiddleware(self._ledger(), CFG.budget)
        input_marker = "INPUT_" + "x" * 200
        output_marker = "OUTPUT_" + "y" * 200
        request = _tool_request(ctx, "tc-levels", args={"query": input_marker})

        with monkeypatch.context() as injected:
            injected.setattr(
                run_budget_module.json,
                "dumps",
                lambda *_args, **_kwargs: (_ for _ in ()).throw(
                    AssertionError("INFO 不应序列化 DEBUG 参数")
                ),
            )
            with _AuditCapture(
                run_budget_module.audit_logger.name, logging.INFO
            ) as cap:
                middleware.wrap_tool_call(
                    request,
                    lambda _request: ToolMessage(
                        content=output_marker, tool_call_id="tc-levels"
                    ),
                )
        info_text = "\n".join(_messages(cap.records))
        assert input_marker not in info_text and output_marker not in info_text
        assert not any(r.levelno == logging.DEBUG for r in cap.records)

        with _AuditCapture(run_budget_module.audit_logger.name, logging.DEBUG) as cap:
            middleware.wrap_tool_call(
                request,
                lambda _request: ToolMessage(
                    content=output_marker, tool_call_id="tc-levels"
                ),
            )
        debug_text = "\n".join(_messages(cap.records))
        assert input_marker in debug_text and output_marker in debug_text
        assert (
            "===== TOOL_INPUT_BEGIN request_id=req-test run_id=run-1 tool=validate_sql id=tc-levels ====="
            in debug_text
        )
        assert (
            "===== TOOL_INPUT_END request_id=req-test run_id=run-1 tool=validate_sql id=tc-levels ====="
            in debug_text
        )
        assert (
            "===== TOOL_OUTPUT_BEGIN request_id=req-test run_id=run-1 tool=validate_sql id=tc-levels ====="
            in debug_text
        )
        assert (
            "===== TOOL_OUTPUT_END request_id=req-test run_id=run-1 tool=validate_sql id=tc-levels ====="
            in debug_text
        )
        assert sum("TOOL_INPUT_BEGIN" in m for m in _messages(cap.records)) == 1
        assert sum("TOOL_OUTPUT_BEGIN" in m for m in _messages(cap.records)) == 1

    def test_tool_business_error_logs_finished_without_traceback(
        self, db_agent_context
    ):
        ctx = replace(db_agent_context, run_id="run-1")
        middleware = RunBudgetMiddleware(self._ledger(), CFG.budget)
        content = json.dumps(
            {
                "status": "error",
                "data": {},
                "warnings": [],
                "error": {
                    "error_type": "invalid_argument",
                    "message": "business validation",
                    "retryable": True,
                },
            }
        )
        outcome = tool_command(content, "tc-business")
        with _AuditCapture(run_budget_module.audit_logger.name, logging.INFO) as cap:
            returned = middleware.wrap_tool_call(
                _tool_request(ctx, "tc-business"), lambda _request: outcome
            )
        assert returned is outcome
        message = outcome.update["messages"][0]
        assert message.status == "success"
        finished = next(m for m in _messages(cap.records) if "event=finished" in m)
        assert "status=error" in finished
        assert "error_type=invalid_argument" in finished
        assert "error_source=business" in finished
        assert all(not r.exc_info for r in cap.records)

    def test_framework_error_tool_message_logs_error_without_traceback(
        self, db_agent_context
    ):
        ctx = replace(db_agent_context, run_id="run-1")
        middleware = RunBudgetMiddleware(self._ledger(), CFG.budget)
        outcome = ToolMessage(
            content="invalid_argument: query(missing)",
            tool_call_id="tc-framework",
            status="error",
        )
        with _AuditCapture(run_budget_module.audit_logger.name, logging.INFO) as cap:
            returned = middleware.wrap_tool_call(
                _tool_request(ctx, "tc-framework"), lambda _request: outcome
            )
        assert returned is outcome
        finished = next(m for m in _messages(cap.records) if "event=finished" in m)
        assert "status=error" in finished
        assert "error_type=invalid_argument" in finished
        assert "error_source=framework" in finished
        assert all(not r.exc_info for r in cap.records)

    def test_framework_error_without_invalid_argument_prefix_uses_tool_error(
        self, db_agent_context
    ):
        ctx = replace(db_agent_context, run_id="run-1")
        middleware = RunBudgetMiddleware(self._ledger(), CFG.budget)
        outcome = ToolMessage(
            content="Error: no_such_tool is not a valid tool",
            tool_call_id="tc-unknown",
            status="error",
        )
        with _AuditCapture(run_budget_module.audit_logger.name, logging.INFO) as cap:
            middleware.wrap_tool_call(
                _tool_request(ctx, "tc-unknown"), lambda _request: outcome
            )
        finished = next(m for m in _messages(cap.records) if "event=finished" in m)
        assert "error_type=tool_error" in finished
        assert "error_source=framework" in finished

    def test_tool_exception_logs_elapsed_and_reraises(self, db_agent_context):
        ctx = replace(db_agent_context, run_id="run-1")
        middleware = RunBudgetMiddleware(self._ledger(), CFG.budget)
        original = RuntimeError("SELECT_SECRET_MARKER")

        def handler(_request):
            raise original

        with (
            _AuditCapture(run_budget_module.audit_logger.name, logging.INFO) as cap,
            pytest.raises(RuntimeError) as caught,
        ):
            middleware.wrap_tool_call(_tool_request(ctx, "tc-error"), handler)
        assert caught.value is original
        error = next(r for r in cap.records if r.levelno == logging.ERROR)
        assert not error.exc_info
        assert "event=failed" in error.getMessage()
        assert "exception_type=RuntimeError" in error.getMessage()
        formatted = logging.Formatter().format(error)
        assert "SELECT_SECRET_MARKER" not in formatted
        assert "Traceback" not in formatted

        with (
            _AuditCapture(run_budget_module.audit_logger.name, logging.DEBUG) as cap,
            pytest.raises(RuntimeError) as caught,
        ):
            middleware.wrap_tool_call(_tool_request(ctx, "tc-error"), handler)
        assert caught.value is original
        failed_index = next(
            i for i, r in enumerate(cap.records) if r.levelno == logging.ERROR
        )
        debug_index = next(
            i
            for i, r in enumerate(cap.records)
            if r.levelno == logging.DEBUG and r.exc_info
        )
        assert failed_index < debug_index
        debug_record = cap.records[debug_index]
        formatted = logging.Formatter().format(debug_record)
        assert "SELECT_SECRET_MARKER" in formatted
        assert "Traceback" in formatted

    def test_tool_rejected_logs_rejected_and_preserves_warning(self, db_agent_context):
        ctx = replace(db_agent_context, run_id="run-1")
        middleware = RunBudgetMiddleware(self._ledger(tool_call=0), CFG.budget)
        called = []
        with (
            _AuditCapture(run_budget_module.audit_logger.name, logging.INFO) as audit,
            _AuditCapture(run_budget_module.logger.name, logging.INFO) as warning,
        ):
            result = middleware.wrap_tool_call(
                _tool_request(ctx, "tc-rejected"),
                lambda _request: called.append(1),
            )
        assert not called and isinstance(result, Command)
        rejected = next(m for m in _messages(audit.records) if "event=rejected" in m)
        assert "error_source=middleware" in rejected
        assert "stop_reason=tool_call_limit" in rejected
        assert any(r.levelno == logging.WARNING for r in warning.records)

    def test_non_json_tool_message_uses_unknown_business_status(self, db_agent_context):
        ctx = replace(db_agent_context, run_id="run-1")
        middleware = RunBudgetMiddleware(self._ledger(), CFG.budget)
        with _AuditCapture(run_budget_module.audit_logger.name, logging.INFO) as cap:
            middleware.wrap_tool_call(
                _tool_request(ctx, "tc-final", name="submit_final_answer"),
                lambda _request: ToolMessage(
                    content="已接收最终答案", tool_call_id="tc-final"
                ),
            )
        finished = next(m for m in _messages(cap.records) if "event=finished" in m)
        assert "status=- error_type=- error_source=-" in finished

    def test_non_text_tool_message_skips_binary_payload(self, db_agent_context):
        ctx = replace(db_agent_context, run_id="run-1")
        middleware = RunBudgetMiddleware(self._ledger(), CFG.budget)
        content = [
            {"type": "image", "base64": "BASE64_MARKER", "mime_type": "image/png"}
        ]
        with _AuditCapture(run_budget_module.audit_logger.name, logging.DEBUG) as cap:
            middleware.wrap_tool_call(
                _tool_request(ctx, "tc-image", name="read_file"),
                lambda _request: ToolMessage(content=content, tool_call_id="tc-image"),
            )
            middleware.wrap_tool_call(
                _tool_request(ctx, "tc-image-error", name="read_file"),
                lambda _request: ToolMessage(
                    content=content, tool_call_id="tc-image-error", status="error"
                ),
            )
        text = "\n".join(_messages(cap.records))
        assert "BASE64_MARKER" not in text
        assert "block_count=1" in text and "block_types=image" in text
        finished = [m for m in _messages(cap.records) if "event=finished" in m]
        assert "output_chars=-" in finished[0]
        assert "status=- error_type=- error_source=-" in finished[0]
        assert "status=error" in finished[1]
        assert "error_type=tool_error" in finished[1]
        assert "error_source=framework" in finished[1]

    def test_malformed_envelope_does_not_change_tool_result(self, db_agent_context):
        ctx = replace(db_agent_context, run_id="run-1")
        middleware = RunBudgetMiddleware(self._ledger(), CFG.budget)
        malformed = ToolMessage(
            content='{"status":"error","error":"not-an-object"}',
            tool_call_id="tc-malformed",
        )

        returned = middleware.wrap_tool_call(
            _tool_request(ctx, "tc-malformed"), lambda _request: malformed
        )

        assert returned is malformed

    def test_non_string_envelope_status_does_not_change_tool_result(
        self, db_agent_context
    ):
        ctx = replace(db_agent_context, run_id="run-1")
        middleware = RunBudgetMiddleware(self._ledger(), CFG.budget)
        expected = ToolMessage(content='{"status":[]}', tool_call_id="tc-list-status")

        with _AuditCapture(run_budget_module.audit_logger.name, logging.INFO) as cap:
            returned = middleware.wrap_tool_call(
                _tool_request(ctx, "tc-list-status"), lambda _request: expected
            )

        assert returned is expected
        finished = next(m for m in _messages(cap.records) if "event=finished" in m)
        assert "status=- error_type=- error_source=-" in finished
        assert f"output_chars={len(expected.content)}" in finished

    def test_non_string_error_type_is_redacted_from_info(self, db_agent_context):
        ctx = replace(db_agent_context, run_id="run-1")
        middleware = RunBudgetMiddleware(self._ledger(), CFG.budget)
        content = json.dumps(
            {
                "status": "error",
                "error": {"error_type": {"code": "OBJECT_MARKER"}},
            }
        )
        expected = ToolMessage(content=content, tool_call_id="tc-object-error")

        with _AuditCapture(run_budget_module.audit_logger.name, logging.INFO) as cap:
            returned = middleware.wrap_tool_call(
                _tool_request(ctx, "tc-object-error"), lambda _request: expected
            )

        assert returned is expected
        finished = next(m for m in _messages(cap.records) if "event=finished" in m)
        assert "error_type=- error_source=business" in finished
        assert "OBJECT_MARKER" not in finished

    def test_debug_input_does_not_change_budget_reservation(
        self, db_agent_context, monkeypatch
    ):
        ctx = replace(db_agent_context, run_id="run-1")
        clock = SimpleNamespace(value=100.0)
        monotonic = SimpleNamespace(monotonic=lambda: clock.value)
        monkeypatch.setattr(run_budget_module, "time", monotonic)
        monkeypatch.setattr(run_budget_service_module, "time", monotonic)
        ledger = RunBudgetLedger()
        ledger.open_run("run-1", BudgetLimits(10, 10, 10), 105.0)
        middleware = RunBudgetMiddleware(ledger, CFG.budget)
        expected = ToolMessage(content="ok", tool_call_id="tc-deadline")
        calls = []

        def slow_debug(value):
            clock.value = 110.0
            return "{}"

        def handler(_request):
            calls.append(1)
            return expected

        monkeypatch.setattr(run_budget_module, "_debug_value", slow_debug)
        with _AuditCapture(run_budget_module.audit_logger.name, logging.DEBUG) as cap:
            returned = middleware.wrap_tool_call(
                _tool_request(ctx, "tc-deadline"), handler
            )

        assert returned is expected and len(calls) == 1
        messages = _messages(cap.records)
        assert any("event=finished" in message for message in messages)
        assert not any("event=rejected" in message for message in messages)

    def test_debug_formatting_failure_does_not_change_success_result(
        self, db_agent_context, monkeypatch
    ):
        ctx = replace(db_agent_context, run_id="run-1")
        middleware = RunBudgetMiddleware(self._ledger(), CFG.budget)
        expected = ToolMessage(content="ok", tool_call_id="tc-format")
        calls = []

        def handler(_request):
            calls.append(1)
            return expected

        monkeypatch.setattr(
            run_budget_module.json,
            "dumps",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("format boom")),
        )
        with _AuditCapture(run_budget_module.audit_logger.name, logging.DEBUG) as cap:
            outcome = middleware.wrap_tool_call(
                _tool_request(ctx, "tc-format", args={"query": "x"}), handler
            )
        assert outcome is expected and len(calls) == 1
        assert any("event=finished" in m for m in _messages(cap.records))

    def test_exception_debug_logging_failure_preserves_original_exception(
        self, db_agent_context, monkeypatch
    ):
        ctx = replace(db_agent_context, run_id="run-1")
        middleware = RunBudgetMiddleware(self._ledger(), CFG.budget)
        original = RuntimeError("original tool failure")
        calls = []
        injected = []
        real_debug = run_budget_module.audit_logger.debug

        def handler(_request):
            calls.append(1)
            raise original

        def failing_debug(*args, **kwargs):
            if kwargs.get("exc_info"):
                injected.append(kwargs)
                raise ValueError("debug logging failed")
            return real_debug(*args, **kwargs)

        monkeypatch.setattr(run_budget_module.audit_logger, "debug", failing_debug)
        with (
            _AuditCapture(run_budget_module.audit_logger.name, logging.DEBUG),
            pytest.raises(RuntimeError) as caught,
        ):
            middleware.wrap_tool_call(_tool_request(ctx, "tc-debug-error"), handler)
        assert caught.value is original
        assert len(calls) == 1 and len(injected) == 1
        assert injected[0]["exc_info"] is True

    def test_interleaved_tool_calls_keep_separate_identities(
        self, db_agent_context, monkeypatch
    ):
        ctx = replace(db_agent_context, run_id="run-1")
        middleware = RunBudgetMiddleware(self._ledger(), CFG.budget)
        barrier = threading.Barrier(2)
        clock_values = {
            "tc-a": iter((100.0, 100.5)),
            "tc-b": iter((100.125, 100.375)),
        }
        monkeypatch.setattr(
            run_budget_module,
            "time",
            SimpleNamespace(
                monotonic=lambda: next(clock_values[threading.current_thread().name])
            ),
            raising=False,
        )

        def handler(_request):
            barrier.wait(timeout=5)
            return ToolMessage(content="ok", tool_call_id=_request.tool_call["id"])

        def invoke(tool_call_id):
            threading.current_thread().name = tool_call_id
            middleware.wrap_tool_call(_tool_request(ctx, tool_call_id), handler)

        with _AuditCapture(run_budget_module.audit_logger.name, logging.INFO) as cap:
            threads = [threading.Thread(target=invoke, args=(f"tc-{x}",)) for x in "ab"]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)
            assert all(not thread.is_alive() for thread in threads)

        assert _finished_elapsed(cap.records, "tc-a") == 500
        assert _finished_elapsed(cap.records, "tc-b") == 250
        messages = _messages(cap.records)
        for tool_call_id in ("tc-a", "tc-b"):
            started = next(
                i
                for i, m in enumerate(messages)
                if f"id={tool_call_id}" in m and "event=started" in m
            )
            finished = next(
                i
                for i, m in enumerate(messages)
                if f"id={tool_call_id}" in m and "event=finished" in m
            )
            assert started < finished


class TestFinalAnswerGate:
    def test_before_model_ends_after_submission(self, db_agent_context):
        db_agent_context = replace(db_agent_context, run_id="run-1")
        middleware = FinalAnswerGateMiddleware(RunBudgetLedger(), CFG.budget)
        state = {
            "messages": [],
            "db_final_answers": {"run-1": {"run_id": "run-1"}},
        }
        assert middleware.before_model(state, _runtime(db_agent_context)) == {
            "jump_to": "end"
        }

    def test_after_model_reminds_once(self, db_agent_context):
        """无工具调用且未提交 → 追加提醒并 jump_to=model；次数用尽后不干预。"""
        db_agent_context = replace(db_agent_context, run_id="run-1")
        ledger = RunBudgetLedger()
        middleware = FinalAnswerGateMiddleware(ledger, CFG.budget)
        state = {
            "messages": [AIMessage(content="我认为…")],
            "db_final_answers": {},
            "db_run_status": {},
        }
        update = middleware.after_model(state, _runtime(db_agent_context))
        assert update is not None and update["jump_to"] == "model"
        reminder = update["messages"][0]
        assert reminder.additional_kwargs.get("dbda_kind") == "final_answer_reminder"
        assert update["db_run_status"]["run-1"]["final_answer_reminders"] == 1
        # 已达上限 → 不再提醒
        state2 = {
            **state,
            "db_run_status": {
                "run-1": {"final_answer_reminders": CFG.budget.final_answer_reminders}
            },
        }
        assert middleware.after_model(state2, _runtime(db_agent_context)) is None

    def test_no_reminder_when_budget_stopped(self, db_agent_context):
        db_agent_context = replace(db_agent_context, run_id="run-1")
        ledger = RunBudgetLedger()
        ledger.open_run(
            "run-1", BudgetLimits(1, 1, 1), deadline_monotonic=time.monotonic() + 600
        )
        ledger.stop("run-1", "tool_call_limit")
        middleware = FinalAnswerGateMiddleware(ledger, CFG.budget)
        state = {
            "messages": [AIMessage(content="…")],
            "db_final_answers": {},
            "db_run_status": {},
        }
        assert middleware.after_model(state, _runtime(db_agent_context)) is None


class TestRequestAudit:
    def _middleware(self, expected: set[str], tmp_path) -> RequestAuditMiddleware:
        return RequestAuditMiddleware(
            expected_tool_names=lambda: expected,
            budget=RunBudgetLedger(),
            workspace_root=tmp_path,
        )

    def _request(self, ctx, tools):
        return _model_request(ctx, tools=tools)

    def test_mismatch_stops_run(self, db_agent_context, tmp_path):
        db_agent_context = replace(db_agent_context, run_id="run-1")
        ledger = RunBudgetLedger()
        ledger.open_run(
            "run-1", BudgetLimits(5, 5, 5), deadline_monotonic=time.monotonic() + 600
        )
        middleware = RequestAuditMiddleware(
            expected_tool_names=lambda: {"a", "b"},
            budget=ledger,
            workspace_root=tmp_path,
        )
        result = middleware.wrap_model_call(
            self._request(db_agent_context, ["a", "c"]),
            lambda _request: SimpleNamespace(result=[AIMessage(content="x")]),
        )
        assert isinstance(result, AIMessage) and not result.tool_calls
        assert ledger.stop_reason("run-1") == "tool_set_mismatch"

    def test_match_passes_through_and_logs(self, db_agent_context, tmp_path):
        db_agent_context = replace(db_agent_context, run_id="run-2")
        middleware = self._middleware({"validate_sql"}, tmp_path)
        sentinel = SimpleNamespace(result=[AIMessage(content="ok")])
        out = middleware.wrap_model_call(
            self._request(db_agent_context, ["validate_sql"]),
            lambda _request: sentinel,
        )
        assert out is sentinel
        audit = tmp_path / "sessions" / db_agent_context.session_id / "audit.jsonl"
        assert audit.exists() and "model_request" in audit.read_text(encoding="utf-8")


class TestReferenceTimeInjection:
    """doc 20 §4/§8：主 Agent 北京时间背景注入（_with_reference_time）。"""

    def test_none_system_message_gets_clock_block(self, db_agent_context):
        result = _with_reference_time(_model_request(db_agent_context))
        block = result.system_message.content[0]
        assert block["text"].lstrip().startswith(_REFERENCE_TIME_HEADER)
        assert "本轮参考日期：" in block["text"]
        assert "时区：Asia/Shanghai" in block["text"]

    def test_str_system_message_appends_block_and_keeps_original(
        self, db_agent_context
    ):
        original = _model_request(
            db_agent_context, system_message=SystemMessage(content="基础提示")
        )
        result = _with_reference_time(original)
        blocks = result.system_message.content
        assert len(blocks) == 2
        assert blocks[0]["text"] == "基础提示"
        assert blocks[1]["text"].lstrip().startswith(_REFERENCE_TIME_HEADER)
        # 原请求不变（doc 20 §4 约定）
        assert original.system_message.content == "基础提示"

    def test_list_system_message_preserves_other_blocks(self, db_agent_context):
        image_block = {"type": "image_url", "image_url": {"url": "https://x"}}
        original = _model_request(
            db_agent_context,
            system_message=SystemMessage(
                content=[{"type": "text", "text": "A"}, image_block]
            ),
        )
        result = _with_reference_time(original)
        blocks = result.system_message.content
        assert len(blocks) == 3
        assert blocks[0] == {"type": "text", "text": "A"}
        assert blocks[1] == image_block
        assert blocks[2]["text"].lstrip().startswith(_REFERENCE_TIME_HEADER)
        assert len(original.system_message.content) == 2  # 原请求不变

    def test_beijing_conversion_crosses_midnight(self, db_agent_context):
        ctx = replace(
            db_agent_context,
            now=datetime(2026, 11, 16, 18, 30, tzinfo=UTC),
        )
        result = _with_reference_time(_model_request(ctx))
        clock = _clock_blocks(result.system_message)[0]["text"]
        assert "本轮参考日期：2026-11-17" in clock  # UTC 18:30 → 北京次日 02:30
        assert "2026-11-17T02:30:00+08:00" in clock

    def test_rewrap_original_keeps_single_block(self, db_agent_context):
        original = _model_request(
            db_agent_context, system_message=SystemMessage(content="基础提示")
        )
        once = _with_reference_time(original)
        twice = _with_reference_time(once)
        assert len(_clock_blocks(twice.system_message)) == 1

    def test_same_original_processed_twice_stable(self, db_agent_context):
        """doc 20 §4 / R1 C1-02：同一原请求对象独立处理两次——各自只含
        一个时间块、内容一致，原请求保持不变（区别于重包返回值场景）。"""
        original = _model_request(
            db_agent_context, system_message=SystemMessage(content="基础提示")
        )
        first = _with_reference_time(original)
        second = _with_reference_time(original)
        first_clock = _clock_blocks(first.system_message)
        second_clock = _clock_blocks(second.system_message)
        assert len(first_clock) == 1 and len(second_clock) == 1
        assert first_clock[0]["text"] == second_clock[0]["text"]
        assert first is not second and first is not original
        assert original.system_message.content == "基础提示"

    def test_rewrap_injected_refreshes_time(self, db_agent_context):
        ctx_late = replace(
            db_agent_context,
            now=datetime(2026, 11, 20, 9, 0, tzinfo=UTC),
        )
        once = _with_reference_time(
            _model_request(db_agent_context, system_message=SystemMessage(content="s"))
        )
        # 以新上下文重包已注入请求（模拟新 run 复用请求对象）
        twice = _with_reference_time(
            ModelRequest(
                model=SimpleNamespace(),
                messages=once.messages,
                system_message=once.system_message,
                runtime=_runtime(ctx_late),
            )
        )
        clocks = _clock_blocks(twice.system_message)
        assert len(clocks) == 1
        assert "2026-11-20" in clocks[0]["text"]  # 以当前 ctx.now 刷新

    def test_multiple_stale_blocks_normalized(self, db_agent_context):
        stale = {"type": "text", "text": f"\n{_REFERENCE_TIME_HEADER}\n旧日期"}
        other = {"type": "text", "text": "B"}
        original = _model_request(
            db_agent_context,
            system_message=SystemMessage(content=[dict(stale), dict(stale), other]),
        )
        result = _with_reference_time(original)
        blocks = result.system_message.content
        assert len(_clock_blocks(result.system_message)) == 1
        assert blocks[0] == other  # 非时间块保留在前

    def test_header_inside_other_block_not_removed(self, db_agent_context):
        mention = {
            "type": "text",
            "text": "说明：本方案保留标题 ## 本轮北京时间背景 出现在句中，不应删除。",
        }
        original = _model_request(
            db_agent_context, system_message=SystemMessage(content=[mention])
        )
        result = _with_reference_time(original)
        blocks = result.system_message.content
        assert blocks[0] == mention
        assert len(_clock_blocks(result.system_message)) == 1

    def test_middleware_passes_injected_request_to_handler(
        self, db_agent_context, tmp_path
    ):
        """wrap_model_call 注入发生在估算与 handler 之前（doc 20 §8.4）。"""
        db_agent_context = replace(db_agent_context, run_id="run-inj")
        middleware = RequestAuditMiddleware(
            expected_tool_names=lambda: {"a"},
            budget=RunBudgetLedger(),
            workspace_root=tmp_path,
        )
        captured = []

        def handler(request):
            captured.append(request)
            return SimpleNamespace(result=[AIMessage(content="ok")])

        original = _model_request(db_agent_context, tools=["a"])
        middleware.wrap_model_call(original, handler)
        assert captured, "handler 必须被调用"
        assert captured[0] is not original
        assert _clock_blocks(captured[0].system_message)
        assert original.system_message is None  # 原请求未被改动
