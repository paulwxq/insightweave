"""SQL 工具单元测试（docs/v4/5 §7.7–§7.8）

validate_sql / execute_sql 经 ToolNode 调用；execute 消费 validate 落账的
授权记录（模拟图循环：把第一次 Command 的更新并入状态再调用）。
"""

from __future__ import annotations

import json

import pytest
from langgraph.prebuilt import ToolNode

from src.modules.db_deepagent.services import DbAgentServices
from src.modules.db_deepagent.tools.sql_tools import (
    make_execute_sql_tool,
    make_validate_sql_tool,
)

from src.tests.db_deepagent.conftest import invoke_tool_node, make_time_evidence

pytestmark = pytest.mark.unit

SEP = make_time_evidence("T-sep", "2026-09-01", "2026-10-01")
QUERY_SQL = (
    "SELECT id, status FROM public.orders "
    "WHERE created_at >= '2026-09-01' AND created_at < '2026-10-01'"
)


@pytest.fixture()
def node(guard_services: DbAgentServices) -> ToolNode:
    return ToolNode(
        [make_validate_sql_tool(guard_services), make_execute_sql_tool(guard_services)]
    )


def _ledger_state(validations=None) -> dict:
    return {
        "db_time_evidence": [SEP],
        "db_dim_evidence": [],
        "db_validations": validations or {},
    }


class TestValidateSql:
    def test_ok_envelope_and_authorization(self, node, db_agent_context):
        updates, msg = invoke_tool_node(
            node,
            [("validate_sql", {"sql": QUERY_SQL}, "tc-v1")],
            db_agent_context,
            extra_state=_ledger_state(),
        )
        payload = json.loads(msg.content)
        assert payload["status"] == "ok"
        assert payload["data"]["layer"] == "all_passed"
        assert "explain_plan" in payload["data"]
        (record,) = updates["db_validations"].values()
        assert record["status"] == "valid"
        assert record["tool_call_id"] == "tc-v1"

    def test_guard_failure_envelope(self, node, db_agent_context):
        updates, msg = invoke_tool_node(
            node,
            [("validate_sql", {"sql": QUERY_SQL}, "tc-v2")],
            db_agent_context,
            extra_state={
                "db_time_evidence": [],
                "db_dim_evidence": [],
                "db_validations": {},
            },
        )
        payload = json.loads(msg.content)
        assert payload["status"] == "error"
        assert payload["data"]["layer"] == "guard"
        assert payload["data"]["error_codes"]
        assert not updates.get("db_validations")

    def test_extra_arg_rejected_by_schema(self, node, db_agent_context):
        """严格参数：未声明字段由框架拒绝（invalid_argument 文本）。"""
        _u, msg = invoke_tool_node(
            node,
            [("validate_sql", {"sql": QUERY_SQL, "force": True}, "tc-v3")],
            db_agent_context,
        )
        assert msg.content.startswith("invalid_argument")


class TestExecuteSql:
    def test_full_loop(self, node, db_agent_context):
        """validate → 并入账本 → execute 成功。"""
        updates1, _ = invoke_tool_node(
            node,
            [("validate_sql", {"sql": QUERY_SQL}, "tc-v1")],
            db_agent_context,
            extra_state=_ledger_state(),
        )
        assert len(updates1["db_validations"]) == 1
        updates2, msg2 = invoke_tool_node(
            node,
            [("execute_sql", {"sql": QUERY_SQL}, "tc-x1")],
            db_agent_context,
            extra_state=_ledger_state(updates1["db_validations"]),
        )
        payload = json.loads(msg2.content)
        assert payload["status"] == "ok", payload
        data = payload["data"]
        assert data["result_ref"].startswith("res_")
        assert data["saved_row_count"] == 2
        assert data["evidence_id"].startswith("E-")
        assert "executed_sql" not in data  # 无占位符时不返回
        assert updates2["db_executions"][0]["result_ref"] == data["result_ref"]

    def test_not_validated(self, node, db_agent_context):
        _u, msg = invoke_tool_node(
            node,
            [("execute_sql", {"sql": QUERY_SQL}, "tc-x2")],
            db_agent_context,
            extra_state=_ledger_state(),
        )
        payload = json.loads(msg.content)
        assert payload["status"] == "error"
        assert payload["error"]["error_type"] == "not_validated"

    def test_empty_result_status(
        self, pipeline_stack, guard_services, db_agent_context
    ):
        """0 行 → status="empty"。"""
        _pipeline, db, *_ = pipeline_stack
        db.rows = []
        node2 = ToolNode(
            [
                make_validate_sql_tool(guard_services),
                make_execute_sql_tool(guard_services),
            ]
        )
        u1, _ = invoke_tool_node(
            node2,
            [("validate_sql", {"sql": QUERY_SQL}, "tc-v1")],
            db_agent_context,
            extra_state=_ledger_state(),
        )
        _u, msg = invoke_tool_node(
            node2,
            [("execute_sql", {"sql": QUERY_SQL}, "tc-x3")],
            db_agent_context,
            extra_state=_ledger_state(u1["db_validations"]),
        )
        payload = json.loads(msg.content)
        assert payload["status"] == "empty"
        assert payload["data"]["saved_row_count"] == 0

    def test_source_truncated_flag(
        self, pipeline_stack, guard_services, db_agent_context
    ):
        """source_truncated=true 提示不能把已保存行当总体。"""
        _pipeline, db, *_ = pipeline_stack
        db.rows = [(i, "已支付") for i in range(1, 20)]  # 超过 max_saved_rows 时截断
        db.source_truncated = True
        node2 = ToolNode(
            [
                make_validate_sql_tool(guard_services),
                make_execute_sql_tool(guard_services),
            ]
        )
        u1, _ = invoke_tool_node(
            node2,
            [("validate_sql", {"sql": QUERY_SQL}, "tc-v1")],
            db_agent_context,
            extra_state=_ledger_state(),
        )
        _u, msg = invoke_tool_node(
            node2,
            [("execute_sql", {"sql": QUERY_SQL}, "tc-x4")],
            db_agent_context,
            extra_state=_ledger_state(u1["db_validations"]),
        )
        payload = json.loads(msg.content)
        assert payload["data"]["source_truncated"] is True
