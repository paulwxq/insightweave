"""task_completion 共享规则测试（doc 6.3 §7、§11）

覆盖：零有效任务、pending 拒绝、缺主证据、跨任务贴标签、旧版 finding、
无效血缘、漏覆盖、unresolved 恰覆盖、旁证资格（合法旁证/仅探索不能覆盖/
非法旁证整次拒绝/失败任务旁证拒绝）、状态映射全表、可靠空结果矩阵
（行数非法值与截断聚合）、问题合并排序与去重。
"""

from __future__ import annotations

import pytest

from src.modules.db_deepagent.services.result_facts import (
    aggregate_known_saved_row_counts,
    aggregate_proven_untruncated,
    is_proven_untruncated,
    known_saved_row_count,
)
from src.modules.db_deepagent.services.task_completion import (
    CompletionSnapshot,
    evaluate_submission,
    merge_delivery_open_questions,
)
from src.tests.db_deepagent.conftest import make_task_record

pytestmark = pytest.mark.unit


def _exec(eid, *, count=1, truncated=False, task=("T1", 1)):
    return {
        "evidence_id": eid,
        "tool_call_id": "tc",
        "run_id": "run-x",
        "request_id": "req-x",
        "purpose": "query",
        "datasource_id": "orders",
        "sql_template": "SELECT 1",
        "executed_sql": "SELECT 1",
        "executed_sql_sha256": "h",
        "result_ref": "res_" + eid[-1] * 32,
        "columns": [],
        "saved_row_count": count,
        "source_truncated": truncated,
        "elapsed_ms": 1,
        "warnings": [],
        "sub_task_id": task[0] if task else None,
        "sub_task_revision": task[1] if task else None,
        "input_result_refs": [],
    }


def _analysis(eid, *, task=("T1", 1), count=1, truncated=False, inputs=None):
    return {
        "evidence_id": eid,
        "tool_call_id": "tc-a",
        "run_id": "run-x",
        "request_id": "req-x",
        "pushdown_execution_id": None,
        "operation": {"name": "aggregate"},
        "input_result_refs": inputs if inputs is not None else [],
        "output_result_ref": "res_" + eid[-1] * 31 + "a",
        "executed_sql": None,
        "saved_row_count": count,
        "source_truncated": truncated,
        "warnings": [],
        "sub_task_id": task[0] if task else None,
        "sub_task_revision": task[1] if task else None,
    }


def _snapshot(tasks, executions=(), analyses=(), counts=None, untruncated=None):
    run_id = "run-x"
    executions = list(executions)
    analyses = list(analyses)
    return CompletionSnapshot(
        run_id=run_id,
        tasks=tasks,
        executions=executions,
        analyses=analyses,
        errors=[],
        known_counts=(
            counts
            if counts is not None
            else aggregate_known_saved_row_counts(
                {"db_executions": executions, "db_analyses": analyses}, run_id
            )
        ),
        proven_untruncated=(
            untruncated
            if untruncated is not None
            else aggregate_proven_untruncated(
                {"db_executions": executions, "db_analyses": analyses}, run_id
            )
        ),
        plan_version=1,
    )


def _submission(status="completed", findings=(), unresolved=(), answer="答案", **extra):
    return {
        "status": status,
        "answer": answer,
        "findings": [
            {
                "text": f"发现{i}",
                "finding_id": f"F-{i}",
                "task_refs": refs,
                "evidence_ids": evidence,
            }
            for i, (refs, evidence) in enumerate(findings, start=1)
        ],
        "metric_definitions": [],
        "limitations": [],
        "open_questions": [],
        "unresolved_task_refs": list(unresolved),
        **extra,
    }


def _task(tid="T1", status="completed", evidence=(), revision=1, deps=(), **kw):
    return make_task_record(
        "run-x",
        tid,
        revision,
        status=status,
        evidence_ids=list(evidence),
        dependencies=list(deps),
        **kw,
    )


def _codes(decision):
    return [e.code for e in decision.errors]


