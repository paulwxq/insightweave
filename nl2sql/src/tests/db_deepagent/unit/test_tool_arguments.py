"""工具参数契约单元测试（docs/v4/5 §7.1、§18.2）

全部 9 件自定义工具：可见 Schema 只含业务字段、不含 runtime／min_score；
额外字段由框架拒绝（invalid_argument 文本，业务函数不执行）。
"""

from __future__ import annotations

import pytest
from langgraph.prebuilt import ToolNode

from src.modules.db_deepagent.tools import build_business_tools
from src.modules.db_deepagent.tools.final_answer import make_submit_final_answer_tool

from src.tests.db_deepagent.conftest import invoke_tool_node

pytestmark = pytest.mark.unit


@pytest.fixture()
def all_tools(guard_services) -> list:
    tools = list(build_business_tools(guard_services))
    tools.append(make_submit_final_answer_tool())
    return tools


class TestVisibleSchemas:
    def test_no_runtime_or_min_score_in_any_schema(self, all_tools):
        for tool in all_tools:
            schema = tool.tool_call_schema.model_json_schema()
            props = schema.get("properties") or {}
            assert "runtime" not in props, tool.name
            assert "min_score" not in props, tool.name
            assert props, f"{tool.name} 可见 Schema 不能为空"

    def test_extra_fields_rejected_by_framework(self, all_tools, db_agent_context):
        """额外字段 → invalid_argument 文本 ToolMessage；业务函数不执行（§7.1）。

        额外字段在框架层先于业务函数被拒（缺必填项同样汇总为 invalid_argument）。
        """
        node = ToolNode(all_tools)
        for tool in all_tools:
            _u, msg = invoke_tool_node(
                node,
                [(tool.name, {"unexpected_field": 1}, f"tc-{tool.name}")],
                db_agent_context,
            )
            assert msg.content.startswith("invalid_argument"), tool.name
            assert "unexpected_field" in msg.content, tool.name
