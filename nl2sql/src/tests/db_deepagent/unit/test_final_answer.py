"""submit_final_answer 单元测试（docs/v4/5 §7.11、§18.2；doc 6.3 §7.2/§7.3）

单独调用／未重复提交／共享任务门控（证据资格、覆盖、unresolved 恰覆盖、
status 严格匹配）与登记；拒绝不写记录。doc 6.3 §13.2 P3-2：原
"no_data 空 findings 可成功"改为拒绝，并新增登记空结果任务的正反例。
"""

from __future__ import annotations

import json

import pytest
from langgraph.prebuilt import ToolNode

from src.modules.db_deepagent.tools.final_answer import make_submit_final_answer_tool

from src.tests.db_deepagent.conftest import (
    invoke_tool_node,
    make_task_record,
    make_task_state,
    make_time_evidence,
)

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
    "sub_task_id": "T1",
    "sub_task_revision": 1,
    "input_result_refs": [],
}
PROBE_EXEC = {
    **EXEC,
    "evidence_id": "E-probe01",
    "purpose": "probe",
    "sub_task_id": None,
    "sub_task_revision": None,
}


@pytest.fixture()
def node() -> ToolNode:
    return ToolNode([make_submit_final_answer_tool()])


def _args(answer="答案是 42", evidence_ids=("E-abc12345",), status="completed"):
    return {
        "status": status,
        "answer": answer,
        "findings": [
            {
                "text": "发现一",
                "finding_id": "F-1",
                "task_refs": [{"sub_task_id": "T1", "revision": 1}],
                "evidence_ids": list(evidence_ids),
            }
        ],
        "metric_definitions": [],
        "limitations": [],
        "open_questions": [],
    }


def _ledger_state(status="completed", evidence_ids=("E-abc12345",), **task_kwargs):
    task = make_task_record(
        "run-test",
        "T1",
        1,
        status=status,
        evidence_ids=list(evidence_ids),
        **task_kwargs,
    )
    return make_task_state([task])


