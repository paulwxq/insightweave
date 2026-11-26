"""独立摘要模型构造器单元测试（全部离线）。"""

from __future__ import annotations

import pytest

from src.modules.db_deepagent.config import DbAgentConfig
from src.services.llm_factory import LLMWithMeta

pytestmark = pytest.mark.unit


class _FakeLLM:
    def __init__(self, metadata: dict | None = None):
        self.metadata = dict(metadata or {})


@pytest.fixture()
def flash_cfg(db_agent_config: DbAgentConfig) -> DbAgentConfig:
    cfg = db_agent_config.model_copy(deep=True)
    assert cfg.summarization.llm_name == "qwen3.8-flash"
    assert cfg.model.llm_name == "qwen3.8-max"
    assert cfg.model.params
    return cfg


def _install_fake_factory(monkeypatch, llm, adapter="dashscope_openai"):
    import src.services.llm_factory as llm_factory_mod

    calls = []

    def fake_get_llm(provider, llm_name, *, params=None, request_timeout_seconds=None):
        if request_timeout_seconds is not None and params and "timeout" in params:
            raise ValueError("params.timeout 与 request_timeout_seconds 冲突")
        calls.append((provider, llm_name, dict(params or {}), request_timeout_seconds))
        return LLMWithMeta(llm=llm, provider=provider, model=llm_name)

    monkeypatch.setattr(llm_factory_mod, "get_llm", fake_get_llm)
    monkeypatch.setattr(
        "src.modules.db_deepagent.agent.resolve_llm_config",
        lambda *_args: ({"adapter": adapter}, {}),
    )
    return calls


class TestIndependentConstruction:
    def test_builds_flash_without_main_params(self, flash_cfg, monkeypatch):
        import src.modules.db_deepagent.agent as agent_mod

        fake_llm = _FakeLLM()
        calls = _install_fake_factory(monkeypatch, fake_llm)
        summary_model = agent_mod.build_summarizer_model(flash_cfg)
        assert calls == [("dashscope_openai", "qwen3.8-flash", {}, 120)]
        assert summary_model is fake_llm


class TestOverridePriority:
    def test_summary_params_and_timeout_conflict(self, flash_cfg, monkeypatch):
        import src.modules.db_deepagent.agent as agent_mod

        fake_llm = _FakeLLM()
        calls = _install_fake_factory(monkeypatch, fake_llm)
        cfg = flash_cfg.model_copy(deep=True)
        cfg.summarization.params = {"max_tokens": 4096}
        agent_mod.build_summarizer_model(cfg)
        assert calls == [
            ("dashscope_openai", "qwen3.8-flash", {"max_tokens": 4096}, 120)
        ]

        cfg_explicit = flash_cfg.model_copy(deep=True)
        cfg_explicit.summarization.params = {"timeout": 90}
        with pytest.raises(ValueError, match="params.timeout"):
            agent_mod.build_summarizer_model(cfg_explicit)


class TestMetadataAndErrors:
    def test_metadata_merges_call_kind(self, flash_cfg, monkeypatch):
        import src.modules.db_deepagent.agent as agent_mod

        fake_llm = _FakeLLM(metadata={"ls_provider": "openai", "sdk_version": "0.7.34"})
        _install_fake_factory(monkeypatch, fake_llm)
        summary_model = agent_mod.build_summarizer_model(flash_cfg)
        assert summary_model.metadata == {
            "ls_provider": "openai",
            "sdk_version": "0.7.34",
            "dbda_call_kind": "summarization",
        }

    def test_factory_error_propagates(self, flash_cfg, monkeypatch):
        import src.modules.db_deepagent.agent as agent_mod
        import src.services.llm_factory as llm_factory_mod

        calls = []

        def raising_get_llm(provider, llm_name, **_kwargs):
            calls.append((provider, llm_name))
            raise ValueError("模型参数不合法")

        monkeypatch.setattr(llm_factory_mod, "get_llm", raising_get_llm)
        monkeypatch.setattr(
            "src.modules.db_deepagent.agent.resolve_llm_config",
            lambda *_args: ({"adapter": "dashscope_openai"}, {}),
        )
        with pytest.raises(ValueError, match="summarization: 模型参数不合法"):
            agent_mod.build_summarizer_model(flash_cfg)
        assert calls == [("dashscope_openai", "qwen3.8-flash")]


class TestNativeDashScopeBranch:
    def test_post_construction_timeout(self, flash_cfg, monkeypatch):
        import src.modules.db_deepagent.agent as agent_mod

        fake_llm = _FakeLLM()
        calls = _install_fake_factory(monkeypatch, fake_llm, adapter="dashscope_native")
        agent_mod.build_summarizer_model(flash_cfg)
        assert calls == [("dashscope_openai", "qwen3.8-flash", {}, None)]
        assert fake_llm.request_timeout == 120
        assert fake_llm.metadata["dbda_call_kind"] == "summarization"
