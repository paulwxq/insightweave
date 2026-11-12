"""submit_final_answer 单元测试（docs/v4/5 §7.11、§18.2）

单独调用／未重复提交／证据引用／状态一致性四层检查与登记。
"""

from __future__ import annotations

import json

import pytest
from langgraph.prebuilt import ToolNode

from src.modules.db_deepagent.tools.final_answer import make_submit_final_answer_tool

from src.tests.db_deepagent.conftest import invoke_tool_node, make_time_evidence

pytestmark = pytest.mark.unit

SEP = make_time_evidence("T-sep", "2026-09-01", "2026-10-01")
EXEC = {
    "evidence_id": "E-abc12345",
    "tool_call_id": "tc-1",
    "run_id": "run-test",
    "request_id": "req-test",
    "purpose": "query",
    "datasource_id": "orders",
    "sql_template": "SELECT 1",
    "executed_sql": "SELECT 1",
    "executed_sql_sha256": "x",
    "result_ref": "res_" + "a" * 32,
    "columns": [],
    "saved_row_count": 1,
    "source_truncated": False,
    "elapsed_ms": 5,
    "warnings": [],
}
PROBE_EXEC = {**EXEC, "evidence_id": "E-probe01", "purpose": "probe"}


@pytest.fixture()
def node() -> ToolNode:
    return ToolNode([make_submit_final_answer_tool()])


def _args(answer="答案是 42", evidence_ids=("E-abc12345",), status="completed"):
    return {
        "status": status,
        "answer": answer,
        "findings": [{"text": "发现一", "evidence_ids": list(evidence_ids)}],
        "metric_definitions": [],
        "limitations": [],
        "open_questions": [],
    }


def _extra_state(validations=None, answers=None, messages_tail=None):
    return {
        "db_time_evidence": [SEP],
        "db_executions": [EXEC, PROBE_EXEC],
        "db_analyses": [],
        "db_validations": validations or {},
        "db_final_answers": answers or {},
    }