def _extra_state(validations=None, answers=None, messages_tail=None, ledger=None):
    return {
        "db_time_evidence": [SEP],
        "db_executions": [EXEC, PROBE_EXEC],
        "db_analyses": [],
        "db_validations": validations or {},
        "db_final_answers": answers or {},
        **(ledger if ledger is not None else _ledger_state()),
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
        # doc 6.3 §7.1：审计字段随记录保存
        assert record["plan_version"] == 1
        assert record["completion_rules_version"] >= 1
        assert record["coverage_map"]["T1@1"] == ["F-1"]

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

    def test_no_task_ledger_rejected(self, node, db_agent_context):
        """doc 6.3 §7.4 + doc 19：零任务且携带 findings → 直接分支空约束
        拒绝（零任务的 completed/澄清可无 findings 直答，另见
        test_doc19_direct_answer.py）。"""
        updates, msg = invoke_tool_node(
            node,
            [("submit_final_answer", _args(), "tc-sub")],
            db_agent_context,
            extra_state={
                "db_time_evidence": [SEP],
                "db_executions": [EXEC],
                "db_analyses": [],
                "db_validations": {},
                "db_final_answers": {},
            },
        )
        payload = json.loads(msg.content)
        assert payload["error"]["error_type"] == "direct_answer_task_refs_not_allowed"
        assert "db_final_answers" not in updates

    def test_invalid_evidence_reference(self, node, db_agent_context):
        """引用不在本轮记录中的证据 → task_evidence_mismatch（doc 6.3 §7.3）。"""
        _updates, msg = invoke_tool_node(
            node,
            [("submit_final_answer", _args(evidence_ids=("E-ghost",)), "tc-sub")],
            db_agent_context,
            extra_state=_extra_state(),
        )
        payload = json.loads(msg.content)
        assert payload["error"]["error_type"] == "task_evidence_mismatch"
        assert "E-ghost" in payload["error"]["message"]

    def test_probe_execution_not_citable(self, node, db_agent_context):
        _updates, msg = invoke_tool_node(
            node,
            [("submit_final_answer", _args(evidence_ids=("E-probe01",)), "tc-sub")],
            db_agent_context,
            extra_state=_extra_state(),
        )
        payload = json.loads(msg.content)
        assert payload["error"]["error_type"] == "task_evidence_mismatch"

    def test_other_run_evidence_not_citable(self, node, db_agent_context):
        other = {**EXEC, "run_id": "run-earlier"}
        _updates, msg = invoke_tool_node(
            node,
            [("submit_final_answer", _args(), "tc-sub")],
            db_agent_context,
            extra_state={
                **_extra_state(),
                "db_executions": [other],
            },
        )
        payload = json.loads(msg.content)
        assert payload["error"]["error_type"] == "task_evidence_mismatch"

    def test_evidence_not_selected_as_outcome(self, node, db_agent_context):
        """证据真实存在但未选入任务成果 → 覆盖规则 2 拒绝（§7.2）。"""
        ledger = _ledger_state(evidence_ids=())  # completed 但成果为空清单
        _updates, msg = invoke_tool_node(
            node,
            [("submit_final_answer", _args(), "tc-sub")],
            db_agent_context,
            extra_state=_extra_state(ledger=ledger),
        )
        payload = json.loads(msg.content)
        assert payload["error"]["error_type"] == "task_evidence_mismatch"

    def test_completed_requires_findings(self, node, db_agent_context):
        """无 finding 覆盖 completed 任务 → task_answer_uncovered（规则 4）。"""
        args = _args()
        args["findings"] = []
        _updates, msg = invoke_tool_node(
            node,
            [("submit_final_answer", args, "tc-sub")],
            db_agent_context,
            extra_state=_extra_state(),
        )
        payload = json.loads(msg.content)
        assert payload["error"]["error_type"] == "task_answer_uncovered"
        assert payload["data"]["uncovered_tasks"] == ["T1"]

    def test_pending_task_rejected(self, node, db_agent_context):
        """存在 pending/in_progress → task_not_terminal（§7.3 步骤 5）。"""
        ledger = _ledger_state(status="pending", evidence_ids=())
        _updates, msg = invoke_tool_node(
            node,
            [("submit_final_answer", _args(), "tc-sub")],
            db_agent_context,
            extra_state=_extra_state(ledger=ledger),
        )
        payload = json.loads(msg.content)
        assert payload["error"]["error_type"] == "task_not_terminal"
        assert payload["data"]["pending_tasks"] == ["T1"]

    def test_status_mismatch_returns_expected(self, node, db_agent_context):
        """全任务零行（未截断）却报 completed → final_status_mismatch +
        expected_status=no_data；不重执行 SQL（§7.3 步骤 7）。"""
        empty = {
            **EXEC,
            "evidence_id": "E-zero000",
            "saved_row_count": 0,
            "sub_task_id": "T1",
        }
        ledger = _ledger_state(evidence_ids=["E-zero000"])
        args = _args(evidence_ids=("E-zero000",))
        _updates, msg = invoke_tool_node(
            node,
            [("submit_final_answer", args, "tc-sub")],
            db_agent_context,
            extra_state=_extra_state(
                ledger=ledger,
            )
            | {"db_executions": [empty, PROBE_EXEC]},
        )
        payload = json.loads(msg.content)
        assert payload["error"]["error_type"] == "final_status_mismatch"
        assert payload["data"]["expected_status"] == "no_data"

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


class TestFindingIdRules:
    """doc 6.3 §7.2：finding_id 模型填写、提交内唯一、拒绝后可重用。"""

    @pytest.mark.parametrize(
        "bad", ["", "F-0", "F-1a", "f-1", "F-1234567", "X-1", None]
    )
    def test_bad_finding_id_rejected(self, node, db_agent_context, bad):
        args = _args()
        args["findings"][0]["finding_id"] = bad
        updates, msg = invoke_tool_node(
            node,
            [("submit_final_answer", args, "tc-sub")],
            db_agent_context,
            extra_state=_extra_state(),
        )
        assert msg.content.startswith("invalid_argument:") or (
            json.loads(msg.content)["status"] == "error"
        )
        assert "db_final_answers" not in updates

    def test_duplicate_finding_id_rejected(self, node, db_agent_context):
        """finding_id 提交内重复 → 参数校验拒绝（§7.2）。"""
        args = _args()
        args["findings"].append(dict(args["findings"][0]))
        updates, msg = invoke_tool_node(
            node,
            [("submit_final_answer", args, "tc-sub")],
            db_agent_context,
            extra_state=_extra_state(),
        )
        assert msg.content.startswith("invalid_argument:")
        assert "findings" in msg.content
        assert "db_final_answers" not in updates

    def test_valid_f_ids_accepted_and_reusable_after_reject(
        self, node, db_agent_context
    ):
        """合法 F-1/F-2 可接受；拒绝后的标签重提可复用（§7.2）。"""
        task = make_task_record(
            "run-test",
            "T1",
            1,
            status="completed",
            evidence_ids=["E-abc12345", "E-def67890"],
        )
        ledger_state = make_task_state([task])
        second = {**EXEC, "evidence_id": "E-def67890"}
        args = _args()
        args["findings"] = [
            {
                "text": "发现一",
                "finding_id": "F-1",
                "task_refs": [{"sub_task_id": "T1", "revision": 1}],
                "evidence_ids": ["E-abc12345"],
            },
            {
                "text": "发现二",
                "finding_id": "F-2",
                "task_refs": [{"sub_task_id": "T1", "revision": 1}],
                "evidence_ids": ["E-def67890"],
            },
        ]
        updates, msg = invoke_tool_node(
            node,
            [("submit_final_answer", args, "tc-sub")],
            db_agent_context,
            extra_state={
                "db_time_evidence": [SEP],
                "db_executions": [EXEC, second, PROBE_EXEC],
                "db_analyses": [],
                "db_validations": {},
                "db_final_answers": {},
                **ledger_state,
            },
        )
        assert msg.content == "已接收最终答案"
        assert set(
            updates["db_final_answers"][db_agent_context.run_id]["coverage_map"]["T1@1"]
        ) == {"F-1", "F-2"}


class TestNoDataAndUnresolved:
    """doc 6.3 §13.2 P3-2：no_data 严格空结果判定与 unresolved 恰覆盖。"""

    def test_no_data_without_task_rejected(self, node, db_agent_context):
        """旧"无任务 no_data 空 findings 可成功"改为拒绝。"""
        args = _args(status="no_data")
        args["findings"] = []
        updates, msg = invoke_tool_node(
            node,
            [("submit_final_answer", args, "tc-sub")],
            db_agent_context,
            extra_state={
                "db_time_evidence": [SEP],
                "db_executions": [EXEC],
                "db_analyses": [],
                "db_validations": {},
                "db_final_answers": {},
            },
        )
        payload = json.loads(msg.content)
        assert payload["error"]["error_type"] == "task_not_registered"
        assert "db_final_answers" not in updates

    def test_registered_empty_task_without_findings_rejected(
        self, node, db_agent_context
    ):
        """已登记完成空结果任务但 findings 为空 → 仍拒绝（覆盖不可缺）。"""
        empty = {
            **EXEC,
            "evidence_id": "E-zero000",
            "saved_row_count": 0,
            "sub_task_id": "T1",
        }
        ledger = _ledger_state(evidence_ids=["E-zero000"])
        args = _args(status="no_data", evidence_ids=("E-zero000",))
        args["findings"] = []
        _updates, msg = invoke_tool_node(
            node,
            [("submit_final_answer", args, "tc-sub")],
            db_agent_context,
            extra_state=_extra_state(ledger=ledger)
            | {"db_executions": [empty, PROBE_EXEC]},
        )
        payload = json.loads(msg.content)
        assert payload["error"]["error_type"] == "task_answer_uncovered"

    def test_legal_empty_result_with_findings_submits_no_data(
        self, node, db_agent_context
    ):
        """合法空结果 + 覆盖 findings → no_data 正例。"""
        empty = {
            **EXEC,
            "evidence_id": "E-zero000",
            "saved_row_count": 0,
            "sub_task_id": "T1",
        }
        ledger = _ledger_state(evidence_ids=["E-zero000"])
        args = _args(status="no_data", evidence_ids=("E-zero000",))
        updates, msg = invoke_tool_node(
            node,
            [("submit_final_answer", args, "tc-sub")],
            db_agent_context,
            extra_state=_extra_state(ledger=ledger)
            | {"db_executions": [empty, PROBE_EXEC]},
        )
        assert msg.content == "已接收最终答案"
        record = updates["db_final_answers"][db_agent_context.run_id]
        assert record["answer"]["status"] == "no_data"

    def test_count_zero_one_row_is_completed_not_no_data(self, node, db_agent_context):
        """COUNT(*)=0 返回一行：不满足零行规则，作为 completed。"""
        one_row_zero = {
            **EXEC,
            "evidence_id": "E-count00",
            "saved_row_count": 1,
            "sub_task_id": "T1",
        }
        ledger = _ledger_state(evidence_ids=["E-count00"])
        args = _args(evidence_ids=("E-count00",))
        updates, msg = invoke_tool_node(
            node,
            [("submit_final_answer", args, "tc-sub")],
            db_agent_context,
            extra_state=_extra_state(ledger=ledger)
            | {"db_executions": [one_row_zero, PROBE_EXEC]},
        )
        assert msg.content == "已接收最终答案"
        record = updates["db_final_answers"][db_agent_context.run_id]
        assert record["answer"]["status"] == "completed"

    def test_failed_task_requires_exact_unresolved(self, node, db_agent_context):
        """T1 completed、T2 failed：unresolved 必须恰列 T2（规则 5）。"""
        t1 = make_task_record(
            "run-test",
            "T1",
            1,
            status="completed",
            evidence_ids=["E-abc12345"],
        )
        t2 = make_task_record(
            "run-test",
            "T2",
            1,
            status="failed",
            reason_code="agent_abandoned",
            reason="上游无可用口径",
        )
        base = {
            "db_time_evidence": [SEP],
            "db_executions": [EXEC, PROBE_EXEC],
            "db_analyses": [],
            "db_validations": {},
            "db_final_answers": {},
            **make_task_state([t1, t2]),
        }
        args = _args()
        args["status"] = "partial"
        # 遗漏 unresolved → 拒绝
        args_missing = dict(args)
        _u, msg1 = invoke_tool_node(
            node,
            [("submit_final_answer", args_missing, "tc-sub")],
            db_agent_context,
            extra_state=base,
        )
        payload1 = json.loads(msg1.content)
        assert payload1["error"]["error_type"] == "invalid_task_outcomes"
        # 恰覆盖 → partial 接受
        args["unresolved_task_refs"] = [{"sub_task_id": "T2", "revision": 1}]
        updates2, msg2 = invoke_tool_node(
            node,
            [("submit_final_answer", args, "tc-sub")],
            db_agent_context,
            extra_state=base,
        )
        assert msg2.content == "已接收最终答案"
        record = updates2["db_final_answers"][db_agent_context.run_id]
        assert record["answer"]["status"] == "partial"

    def test_open_questions_nonblocking_alone_allowed(self, node, db_agent_context):
        """unresolved 为空 + 非阻塞 open_questions 非空 → 合法（§7.2 分工）。"""
        args = _args()
        args["open_questions"] = ["是否需要按渠道拆分？（补充）"]
        updates, msg = invoke_tool_node(
            node,
            [("submit_final_answer", args, "tc-sub")],
            db_agent_context,
            extra_state=_extra_state(),
        )
        assert msg.content == "已接收最终答案"


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
