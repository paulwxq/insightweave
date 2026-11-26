"""摘要窗口实测（docs/v4/5 §18.3、§21"摘要输入范围与模型容量"；requires_llm）

用真实配置模型完成一次接近触发阈值的摘要请求：调低触发阈值强制摘要发生
（§21 允许），记录摘要请求输入规模、耗时与是否溢出重试；验证
trim_tokens_to_summarize=None（待摘要历史不预裁剪）。数字写入
var/db_deepagent/acceptance/summary_window.json 作为 §18.3 证据。

doc 5.1 §5.4：主模型走 build_model（qwen3.8-max），摘要模型走独立构造器
build_summarizer_model（qwen3.8-flash）；通过回调逐条断言两类调用的
(ls_provider, ls_model_name)，并核对两模型的 openai_api_base。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from langchain_core.callbacks import BaseCallbackHandler

pytestmark = [pytest.mark.integration, pytest.mark.requires_llm]

_TRIGGER = 1200  # 调低触发阈值：两轮内强制摘要（生产 96000 的同一路径）
_KEEP = 300


class _IdentityRecorder(BaseCallbackHandler):
    """逐条记录模型调用的用途、模型 ID 与适配器名（doc 5.1 §5.4）。

    ls_model_name / ls_provider 取自回调 metadata（本机 LangChain 版本下
    serialized 为 None）；ls_provider 是 SDK 适配器名（ChatOpenAI 固定为
    openai），不等于配置中的 provider 名，也不能区分不同兼容服务。
    """

    def __init__(self):
        self.records: list[tuple[object, object, object]] = []

    def on_chat_model_start(
        self,
        serialized,
        messages,
        *,
        run_id,
        parent_run_id=None,
        tags=None,
        metadata=None,
        **kwargs,
    ):
        md = metadata if isinstance(metadata, dict) else {}
        self.records.append(
            (
                md.get("dbda_call_kind"),
                md.get("ls_model_name"),
                md.get("ls_provider"),
            )
        )


def test_summary_near_trigger_real_model(db_agent_config, tmp_path):
    from deepagents.backends import FilesystemBackend
    from deepagents.middleware.summarization import SummarizationMiddleware
    from langchain_core.messages import HumanMessage
    from src.modules.db_deepagent.agent import build_model, build_summarizer_model
    from src.modules.db_deepagent.middleware.model_observer import ModelCallObserver
    from src.services.config_loader import get_config

    cfg = db_agent_config
    model = build_model(cfg)
    # 本脚本显式标记主模型；生产主调用的 agent 标记由 Runner config 提供。
    agent_model = model.model_copy()
    agent_model.metadata = {**(model.metadata or {}), "dbda_call_kind": "agent"}
    # doc 5.1 §5.4：摘要模型经独立构造器创建（qwen3_8_flash / qwen3.8-flash）
    summary_model = build_summarizer_model(cfg)
    backend = FilesystemBackend(root_dir=tmp_path, virtual_mode=True)
    middleware = SummarizationMiddleware(
        model=agent_model,
        summarizer=summary_model,
        backend=backend,
        trigger=("tokens", _TRIGGER),
        keep=("tokens", _KEEP),
        trim_tokens_to_summarize=cfg.summarization.trim_tokens_to_summarize,  # None
    )
    observer = ModelCallObserver("req-summary-window", "run-summary-window")
    identity = _IdentityRecorder()
    config = {
        "configurable": {"thread_id": "t-summary-window"},
        "callbacks": [observer, identity],
        "metadata": {"request_id": "req-summary-window", "dbda_call_kind": "agent"},
    }

    from deepagents import create_deep_agent
    from langgraph.checkpoint.memory import InMemorySaver

    agent = create_deep_agent(
        model=agent_model,
        tools=[],
        middleware=[middleware],
        backend=backend,
        checkpointer=InMemorySaver(),
    )
    filler = "请逐字记住以下业务口径：" + "订单状态口径说明。" * 90  # ~2k tokens
    started = time.monotonic()
    summarized = False
    for i in range(4):
        agent.invoke(
            {"messages": [HumanMessage(content=f"第{i + 1}轮。{filler}")]},
            config=config,
        )
        values = agent.get_state(config).values
        event = values.get("_summarization_event")
        if event is not None:
            summarized = True
            break
    elapsed_ms = int((time.monotonic() - started) * 1000)
    assert summarized, "四轮内未触发摘要（阈值未生效？）"

    # ---- 身份断言（doc 5.1 §5.4）：逐调用核对用途 × (适配器, 模型 ID) ----
    agent_pairs = sorted(
        {
            (provider, name)
            for kind, name, provider in identity.records
            if kind == "agent"
        }
    )
    summary_pairs = sorted(
        {
            (provider, name)
            for kind, name, provider in identity.records
            if kind == "summarization"
        }
    )
    assert agent_pairs == [("openai", "qwen3.8-max")], identity.records
    assert summary_pairs == [("openai", "qwen3.8-flash")], identity.records
    # 仅允许两类已知用途；两类都必须实际发生（集合非空由上面的相等保证）
    assert {kind for kind, _, _ in identity.records} == {"agent", "summarization"}

    # ---- 目标地址核对：两模型实际 base_url 均为现有 provider 的 compatible-mode ----
    base_url = (
        get_config()
        .get("llm_providers", {})
        .get("dashscope_openai", {})
        .get("base_url")
    )
    assert base_url, "llm_providers.dashscope_openai.base_url 未配置"
    assert agent_model.openai_api_base == base_url
    assert summary_model.openai_api_base == base_url

    snapshot = observer.finalize()
    assert snapshot.summary_model_calls >= 1, "摘要模型调用未被观测到（K22 继承失效？）"
    assert snapshot.token_usage_complete, "摘要调用用量未知，无法记录输入规模"
    assert snapshot.anomalies == [], f"存在用途异常: {snapshot.anomalies}"
    # 溢出重试：无溢出时摘要尝试数 == 观测到的摘要成功数（失败重试会多计开始次数）
    agent_model_id = agent_pairs[0][1]
    agent_ls_provider = agent_pairs[0][0]
    summary_model_id = summary_pairs[0][1]
    summary_ls_provider = summary_pairs[0][0]
    record = {
        "trigger_tokens": _TRIGGER,
        "keep_tokens": _KEEP,
        "trim_tokens_to_summarize": cfg.summarization.trim_tokens_to_summarize,
        "rounds_elapsed_ms": elapsed_ms,
        "total_model_calls": snapshot.total_model_calls,
        "summary_model_calls": snapshot.summary_model_calls,
        "input_tokens_all_calls": snapshot.input_tokens,
        "output_tokens_all_calls": snapshot.output_tokens,
        "production_trigger_tokens": cfg.summarization.summary_trigger_tokens,
        "production_keep_tokens": cfg.summarization.recent_context_tokens,
        # ---- 六项身份字段（doc 5.1 §5.4）：profile 来自配置，模型 ID 与
        # ls_provider 来自回调记录（ls_provider 为适配器名，非配置 provider）----
        "agent_provider": cfg.model.provider,
        "agent_llm_name": cfg.model.llm_name,
        "agent_model": agent_model_id,
        "agent_ls_provider": agent_ls_provider,
        "summary_provider": cfg.summarization.provider,
        "summary_llm_name": cfg.summarization.llm_name,
        "summary_model": summary_model_id,
        "summary_ls_provider": summary_ls_provider,
        "note": "低阈值强制触发验证摘要路径；生产 96000/24000 的余量=模型输入上限-触发点，"
        "主模型 qwen3.8-max 输入上限以供应商文档为准；摘要模型 qwen3.8-flash "
        "上下文 1,000,000 tokens 为模型能力，不据此调整阈值",
    }
    out = Path(cfg.workspace.root) / "acceptance" / "summary_window.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8")
    # trim=None 已显式传入（装配层另有 test_summary_context 断言配置值）
    assert cfg.summarization.trim_tokens_to_summarize is None