class TestChecks:
    def test_accepted_registers_record(self, node, db_agent_context):
        updates, msg = invoke_tool_node(
            node,
            [("submit_final_answer", _args(), "tc-sub")],
            db_agent_context,
            extra_state=_extra_state(),
        )
        assert msg.content == "已接收最终答案"
        record = updates["db_final_answers"][db_agent_context.run_id]
        assert record["answer"]["status"] == "completed"
        assert record["tool_call_id"] == "tc-sub"

    def test_not_alone_rejected(self, node, db_agent_context):
        """同批还有别的工具调用 → final_answer_not_alone（K20）。"""
        updates, msg = invoke_tool_node(
            node,
            [
                ("validate_sql", {"sql": "SELECT 1"}, "tc-other"),
                ("submit_final_answer", _args(), "tc-sub"),
            ],
            db_agent_context,
            extra_state=_extra_state(),
        )
        payload = json.loads(msg.content)
        assert payload["error"]["error_type"] == "final_answer_not_alone"
        assert "db_final_answers" not in updates

    def test_duplicate_rejected(self, node, db_agent_context):
        existing = {
            db_agent_context.run_id: {"run_id": db_agent_context.run_id, "answer": {}}
        }
        _updates, msg = invoke_tool_node(
            node,
            [("submit_final_answer", _args(), "tc-sub")],
            db_agent_context,
            extra_state=_extra_state(answers=existing),
        )
        payload = json.loads(msg.content)
        assert payload["error"]["error_type"] == "final_answer_already_submitted"

    def test_invalid_evidence_reference(self, node, db_agent_context):
        _updates, msg = invoke_tool_node(
            node,
            [("submit_final_answer", _args(evidence_ids=("E-ghost",)), "tc-sub")],
            db_agent_context,
            extra_state=_extra_state(),
        )
        payload = json.loads(msg.content)
        assert payload["error"]["error_type"] == "invalid_evidence_reference"
        assert "E-ghost" in payload["error"]["message"]

    def test_probe_execution_not_citable(self, node, db_agent_context):
        _updates, msg = invoke_tool_node(
            node,
            [("submit_final_answer", _args(evidence_ids=("E-probe01",)), "tc-sub")],
            db_agent_context,
            extra_state=_extra_state(),
        )
        payload = json.loads(msg.content)
        assert payload["error"]["error_type"] == "invalid_evidence_reference"

    def test_other_run_evidence_not_citable(self, node, db_agent_context):
        other = {**EXEC, "run_id": "run-earlier"}
        _updates, msg = invoke_tool_node(
            node,
            [("submit_final_answer", _args(), "tc-sub")],
            db_agent_context,
            extra_state={**_extra_state(), "db_executions": [other]},
        )
        payload = json.loads(msg.content)
        assert payload["error"]["error_type"] == "invalid_evidence_reference"

    def test_completed_requires_findings(self, node, db_agent_context):
        args = _args()
        args["findings"] = []
        _updates, msg = invoke_tool_node(
            node,
            [("submit_final_answer", args, "tc-sub")],
            db_agent_context,
            extra_state=_extra_state(),
        )
        payload = json.loads(msg.content)
        assert payload["error"]["error_type"] == "invalid_argument"

    def test_no_data_without_findings_ok(self, node, db_agent_context):
        args = _args(status="no_data")
        args["findings"] = []
        updates, msg = invoke_tool_node(
            node,
            [("submit_final_answer", args, "tc-sub")],
            db_agent_context,
            extra_state=_extra_state(),
        )
        assert msg.content == "已接收最终答案"
        assert db_agent_context.run_id in updates["db_final_answers"]

    def test_error_json_shared_envelope_layout_regression(self, node, db_agent_context):
        """doc 9 §3：共享 error_envelope 的缩进布局影响本工具错误 JSON。

        布局变为 indent=2 多行，但错误语义不变：error_type/retryable、
        未接受时不写正式答案。成功回执与登记语义由上方用例覆盖。
        """
        existing = {
            db_agent_context.run_id: {"run_id": db_agent_context.run_id, "answer": {}}
        }
        updates, msg = invoke_tool_node(
            node,
            [("submit_final_answer", _args(), "tc-sub")],
            db_agent_context,
            extra_state=_extra_state(answers=existing),
        )
        assert "\n  " in msg.content  # 共享信封的 indent=2 多行布局
        payload = json.loads(msg.content)
        assert payload["status"] == "error"
        assert payload["error"]["error_type"] == "final_answer_already_submitted"
        assert payload["error"]["retryable"] is False
        assert "db_final_answers" not in updates  # 未接受时不写正式答案


class TestNonblankAnswerRule:
    """doc 6.2 §7.2.1：正文非空白规则在提交参数层生效（工具与 Runner 同一结构）。"""

    @pytest.mark.parametrize("blank", ["", "   ", "\n\t \n", "　"])
    def test_blank_answer_rejected_before_record(self, node, db_agent_context, blank):
        """空串/空格/换行/全角空格：参数校验拒绝，不写 FinalAnswerRecord。"""
        updates, msg = invoke_tool_node(
            node,
            [("submit_final_answer", _args(answer=blank), "tc-sub")],
            db_agent_context,
            extra_state=_extra_state(),
        )
        assert msg.content.startswith("invalid_argument:")
        assert "answer" in msg.content  # 错误定位在 answer 字段
        assert "db_final_answers" not in updates

    def test_nonblank_answer_preserved_verbatim(self, node, db_agent_context):
        """正常正文原样保留（不 strip 重写首尾格式）。"""
        text = "  答案前后留空格保留  "
        updates, msg = invoke_tool_node(
            node,
            [("submit_final_answer", _args(answer=text), "tc-sub")],
            db_agent_context,
            extra_state=_extra_state(),
        )
        assert msg.content == "已接收最终答案"
        record = updates["db_final_answers"][db_agent_context.run_id]
        assert record["answer"]["answer"] == text