class TestGateBasics:
    def test_no_tasks_rejected(self):
        decision = evaluate_submission(_submission(), _snapshot({}))
        assert not decision.accepted
        assert "task_not_registered" in _codes(decision)

    def test_all_voided_rejected(self):
        tasks = {"T1": _task(status="voided")}
        tasks["T1"]["reason"] = "计划调整"
        decision = evaluate_submission(_submission(), _snapshot(tasks))
        assert not decision.accepted
        assert "task_not_registered" in _codes(decision)

    def test_pending_rejected(self):
        tasks = {"T1": _task(status="pending")}
        decision = evaluate_submission(_submission(), _snapshot(tasks))
        assert not decision.accepted
        assert "task_not_terminal" in _codes(decision)
        assert decision.pending_task_refs == ["T1"]

    def test_cross_task_label_without_evidence_rejected(self):
        """一个 evidence 贴三个任务标签 → task_evidence_mismatch（规则 3）。"""
        e1 = _exec("E-aaaaaaaa", task=("T1", 1))
        tasks = {
            "T1": _task(evidence=["E-aaaaaaaa"]),
            "T2": _task(evidence=[]),  # 无主证据
            "T3": _task(evidence=[]),
        }
        submission = _submission(
            findings=[
                (
                    [{"sub_task_id": t, "revision": 1} for t in ("T1", "T2", "T3")],
                    ["E-aaaaaaaa"],
                )
            ]
        )
        decision = evaluate_submission(submission, _snapshot(tasks, [e1]))
        assert not decision.accepted
        assert "task_evidence_mismatch" in _codes(decision)

    def test_old_revision_finding_rejected(self):
        e1 = _exec("E-bbbbbbbb", task=("T1", 1))
        tasks = {"T1": _task(revision=2, evidence=["E-bbbbbbbb"])}
        tasks["T1"]["creation_operation"] = {"op": "revise"}
        submission = _submission(
            findings=[([{"sub_task_id": "T1", "revision": 1}], ["E-bbbbbbbb"])]
        )
        decision = evaluate_submission(submission, _snapshot(tasks, [e1]))
        assert not decision.accepted
        assert "task_revision_mismatch" in _codes(decision)

    def test_uncovered_completed_rejected(self):
        e1 = _exec("E-cccccccc", task=("T1", 1))
        tasks = {"T1": _task(evidence=["E-cccccccc"])}
        submission = _submission(findings=[])  # 无 finding
        decision = evaluate_submission(submission, _snapshot(tasks, [e1]))
        assert not decision.accepted
        assert "task_answer_uncovered" in _codes(decision)
        assert decision.uncovered_task_refs == ["T1"]

    def test_unresolved_must_cover_exactly(self):
        t1 = _task(evidence=["E-dddddddd"])
        t2 = _task(
            tid="T2", status="failed", reason_code="agent_abandoned", reason="无法推进"
        )
        e1 = _exec("E-dddddddd", task=("T1", 1))
        tasks = {"T1": t1, "T2": t2}
        submission = _submission(
            status="partial",
            findings=[([{"sub_task_id": "T1", "revision": 1}], ["E-dddddddd"])],
            unresolved=[{"sub_task_id": "T2", "revision": 1}],
        )
        decision = evaluate_submission(submission, _snapshot(tasks, [e1]))
        assert decision.accepted, _codes(decision)
        assert decision.expected_status == "partial"
        # 遗漏 unresolved
        submission2 = _submission(
            status="partial",
            findings=[([{"sub_task_id": "T1", "revision": 1}], ["E-dddddddd"])],
        )
        decision2 = evaluate_submission(submission2, _snapshot(tasks, [e1]))
        assert not decision2.accepted
        assert "invalid_task_outcomes" in _codes(decision2)


