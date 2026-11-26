"""跨供应商角色装配的本地 HTTP 联调（无真实供应商请求）。"""

from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace

import httpx
from langchain_core.messages import HumanMessage, ToolMessage
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI

from src.modules.db_deepagent.agent import build_model, build_summarizer_model
from src.modules.db_deepagent.services.time_resolver import build_time_resolver


@tool
def count_orders(month: str) -> str:
    """返回固定订单数。"""
    return '{"status":"ok","data":{"row_count":1234}}'


@tool
def submit_final_answer(answer: str) -> str:
    """接收最终答案。"""
    return "accepted"


def _message(content=None, tool_calls=None):
    message = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 1,
        "model": "test-model",
        "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


def _tool_call(call_id, name, arguments):
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }


def test_cross_provider_roles_and_tool_ids(db_agent_config, db_agent_context, monkeypatch):
    db_agent_context = replace(db_agent_context, user_texts=("前天的订单",))
    cfg = db_agent_config.model_copy(deep=True)
    cfg.model.provider = "glm_coding"
    cfg.model.llm_name = "glm-5.4"
    cfg.model.params = {}
    cfg.model.retry.enabled = False
    cfg.summarization.provider = "kimi_code"
    cfg.summarization.llm_name = "k3.1-pro"
    cfg.summarization.params = {}
    cfg.time_resolution.provider = "deepseek"
    cfg.time_resolution.llm_name = "deepseek-flash"
    cfg.time_resolution.params = {
        "thinking": {"type": "disabled"},
        "response_format": {"type": "json_object"},
        "max_retries": 2,
    }
    registry = {
        "llm_providers": {
            "glm_coding": {
                "adapter": "openai_chat_completions",
                "api_key": "synthetic-glm-key",
                "base_url": "https://open.bigmodel.cn/api/coding/paas/v4",
                "models": {"glm-5.4": {}},
            },
            "kimi_code": {
                "adapter": "openai_chat_completions",
                "api_key": "synthetic-kimi-key",
                "base_url": "https://api.kimi.com/coding/v1",
                "models": {"k3.1-pro": {}},
            },
            "deepseek": {
                "adapter": "openai_chat_completions",
                "api_key": "synthetic-deepseek-key",
                "base_url": "https://api.deepseek.com",
                "models": {"deepseek-flash": {}},
            },
        }
    }
    config = SimpleNamespace(
        get=lambda key, default=None: registry.get(key, default)
    )
    monkeypatch.setattr("src.services.llm_factory.get_config", lambda: config)
    captured = []
    glm_turn = 0

    def handler(request):
        nonlocal glm_turn
        body = json.loads(request.content)
        captured.append((str(request.url), body))
        if request.url.host == "open.bigmodel.cn":
            glm_turn += 1
            if glm_turn == 1:
                payload = _message(
                    tool_calls=[_tool_call("call-count", "count_orders", {"month": "2026-09"})]
                )
            elif glm_turn == 2:
                payload = _message(
                    tool_calls=[
                        _tool_call(
                            "call-submit",
                            "submit_final_answer",
                            {"answer": "共有1234笔订单"},
                        )
                    ]
                )
            else:
                payload = _message(content="done")
        elif request.url.host == "api.kimi.com":
            payload = _message(content="summary")
        else:
            payload = _message(
                content=json.dumps(
                    {
                        "status": "ok",
                        "start": "2026-11-13",
                        "end": "2026-11-14",
                        "grain": "day",
                        "interpretation": "前天",
                        "clarification_question": None,
                    }
                )
            )
        return httpx.Response(200, json=payload)

    transport = httpx.MockTransport(handler)
    real_class = ChatOpenAI

    def client_factory(**kwargs):
        kwargs["http_client"] = httpx.Client(transport=transport)
        return real_class(**kwargs)

    monkeypatch.setattr(
        "src.services.llm_factory._import_class", lambda *_args: client_factory
    )
    main_model = build_model(cfg)
    summary_model = build_summarizer_model(cfg)
    resolver = build_time_resolver(cfg)

    from deepagents import create_deep_agent

    agent = create_deep_agent(
        model=main_model,
        tools=[count_orders, submit_final_answer],
        system_prompt="先调用 count_orders，再调用 submit_final_answer。",
    )
    result = agent.invoke(
        {"messages": [HumanMessage(content="2026-09月有多少订单？")]},
        config={"recursion_limit": 10},
    )
    called = [
        call["name"]
        for message in result["messages"]
        for call in getattr(message, "tool_calls", [])
    ]
    assert called == ["count_orders", "submit_final_answer"]
    tool_ids = [
        message.tool_call_id
        for message in result["messages"]
        if isinstance(message, ToolMessage)
    ]
    assert tool_ids == ["call-count", "call-submit"]
    glm_bodies = [
        body
        for url, body in captured
        if url.startswith("https://open.bigmodel.cn")
    ]
    assert len(glm_bodies) >= 2
    second_request = glm_bodies[1]
    assert second_request["messages"][-1]["role"] == "tool"
    assert second_request["messages"][-1]["tool_call_id"] == "call-count"
    assert second_request["messages"][-2]["tool_calls"][0]["id"] == "call-count"

    assert summary_model.invoke([HumanMessage(content="summary")]).content == "summary"
    resolved = resolver.resolve(db_agent_context, source_text="前天")
    assert resolved.ok

    urls = [url for url, _ in captured]
    assert any(url.startswith("https://open.bigmodel.cn/api/coding/paas/v4/chat/completions") for url in urls)
    assert any(url.startswith("https://api.kimi.com/coding/v1/chat/completions") for url in urls)
    assert any(url.startswith("https://api.deepseek.com/chat/completions") for url in urls)
    for _, body in captured:
        if body["model"] == "glm-5.4":
            assert "temperature" not in body and "thinking" not in body
        if body["model"] == "k3.1-pro":
            assert "temperature" not in body
            assert "thinking" not in body and "enable_thinking" not in body
        if body["model"] == "deepseek-flash":
            assert body["thinking"] == {"type": "disabled"}
            assert body["response_format"] == {"type": "json_object"}
