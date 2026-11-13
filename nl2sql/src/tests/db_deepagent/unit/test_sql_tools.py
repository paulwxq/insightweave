"""SQL 工具单元测试（docs/v4/5 §7.7–§7.8；doc 9 §4 步骤 1）

validate_sql / execute_sql 经 ToolNode 调用；execute 消费 validate 落账的
授权记录（模拟图循环：把第一次 Command 的更新并入状态再调用）。
大预览返回完整 JSON；账本与 ResultStore 引用一致。
"""

from __future__ import annotations

import json
from datetime import date

import pytest
from langgraph.prebuilt import ToolNode

from src.modules.db_deepagent.services import DbAgentServices
from src.modules.db_deepagent.tools.sql_tools import (
    make_execute_sql_tool,
    make_validate_sql_tool,
)

from src.tests.db_deepagent.conftest import invoke_tool_node, make_time_evidence

pytestmark = pytest.mark.unit


def _reject_constant(value: str):
    raise ValueError(f"非标准 JSON 常量: {value}")


def _strict_loads(text: str):
    return json.loads(text, parse_constant=_reject_constant)


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
    from src.tests.db_deepagent.conftest import make_task_record, make_task_state

    task = make_task_record("run-test", "T1", 1, status="pending", content="查询订单")
    return {
        "db_time_evidence": [SEP],
        "db_dim_evidence": [],
        "db_validations": validations or {},
        **make_task_state([task]),
    }


def _task_only_state() -> dict:
    from src.tests.db_deepagent.conftest import make_task_record, make_task_state

    task = make_task_record("run-test", "T1", 1, status="pending", content="查询订单")
    return make_task_state([task])


class TestValidateSql:
    def test_ok_envelope_and_authorization(self, node, db_agent_context):
        updates, msg = invoke_tool_node(
            node,
            [
                (
                    "validate_sql",
                    {"sql": QUERY_SQL, "sub_task_id": "T1", "sub_task_revision": 1},
                    "tc-v1",
                )
            ],
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
            [
                (
                    "validate_sql",
                    {"sql": QUERY_SQL, "sub_task_id": "T1", "sub_task_revision": 1},
                    "tc-v2",
                )
            ],
            db_agent_context,
            extra_state={
                "db_time_evidence": [],
                "db_dim_evidence": [],
                "db_validations": {},
                **_task_only_state(),
            },
        )
        payload = json.loads(msg.content)
        assert payload["status"] == "error"
        assert payload["data"]["layer"] == "guard"
        assert payload["data"]["error_codes"]
        assert not updates.get("db_validations")

    def test_error_fields_complete(self, node, db_agent_context):
        """doc 9 §5：校验错误包全字段保留，错误不变成功。"""
        _u, msg = invoke_tool_node(
            node,
            [
                (
                    "validate_sql",
                    {"sql": QUERY_SQL, "sub_task_id": "T1", "sub_task_revision": 1},
                    "tc-v2b",
                )
            ],
            db_agent_context,
            extra_state={
                "db_time_evidence": [],
                "db_dim_evidence": [],
                "db_validations": {},
                **_task_only_state(),
            },
        )
        payload = _strict_loads(msg.content)
        assert payload["status"] == "error"
        assert payload["data"]["errors"]
        assert payload["data"]["error_codes"]
        error = payload["error"]
        assert error["error_type"] == "validation_failed"
        assert error["retryable"] is True
        assert error["message"]
        assert "error_codes" in error["hint"] or "时间" in error["hint"]

    def test_extra_arg_rejected_by_schema(self, node, db_agent_context):
        """严格参数：未声明字段由框架拒绝（invalid_argument 文本）。"""
        _u, msg = invoke_tool_node(
            node,
            [
                (
                    "validate_sql",
                    {
                        "sql": QUERY_SQL,
                        "force": True,
                        "sub_task_id": "T1",
                        "sub_task_revision": 1,
                    },
                    "tc-v3",
                )
            ],
            db_agent_context,
        )
        assert msg.content.startswith("invalid_argument")