class TestSideEvidence:
    """§11 旁证资格：合法旁证允许但不自动覆盖；非法旁证整次拒绝。"""

    def test_exploration_side_evidence_allowed(self):
        """探索记录（同任务完成版本、未选为主成果）可作附加旁证。"""
        e_main = _exec("E-eeeeeeee", task=("T1", 1))
        e_explore = _exec("E-ffffffff", task=("T1", 1))
        tasks = {"T1": _task(evidence=["E-eeeeeeee"])}
        submission = _submission(
            findings=[
                (
                    [{"sub_task_id": "T1", "revision": 1}],
                    ["E-eeeeeeee", "E-ffffffff"],
                )
            ]
        )
        decision = evaluate_submission(
            submission, _snapshot(tasks, [e_main, e_explore])
        )
        assert decision.accepted, _codes(decision)

    def test_cross_task_side_evidence_from_completed_allowed(self):
        """其他最新 completed 任务的合法旁证允许，但不自动覆盖该任务。"""
        e1 = _exec("E-11111111", task=("T1", 1))
        e2 = _exec("E-22222222", task=("T2", 1))
        tasks = {
            "T1": _task(evidence=["E-11111111"]),
            "T2": _task(tid="T2", evidence=["E-22222222"]),
        }
        submission = _submission(
            findings=[
                ([{"sub_task_id": "T1", "revision": 1}], ["E-11111111", "E-22222222"]),
                ([{"sub_task_id": "T2", "revision": 1}], ["E-22222222"]),
            ]
        )
        decision = evaluate_submission(submission, _snapshot(tasks, [e1, e2]))
        assert decision.accepted, _codes(decision)

    def test_exploration_only_cannot_cover(self):
        """仅探索证据（未选为主成果）不能取得覆盖资格（规则 2）。"""
        e_explore = _exec("E-33333333", task=("T1", 1))
        tasks = {"T1": _task(evidence=[])}  # completed 但未选定成果
        tasks["T1"]["evidence_ids"] = []
        submission = _submission(
            findings=[([{"sub_task_id": "T1", "revision": 1}], ["E-33333333"])]
        )
        decision = evaluate_submission(submission, _snapshot(tasks, [e_explore]))
        assert not decision.accepted
        assert "task_evidence_mismatch" in _codes(decision)

    def test_failed_task_evidence_as_side_rejected(self):
        """partial 场景：T2 查询成功但任务 failed，其结果不能作 T1 旁证。"""
        e1 = _exec("E-44444444", task=("T1", 1))
        e2_failed = _exec("E-55555555", task=("T2", 1))
        t2 = _task(
            tid="T2", status="failed", reason_code="agent_abandoned", reason="分析失败"
        )
        tasks = {"T1": _task(evidence=["E-44444444"]), "T2": t2}
        submission = _submission(
            status="partial",
            findings=[
                (
                    [{"sub_task_id": "T1", "revision": 1}],
                    ["E-44444444", "E-55555555"],  # 引用了失败任务的证据
                )
            ],
            unresolved=[{"sub_task_id": "T2", "revision": 1}],
        )
        decision = evaluate_submission(submission, _snapshot(tasks, [e1, e2_failed]))
        assert not decision.accepted
        assert "task_evidence_mismatch" in _codes(decision)
        assert "E-55555555" in decision.stale_evidence_ids


class TestStatusMapping:
    def _covered_submission(self, eid="E-66666666"):
        return _submission(findings=[([{"sub_task_id": "T1", "revision": 1}], [eid])])

    def test_needs_clarification_wins(self):
        t_nc = _task(
            status="needs_clarification",
            reason="口径不明",
            clarification_question="统计含退款吗？",
        )
        t_nc["clarification_question"] = "统计含退款吗？"
        tasks = {"T1": t_nc}
        submission = _submission(
            status="needs_clarification",
            findings=[],
            unresolved=[{"sub_task_id": "T1", "revision": 1}],
        )
        decision = evaluate_submission(submission, _snapshot(tasks))
        assert decision.accepted, _codes(decision)
        assert decision.expected_status == "needs_clarification"
        assert decision.authoritative_questions == ["统计含退款吗？"]

    def test_all_failed(self):
        t = _task(status="failed", reason_code="agent_abandoned", reason="无数据")
        tasks = {"T1": t}
        submission = _submission(
            status="failed",
            findings=[],
            unresolved=[{"sub_task_id": "T1", "revision": 1}],
        )
        decision = evaluate_submission(submission, _snapshot(tasks))
        assert decision.accepted, _codes(decision)
        assert decision.expected_status == "failed"

    def test_status_mismatch_reports_expected(self):
        e1 = _exec("E-77777777", count=3, task=("T1", 1))
        tasks = {"T1": _task(evidence=["E-77777777"])}
        submission = _submission(
            status="no_data",
            findings=[([{"sub_task_id": "T1", "revision": 1}], ["E-77777777"])],
        )
        decision = evaluate_submission(submission, _snapshot(tasks, [e1]))
        assert not decision.accepted
        assert "final_status_mismatch" in _codes(decision)
        assert decision.expected_status == "completed"


