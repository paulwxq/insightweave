"""SQL 生成节点日志埋点测试"""

from unittest.mock import MagicMock, patch

import pytest


def _mock_get_llm_return(content: str = "SELECT 1"):
    mock_response = MagicMock()
    mock_response.content = content

    mock_llm = MagicMock()
    mock_llm.invoke.return_value = mock_response

    mock_meta = MagicMock()
    mock_meta.llm = mock_llm
    mock_meta.provider = "dashscope_openai"
    mock_meta.model = "qwen3.5-plus"
    return mock_meta


def test_generate_emits_timing_logs(caplog):
    from src.modules.sql_generation.subgraph.nodes.sql_generation import (
        SQLGenerationAgent,
    )

    with patch(
        "src.modules.sql_generation.subgraph.nodes.sql_generation.get_llm"
    ) as mock_get_llm:
        mock_get_llm.return_value = _mock_get_llm_return("SELECT 1")
        agent = SQLGenerationAgent(
            {
                "provider": "dashscope_openai",
                "llm_name": "qwen3.5-plus",
                "params": {"temperature": 0, "max_tokens": 2000},
            }
        )
        agent._build_prompt = MagicMock(return_value="问题：测试 SQL 生成")

        with caplog.at_level("DEBUG", logger="nl2sql.sql_generation"):
            sql = agent.generate(
                query="测试问题",
                schema_context={},
                query_id="test-sql-gen-log",
            )

    assert sql == "SELECT 1"
    log_text = "\n".join(caplog.messages)
    assert "SQL Generation prompt 构造完成" in log_text
    assert "SQL Generation messages 构造完成" in log_text
    assert "SQL Generation 即将调用 LLM" in log_text
    assert "SQL Generation LLM 调用返回" in log_text


def test_legacy_flat_model_param_is_rejected():
    from src.modules.sql_generation.subgraph.nodes.sql_generation import (
        SQLGenerationAgent,
    )

    with pytest.raises(ValueError, match="sql_generation.*top_p.*params"):
        SQLGenerationAgent(
            {
                "provider": "dashscope_openai",
                "llm_name": "qwen3.5-plus",
                "params": {"temperature": 0, "max_tokens": 2000},
                "top_p": 0.9,
            }
        )
