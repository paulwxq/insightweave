"""ModelCallObserver 单元测试（docs/v4/5 §11.2.1、§18.2 模型观测完整性）

归属取实例、用途取 metadata、尝试 ID 去重、用量不重复累计、未知不填零、
finalize 幂等。doc 13：主 Agent 调用的完整上下文日志（输入/输出/失败标记）。
"""

from __future__ import annotations

import logging
import re
from types import SimpleNamespace
from uuid import uuid4

import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from src.modules.db_deepagent.middleware import model_observer as observer_module
from src.modules.db_deepagent.middleware.model_observer import ModelCallObserver

pytestmark = pytest.mark.unit

_SQL = "SELECT region, SUM(amount) FROM orders GROUP BY region"


def _usage(inp: int, out: int) -> dict:
    return {"input_tokens": inp, "output_tokens": out, "total_tokens": inp + out}


def _llm_result(inp: int | None, out: int | None):
    message = SimpleNamespace(
        usage_metadata=_usage(inp, out) if inp is not None else None
    )
    generation = SimpleNamespace(message=message)
    return SimpleNamespace(generations=[[generation]], llm_output=None)


class TestObserver:
    def test_dedupe_start_events(self, tmp_path):
        """on_chat_model_start 与 on_llm_start 同一尝试只计一次。"""
        observer = ModelCallObserver("req", "run", tmp_path / "audit.jsonl")
        attempt = uuid4()
        observer.on_chat_model_start(
            {}, [], run_id=attempt, metadata={"dbda_call_kind": "agent"}
        )
        observer.on_llm_start(
            {}, [], run_id=attempt, metadata={"dbda_call_kind": "agent"}
        )
        observer.on_llm_end(_llm_result(10, 5), run_id=attempt)
        snapshot = observer.finalize()
        assert snapshot.total_model_calls == 1
        assert snapshot.input_tokens == 10 and snapshot.output_tokens == 5

    def test_summary_vs_agent_kind(self, tmp_path):
        observer = ModelCallObserver("req", "run", None)
        a1, a2 = uuid4(), uuid4()
        observer.on_chat_model_start(
            {}, [], run_id=a1, metadata={"dbda_call_kind": "agent"}
        )
        observer.on_chat_model_start(
            {}, [], run_id=a2, metadata={"dbda_call_kind": "summarization"}
        )
        observer.on_llm_end(_llm_result(7, 3), run_id=a1)
        observer.on_llm_end(_llm_result(4, 2), run_id=a2)
        snapshot = observer.finalize()
        assert snapshot.total_model_calls == 2
        assert snapshot.summary_model_calls == 1
        assert snapshot.input_tokens == 11 and snapshot.output_tokens == 5

    def test_unknown_usage_marks_incomplete(self, tmp_path):
        """任一调用用量未知 → 合计 None、完整性 False；不以零填补。"""
        observer = ModelCallObserver("req", "run", None)
        a1, a2 = uuid4(), uuid4()
        observer.on_chat_model_start(
            {}, [], run_id=a1, metadata={"dbda_call_kind": "agent"}
        )
        observer.on_chat_model_start(
            {}, [], run_id=a2, metadata={"dbda_call_kind": "agent"}
        )
        observer.on_llm_end(_llm_result(10, 4), run_id=a1)
        observer.on_llm_end(_llm_result(None, None), run_id=a2)
        snapshot = observer.finalize()
        assert snapshot.total_model_calls == 2
        assert snapshot.input_tokens is None and snapshot.output_tokens is None
        assert snapshot.token_usage_complete is False

    def test_missing_kind_marks_anomaly(self, tmp_path):
        """用途标记缺失：不能默认归为主模型，统计标记不完整。"""
        observer = ModelCallObserver("req", "run", None)
        attempt = uuid4()
        observer.on_chat_model_start({}, [], run_id=attempt, metadata={})
        observer.on_llm_end(_llm_result(5, 5), run_id=attempt)
        snapshot = observer.finalize()
        assert snapshot.total_model_calls == 1
        assert snapshot.summary_model_calls == 0
        assert snapshot.token_usage_complete is False
        assert snapshot.anomalies

    def test_error_attempt_counted_no_usage(self, tmp_path):
        observer = ModelCallObserver("req", "run", None)
        attempt = uuid4()
        observer.on_chat_model_start(
            {}, [], run_id=attempt, metadata={"dbda_call_kind": "agent"}
        )
        observer.on_llm_error(RuntimeError("boom"), run_id=attempt)
        snapshot = observer.finalize()
        assert snapshot.total_model_calls == 1
        assert snapshot.input_tokens is None

    def test_finalize_idempotent_ignores_late_callbacks(self, tmp_path):
        observer = ModelCallObserver("req", "run", None)
        first = observer.finalize()
        late = uuid4()
        observer.on_chat_model_start(
            {}, [], run_id=late, metadata={"dbda_call_kind": "agent"}
        )
        assert observer.finalize().total_model_calls == first.total_model_calls

    def test_no_calls_zero_and_complete(self, tmp_path):
        snapshot = ModelCallObserver("req", "run", None).finalize()
        assert snapshot.total_model_calls == 0
        assert snapshot.input_tokens == 0 and snapshot.token_usage_complete is True

    def test_attribution_from_instance_not_metadata(self, tmp_path):
        """摘要显式 metadata 无业务归属：快照归属仍取构造实例（§11.2.1）。"""
        observer = ModelCallObserver("req-9", "run-9", None)
        attempt = uuid4()
        observer.on_chat_model_start(
            {}, [], run_id=attempt, metadata={"dbda_call_kind": "summarization"}
        )
        observer.on_llm_end(_llm_result(3, 1), run_id=attempt)
        snapshot = observer.finalize()
        assert snapshot.request_id == "req-9" and snapshot.run_id == "run-9"
        assert snapshot.summary_model_calls == 1