class TestNoDataProof:
    def test_true_zero_rows_untruncated_is_no_data(self):
        e1 = _exec("E-88888888", count=0, truncated=False, task=("T1", 1))
        tasks = {"T1": _task(evidence=["E-88888888"])}
        submission = _submission(
            status="no_data",
            findings=[([{"sub_task_id": "T1", "revision": 1}], ["E-88888888"])],
        )
        decision = evaluate_submission(submission, _snapshot(tasks, [e1]))
        assert decision.accepted, _codes(decision)
        assert decision.expected_status == "no_data"
        assert decision.no_data_proven

    @pytest.mark.parametrize("count", [None, False, True, -1, "0", 1.0, 3])
    def test_nonzero_or_unknown_count_is_completed(self, count):
        e1 = _exec("E-99999999", count=count, truncated=False, task=("T1", 1))
        tasks = {"T1": _task(evidence=["E-99999999"])}
        submission = _submission(
            status="no_data",
            findings=[([{"sub_task_id": "T1", "revision": 1}], ["E-99999999"])],
        )
        decision = evaluate_submission(submission, _snapshot(tasks, [e1]))
        if count == 3:
            assert not decision.accepted  # 有数据报 no_data → mismatch
            assert decision.expected_status == "completed"
        else:
            # 未知行数：不能证明全空 → 保守 completed，报 no_data 则 mismatch
            assert not decision.accepted
            assert decision.expected_status == "completed"

    @pytest.mark.parametrize("truncated", [True, None, 0, 1, "false"])
    def test_truncated_not_false_cannot_prove_empty(self, truncated):
        record = {"saved_row_count": 0, "source_truncated": truncated}
        assert not is_proven_untruncated([record])

    def test_mixed_false_true_not_proven(self):
        assert not is_proven_untruncated(
            [
                {"saved_row_count": 0, "source_truncated": False},
                {"saved_row_count": 0, "source_truncated": True},
            ]
        )

    def test_repeated_false_proven_and_order_independent(self):
        records = [
            {"saved_row_count": 0, "source_truncated": False},
            {"saved_row_count": 0, "source_truncated": False},
        ]
        assert is_proven_untruncated(records)
        assert is_proven_untruncated(list(reversed(records)))

    def test_known_saved_row_count_strict(self):
        assert known_saved_row_count({"saved_row_count": 0}) == 0
        assert known_saved_row_count({"saved_row_count": 5}) == 5
        for bad in [None, False, True, -1, "3", 2.0]:
            assert known_saved_row_count({"saved_row_count": bad}) is None
        assert known_saved_row_count({}) is None

    def test_conflicting_duplicate_counts_unknown(self):
        values = {
            "db_executions": [
                _exec("E-aaaa1111", count=0),
                _exec("E-aaaa1111", count=2),
            ]
        }
        counts = aggregate_known_saved_row_counts(values, "run-x")
        assert counts["E-aaaa1111"] is None


