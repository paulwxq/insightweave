"""真实模型能力用例（docs/v4/5 §19 步骤 1；requires_llm）

用临时业务工具 + 临时 submit_final_answer 跑完"工具调用 → 接收反馈 → 最终提交"，
检查思考模式设置与实际行为。正式工具与 FinalAnswerGateMiddleware 在步骤 4 交付。
"""

from __future__ import annotations

import pytest
from langchain_core.messages import ToolMessage
from langchain_core.tools import tool

pytestmark = [pytest.mark.integration, pytest.mark.requires_llm]


@tool
def count_orders(month: str) -> str:
    """临时业务工具：返回指定月份的订单行数（固定值，不连库）。

    Args:
        month: 月份，例如 "2026-09"
    """
    return f'{{"status": "ok", "data": {{"month": "{month}", "row_count": 1234}}}}'


@tool
def submit_final_answer(answer: str) -> str:
    """临时提交工具：提交本问题的最终答案（正式版见 §7.11）。"""
    return (
        f'{{"status": "ok", "data": {{"accepted": true, "answer_len": {len(answer)}}}}}'
    )


def test_tool_loop_then_final_submission(db_agent_config):
    """可用 DBDA_TEST_LLM_PROFILE 覆盖 profile（默认用配置的 qwen3_max；
    当前开发 key 无 qwen3-max 访问权时用可访问的 profile 验证同一能力链路）。"""
    import os

    from deepagents import create_deep_agent

    from src.modules.db_deepagent.agent import build_model

    override = os.environ.get("DBDA_TEST_LLM_PROFILE")
    if override:
        db_agent_config = db_agent_config.model_copy(
            update={
                "model": db_agent_config.model.model_copy(
                    update={"llm_profile": override}
                )
            }
        )
    model = build_model(db_agent_config)
    # 思考模式设置核对（§4.1）：qwen3_8_max 走 dashscope_openai/ChatOpenAI，
    # enable_thinking 经 extra_body 显式关闭；ChatTongyi 路径则在 model_kwargs
    extra_body = getattr(model, "extra_body", None) or {}
    assert not model.model_kwargs.get("enable_thinking", False)
    assert not extra_body.get("enable_thinking", False)

    agent = create_deep_agent(
        model=model,
        tools=[count_orders, submit_final_answer],
        system_prompt=(
            "你是数据库问答助手。回答用户问题必须：先用 count_orders 查询数据，"
            "收到结果后再调用 submit_final_answer 提交最终答案（answer 用一句中文，包含数字）。"
            "不要直接输出答案文本。"
        ),
    )
    result = agent.invoke(
        {"messages": [{"role": "user", "content": "2026-09 月有多少笔订单？"}]},
        config={"recursion_limit": 30},
    )
    messages = result["messages"]
    # 工具调用名取自 AIMessage.tool_calls（消息自身的 name 属性不是工具名）
    called_names = [
        tc["name"] for m in messages for tc in (getattr(m, "tool_calls", None) or [])
    ]
    tool_messages = [m for m in messages if isinstance(m, ToolMessage)]

    # 工具调用 → 接收反馈 → 最终提交 的完整链路
    assert "count_orders" in called_names, called_names
    assert tool_messages, "未收到工具反馈 ToolMessage"
    assert "submit_final_answer" in called_names, called_names

    # submit_final_answer 在 count_orders 之后（先查后提交）
    assert called_names.index("submit_final_answer") > called_names.index(
        "count_orders"
    )

    # 提交参数包含查询得到的行数 1234
    final_call = next(
        tc
        for m in messages
        for tc in (getattr(m, "tool_calls", None) or [])
        if tc["name"] == "submit_final_answer"
    )
    assert "1234" in final_call["args"]["answer"], final_call["args"]

    # 思考模式行为核对：响应不含 reasoning 内容
    for m in messages:
        assert m.additional_kwargs.get("reasoning_content") is None
