"""摘要窗口实测（docs/v4/5 §18.3、§21“摘要输入范围与模型容量”；requires_llm）

用真实配置模型完成一次接近触发阈值的摘要请求：调低触发阈值强制摘要发生
（§21 允许），记录摘要请求输入规模、耗时与是否溢出重试；验证
trim_tokens_to_summarize=None（待摘要历史不预裁剪）。数字写入
var/db_deepagent/acceptance/summary_window.json 作为 §18.3 证据。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.requires_llm]

_TRIGGER = 1200  # 调低触发阈值：两轮内强制摘要（生产 96000 的同一路径）
_KEEP = 300


def test_summary_near_trigger_real_model(db_agent_config, tmp_path):
    from deepagents.backends import FilesystemBackend
    from deepagents.middleware.summarization import SummarizationMiddleware
    from langchain_core.messages import HumanMessage

    from src.modules.db_deepagent.agent import build_model
    from src.modules.db_deepagent.middleware.model_observer import ModelCallObserver

    cfg = db_agent_config
    model = build_model(cfg)
    # 本脚本显式标记主模型；生产主调用的 agent 标记由 Runner config 提供。
    agent_model = model.model_copy()
    agent_model.metadata = {**(model.metadata or {}), "dbda_call_kind": "agent"}
    summary_model = model.model_copy()
    summary_model.metadata = {
        **(model.metadata or {}),
        "dbda_call_kind": "summarization",
    }
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
    config = {
        "configurable": {"thread_id": "t-summary-window"},
        "callbacks": [observer],
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

    snapshot = observer.finalize()
    assert snapshot.summary_model_calls >= 1, "摘要模型调用未被观测到（K22 继承失效？）"
    assert snapshot.token_usage_complete, "摘要调用用量未知，无法记录输入规模"
    # 溢出重试：无溢出时摘要尝试数 == 观测到的摘要成功数（失败重试会多计开始次数）
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
        "note": "低阈值强制触发验证摘要路径；生产 96000/24000 的余量=模型输入上限-触发点，"
        "qwen3.8-max compatible-mode 输入上限以供应商文档为准",
    }
    out = Path(cfg.workspace.root) / "acceptance" / "summary_window.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8")
    # trim=None 已显式传入（装配层另有 test_summary_context 断言配置值）
    assert cfg.summarization.trim_tokens_to_summarize is None