class TestExecuteSql:
    def test_full_loop(self, node, db_agent_context):
        """validate → 并入账本 → execute 成功。"""
        updates1, _ = invoke_tool_node(
            node,
            [
                (
                    "validate_sql",
                    {"sql": QUERY_SQL, "sub_task_id": "T1", "sub_task_revision": 1},
                    "tc-v1",
                )
            ],
            db_agent_context,
            extra_state=_ledger_state(),
        )
        assert len(updates1["db_validations"]) == 1
        updates2, msg2 = invoke_tool_node(
            node,
            [
                (
                    "execute_sql",
                    {"sql": QUERY_SQL, "sub_task_id": "T1", "sub_task_revision": 1},
                    "tc-x1",
                )
            ],
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
            [
                (
                    "execute_sql",
                    {"sql": QUERY_SQL, "sub_task_id": "T1", "sub_task_revision": 1},
                    "tc-x2",
                )
            ],
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
            [
                (
                    "validate_sql",
                    {"sql": QUERY_SQL, "sub_task_id": "T1", "sub_task_revision": 1},
                    "tc-v1",
                )
            ],
            db_agent_context,
            extra_state=_ledger_state(),
        )
        _u, msg = invoke_tool_node(
            node2,
            [
                (
                    "execute_sql",
                    {"sql": QUERY_SQL, "sub_task_id": "T1", "sub_task_revision": 1},
                    "tc-x3",
                )
            ],
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
        db.rows = [
            (i, "已支付") for i in range(1, 20)
        ]  # 超过 max_saved_result_rows 时截断
        db.source_truncated = True
        node2 = ToolNode(
            [
                make_validate_sql_tool(guard_services),
                make_execute_sql_tool(guard_services),
            ]
        )
        u1, _ = invoke_tool_node(
            node2,
            [
                (
                    "validate_sql",
                    {"sql": QUERY_SQL, "sub_task_id": "T1", "sub_task_revision": 1},
                    "tc-v1",
                )
            ],
            db_agent_context,
            extra_state=_ledger_state(),
        )
        _u, msg = invoke_tool_node(
            node2,
            [
                (
                    "execute_sql",
                    {"sql": QUERY_SQL, "sub_task_id": "T1", "sub_task_revision": 1},
                    "tc-x4",
                )
            ],
            db_agent_context,
            extra_state=_ledger_state(u1["db_validations"]),
        )
        payload = json.loads(msg.content)
        assert payload["data"]["source_truncated"] is True

    def test_large_preview_complete_with_ledger(
        self, pipeline_stack, guard_services, db_agent_context
    ):
        """doc 9 §4 步骤 1：execute_sql 大预览（20 行 × ~800 字符）。

        旧实现整体截断产出非法 JSON；修复后预览逐行完整，账本与
        ResultStore 原始结果不随消息层丢失。
        """
        _pipeline, db, results, *_ = pipeline_stack
        wide = "P" * 760
        db.rows = [
            (i, "已支付", wide, f"C{i:03d}", date(2026, 9, 5)) for i in range(1, 21)
        ]
        node2 = ToolNode(
            [
                make_validate_sql_tool(guard_services),
                make_execute_sql_tool(guard_services),
            ]
        )
        u1, _ = invoke_tool_node(
            node2,
            [
                (
                    "validate_sql",
                    {"sql": QUERY_SQL, "sub_task_id": "T1", "sub_task_revision": 1},
                    "tc-v1",
                )
            ],
            db_agent_context,
            extra_state=_ledger_state(),
        )
        _u, msg = invoke_tool_node(
            node2,
            [
                (
                    "execute_sql",
                    {"sql": QUERY_SQL, "sub_task_id": "T1", "sub_task_revision": 1},
                    "tc-x5",
                )
            ],
            db_agent_context,
            extra_state=_ledger_state(u1["db_validations"]),
        )
        payload = _strict_loads(msg.content)  # 旧实现此处 JSONDecodeError（硬截断）
        assert len(msg.content) > 12000  # 修复后实测长度越过旧预算
        data = payload["data"]
        assert len(data["preview_rows"]) == 20  # 预览行数=results.preview_rows
        assert data["preview_truncated"] is False  # 保存 20 行=预览 20 行
        assert data["saved_row_count"] == 20
        assert data["preview_rows"][0][2] == wide  # 超宽单元格整段保留
        assert data["preview_rows"][19][3] == "C020"
        # 账本与消息一致；ResultStore 仍可完整翻页读取
        exec_record = _u["db_executions"][0]
        assert exec_record["evidence_id"] == data["evidence_id"]
        assert exec_record["result_ref"] == data["result_ref"]
        page = results.read_rows(
            data["result_ref"], session_id=db_agent_context.session_id, limit=20
        )
        assert page.rows[0][2] == wide and page.rows[19][3] == "C020"
