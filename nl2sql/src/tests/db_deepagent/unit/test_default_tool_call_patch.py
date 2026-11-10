"""默认工具调用修补中间件行为保留测试（doc 8 步骤 1）

用完整生产装配（真实 build_db_agent + assembly_services + 内存检查点）验证
SDK 修补语义：正常历史不被改动、缺失/畸形调用补 error 结果、部分历史只补
缺失项、同一检查点二次 invoke 不重复补齐；全程不重新执行历史工具。
行为保留方法：本组测试在删除显式覆盖前后都应通过，不设 RED。
"""

from __future__ import annotations

import time
from datetime import UTC, datetime

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from src.modules.db_deepagent.agent import build_db_agent
from src.modules.db_deepagent.context import DbAgentContext
from src.modules.db_deepagent.services.run_budget import limits_from_config
from src.tests.db_deepagent.conftest import ScriptedChatModel

pytestmark = pytest.mark.unit


class _RecordingModel(ScriptedChatModel):
    """记录每轮收到的消息，始终返回无工具调用的普通文本。

    首轮普通文本会触发一次最终答案提醒（生产 final_answer_reminders=1），
    提醒轮再回普通文本后运行自然结束；不伪造 SQL 证据。
    """

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        turns = getattr(self, "_seen_turns", None)
        if turns is None:
            turns = []
            object.__setattr__(self, "_seen_turns", turns)
        turns.append(list(messages))
        from langchain_core.outputs import ChatGeneration, ChatResult

        return ChatResult(
            generations=[
                ChatGeneration(message=AIMessage(content="普通文本回复（测试）"))
            ]
        )


def _ai_tool_call(
    msg_id: str, calls: list[dict], *, invalid: list[dict] | None = None
) -> AIMessage:
    return AIMessage(
        id=msg_id, content="", tool_calls=calls, invalid_tool_calls=invalid or []
    )


def _tool_result(
    msg_id: str, call_id: str, *, status: str = "success", content: str = '{"ok": 1}'
) -> ToolMessage:
    return ToolMessage(
        id=msg_id,
        content=content,
        name="inspect_tables",
        tool_call_id=call_id,
        status=status,
    )


def _invoke_once(
    handle,
    services,
    *,
    thread_id: str,
    request_id: str,
    run_id: str,
    history: list,
):
    """注册预算并驱动一次生产图；deadline 用有效时限（不用 fixture 的 0）。"""
    services.budget.open_run(
        run_id,
        limits_from_config(services.config.budget),
        deadline_monotonic=time.monotonic() + 300,
    )
    ctx = DbAgentContext(
        session_id="t-patch",
        thread_id=thread_id,
        request_id=request_id,
        run_id=run_id,
        datasource_id="pg:test",
        validation_fingerprint="fp",
        now=datetime.now(UTC),
        timezone="Asia/Shanghai",
        user_texts=("继续处理",),
        carried_evidence=(),
        deadline_monotonic=time.monotonic() + 300,
    )
    config = {
        "configurable": {"thread_id": thread_id},
        "recursion_limit": 50,
        "metadata": {"request_id": request_id, "dbda_call_kind": "agent"},
    }
    handle.agent.invoke({"messages": history}, config=config, context=ctx)


def _tool_msgs_by_call_id(messages):
    grouped: dict[str, list[ToolMessage]] = {}
    for m in messages:
        if isinstance(m, ToolMessage):
            grouped.setdefault(m.tool_call_id, []).append(m)
    return grouped


def _history_snapshot(messages):
    """调用前保存语义快照，避免原消息被原地修改后比较同一对象。"""
    fields = {
        "type",
        "id",
        "content",
        "tool_calls",
        "invalid_tool_calls",
        "name",
        "tool_call_id",
        "status",
    }
    return [m.model_dump(include=fields) for m in messages]


def _assert_history_preserved(messages, expected):
    """忽略新增修补/提醒消息，原历史的内容、参数、数量及相对顺序须一致。"""
    history_ids = {m["id"] for m in expected}
    actual = _history_snapshot(m for m in messages if m.id in history_ids)
    assert actual == expected