class TestDependencyChecks:
    def test_completed_with_incomplete_dep_rejected(self):
        """completed 任务依赖未完成 → 结构拒绝（§5.2 使用成果前置）。"""
        e1 = _exec("E-bbbb2222", task=("T2", 1))
        tasks = {
            "T1": _task(
                evidence=["E-bbbb2222"], deps=[{"sub_task_id": "T2", "revision": 1}]
            ),
            "T2": _task(tid="T2", status="pending", evidence=[]),
        }
        submission = _submission(
            findings=[([{"sub_task_id": "T1", "revision": 1}], ["E-bbbb2222"])],
            unresolved=[],
        )
        decision = evaluate_submission(submission, _snapshot(tasks, [e1]))
        assert not decision.accepted
        assert "invalid_task_dependency" in _codes(decision)

    def test_terminated_dep_audit_only_allowed(self):
        """失败下游保留对失败上游的历史依赖 → 允许（解释性提交）。"""
        t1 = _task(
            tid="T1",
            status="failed",
            reason_code="agent_abandoned",
            reason="SQL 失败放弃",
        )
        t2 = _task(
            tid="T2",
            status="failed",
            evidence=[],
            reason_code="dependency_blocked",
            reason="上游失败",
            deps=[{"sub_task_id": "T1", "revision": 1}],
        )
        t2["blocked_by"] = [{"sub_task_id": "T1", "revision": 1}]
        # R2-13：合法 dependency_blocked 必须携带决策时终止依据（服务端
        # 生成的根因与依赖快照），否则门控拒绝。
        t2["termination_context"] = {
            "decision_plan_version": 1,
            "dependency_snapshots": [
                {
                    "sub_task_id": "T1",
                    "revision": 1,
                    "status": "failed",
                    "reason_code": "agent_abandoned",
                    "reason": "SQL 失败放弃",
                }
            ],
            "blocked_by_root": {
                "sub_task_id": "T1",
                "revision": 1,
                "status": "failed",
                "reason_code": "agent_abandoned",
                "reason": "SQL 失败放弃",
            },
        }
        tasks = {"T1": t1, "T2": t2}
        submission = _submission(
            status="failed",
            findings=[],
            unresolved=[{"sub_task_id": t, "revision": 1} for t in ("T1", "T2")],
        )
        decision = evaluate_submission(submission, _snapshot(tasks))
        assert decision.accepted, _codes(decision)
        assert decision.counts["dependency_blocked"] == 1


class TestQuestionMerge:
    def test_authoritative_first_codepoint_order(self):
        """T10 排在 T2 前（Unicode 码点序）；权威在前、补充按提交序。"""
        merged = merge_delivery_open_questions(["q1", "q2"], ["s1", "s2"])
        assert merged == ["q1", "q2", "s1", "s2"]

    def test_strip_dedupe_keeps_first(self):
        """strip 含全角空格（U+3000 属 Unicode 空白）；保留内部空白。"""
        merged = merge_delivery_open_questions(
            ["  问题一  ", "问题二"], ["问题一", "", "  ", "问题三　", "问题三"]
        )
        assert merged == ["问题一", "问题二", "问题三"]

    def test_internal_whitespace_and_case_preserved(self):
        merged = merge_delivery_open_questions([], ["A  b", "a  b", "X\tY"])
        assert merged == ["A  b", "a  b", "X\tY"]

    def test_none_and_nonstring_dropped(self):
        merged = merge_delivery_open_questions([], ["ok", None, 3, "ok"])
        assert merged == ["ok"]

    def test_no_authoritative_keeps_supplementary_order(self):
        merged = merge_delivery_open_questions([], ["b", "a", "c"])
        assert merged == ["b", "a", "c"]

    def test_authoritative_sorted_by_task_id(self):
        """账本侧权威问题按 (sub_task_id 码点序, revision) 升序提取。"""
        tasks = {}
        for tid, question in [
            ("T10", "问题T10"),
            ("T2", "问题T2"),
            ("T1", "问题T1"),
        ]:
            task = _task(
                tid=tid,
                status="needs_clarification",
                reason="口径",
                clarification_question=question,
            )
            task["clarification_question"] = question
            tasks[tid] = task
        decision = evaluate_submission(None, _snapshot(tasks))
        assert decision.authoritative_questions == ["问题T1", "问题T10", "问题T2"]
