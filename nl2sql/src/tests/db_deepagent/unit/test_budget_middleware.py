"""RunBudgetMiddleware / FinalAnswerGateMiddleware / RequestAuditMiddleware
单元测试（docs/v4/5 §11.1–§11.3）
"""

from __future__ import annotations

import json
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.types import Command

from src.modules.db_deepagent.config import load_db_agent_config
from src.modules.db_deepagent.middleware.final_answer_gate import (
    FinalAnswerGateMiddleware,
)
from src.modules.db_deepagent.middleware.request_audit import RequestAuditMiddleware
from src.modules.db_deepagent.middleware.run_budget import RunBudgetMiddleware
from src.modules.db_deepagent.services.run_budget import BudgetLimits, RunBudgetLedger

from src.tests.db_deepagent.conftest import make_time_evidence

pytestmark = pytest.mark.unit

CFG = load_db_agent_config()
SEP = make_time_evidence("T-sep", "2026-09-01", "2026-10-01")


def _runtime(ctx):
    return SimpleNamespace(context=ctx)


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
        names = [SimpleNamespace(name=n) for n in tools]
        return SimpleNamespace(
            tools=names,
            messages=[HumanMessage(content="q")],
            system_message=None,
            runtime=_runtime(ctx),
        )

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
