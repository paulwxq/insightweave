"""证据工具单元测试（docs/v4/5 §7.5、§7.6、§7.12）

resolve_time_range / probe_dimension_values / reuse_evidence 经 ToolNode
（含 runtime 注入）调用；服务栈为假实现 + 临时工作区。
"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest
from langgraph.prebuilt import ToolNode

from src.modules.db_deepagent.context import DbAgentContext
from src.modules.db_deepagent.services import DbAgentServices
from src.modules.db_deepagent.tools.guard_tools import (
    make_probe_dimension_values_tool,
    make_resolve_time_range_tool,
    make_reuse_evidence_tool,
)

from src.tests.db_deepagent.conftest import (
    invoke_tool_node,
    make_dim_evidence,
    make_time_evidence,
)

pytestmark = pytest.mark.unit


@pytest.fixture()
def node(guard_services: DbAgentServices) -> ToolNode:
    return ToolNode(
        [
            make_resolve_time_range_tool(guard_services),
            make_probe_dimension_values_tool(guard_services),
            make_reuse_evidence_tool(guard_services),
        ]
    )


def _ctx(db_agent_context: DbAgentContext, texts: tuple[str, ...]) -> DbAgentContext:
    return replace(db_agent_context, user_texts=texts)


# ============================================================================
# resolve_time_range
# ============================================================================


class TestResolveTimeRange:
    def test_ok_writes_evidence(self, node, db_agent_context):
        ctx = _ctx(db_agent_context, ("统计2026年9月的订单",))
        updates, msg = invoke_tool_node(
            node,
            [
                (
                    "resolve_time_range",
                    {
                        "source_text": "2026年9月",
                        "kind": "calendar",
                        "year": 2026,
                        "month": 9,
                    },
                    "tc-r1",
                )
            ],
            ctx,
        )
        payload = json.loads(msg.content)
        assert payload["status"] == "ok"
        assert payload["data"]["start"] == "2026-09-01"
        assert payload["data"]["end"] == "2026-10-01"
        assert (
            payload["data"]["sql_hint"]
            == "<时间列> >= '2026-09-01' AND <时间列> < '2026-10-01'"
        )
        ev = updates["db_time_evidence"][0]
        assert ev["time_evidence_id"] == payload["data"]["time_evidence_id"]
        assert ev["run_id"] == ctx.run_id

    def test_single_side_hint(self, node, db_agent_context):
        ctx = _ctx(db_agent_context, ("2026年10月1日之前，不含当天的订单",))
        updates, msg = invoke_tool_node(
            node,
            [
                (
                    "resolve_time_range",
                    {
                        "source_text": "2026年10月1日之前，不含当天",
                        "kind": "open_range",
                        "boundary_date": "2026-10-01",
                        "relation": "before",
                    },
                    "tc-r2",
                )
            ],
            ctx,
        )
        payload = json.loads(msg.content)
        assert payload["data"]["sql_hint"] == "<时间列> < '2026-10-01'"

    def test_text_not_in_question(self, node, db_agent_context):
        """原文核对失败 → 错误包，不写证据。"""
        ctx = _ctx(db_agent_context, ("统计订单",))
        updates, msg = invoke_tool_node(
            node,
            [
                (
                    "resolve_time_range",
                    {
                        "source_text": "昨天",
                        "kind": "relative",
                        "relative": "yesterday",
                    },
                    "tc-r3",
                )
            ],
            ctx,
        )
        payload = json.loads(msg.content)
        assert payload["status"] == "error"
        assert payload["error"]["error_type"] == "time_text_not_in_question"
        assert "db_time_evidence" not in updates


# ============================================================================
# probe_dimension_values
# ============================================================================


class TestProbeDimensionValues:
    def test_exact_hit_registers_dim_evidence(self, node, db_agent_context):
        updates, msg = invoke_tool_node(
            node,
            [
                (
                    "probe_dimension_values",
                    {
                        "table_id": "public.orders",
                        "column": "status",
                        "mode": "exact",
                        "value_text": "已支付",
                    },
                    "tc-p1",
                )
            ],
            db_agent_context,
        )
        payload = json.loads(msg.content)
        assert payload["status"] == "ok", payload
        assert payload["data"]["values"] == ["已支付"]
        assert not payload["data"]["values_truncated"]
        assert payload["data"]["dim_evidence_id"].startswith("D-")
        dim = updates["db_dim_evidence"][-1]
        assert dim["source"] == "db_probe"
        assert dim["values"] == ["已支付"]
        assert dim["ref"] == payload["data"]["evidence_id"]
        # 执行记录也随同一 Command 落账
        assert updates["db_executions"][0]["purpose"] == "probe"

    def test_distinct_truncated_warning(
        self, pipeline_stack, guard_services, db_agent_context
    ):
        """3 个不同值、limit=2 → 返回 2 值 + values_truncated + warning。"""
        _pipeline, db, *_ = pipeline_stack
        db.probe_rows = [("微信",), ("支付宝",), ("现金",)]
        node2 = ToolNode([make_probe_dimension_values_tool(guard_services)])
        _u, msg = invoke_tool_node(
            node2,
            [
                (
                    "probe_dimension_values",
                    {
                        "table_id": "public.orders",
                        "column": "status",
                        "mode": "distinct",
                        "limit": 2,
                    },
                    "tc-p2",
                )
            ],
            db_agent_context,
        )
        payload = json.loads(msg.content)
        assert payload["status"] == "ok"
        assert payload["data"]["values"] == ["微信", "支付宝"]
        assert payload["data"]["values_truncated"] is True
        assert any("只有列出的值有证据" in w for w in payload["warnings"])

    def test_empty_no_evidence(self, pipeline_stack, guard_services, db_agent_context):
        _pipeline, db, *_ = pipeline_stack
        db.probe_rows = []
        node2 = ToolNode([make_probe_dimension_values_tool(guard_services)])
        updates, msg = invoke_tool_node(
            node2,
            [
                (
                    "probe_dimension_values",
                    {
                        "table_id": "public.orders",
                        "column": "status",
                        "mode": "exact",
                        "value_text": "不存在值",
                    },
                    "tc-p3",
                )
            ],
            db_agent_context,
        )
        payload = json.loads(msg.content)
        assert payload["status"] == "empty"
        assert "不要放宽条件" in payload["error"]["message"]
        assert "db_dim_evidence" not in updates

    def test_column_not_found(self, node, db_agent_context):
        _u, msg = invoke_tool_node(
            node,
            [
                (
                    "probe_dimension_values",
                    {"table_id": "public.orders", "column": "nope", "mode": "distinct"},
                    "tc-p4",
                )
            ],
            db_agent_context,
        )
        payload = json.loads(msg.content)
        assert payload["status"] == "error"
        assert payload["error"]["error_type"] == "column_not_found"

    def test_non_text_column_rejected(self, node, db_agent_context):
        _u, msg = invoke_tool_node(
            node,
            [
                (
                    "probe_dimension_values",
                    {"table_id": "public.orders", "column": "id", "mode": "distinct"},
                    "tc-p5",
                )
            ],
            db_agent_context,
        )
        payload = json.loads(msg.content)
        assert payload["status"] == "error"
        assert payload["error"]["error_type"] == "invalid_argument"

    def test_bad_table_id(self, node, db_agent_context):
        _u, msg = invoke_tool_node(
            node,
            [
                (
                    "probe_dimension_values",
                    {"table_id": "orders", "column": "status", "mode": "distinct"},
                    "tc-p6",
                )
            ],
            db_agent_context,
        )
        payload = json.loads(msg.content)
        assert payload["error"]["error_type"] == "invalid_argument"


# ============================================================================
# reuse_evidence
# ============================================================================


class TestReuseEvidence:
    def test_reuse_ledger_time_evidence(self, node, db_agent_context):
        """沿用早前运行的时间证据：新 ID + inherited_from + 保留区间。"""
        old = make_time_evidence(
            "T-old", "2026-09-01", "2026-10-01", run_id="run-earlier"
        )
        updates, msg = invoke_tool_node(
            node,
            [("reuse_evidence", {"evidence_ids": ["T-old"]}, "tc-e1")],
            db_agent_context,
            extra_state={"db_time_evidence": [old]},
        )
        payload = json.loads(msg.content)
        assert payload["status"] == "ok", payload
        mapping = payload["data"]["mapping"][0]
        assert mapping["old_id"] == "T-old"
        assert mapping["new_id"] != "T-old"
        new_ev = updates["db_time_evidence"][0]
        assert new_ev["time_evidence_id"] == mapping["new_id"]
        assert new_ev["run_id"] == db_agent_context.run_id
        assert new_ev["start"] == "2026-09-01" and new_ev["end"] == "2026-10-01"
        assert new_ev["inherited_from"]["evidence_id"] == "T-old"

    def test_reuse_carried_dim_evidence(self, node, db_agent_context):
        carried = {
            "kind": "dim",
            "record": make_dim_evidence(
                "sales.orders",
                "status",
                ["已支付"],
                evidence_id="D-carried",
                run_id="run-old",
                datasource_id="orders",
            ),
            "origin": {
                "evidence_id": "D-carried",
                "thread_id": "t-prev",
                "request_id": "req-prev",
                "run_id": "run-old",
                "datasource_id": "orders",
                "user_text": None,
            },
        }
        ctx = replace(db_agent_context, carried_evidence=(carried,))
        updates, msg = invoke_tool_node(
            node, [("reuse_evidence", {"evidence_ids": ["D-carried"]}, "tc-e2")], ctx
        )
        payload = json.loads(msg.content)
        assert payload["status"] == "ok"
        new_ev = updates["db_dim_evidence"][0]
        assert new_ev["inherited_from"]["thread_id"] == "t-prev"
        assert new_ev["values"] == ["已支付"]

    def test_not_found_registers_nothing(self, node, db_agent_context):
        updates, msg = invoke_tool_node(
            node,
            [("reuse_evidence", {"evidence_ids": ["T-ghost", "D-ghost"]}, "tc-e3")],
            db_agent_context,
        )
        payload = json.loads(msg.content)
        assert payload["status"] == "error"
        assert payload["error"]["error_type"] == "evidence_not_found"
        assert "T-ghost" in payload["error"]["message"]
        assert "db_time_evidence" not in updates and "db_dim_evidence" not in updates

    def test_prior_result_not_reusable(self, node, db_agent_context):
        prior = make_dim_evidence(
            "public.orders",
            "customer_id",
            ["C001"],
            evidence_id="D-prior",
            run_id="run-earlier",
            source="prior_result",
        )
        _u, msg = invoke_tool_node(
            node,
            [("reuse_evidence", {"evidence_ids": ["D-prior"]}, "tc-e4")],
            db_agent_context,
            extra_state={"db_dim_evidence": [prior]},
        )
        payload = json.loads(msg.content)
        assert payload["error"]["error_type"] == "invalid_argument"
        assert "占位符" in payload["error"]["hint"]

    def test_dim_datasource_mismatch(self, node, db_agent_context):
        other_ds = make_dim_evidence(
            "sales.orders",
            "status",
            ["已支付"],
            evidence_id="D-other",
            run_id="run-earlier",
            datasource_id="pg:other",
        )
        _u, msg = invoke_tool_node(
            node,
            [("reuse_evidence", {"evidence_ids": ["D-other"]}, "tc-e5")],
            db_agent_context,
            extra_state={"db_dim_evidence": [other_ds]},
        )
        payload = json.loads(msg.content)
        assert payload["error"]["error_type"] == "evidence_datasource_mismatch"
        assert "pg:other" in payload["error"]["message"]

    def test_time_cross_datasource_allowed(self, node, db_agent_context):
        """时间证据允许跨数据源沿用，原 datasource_id 保留。"""
        old = make_time_evidence(
            "T-xds",
            "2026-09-01",
            "2026-10-01",
            run_id="run-earlier",
            datasource_id="pg:other",
        )
        updates, _msg = invoke_tool_node(
            node,
            [("reuse_evidence", {"evidence_ids": ["T-xds"]}, "tc-e6")],
            db_agent_context,
            extra_state={"db_time_evidence": [old]},
        )
        new_ev = updates["db_time_evidence"][0]
        assert new_ev["datasource_id"] == "pg:other"

    def test_current_run_evidence_not_reusable(self, node, db_agent_context):
        """本运行的证据不在可沿用来源（run_id 相同被排除）。"""
        current = make_time_evidence(
            "T-cur", "2026-09-01", "2026-10-01", run_id=db_agent_context.run_id
        )
        _u, msg = invoke_tool_node(
            node,
            [("reuse_evidence", {"evidence_ids": ["T-cur"]}, "tc-e7")],
            db_agent_context,
            extra_state={"db_time_evidence": [current]},
        )
        payload = json.loads(msg.content)
        assert payload["error"]["error_type"] == "evidence_not_found"
