"""独立摘要模型构造器单元测试（doc 5.1 §4.2 / §5.2）

不使用 assembly_services（该 fixture 会替换被测函数 build_summarizer_model）；
此处用假 get_llm() 与假全局 llm_profiles，核对独立构造、参数优先级、
metadata 合并、异常透传与原生 DashScope 分支。全部离线，不访问网络。
"""

from __future__ import annotations

import pytest

from src.modules.db_deepagent.config import DbAgentConfig
from src.services.llm_factory import LLMWithMeta

pytestmark = pytest.mark.unit

# 假全局注册表：默认摘要 profile 走 dashscope_openai 兼容路径（与真实 YAML 一致）
_COMPAT_PROFILES = {
    "qwen3_8_flash": {"provider": "dashscope_openai", "model": "qwen3.8-flash"},
}


class _FakeLLM:
    """工厂返回的假模型：metadata 可读写，支持 request_timeout 属性赋值"""

    def __init__(self, metadata: dict | None = None):
        self.metadata = dict(metadata or {})


@pytest.fixture()
def flash_cfg(db_agent_config: DbAgentConfig) -> DbAgentConfig:
    """摘要指向 qwen3_8_flash、主模型带 overrides 的真实配置副本。"""
    cfg = db_agent_config.model_copy(deep=True)
    assert cfg.summarization.llm_profile == "qwen3_8_flash"
    assert cfg.model.llm_profile == "qwen3_8_max"
    # 主模型确实带 overrides，才能验证摘要不继承
    assert cfg.model.overrides
    return cfg


def _install_fake_factory(monkeypatch, llm, profiles=None):
    """替换 get_llm / get_config 并记录工厂调用；返回调用记录列表。"""
    import src.services.config_loader as config_loader_mod
    import src.services.llm_factory as llm_factory_mod

    calls: list[tuple[str, dict]] = []

    def fake_get_llm(profile_name, **overrides):
        calls.append((profile_name, dict(overrides)))
        return LLMWithMeta(llm=llm, provider="dashscope_openai", model="qwen3.8-flash")

    monkeypatch.setattr(llm_factory_mod, "get_llm", fake_get_llm)
    monkeypatch.setattr(
        config_loader_mod, "get_config", lambda: {"llm_profiles": profiles}
    )
    return calls


class TestIndependentConstruction:
    def test_builds_from_flash_profile_without_main_overrides(
        self, flash_cfg, monkeypatch
    ):
        """工厂收到 qwen3_8_flash 且仅摘要侧参数：不继承主模型 overrides。"""
        import src.modules.db_deepagent.agent as agent_mod

        fake_llm = _FakeLLM()
        calls = _install_fake_factory(monkeypatch, fake_llm, _COMPAT_PROFILES)

        summary_model = agent_mod.build_summarizer_model(flash_cfg)

        assert calls == [("qwen3_8_flash", {"timeout": 120})]
        assert summary_model is fake_llm


class TestOverridePriority:
    def test_summary_overrides_effective_and_timeout_priority(
        self, flash_cfg, monkeypatch
    ):
        """摘要 overrides 生效；显式 timeout 高于 request_timeout_seconds。"""
        import src.modules.db_deepagent.agent as agent_mod

        fake_llm = _FakeLLM()
        calls = _install_fake_factory(monkeypatch, fake_llm, _COMPAT_PROFILES)

        cfg = flash_cfg.model_copy(deep=True)
        cfg.summarization.overrides = {"max_tokens": 4096}
        agent_mod.build_summarizer_model(cfg)
        # 兼容 provider：超时在构造时传入（映射 request_timeout）
        assert calls == [("qwen3_8_flash", {"max_tokens": 4096, "timeout": 120})]

        cfg_explicit = flash_cfg.model_copy(deep=True)
        cfg_explicit.summarization.overrides = {"timeout": 90}
        agent_mod.build_summarizer_model(cfg_explicit)
        assert calls[-1] == ("qwen3_8_flash", {"timeout": 90})


class TestMetadataAndErrors:
    def test_metadata_merges_call_kind_over_factory_metadata(
        self, flash_cfg, monkeypatch
    ):
        """保留工厂返回模型的原 metadata（含 SDK 版本字段）并追加摘要用途。"""
        import src.modules.db_deepagent.agent as agent_mod

        fake_llm = _FakeLLM(metadata={"ls_provider": "openai", "sdk_version": "0.7.34"})
        _install_fake_factory(monkeypatch, fake_llm, _COMPAT_PROFILES)

        summary_model = agent_mod.build_summarizer_model(flash_cfg)

        assert summary_model.metadata == {
            "ls_provider": "openai",
            "sdk_version": "0.7.34",
            "dbda_call_kind": "summarization",
        }

    def test_factory_error_propagates_without_fallback(self, flash_cfg, monkeypatch):
        """工厂错误原样透传：不重试、不回退主模型 profile。"""
        import src.modules.db_deepagent.agent as agent_mod
        import src.services.config_loader as config_loader_mod
        import src.services.llm_factory as llm_factory_mod

        calls: list[str] = []

        def raising_get_llm(profile_name, **overrides):
            calls.append(profile_name)
            raise ValueError("profile 参数不合法")

        monkeypatch.setattr(llm_factory_mod, "get_llm", raising_get_llm)
        monkeypatch.setattr(
            config_loader_mod, "get_config", lambda: {"llm_profiles": _COMPAT_PROFILES}
        )

        with pytest.raises(ValueError, match="profile 参数不合法"):
            agent_mod.build_summarizer_model(flash_cfg)
        assert calls == ["qwen3_8_flash"]


class TestNativeDashScopeBranch:
    def test_no_timeout_param_and_post_construction_timeout(
        self, flash_cfg, monkeypatch
    ):
        """原生 dashscope：不向 get_llm 传 timeout，构造后 apply_request_timeout。"""
        import src.modules.db_deepagent.agent as agent_mod

        fake_llm = _FakeLLM()
        native_profiles = {
            "qwen3_8_flash": {"provider": "dashscope", "model": "qwen3.8-flash"},
        }
        calls = _install_fake_factory(monkeypatch, fake_llm, native_profiles)

        agent_mod.build_summarizer_model(flash_cfg)

        assert calls == [("qwen3_8_flash", {})]
        assert fake_llm.request_timeout == 120
        assert fake_llm.metadata["dbda_call_kind"] == "summarization"
