"""result_tools 单元测试（docs/v4/5 §7.9/§7.10）

read_result 分页字段；analyze_result 本地路径的 AnalysisRecord 登记
（evidence_id、input/output refs、executed_sql=None、Command 合并）。
"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest
from langgraph.prebuilt import ToolNode

from src.modules.db_deepagent.context import DbAgentContext
from src.modules.db_deepagent.services import DbAgentServices
from src.modules.db_deepagent.tools.result_tools import (
    make_analyze_result_tool,
    make_read_result_tool,
)

from src.tests.db_deepagent.conftest import invoke_tool_node

pytestmark = pytest.mark.unit


@pytest.fixture()
def node(guard_services: DbAgentServices) -> ToolNode:
    return ToolNode(
        [
            make_read_result_tool(guard_services),
            make_analyze_result_tool(guard_services),
        ]
    )


def _save_input(services: DbAgentServices, ctx: DbAgentContext, *, datasource_id=None):
    return services.results.save(
        ctx=ctx,
        purpose="query",
        sql_template="SELECT k, x FROM t",
        executed_sql="SELECT k, x FROM t",
        columns=[
            {"name": "k", "pg_type": "int4"},
            {"name": "x", "pg_type": "numeric"},
        ],
        rows=[[1, Decimal("1.5")], [1, Decimal("2.5")], [2, None]],
        source_truncated=False,
        derived_from=[],
        guard_evidence={"time": [], "dim": []},
        datasource_id=datasource_id,
    )


class TestReadResult:
    def test_pagination_fields(self, node, guard_services, db_agent_context):
        meta = _save_input(guard_services, db_agent_context)
        _u, msg = invoke_tool_node(
            node,
            [
                (
                    "read_result",
                    {"result_ref": meta.result_ref, "offset": 0, "limit": 2},
                    "tc-r1",
                )
            ],
            db_agent_context,
        )
        payload = json.loads(msg.content)
        assert payload["status"] == "ok"
        data = payload["data"]
        assert data["saved_row_count"] == 3
        assert data["next_offset"] == 2 and data["preview_truncated"] is True
        assert data["source_truncated"] is False
        assert data["rows"][0] == [1, {"$dec": "1.5"}]  # §6.8 编码

    def test_session_denied(self, node, guard_services, db_agent_context):
        from dataclasses import replace

        meta = _save_input(guard_services, db_agent_context)
        other = replace(db_agent_context, session_id="s-other")
        _u, msg = invoke_tool_node(
            node, [("read_result", {"result_ref": meta.result_ref}, "tc-r2")], other
        )
        payload = json.loads(msg.content)
        assert payload["error"]["error_type"] == "result_access_denied"


class TestAnalyzeResult:
    def test_local_aggregate_registers_analysis(
        self, node, guard_services, db_agent_context
    ):
        meta = _save_input(guard_services, db_agent_context)
        updates, msg = invoke_tool_node(
            node,
            [
                (
                    "analyze_result",
                    {
                        "operation": "aggregate",
                        "inputs": [meta.result_ref],
                        "params": {
                            "group_by": ["k"],
                            "metrics": [{"column": "x", "func": "sum"}],
                        },
                        "allow_pushdown": True,
                    },
                    "tc-a1",
                )
            ],
            db_agent_context,
        )
        payload = json.loads(msg.content)
        assert payload["status"] == "ok", payload
        record = updates["db_analyses"][0]
        assert record["evidence_id"].startswith("A-")
        assert record["executed_sql"] is None  # 本地计算不伪造 SQL
        assert record["input_result_refs"] == [meta.result_ref]
        assert record["output_result_ref"]
        assert record["pushdown_execution_id"] is None
        assert payload["data"]["evidence_id"] == record["evidence_id"]

    def test_invalid_params_reported(self, node, guard_services, db_agent_context):
        meta = _save_input(guard_services, db_agent_context)
        _u, msg = invoke_tool_node(
            node,
            [
                (
                    "analyze_result",
                    {
                        "operation": "aggregate",
                        "inputs": [meta.result_ref],
                        "params": {"group_by": ["k"]},  # 缺 metrics
                        "allow_pushdown": True,
                    },
                    "tc-a2",
                )
            ],
            db_agent_context,
        )
        payload = json.loads(msg.content)
        assert payload["error"]["error_type"] == "invalid_argument"

    def test_stale_input_ref_returns_error_envelope(
        self, node, guard_services, db_agent_context
    ):
        """失效输入引用返回结构化错误包（Agent 可换引用重试），不抛异常。"""
        _u, msg = invoke_tool_node(
            node,
            [
                (
                    "analyze_result",
                    {
                        "operation": "aggregate",
                        "inputs": ["res_" + "0" * 32],  # 不存在
                        "params": {
                            "group_by": [],
                            "metrics": [{"column": "x", "func": "sum"}],
                        },
                        "allow_pushdown": True,
                    },
                    "tc-a3",
                )
            ],
            db_agent_context,
        )
        payload = json.loads(msg.content)
        assert payload["status"] == "error", payload
        assert payload["error"]["error_type"] == "result_not_found"
        assert payload["error"]["retryable"] is False

    def test_distinct_truncated_flags_persisted(
        self, node, guard_services, db_agent_context
    ):
        """distinct_values 超 max_values：结果文件、分析记录与返回都带截断标记。

        修复前固定 source_truncated=False，后续 count 会把截断清单当完整集合。
        """
        meta = _save_input(guard_services, db_agent_context)
        updates, msg = invoke_tool_node(
            node,
            [
                (
                    "analyze_result",
                    {
                        "operation": "distinct_values",
                        "inputs": [meta.result_ref],
                        "params": {"column": "k", "max_values": 1},
                        "allow_pushdown": True,
                    },
                    "tc-d1",
                )
            ],
            db_agent_context,
        )
        payload = json.loads(msg.content)
        assert payload["status"] == "ok", payload
        assert payload["data"]["source_truncated"] is True
        record = updates["db_analyses"][0]
        assert record["source_truncated"] is True
        saved = guard_services.results.load_meta(
            record["output_result_ref"], session_id=db_agent_context.session_id
        )
        assert saved.source_truncated is True
        assert saved.saved_row_count == 1

    def test_local_output_keeps_input_datasource(
        self, node, guard_services, db_agent_context
    ):
        """分析其他数据源的历史结果：本地输出沿用输入数据源（不改写为当前）。"""
        meta = _save_input(guard_services, db_agent_context, datasource_id="ds-history")
        _updates, msg = invoke_tool_node(
            node,
            [
                (
                    "analyze_result",
                    {
                        "operation": "aggregate",
                        "inputs": [meta.result_ref],
                        "params": {
                            "group_by": [],
                            "metrics": [{"column": "x", "func": "sum"}],
                        },
                        "allow_pushdown": True,
                    },
                    "tc-d2",
                )
            ],
            db_agent_context,
        )
        payload = json.loads(msg.content)
        assert payload["status"] == "ok", payload
        saved = guard_services.results.load_meta(
            payload["data"]["result_ref"], session_id=db_agent_context.session_id
        )
        assert saved.datasource_id == "ds-history"

    def test_aggregate_output_types_preserved(
        self, node, guard_services, db_agent_context
    ):
        """数值分组键输出保留 int4（修复前被标 text）。"""
        meta = _save_input(guard_services, db_agent_context)
        _updates, msg = invoke_tool_node(
            node,
            [
                (
                    "analyze_result",
                    {
                        "operation": "aggregate",
                        "inputs": [meta.result_ref],
                        "params": {
                            "group_by": ["k"],
                            "metrics": [{"column": "x", "func": "sum"}],
                        },
                        "allow_pushdown": True,
                    },
                    "tc-d3",
                )
            ],
            db_agent_context,
        )
        payload = json.loads(msg.content)
        assert payload["status"] == "ok", payload
        saved = guard_services.results.load_meta(
            payload["data"]["result_ref"], session_id=db_agent_context.session_id
        )
        types = {c["name"]: c["pg_type"] for c in saved.columns}
        assert types["k"] == "int4"