class _CaptureHandler(logging.Handler):
    """把 LogRecord 消息收进外部列表（供 _DebugCapture 使用）。"""

    def __init__(self, sink: list):
        super().__init__(level=logging.DEBUG)
        self._sink = sink

    def emit(self, record: logging.LogRecord) -> None:
        self._sink.append(record.getMessage())


class _DebugCapture:
    """把 model_observer 模块日志器的 DEBUG 记录收进列表（不动全局配置）。"""

    def __init__(self):
        self.records: list[str] = []

    def __enter__(self):
        self._handler = _CaptureHandler(self.records)
        self._logger = observer_module.logger
        self._old_level = self._logger.level
        self._logger.addHandler(self._handler)
        self._logger.setLevel(logging.DEBUG)
        return self

    def __exit__(self, *exc_info):
        self._logger.removeHandler(self._handler)
        self._logger.setLevel(self._old_level)

    @property
    def text(self) -> str:
        return "\n".join(self.records)

    def attempt_ids(self, marker: str) -> list[str]:
        ids = []
        for record in self.records:
            for match in re.finditer(
                rf"{marker} request_id=\S+ run_id=\S+ attempt_id=(\S+)", record
            ):
                ids.append(match.group(1))
        return ids


def _agent_messages():
    """§5.1 的批次包装 [[System, Human, AI(工具调用), Tool]]。"""
    ai = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "search_schema",
                "args": {"query": "订单金额", "top_k": 5},
                "id": "call_abc",
                "type": "tool_call",
            }
        ],
    )
    tool = ToolMessage(
        content='{"status": "ok", "data": {"tables": ["订单表"]}}',
        tool_call_id="call_abc",
        name="search_schema",
    )
    return [
        [
            SystemMessage(content="你是 SQL 助手"),
            HumanMessage(content="上个月订单总额"),
            ai,
            tool,
        ]
    ]


def _llm_result_with_message(message):
    generation = SimpleNamespace(message=message)
    return SimpleNamespace(generations=[[generation]], llm_output=None)


class _ScriptedChatModel(BaseChatModel):
    """真实回调链路测试用：固定返回 validate_sql 工具调用；bind_tools 走通用 bind。"""

    @property
    def _llm_type(self) -> str:
        return "scripted-test"

    def bind_tools(self, tools, **kwargs):
        return self.bind(tools=tools)

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        message = AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "validate_sql",
                    "args": {"sql": _SQL, "sub_task_id": "T-1", "sub_task_revision": 1},
                    "id": "call_vs",
                    "type": "tool_call",
                }
            ],
        )
        return ChatResult(generations=[ChatGeneration(message=message)])


