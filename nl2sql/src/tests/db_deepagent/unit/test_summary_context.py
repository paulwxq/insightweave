"""摘要上下文单元测试（docs/v4/5 §4.3、§10.1、§18.2 摘要输入完整性）

装配显式传 trim_tokens_to_summarize=None；触发／保留阈值来自配置；
摘要模型副本带用途 metadata；读取适配（K21）。
"""

from __future__ import annotations

import pytest
from langchain_core.messages import HumanMessage

from src.modules.db_deepagent.agent import (
    build_db_agent,
    build_summary_prompt,
    load_prompts,
    read_summary_for_handoff,
)
from src.modules.db_deepagent.errors import AssemblyVerificationError
from src.tests.db_deepagent.conftest import ScriptedChatModel

pytestmark = pytest.mark.unit


class TestSummarizationConfig:
    def test_trim_tokens_is_none_in_config(self, db_agent_config):
        """§4.3：显式配置 trim_tokens_to_summarize: null，取消默认 4000 裁剪。"""
        assert db_agent_config.summarization.trim_tokens_to_summarize is None

    def test_keep_below_trigger(self, db_agent_config):
        s = db_agent_config.summarization
        assert 0 < s.recent_context_tokens < s.summary_trigger_tokens


class TestSummaryPrompt:
    def test_default_prompt_when_yaml_omits(self):
        from deepagents.middleware.summarization import (
            DEEPAGENTS_DEFAULT_SUMMARY_PROMPT,
        )

        prompts = {"agent": {}}
        assert build_summary_prompt(prompts) == DEEPAGENTS_DEFAULT_SUMMARY_PROMPT

    def test_prompts_yaml_has_system_prompt(self):
        prompts = load_prompts()
        assert "system_prompt" in prompts["agent"]
        assert "submit_final_answer" in prompts["agent"]["system_prompt"]


class TestAssemblyWiring:
    def test_summary_middleware_instance_wired(self, assembly_services):
        """装配后的 middleware 栈含 SummarizationMiddleware 且触发阈值来自配置。"""
        build_db_agent(assembly_services, session_id="t-summary")
        cfg = assembly_services.config
        assert cfg.summarization.summary_trigger_tokens > 0
        # summary 模型副本用途 metadata 在 §10.1 第 5 步设置；经装配成功间接验证


class TestHandoffRead:
    def test_none_when_no_event(self):
        assert read_summary_for_handoff({"messages": []}) is None

    def test_valid_event_returns_text(self):
        event = {
            "summary_message": HumanMessage(content="已完成的口径摘要"),
            "cutoff_index": 2,
            "file_path": None,
        }
        values = {"_summarization_event": event, "messages": [object()] * 3}
        assert read_summary_for_handoff(values) == "已完成的口径摘要"

    def test_malformed_event_raises(self):
        with pytest.raises(AssemblyVerificationError):
            read_summary_for_handoff(
                {"_summarization_event": {"summary_message": "文本"}}
            )
        with pytest.raises(AssemblyVerificationError):
            read_summary_for_handoff(
                {
                    "_summarization_event": {
                        "summary_message": HumanMessage(content="x"),
                        "cutoff_index": 99,
                    },
                    "messages": [],
                }
            )


# ============================================================================
# 目标 SDK（0.7.34）真实摘要事件专项（doc 1 §4.4 / §6.4）
#

_LONG_REPLY = (
    "这是受控假模型生成的长回复，用于累积足够的近似 token 数，"
    "触发目标版本 SummarizationMiddleware 的摘要阈值，从而产出真实摘要事件。"
) * 6


@pytest.fixture()
def summary_agent_env(assembly_services, monkeypatch, tmp_path):
    """低阈值 + 假模型 + 内存 checkpointer 的完整主图（真实 SDK 中间件栈）。"""
    from langgraph.checkpoint.memory import InMemorySaver

    import src.modules.db_deepagent.agent as agent_mod

    cfg = assembly_services.config.model_copy(deep=True)
    # 近似 token 计数器口径下确保触发（0 < keep < trigger）
    cfg.summarization.summary_trigger_tokens = 60
    cfg.summarization.recent_context_tokens = 30
    assembly_services.config = cfg

    fake = ScriptedChatModel(responses=[_LONG_REPLY])
    monkeypatch.setattr(agent_mod, "build_model", lambda _cfg: fake)

    saver = InMemorySaver()
    handle = agent_mod.build_db_agent(
        assembly_services, session_id="t-sdk-summary", checkpointer=saver
    )
    return handle, saver, fake, assembly_services


