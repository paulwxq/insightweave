"""HarnessProfile 注册与验证测试（docs/v4/5 §10.2；P12；§19 步骤 1）

2026-10-15 起初始 profile 为 qwen3_8_max：qwen3.8-max 仅在 compatible-mode 服务，
经 dashscope_openai provider 构造为 ChatOpenAI，匹配键 openai:qwen3.8-max（K5）。
"""

from __future__ import annotations

import pytest

from src.modules.db_deepagent.agent import (
    build_model,
    register_and_verify_harness_profile,
)
from src.modules.db_deepagent.errors import HarnessProfileError

RESOLVED_KEY = "openai:qwen3.8-max"


@pytest.fixture(scope="module")
def model(db_agent_config):
    """构造真实模型实例（不调用模型 API）。"""
    return build_model(db_agent_config)


def test_request_timeout_applied(model, db_agent_config):
    """§11.1 断言：超时在构造期生效——直接核对已构建客户端的实际超时。

    langchain-openai 1.7.x 在构造期按当时 request_timeout 构建 root_client，
    构造后改字段不会重建客户端；只查字段会掩盖“客户端仍无超时”的回归。
    """
    from src.modules.db_deepagent.agent import effective_request_timeout

    expected = db_agent_config.model.request_timeout_seconds
    client = getattr(model, "root_client", None) or getattr(
        model, "root_async_client", None
    )
    assert client is not None, "ChatOpenAI 应在构造期构建客户端"
    assert (
        client.timeout == expected
    ), f"实际客户端超时 {client.timeout!r} != 配置 {expected!r}"
    assert effective_request_timeout(model) == expected


def test_harness_profile_exact_key_hit(model, db_agent_config):
    """正确键命中：openai:qwen3.8-max 精确键注册后 general_purpose_subagent 关闭。"""
    key = register_and_verify_harness_profile(model, db_agent_config)
    assert key == RESOLVED_KEY
    # 幂等：重复注册同键不报错
    assert register_and_verify_harness_profile(model, db_agent_config) == key


def test_expected_key_mismatch_fails_init(model, db_agent_config):
    """P12 错误键场景：expected_harness_key 配错值必须初始化失败。"""
    cfg = db_agent_config.model_copy(deep=True)
    cfg.model.expected_harness_key = "tongyi:qwen3.8-max"  # 原生 provider 前缀配错
    with pytest.raises(HarnessProfileError, match="tongyi:qwen3.8-max"):
        register_and_verify_harness_profile(model, cfg)


def test_unregistered_model_has_no_gp_subagent():
    """对照实验：用未注册的模型标识，_harness_profile_for_model 未命中时
    general_purpose_subagent 为空——不能把未命中当作成功（P12）。

    注：register_harness_profile 是进程级全局注册，openai:qwen3.8-max 已被同模块
    其他用例注册，因此这里必须用一个全新标识做对照。
    """
    from deepagents.profiles.harness.harness_profiles import _harness_profile_for_model
    from langchain_community.chat_models import ChatTongyi

    unregistered = ChatTongyi(model="qwen-unregistered-contrast-model")
    resolved = _harness_profile_for_model(unregistered, None)
    assert resolved.general_purpose_subagent is None


def test_expected_key_accepts_resolved_key(model, db_agent_config):
    """expected_harness_key 与实际解析键一致时通过（显式声明路径）。"""
    cfg = db_agent_config.model_copy(deep=True)
    cfg.model.expected_harness_key = RESOLVED_KEY
    assert register_and_verify_harness_profile(model, cfg) == RESOLVED_KEY