class TestContextLogging:
    """doc 13 §5：主 Agent 完整上下文日志。"""

    def _start_agent(
        self, observer, messages, attempt=None, *, invocation_params=None, options=None
    ):
        kwargs = {}
        if invocation_params is not None:
            kwargs["invocation_params"] = invocation_params
        if options is not None:
            kwargs["options"] = options
        observer.on_chat_model_start(
            {},
            messages,
            run_id=attempt or uuid4(),
            metadata={"dbda_call_kind": "agent"},
            **kwargs,
        )

    def test_input_block_full_messages(self):
        """§5.1：批次包装逐条展示，角色/顺序/中文/关联 ID/工具参数完整。"""
        observer = ModelCallObserver("req-ctx", "run-ctx", None)
        with _DebugCapture() as capture:
            self._start_agent(observer, _agent_messages())
        text = capture.text
        assert "===== MODEL_INPUT_BEGIN request_id=req-ctx run_id=run-ctx" in text
        assert "===== MODEL_INPUT_END request_id=req-ctx run_id=run-ctx" in text
        assert "[消息 1 / System]" in text and "你是 SQL 助手" in text
        assert "[消息 2 / Human]" in text and "上个月订单总额" in text
        assert "[消息 3 / AI / 工具调用]" in text
        assert "tool_call_id=call_abc" in text
        assert "name=search_schema" in text
        assert "订单金额" in text  # 工具参数 JSON 保留
        assert "[消息 4 / Tool]" in text and "订单表" in text

    def test_multi_batch_boundaries(self):
        """§5.1：多批次输入逐批逐条保留，批次边界可辨。"""
        observer = ModelCallObserver("req-b", "run-b", None)
        batch1 = [SystemMessage(content="S1"), HumanMessage(content="Q1")]
        batch2 = [HumanMessage(content="Q2")]
        with _DebugCapture() as capture:
            self._start_agent(observer, [batch1, batch2])
        text = capture.text
        assert "[批次 1]" in text and "[批次 2]" in text
        for piece in ("S1", "Q1", "Q2"):
            assert piece in text
        assert text.index("S1") < text.index("Q1") < text.index("Q2")

    def test_long_content_not_truncated(self):
        """§5.1：长正文不截断，头尾均在同一次日志调用中。"""
        long_text = "头部标记" + "数据行，" * 20000 + "尾部标记END"
        observer = ModelCallObserver("req-l", "run-l", None)
        with _DebugCapture() as capture:
            self._start_agent(observer, [[HumanMessage(content=long_text)]])
        text = capture.text
        assert "头部标记" in text and "尾部标记END" in text
        assert len(text) > len(long_text)

    def test_structured_content_block_kept(self):
        """§5.1：结构化内容块按 JSON 保留，不只提取字符串正文。"""
        block = {"type": "table", "table": {"name": "orders", "rows": 3}}
        observer = ModelCallObserver("req-s", "run-s", None)
        with _DebugCapture() as capture:
            self._start_agent(
                observer,
                [[HumanMessage(content=[{"type": "text", "text": "看下表"}, block])]],
            )
        text = capture.text
        assert "看下表" in text
        assert '"table"' in text and "orders" in text

    def test_tools_fields_recorded_separately(self):
        """§5.2：invocation_params 与 options 的结构化输出字段分别保留、不互代。"""
        observer = ModelCallObserver("req-t", "run-t", None)
        invocation_params = {
            "model": "test-model",
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "validate_sql",
                        "description": "校验 SQL",
                        "parameters": {"type": "object"},
                    },
                }
            ],
            "tool_choice": "auto",
            "response_format": {"type": "json_object"},
        }
        options = {
            "ls_structured_output_format": {
                "kwargs": {"method": "json_schema"},
                "schema": {"type": "object", "properties": {"sql": {"type": "string"}}},
            }
        }
        with _DebugCapture() as capture:
            self._start_agent(
                observer,
                [[HumanMessage(content="q")]],
                invocation_params=invocation_params,
                options=options,
            )
        text = capture.text
        assert "模型：test-model" in text
        assert "tools=" in text and '"validate_sql"' in text and "校验 SQL" in text
        assert "tool_choice=" in text and "auto" in text
        assert "response_format=" in text and "json_object" in text
        assert "ls_structured_output_format=" in text
        assert "json_schema" in text

    def test_optional_fields_absent_not_fabricated(self):
        """§5.2：缺失字段不推造：无 tools/tool_choice/response_format/options 段。"""
        observer = ModelCallObserver("req-n", "run-n", None)
        with _DebugCapture() as capture:
            self._start_agent(observer, [[HumanMessage(content="q")]])
        text = capture.text
        assert "MODEL_INPUT_BEGIN" in text
        assert "[Tools]" not in text
        assert "tools=" not in text
        assert "tool_choice=" not in text
        assert "response_format=" not in text
        assert "ls_structured_output_format=" not in text

    def test_output_block_shares_attempt_and_validate_sql(self):
        """§3.2：输出块与输入块共享 attempt_id，validate_sql 参数完整。"""
        observer = ModelCallObserver("req-o", "run-o", None)
        attempt = uuid4()
        output = AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "validate_sql",
                    "args": {"sql": _SQL, "sub_task_id": "T-1", "sub_task_revision": 1},
                    "id": "call_vs",
                    "type": "tool_call",
                }
            ],
        )
        with _DebugCapture() as capture:
            self._start_agent(observer, [[HumanMessage(content="q")]], attempt=attempt)
            observer.on_llm_end(_llm_result_with_message(output), run_id=attempt)
        text = capture.text
        assert "===== MODEL_OUTPUT_BEGIN request_id=req-o run_id=run-o" in text
        assert "===== MODEL_OUTPUT_END request_id=req-o run_id=run-o" in text
        assert "name=validate_sql" in text and _SQL in text
        assert capture.attempt_ids("MODEL_INPUT_BEGIN") == capture.attempt_ids(
            "MODEL_OUTPUT_BEGIN"
        )

    def test_summarization_kind_not_logged(self):
        """§5.3：摘要调用不输出完整正文。"""
        observer = ModelCallObserver("req-sum", "run-sum", None)
        attempt = uuid4()
        with _DebugCapture() as capture:
            observer.on_chat_model_start(
                {},
                [[HumanMessage(content="长历史")]],
                run_id=attempt,
                metadata={"dbda_call_kind": "summarization"},
            )
            observer.on_llm_end(
                _llm_result_with_message(AIMessage(content="摘要")), run_id=attempt
            )
        assert capture.records == []

    def test_failure_marker_keeps_input(self):
        """§3.2：失败尝试保留输入块并输出同 attempt_id 的失败标记及错误类型。"""
        observer = ModelCallObserver("req-f", "run-f", None)
        attempt = uuid4()
        with _DebugCapture() as capture:
            self._start_agent(observer, [[HumanMessage(content="q")]], attempt=attempt)
            observer.on_llm_error(RuntimeError("boom"), run_id=attempt)
        text = capture.text
        assert "MODEL_INPUT_BEGIN" in text
        assert "MODEL_OUTPUT_BEGIN" in text
        assert "RuntimeError" in text and "boom" in text
        assert capture.attempt_ids("MODEL_INPUT_BEGIN") == capture.attempt_ids(
            "MODEL_OUTPUT_BEGIN"
        )

    def test_debug_level_gate(self):
        """§5.6：logger 有效级别不允许 DEBUG 时不输出这些日志。"""
        observer = ModelCallObserver("req-g", "run-g", None)
        with _DebugCapture() as capture:  # 正控制：DEBUG 下有输出
            self._start_agent(observer, [[HumanMessage(content="q")]])
        assert capture.records, "DEBUG 级别应输出完整上下文"
        with _DebugCapture() as capture:
            observer_module.logger.setLevel(logging.INFO)
            try:
                self._start_agent(observer, [[HumanMessage(content="q2")]])
            finally:
                observer_module.logger.setLevel(logging.DEBUG)
        assert capture.records == []

    def test_real_chain_scripted_model(self):
        """§5.4：真实 LangChain 回调链路（含 _format_for_tracing 转换与绑定工具）。"""
        model = _ScriptedChatModel()
        bound = model.bind_tools(
            [
                {
                    "type": "function",
                    "function": {
                        "name": "validate_sql",
                        "description": "校验 SQL",
                        "parameters": {"type": "object"},
                    },
                }
            ]
        )
        observer = ModelCallObserver("req-rt", "run-rt", None)
        question = HumanMessage(content=[{"text": "查询上个月订单总额"}])
        with _DebugCapture() as capture:
            result = bound.invoke(
                [SystemMessage(content="你是 SQL 助手"), question],
                config={
                    "callbacks": [observer],
                    "metadata": {"dbda_call_kind": "agent"},
                },
            )
        assert result.tool_calls[0]["name"] == "validate_sql"
        text = capture.text
        # 输入块：真实回调实参（含 _format_for_tracing 补 type 的转换结果）
        assert "MODEL_INPUT_BEGIN request_id=req-rt run_id=run-rt" in text
        assert "你是 SQL 助手" in text and "查询上个月订单总额" in text
        assert '"type": "text"' in text  # 单键块被补 type：允许并验证该转换
        assert "tools=" in text and '"validate_sql"' in text
        # 输出块：validate_sql 可定位到其生成前输入（同 attempt_id）
        assert "MODEL_OUTPUT_BEGIN" in text and "name=validate_sql" in text
        assert _SQL in text
        assert capture.attempt_ids("MODEL_INPUT_BEGIN") == capture.attempt_ids(
            "MODEL_OUTPUT_BEGIN"
        )
        # §5.5：日志代码不修改原消息（SDK 转换在副本上进行）
        assert question.content == [{"text": "查询上个月订单总额"}]
        snapshot = observer.finalize()
        assert snapshot.total_model_calls == 1