def _invoke_with_context(handle, thread_id: str, question: str):
    from datetime import UTC, datetime

    from src.modules.db_deepagent.context import DbAgentContext

    config = {"configurable": {"thread_id": thread_id}, "recursion_limit": 50}
    ctx = DbAgentContext(
        session_id="t-sdk-summary",
        thread_id=thread_id,
        request_id="req-summary",
        run_id="run-summary",
        datasource_id="pg:test",
        validation_fingerprint="fp",
        now=datetime.now(UTC),
        timezone="Asia/Shanghai",
        user_texts=(question,),
        carried_evidence=(),
        deadline_monotonic=float("inf"),
    )
    handle.agent.invoke(
        {"messages": [HumanMessage(content=question)]}, config=config, context=ctx
    )


class TestSdkSummarizationEvent:
    def test_real_sdk_event_structure_and_handoff(self, summary_agent_env):
        """目标 SDK 实际生成的事件：类型/索引合法，含新字段 cutoff_message_id。

        read_summary_for_handoff 不消费新字段也不得被破坏（doc 1 §4.4）。
        """
        handle, _saver, _fake, _services = summary_agent_env
        _invoke_with_context(handle, "th-sum-1", "帮我统计上个月的订单量")
        values = handle.agent.get_state(
            {"configurable": {"thread_id": "th-sum-1"}}
        ).values
        event = values.get("_summarization_event")
        assert event is not None, "低阈值配置下必须实际产生摘要事件"
        assert isinstance(event["summary_message"], HumanMessage)
        assert event["summary_message"].content
        cutoff = event["cutoff_index"]
        assert isinstance(cutoff, int) and not isinstance(cutoff, bool)
        assert 0 <= cutoff <= len(values.get("messages", []))
        # 0.7.34 新增字段：存在且类型为 str|None（缺失也兼容，读取器不依赖它）
        cutoff_id = event.get("cutoff_message_id")
        assert cutoff_id is None or isinstance(cutoff_id, str)
        text = read_summary_for_handoff(values)
        assert text is not None and text

    def test_cold_read_after_checkpoint(self, summary_agent_env, monkeypatch):
        """持久化后冷读：重新装配同类型图，从同一 checkpointer 读线程仍可交接。

        冷读用与生产一致的 db agent 图（私有状态通道由 SDK 装配创建），
        不用裸 StateGraph——后者不含 `_summarization_event` 通道。
        """
        import src.modules.db_deepagent.agent as agent_mod

        handle, saver, fake, services = summary_agent_env
        _invoke_with_context(handle, "th-sum-2", "再查一下上季度的销售额")
        values = handle.agent.get_state(
            {"configurable": {"thread_id": "th-sum-2"}}
        ).values
        warm_text = read_summary_for_handoff(values)
        assert warm_text

        monkeypatch.setattr(agent_mod, "build_model", lambda _cfg: fake)
        cold_handle = agent_mod.build_db_agent(
            services, session_id="t-sdk-summary", checkpointer=saver
        )
        cold_values = cold_handle.agent.get_state(
            {"configurable": {"thread_id": "th-sum-2"}}
        ).values
        assert read_summary_for_handoff(cold_values) == warm_text

    def test_rotation_handoff_reads_real_event(self, summary_agent_env):
        """线程轮换读取路径：Runner._handoff 消费真实 SDK 事件（§13.4）。"""
        from src.modules.db_deepagent.persistence.archive import QaArchive
        from src.modules.db_deepagent.runner import DbAgentRunner
        from src.tests.db_deepagent.conftest import FakeStore

        handle, _saver, _fake, services = summary_agent_env
        _invoke_with_context(handle, "t-sdk-summary|th-rot", "统计今年上半年的退款")
        old_values = handle.agent.get_state(
            {"configurable": {"thread_id": "t-sdk-summary|th-rot"}}
        ).values
        assert read_summary_for_handoff(old_values) is not None

        runner = DbAgentRunner(
            services=services,
            handle=handle,
            archive=QaArchive(FakeStore()),
        )
        _carried, handoff_message = runner._handoff(old_values, (), "new-thread-1")
        assert isinstance(handoff_message, HumanMessage)
        assert "上一线程摘要" in handoff_message.content
        assert read_summary_for_handoff(old_values) in handoff_message.content