class TestPatchBehaviorPreserved:
    @pytest.mark.parametrize("status", ["success", "error"])
    def test_completed_tool_history_is_preserved(
        self, assembly_services, monkeypatch, status
    ):
        """已有结果的历史（success/error）：内容/ID/name/status 保留且仅一条。"""
        from langgraph.checkpoint.memory import InMemorySaver

        import src.modules.db_deepagent.agent as agent_mod

        model = _RecordingModel()
        monkeypatch.setattr(agent_mod, "build_model", lambda _cfg: model)
        handle = build_db_agent(
            assembly_services, session_id="t-patch", checkpointer=InMemorySaver()
        )

        history = [
            HumanMessage(id="h1", content="查订单表结构"),
            _ai_tool_call(
                "a1",
                [
                    {
                        "name": "inspect_tables",
                        "args": {"table_ids": ["public.orders"]},
                        "id": "call-done",
                        "type": "tool_call",
                    }
                ],
            ),
            _tool_result("t1", "call-done", status=status),
            HumanMessage(id="h2", content="继续"),
        ]
        expected_history = _history_snapshot(history)
        _invoke_once(
            handle,
            assembly_services,
            thread_id="th-done",
            request_id="req-done",
            run_id="run-done",
            history=history,
        )

        first_turn = model._seen_turns[0]
        grouped = _tool_msgs_by_call_id(first_turn)
        assert len(grouped.get("call-done", [])) == 1, "已有结果不得重复"
        kept = grouped["call-done"][0]
        assert kept.id == "t1"
        assert kept.content == '{"ok": 1}'
        assert kept.name == "inspect_tables"
        assert kept.status == status
        _assert_history_preserved(first_turn, expected_history)
        # 无新增修补结果
        assert len(grouped) == 1
        assembly_services.budget.close_run("run-done")

    def test_missing_tool_result_is_patched_once(self, assembly_services, monkeypatch):
        """缺失结果：补恰一条 error 结果；同检查点二次 invoke 不重复补齐。"""
        import deepagents.middleware.patch_tool_calls as ptc_mod
        from langgraph.checkpoint.memory import InMemorySaver

        import src.modules.db_deepagent.agent as agent_mod

        hook_calls: list[float] = []
        real_before_agent = ptc_mod.PatchToolCallsMiddleware.before_agent

        def counting_before_agent(self, state, runtime):
            hook_calls.append(time.monotonic())
            return real_before_agent(self, state, runtime)

        monkeypatch.setattr(
            ptc_mod.PatchToolCallsMiddleware, "before_agent", counting_before_agent
        )

        model = _RecordingModel()
        monkeypatch.setattr(agent_mod, "build_model", lambda _cfg: model)
        handle = build_db_agent(
            assembly_services, session_id="t-patch", checkpointer=InMemorySaver()
        )

        # C1-1：历史工具 inspect_tables 的业务库入口是 get_columns
        # （TableCatalog.describe → get_columns）；FakeExecDb.executed 只记录
        # run_readonly，不能独立证明结构查询未发生。此处局部薄包装记录调用。
        db = assembly_services.business_db
        real_get_columns = db.get_columns
        get_columns_calls: list[tuple[str, str]] = []

        def _recording_get_columns(schema, table):
            get_columns_calls.append((schema, table))
            return real_get_columns(schema, table)

        db.get_columns = _recording_get_columns

        history = [
            HumanMessage(id="h1", content="查订单表结构"),
            _ai_tool_call(
                "a1",
                [
                    {
                        "name": "inspect_tables",
                        "args": {"table_ids": ["public.orders"]},
                        "id": "call-missing",
                        "type": "tool_call",
                    }
                ],
            ),
            HumanMessage(id="h2", content="继续"),
        ]
        expected_history = _history_snapshot(history)
        _invoke_once(
            handle,
            assembly_services,
            thread_id="th-missing",
            request_id="req-m1",
            run_id="run-m1",
            history=history,
        )

        first_turn = model._seen_turns[0]
        grouped = _tool_msgs_by_call_id(first_turn)
        patched = grouped.get("call-missing", [])
        assert len(patched) == 1, "缺失结果应补恰一条"
        assert patched[0].status == "error"
        assert patched[0].name == "inspect_tables"
        assert patched[0].content
        _assert_history_preserved(first_turn, expected_history)
        assert len(grouped) == 1
        assert len(hook_calls) == 1  # 每次图调用修补钩子恰执行一次
        # C1-2：保存修补消息语义签名，供二次调用与最终状态比对
        patched_signature = (
            patched[0].id,
            patched[0].tool_call_id,
            patched[0].name,
            patched[0].status,
            patched[0].content,
        )
        assert get_columns_calls == []  # 首轮未重读表结构

        # 同一内存检查点 + thread，新 request/run 再调用一次
        turns_before_second = len(model._seen_turns)
        second_history = [HumanMessage(id="h3", content="第二轮")]
        expected_second_history = expected_history + _history_snapshot(second_history)
        _invoke_once(
            handle,
            assembly_services,
            thread_id="th-missing",
            request_id="req-m2",
            run_id="run-m2",
            history=second_history,
        )
        second_turn = model._seen_turns[turns_before_second]
        _assert_history_preserved(second_turn, expected_second_history)
        grouped2 = _tool_msgs_by_call_id(second_turn)
        again = grouped2.get("call-missing", [])
        assert len(again) == 1, "二次调用不得重复补齐"
        assert (
            again[0].id,
            again[0].tool_call_id,
            again[0].name,
            again[0].status,
            again[0].content,
        ) == patched_signature, "二次调用必须保持同一条修补结果（不得替换）"
        assert len(hook_calls) == 2  # 两次 invoke 各恰一次
        assert get_columns_calls == []  # 二轮后仍未重读表结构

        # 两轮均未重新执行历史工具：预算 tool_call=0；SQL 执行记录为空
        assert assembly_services.budget.counters("run-m1").get("tool_call", 0) == 0
        assert assembly_services.budget.counters("run-m2").get("tool_call", 0) == 0
        assert assembly_services.business_db.executed == []
        # 最终图状态中的修补结果与首次签名一致（同一条消息，未被替换）
        final_values = handle.agent.get_state(
            {"configurable": {"thread_id": "th-missing"}}
        ).values
        _assert_history_preserved(final_values["messages"], expected_second_history)
        state_grouped = _tool_msgs_by_call_id(final_values.get("messages", []))
        state_patched = state_grouped.get("call-missing", [])
        assert len(state_patched) == 1
        assert (
            state_patched[0].id,
            state_patched[0].tool_call_id,
            state_patched[0].name,
            state_patched[0].status,
            state_patched[0].content,
        ) == patched_signature
        assembly_services.budget.close_run("run-m1")
        assembly_services.budget.close_run("run-m2")

    def test_partial_tool_history_only_patches_missing_result(
        self, assembly_services, monkeypatch
    ):
        """同一 AI 消息两个调用仅一个有结果：只补缺失项，不重复已有结果。"""
        from langgraph.checkpoint.memory import InMemorySaver

        import src.modules.db_deepagent.agent as agent_mod

        model = _RecordingModel()
        monkeypatch.setattr(agent_mod, "build_model", lambda _cfg: model)
        handle = build_db_agent(
            assembly_services, session_id="t-patch", checkpointer=InMemorySaver()
        )

        def _call(cid: str) -> dict:
            return {
                "name": "inspect_tables",
                "args": {"table_ids": ["public.orders"]},
                "id": cid,
                "type": "tool_call",
            }

        history = [
            HumanMessage(id="h1", content="查两张表"),
            _ai_tool_call("a1", [_call("call-a"), _call("call-b")]),
            _tool_result("t1", "call-a", status="success", content='{"kept": true}'),
            HumanMessage(id="h2", content="继续"),
        ]
        expected_history = _history_snapshot(history)
        _invoke_once(
            handle,
            assembly_services,
            thread_id="th-partial",
            request_id="req-partial",
            run_id="run-partial",
            history=history,
        )

        first_turn = model._seen_turns[0]
        _assert_history_preserved(first_turn, expected_history)
        grouped = _tool_msgs_by_call_id(first_turn)
        assert len(grouped.get("call-a", [])) == 1
        kept = grouped["call-a"][0]
        assert kept.id == "t1" and kept.content == '{"kept": true}'
        assert kept.status == "success"
        patched = grouped.get("call-b", [])
        assert len(patched) == 1 and patched[0].status == "error"
        assert patched[0].name == "inspect_tables"
        assert set(grouped) == {"call-a", "call-b"}  # 无其他新增
        assembly_services.budget.close_run("run-partial")

    def test_invalid_tool_call_history_gets_error_result(
        self, assembly_services, monkeypatch
    ):
        """invalid_tool_calls（带 ID 的畸形参数）：补 error，不执行该调用。"""
        from langgraph.checkpoint.memory import InMemorySaver

        import src.modules.db_deepagent.agent as agent_mod

        # 消息构造按 doc 8 示例；先确认构造成功（args 为畸形字符串）
        invalid_ai = AIMessage(
            id="a1",
            content="",
            invalid_tool_calls=[
                {
                    "name": "inspect_tables",
                    "args": '{"table_ids": [',
                    "id": "call-invalid",
                    "error": "invalid JSON (test)",
                }
            ],
        )
        assert invalid_ai.invalid_tool_calls[0]["id"] == "call-invalid"

        model = _RecordingModel()
        monkeypatch.setattr(agent_mod, "build_model", lambda _cfg: model)
        handle = build_db_agent(
            assembly_services, session_id="t-patch", checkpointer=InMemorySaver()
        )

        # C1-1：独立观察结构查询入口（executed 只记录 run_readonly/SQL）
        db = assembly_services.business_db
        real_get_columns = db.get_columns
        get_columns_calls: list[tuple[str, str]] = []

        def _recording_get_columns(schema, table):
            get_columns_calls.append((schema, table))
            return real_get_columns(schema, table)

        db.get_columns = _recording_get_columns

        history = [
            HumanMessage(id="h1", content="查订单表结构"),
            invalid_ai,
            HumanMessage(id="h2", content="继续"),
        ]
        expected_history = _history_snapshot(history)
        _invoke_once(
            handle,
            assembly_services,
            thread_id="th-invalid",
            request_id="req-invalid",
            run_id="run-invalid",
            history=history,
        )

        first_turn = model._seen_turns[0]
        _assert_history_preserved(first_turn, expected_history)
        grouped = _tool_msgs_by_call_id(first_turn)
        patched = grouped.get("call-invalid", [])
        assert len(patched) == 1
        assert patched[0].status == "error"
        assert patched[0].name == "inspect_tables"
        assert patched[0].content
        assert set(grouped) == {"call-invalid"}
        # 不把它当成功、不执行该调用：预算 0、结构查询未发生、SQL 记录为空
        assert assembly_services.budget.counters("run-invalid").get("tool_call", 0) == 0
        assert get_columns_calls == []
        assert assembly_services.business_db.executed == []
        assembly_services.budget.close_run("run-invalid")
