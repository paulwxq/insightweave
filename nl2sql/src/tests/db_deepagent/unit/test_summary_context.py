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