class TestReferenceTimeLogging:
    """doc 20 §6.2/§8.5：注入的时间背景块进入 doc 13 完整上下文 DEBUG 日志。"""

    def test_clock_block_logged_in_model_input(self, tmp_path, caplog):
        observer = ModelCallObserver("req-rt2", "run-rt2", tmp_path / "audit.jsonl")
        clock = SystemMessage(
            content=[
                {
                    "type": "text",
                    "text": "\n\n## 本轮北京时间背景\n本轮参考日期：2026-11-16",
                }
            ]
        )
        with caplog.at_level(
            logging.DEBUG, logger="nl2sql.db_deepagent.middleware.model_observer"
        ):
            observer.on_chat_model_start(
                {},
                [clock, HumanMessage(content="今天几号")],
                run_id=uuid4(),
                metadata={"dbda_call_kind": "agent"},
            )
        assert "本轮北京时间背景" in caplog.text
        assert "本轮参考日期：2026-11-16" in caplog.text

    def test_injected_clock_reaches_model_via_real_assembly(
        self, assembly_services, monkeypatch, caplog
    ):
        """doc 20 §8.5 / R1 C1-01：注入块经真实装配链（build_db_agent +
        RequestAuditMiddleware）到达模型输入，并进入 doc 13 DEBUG 日志。

        ctx.now 取 UTC 2026-11-16 18:30（北京次日 02:30），同时钉死跨日转换。
        """
        from datetime import UTC, datetime

        from langchain_core.messages import HumanMessage
        from langgraph.checkpoint.memory import InMemorySaver

        import src.modules.db_deepagent.agent as agent_mod
        from src.modules.db_deepagent.context import DbAgentContext
        from src.tests.db_deepagent.conftest import ScriptedChatModel

        direct = AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "submit_final_answer",
                    "args": {
                        "status": "completed",
                        "answer": "大前天是 2026年11月14日（周六）。",
                        "findings": [],
                        "metric_definitions": [],
                        "limitations": [],
                        "open_questions": [],
                        "unresolved_task_refs": [],
                    },
                    "id": "call-rt-clock-1",
                    "type": "tool_call",
                }
            ],
        )
        fake = ScriptedChatModel(responses=[direct])
        monkeypatch.setattr(agent_mod, "build_model", lambda _cfg: fake)
        handle = agent_mod.build_db_agent(
            assembly_services, session_id="t-rt-clock", checkpointer=InMemorySaver()
        )
        observer = ModelCallObserver("req-rt-clock", "run-rt-clock", None)
        ctx = DbAgentContext(
            session_id="t-rt-clock",
            thread_id="th-rt-clock",
            request_id="req-rt-clock",
            run_id="run-rt-clock",
            datasource_id="pg:test",
            validation_fingerprint="fp",
            now=datetime(2026, 11, 16, 18, 30, tzinfo=UTC),
            timezone="Asia/Shanghai",
            user_texts=("请问大前天是哪一天？",),
            carried_evidence=(),
            deadline_monotonic=float("inf"),
        )
        with caplog.at_level(
            logging.DEBUG, logger="nl2sql.db_deepagent.middleware.model_observer"
        ):
            handle.agent.invoke(
                {"messages": [HumanMessage(content="请问大前天是哪一天？")]},
                config={
                    "configurable": {"thread_id": "th-rt-clock"},
                    "recursion_limit": 20,
                    "metadata": {"dbda_call_kind": "agent"},
                    "callbacks": [observer],
                },
                context=ctx,
            )
        assert "MODEL_INPUT_BEGIN" in caplog.text
        assert "本轮北京时间背景" in caplog.text
        assert "本轮参考日期：2026-11-17" in caplog.text  # UTC 18:30 → 北京次日
        # 直答一次结束：单次模型尝试（重试/提醒都会推高 cursor）
        assert getattr(fake, "_cursor", 0) == 1
        snapshot = observer.finalize()
        assert snapshot.total_model_calls == 1
