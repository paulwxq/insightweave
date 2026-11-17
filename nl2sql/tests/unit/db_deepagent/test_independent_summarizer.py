"""独立摘要器的离线 SDK 对照实验；不更改生产装配，不访问模型或数据库。

固定摘要只用于验证数据流，不能据此推断模型的信息保留质量或成本。
计数器按非 SystemMessage 的字符数计数，使触发边界确定且与供应商无关。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from deepagents import create_deep_agent
from deepagents.backends import FilesystemBackend
from deepagents.middleware.summarization import SummarizationMiddleware
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    AnyMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import Field

from src.modules.db_deepagent.agent import read_summary_for_handoff
from src.modules.db_deepagent.middleware.model_observer import ModelCallObserver

pytestmark = pytest.mark.unit

_PROMPT = "SUMMARY_TASK\n{messages}"
_SUMMARY = (
    "已确认：2026年10月、Asia/Shanghai、仅支付成功订单；旧 SQL 报字段不存在，尚未修复。"
)


def _count_characters(messages: list[AnyMessage], **kwargs: Any) -> int:
    return sum(
        len(message.text)
        for message in messages
        if not isinstance(message, SystemMessage)
    )


class RecordingChatModel(BaseChatModel):
    """记录真实 SDK 发送的消息，返回固定响应和明确标注为模拟的用量。"""

    model_name: str = "offline-recording"
    calls: list[list[AnyMessage]] = Field(default_factory=list)
    reply: str = "主模型已收到上下文。"

    @property
    def _llm_type(self) -> str:
        return "offline-recording"

    def bind_tools(self, tools: Any, **kwargs: Any) -> RecordingChatModel:
        return self

    def _generate(
        self,
        messages: list[AnyMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        self.calls.append(list(messages))
        # 旧装配的模型副本也必须识别摘要任务，确保两种写法响应完全一致。
        is_summary = any(
            message.text.startswith("SUMMARY_TASK") for message in messages
        )
        response = AIMessage(
            content=_SUMMARY if is_summary else self.reply,
            usage_metadata={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
        )
        return ChatResult(generations=[ChatGeneration(message=response)])


@dataclass
class Experiment:
    """同一组历史在旧装配和候选装配之间的独立运行环境。"""

    agent: Any
    main: RecordingChatModel
    summary: RecordingChatModel
    backend: FilesystemBackend
    saver: InMemorySaver
    observer: ModelCallObserver
    config: dict[str, Any]


def _experiment(
    root: Path,
    *,
    explicit_summarizer: bool,
    trigger: tuple[str, int | float] = ("tokens", 100),
    keep: tuple[str, int] = ("messages", 2),
    trim: int | None = None,
    summarizer: Any = None,
    saver: InMemorySaver | None = None,
    summary_context_tokens: int | None = None,
) -> Experiment:
    root.mkdir(parents=True, exist_ok=True)
    main = RecordingChatModel(
        profile={"max_input_tokens": 100_000},
        metadata={"dbda_call_kind": "agent"},
    )
    summary = main.model_copy()
    summary.calls = []
    summary.metadata = {"dbda_call_kind": "summarization"}
    if summary_context_tokens is not None:
        summary.profile = {"max_input_tokens": summary_context_tokens}
    backend = FilesystemBackend(root_dir=root, virtual_mode=True)
    options: dict[str, Any] = {}
    if explicit_summarizer:
        options["summarizer"] = summary if summarizer is None else summarizer
    middleware = SummarizationMiddleware(
        model=main if explicit_summarizer else summary,
        backend=backend,
        summary_prompt=_PROMPT,
        trigger=trigger,
        keep=keep,
        trim_tokens_to_summarize=trim,
        token_counter=_count_characters,
        **options,
    )
    saver = saver if saver is not None else InMemorySaver()
    agent = create_deep_agent(
        model=main,
        tools=[],
        subagents=[],
        backend=backend,
        middleware=[middleware],
        checkpointer=saver,
    )
    observer = ModelCallObserver("offline-request", "offline-run")
    config = {
        "configurable": {"thread_id": "summary-experiment"},
        "callbacks": [observer],
        "metadata": {"dbda_call_kind": "agent"},
    }
    return Experiment(agent, main, summary, backend, saver, observer, config)


def _history() -> list[AnyMessage]:
    return [
        HumanMessage(
            content="EARLIEST：2026年10月，Asia/Shanghai，仅支付成功订单。" * 4
        ),
        AIMessage(content="已确认上述统计口径。"),
        HumanMessage(
            content="查询 public.orders 的销售额，失败的 SQL 不能当作已完成。" * 3
        ),
        AIMessage(content="旧 SQL 报字段不存在，尚未修复。"),
        HumanMessage(content="RECENT：本轮请先修复字段错误。"),
        AIMessage(content="RECENT_REPLY：接下来检查字段定义。"),
    ]


@pytest.mark.parametrize("explicit", [False, True], ids=["current", "independent"])
def test_short_conversation_does_not_call_summarizer(
    tmp_path: Path, explicit: bool
) -> None:
    """短会话不产生额外摘要请求。"""
    case = _experiment(
        tmp_path, explicit_summarizer=explicit, trigger=("tokens", 100_000)
    )
    case.agent.invoke({"messages": [HumanMessage(content="查订单量")]}, case.config)
    assert len(case.main.calls) == 1
    assert case.summary.calls == []
    assert read_summary_for_handoff(case.agent.get_state(case.config).values) is None
    assert case.observer.finalize().summary_model_calls == 0


def test_current_and_independent_receive_same_history_and_keep_same_recent_messages(
    tmp_path: Path,
) -> None:
    """只改显式分工时，摘要输入、主模型有效上下文和回调统计保持一致。"""
    cases = [
        _experiment(tmp_path / name, explicit_summarizer=explicit)
        for name, explicit in [("current", False), ("independent", True)]
    ]
    for case in cases:
        case.agent.invoke({"messages": _history()}, case.config)
        assert len(case.main.calls) == len(case.summary.calls) == 1
        summary_input = "\n".join(message.text for message in case.summary.calls[0])
        assert "EARLIEST" in summary_input
        assert "尚未修复" in summary_input
        assert "RECENT" not in summary_input
        main_input = case.main.calls[0]
        assert _SUMMARY in "\n".join(message.text for message in main_input)
        assert [message.text for message in main_input[-2:]] == [
            message.text for message in _history()[-2:]
        ]
        snapshot = case.observer.finalize()
        assert snapshot.total_model_calls == 2
        assert snapshot.summary_model_calls == 1
        assert snapshot.token_usage_complete
        assert snapshot.anomalies == []
        # 用量来自固定假响应，验证统计连通性，不是成本测量。
        assert (snapshot.input_tokens, snapshot.output_tokens) == (20, 10)
        assert case.main.metadata["dbda_call_kind"] == "agent"
        assert case.summary.metadata["dbda_call_kind"] == "summarization"
    assert [m.text for m in cases[0].summary.calls[0]] == [
        m.text for m in cases[1].summary.calls[0]
    ]
    # 历史文件指针包含随机会话 ID，不要求摘要包装文本逐字一致。
    assert read_summary_for_handoff(cases[0].agent.get_state(cases[0].config).values)


def test_fractional_trigger_uses_main_model_context_profile(tmp_path: Path) -> None:
    """100k 主窗口的 0.1% 应触发；不能误用 9M 摘要窗口的 0.1%。"""
    case = _experiment(
        tmp_path,
        explicit_summarizer=True,
        trigger=("fraction", 0.001),
        summary_context_tokens=9_000_000,
    )
    assert 100 < _count_characters(_history()) < 9000
    case.agent.invoke({"messages": _history()}, case.config)
    assert len(case.summary.calls) == 1
    assert len(case.main.calls) == 1


@pytest.mark.parametrize(
    "trim,keeps_oldest",
    [(None, True), (4000, False)],
    ids=["full-history", "trimmed-control"],
)
def test_long_history_requires_preserving_explicit_no_trim(
    tmp_path: Path, trim: int | None, keeps_oldest: bool
) -> None:
    """对照证明：取消裁剪可将早期口径交给摘要器；不证明其输出一定保留口径。"""
    case = _experiment(tmp_path, explicit_summarizer=True, trim=trim)
    history = [
        HumanMessage(content="EARLIEST_UNIQUE：仅支付成功订单。"),
        AIMessage(content="历史诊断记录。" * 1500),
        HumanMessage(content="中间一轮业务问题。"),
        AIMessage(content="中间一轮处理结果。"),
        *_history()[-2:],
    ]
    case.agent.invoke({"messages": history}, case.config)
    assert len(case.summary.calls) == 1
    supplied = "\n".join(message.text for message in case.summary.calls[0])
    assert ("EARLIEST_UNIQUE" in supplied) is keeps_oldest


def test_recent_tool_call_and_result_are_not_split(tmp_path: Path) -> None:
    """keep 边界落入工具响应时，SDK 应保留完整调用/响应对。"""
    case = _experiment(tmp_path, explicit_summarizer=True)
    tool_call = AIMessage(
        content="", tool_calls=[{"id": "sql-1", "name": "inspect_schema", "args": {}}]
    )
    tool_result = ToolMessage(
        content="TOOL_RESULT：amount 字段不存在。", tool_call_id="sql-1"
    )
    case.agent.invoke(
        {
            "messages": [
                *_history()[:4],
                tool_call,
                tool_result,
                HumanMessage(content="按最新字段修复。"),
            ]
        },
        case.config,
    )
    main_input = case.main.calls[0]
    assert any(
        isinstance(m, AIMessage) and m.tool_calls == tool_call.tool_calls
        for m in main_input
    )
    assert any(
        isinstance(m, ToolMessage) and m.tool_call_id == "sql-1" for m in main_input
    )
    assert "TOOL_RESULT" not in "\n".join(m.text for m in case.summary.calls[0])


def test_checkpoint_cold_read_supports_existing_handoff_adapter(tmp_path: Path) -> None:
    """候选摘要事件经新图实例冷读后，现有交接适配器仍可消费。"""
    case = _experiment(tmp_path, explicit_summarizer=True)
    case.agent.invoke({"messages": _history()}, case.config)
    warm = case.agent.get_state(case.config).values
    summary_text = read_summary_for_handoff(warm)
    assert summary_text and _SUMMARY in summary_text
    event = warm["_summarization_event"]
    assert event["file_path"]
    saved = case.backend.read(event["file_path"])
    assert saved.error is None
    assert saved.file_data is not None
    assert "EARLIEST" in saved.file_data["content"]
    cold = _experiment(tmp_path, explicit_summarizer=True, saver=case.saver)
    assert (
        read_summary_for_handoff(cold.agent.get_state(cold.config).values)
        == summary_text
    )
    assert cold.main.calls == cold.summary.calls == []


def test_custom_summarizer_failure_propagates_without_main_model_call(
    tmp_path: Path,
) -> None:
    """摘要失败应可见，不能伪造摘要后继续调用主模型。"""

    def fail(messages: list[AnyMessage]) -> str:
        raise RuntimeError("offline summary failure")

    case = _experiment(tmp_path, explicit_summarizer=True, summarizer=fail)
    with pytest.raises(RuntimeError, match="offline summary failure"):
        case.agent.invoke({"messages": _history()}, case.config)
    assert case.main.calls == []
    assert read_summary_for_handoff(case.agent.get_state(case.config).values) is None


def test_async_invocation_uses_independent_summarizer_and_callbacks(
    tmp_path: Path,
) -> None:
    """异步 SDK 路径也应把摘要调用和主模型调用分别观测。"""
    case = _experiment(tmp_path, explicit_summarizer=True)
    asyncio.run(case.agent.ainvoke({"messages": _history()}, case.config))
    assert len(case.main.calls) == len(case.summary.calls) == 1
    assert _SUMMARY in read_summary_for_handoff(
        case.agent.get_state(case.config).values
    )
    snapshot = case.observer.finalize()
    assert snapshot.total_model_calls == 2
    assert snapshot.summary_model_calls == 1
    assert snapshot.anomalies == []
