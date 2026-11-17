"""RunBudgetMiddleware / FinalAnswerGateMiddleware / RequestAuditMiddleware
单元测试（docs/v4/5 §11.1–§11.3）
"""

from __future__ import annotations

import json
import time
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from langchain.agents.middleware import ModelRequest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.types import Command

from src.modules.db_deepagent.config import load_db_agent_config
from src.modules.db_deepagent.middleware.final_answer_gate import (
    FinalAnswerGateMiddleware,
)
from src.modules.db_deepagent.middleware.request_audit import (
    RequestAuditMiddleware,
    _with_reference_time,
)
from src.modules.db_deepagent.middleware.run_budget import RunBudgetMiddleware
from src.modules.db_deepagent.services.run_budget import BudgetLimits, RunBudgetLedger
from src.tests.db_deepagent.conftest import make_time_evidence

pytestmark = pytest.mark.unit

CFG = load_db_agent_config()
SEP = make_time_evidence("T-sep", "2026-09-01", "2026-10-01")

_REFERENCE_TIME_HEADER = "## 本轮北京时间背景"


def _runtime(ctx):
    return SimpleNamespace(context=ctx)


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
