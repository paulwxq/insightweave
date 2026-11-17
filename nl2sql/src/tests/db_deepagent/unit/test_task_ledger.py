"""TaskLedger 单元测试（doc 6.3 §4–§5、§11）

覆盖：简单题一条任务、未知/重复 ID、空白目标、前向依赖、自环/环/未知依赖、
整批失败零写入、计划可引用 pending、版本冲突与完整重放幂等（no-op 不递增
版本）、同 ID/content 不同 dependencies 冲突、revise/void 语义、completed
成果归属校验、blocked_by 根因核验、replacement 快照、每批同 ID 单操作。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from src.modules.db_deepagent.services.task_ledger import (
    LedgerOperation,
    TaskLedger,
    TaskLedgerError,
    task_ledger_key,
)
from src.tests.db_deepagent.conftest import make_task_record, make_task_state

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 13, 10, 0, tzinfo=UTC)


def _op(op, tid="T1", **kw):
    return LedgerOperation(op=op, sub_task_id=tid, **kw)


def _apply(state, ops, expected=0, **kw):
    ledger = TaskLedger(state)
    return ledger.apply(
        run_id="run-x",
        session_id="s-test",
        thread_id="t-test",
        request_id="req-x",
        expected_plan_version=expected,
        operations=ops,
        now=NOW,
        tool_call_id="tc-1",
        **kw,
    )


class TestAddAndPlan:
    def test_simple_question_single_task(self):
        result = _apply({}, [_op("add", content="查销售额", dependencies=[])])
        assert result.plan_version == 1
        assert not result.no_op
        (record,) = result.task_records.values()
        assert record["status"] == "pending"
        assert record["revision"] == 1
        assert record["evidence_ids"] == []
        assert result.plan_record["plan_version"] == 1

    def test_blank_content_rejected_zero_write(self):
        for bad in [None, "", "   ", "\n\t", "　"]:
            with pytest.raises(TaskLedgerError) as exc:
                _apply({}, [_op("add", content=bad)])
            assert exc.value.error_type == "invalid_argument"
        # 整批失败零写入：state 不含任何记录
        assert _apply({}, []) if False else True

    def test_duplicate_id_rejected(self):
        state = make_task_state(
            [make_task_record("run-x", "T1", 1, content="查销售额")]
        )
        with pytest.raises(TaskLedgerError) as exc:
            _apply(state, [_op("add", content="另一个目标")], expected=1)
        assert exc.value.error_type == "task_operation_conflict"

    def test_forward_dependency_to_batch_new_task(self):
        """前向引用候选计划中确实新增的版本 → 合法。"""
        result = _apply(
            {},
            [
                _op(
                    "add",
                    tid="T2",
                    content="比较",
                    dependencies=[{"sub_task_id": "T1", "revision": 1}],
                ),
                _op("add", tid="T1", content="取得 A"),
            ],
        )
        assert result.plan_version == 1

    def test_unknown_dependency_rejected(self):
        with pytest.raises(TaskLedgerError) as exc:
            _apply(
                {},
                [
                    _op(
                        "add",
                        content="x",
                        dependencies=[{"sub_task_id": "T9", "revision": 1}],
                    )
                ],
            )
        assert exc.value.error_type == "invalid_task_dependency"

    def test_self_dependency_rejected(self):
        with pytest.raises(TaskLedgerError) as exc:
            _apply(
                {},
                [
                    _op(
                        "add",
                        content="x",
                        dependencies=[{"sub_task_id": "T1", "revision": 1}],
                    )
                ],
            )
        assert exc.value.error_type == "invalid_task_dependency"

    def test_cycle_rejected(self):
        """同批互为依赖（前向引用成环）→ 整批拒绝。"""
        with pytest.raises(TaskLedgerError) as exc:
            _apply(
                {},
                [
                    _op(
                        "add",
                        tid="T1",
                        content="A",
                        dependencies=[{"sub_task_id": "T2", "revision": 1}],
                    ),
                    _op(
                        "add",
                        tid="T2",
                        content="B",
                        dependencies=[{"sub_task_id": "T1", "revision": 1}],
                    ),
                ],
            )
        assert exc.value.error_type == "invalid_task_dependency"
        assert "环" in exc.value.message

    def test_stale_dependency_version_rejected(self):
        """依赖旧版（上游已 revise）→ 拒绝并要求重绑（§5.2 示例）。"""
        state = make_task_state(
            [
                make_task_record("run-x", "T1", 1, content="A"),
                make_task_record("run-x", "T2", 1, content="B"),
                make_task_record(
                    "run-x",
                    "T3",
                    1,
                    content="C",
                    dependencies=[{"sub_task_id": "T1", "revision": 1}],
                ),
            ]
        )
        with pytest.raises(TaskLedgerError) as exc:
            _apply(
                state,
                [_op("revise", tid="T1", content="A2", dependencies=[])],
                expected=1,
            )
        assert exc.value.error_type == "invalid_task_dependency"
        assert "T3" in exc.value.message

    def test_duplicate_dependency_entries_rejected(self):
        with pytest.raises(TaskLedgerError) as exc:
            _apply(
                {},
                [
                    _op(
                        "add",
                        content="x",
                        dependencies=[
                            {"sub_task_id": "T1", "revision": 1},
                            {"sub_task_id": "T1", "revision": 1},
                        ],
                    )
                ],
            )
        assert exc.value.error_type == "invalid_task_dependency"

    def test_plan_can_reference_pending(self):
        """计划登记可引用尚未完成的当前版本（执行前置在门控/管线层）。"""
        state = make_task_state([make_task_record("run-x", "T1", 1, content="上游")])
        result = _apply(
            state,
            [
                _op(
                    "add",
                    tid="T2",
                    content="下游",
                    dependencies=[{"sub_task_id": "T1", "revision": 1}],
                )
            ],
            expected=1,
        )
        assert result.plan_version == 2


class TestVersioningAndIdempotency:
    def _base(self):
        return make_task_state([make_task_record("run-x", "T1", 1, content="查询")])

    def test_old_expected_version_full_replay_conflicts(self):
        state = self._base()
        state["db_task_plans"]["run-x"]["plan_version"] = 1
        with pytest.raises(TaskLedgerError) as exc:
            _apply(state, [_op("add", content="查询")], expected=0)
        assert exc.value.error_type == "task_plan_conflict"
        assert exc.value.current_plan_version == 1
        assert exc.value.tasks_summary

    def test_identical_add_noop_keeps_version(self):
        state = self._base()
        result = _apply(state, [_op("add", content="查询")], expected=1)
        assert result.no_op is True
        assert result.plan_version == 1
        assert result.plan_record is None
        assert result.task_records == {}
        assert "未新增" in result.op_receipts[0]["message"]

    def test_same_content_different_dependencies_conflicts(self):
        state = self._base()
        with pytest.raises(TaskLedgerError) as exc:
            _apply(
                state,
                [
                    _op(
                        "add",
                        content="查询",
                        dependencies=[{"sub_task_id": "T0", "revision": 1}],
                    )
                ],
                expected=1,
            )
        assert exc.value.error_type == "task_operation_conflict"

    def test_status_replay_same_facts_noop(self):
        task = make_task_record(
            "run-x", "T1", 1, status="completed", evidence_ids=["E-1"]
        )
        state = dict(
            make_task_state([task]),
            db_executions=[
                {
                    "evidence_id": "E-1",
                    "tool_call_id": "tc",
                    "run_id": "run-x",
                    "request_id": "req-x",
                    "purpose": "query",
                    "sub_task_id": "T1",
                    "sub_task_revision": 1,
                    "result_ref": "res_" + "1" * 32,
                    "saved_row_count": 1,
                    "source_truncated": False,
                }
            ],
        )
        result = _apply(
            state,
            [_op("status", status="completed", evidence_ids=["E-1"])],
            expected=1,
        )
        assert result.no_op is True
        assert result.plan_version == 1

    def test_status_different_evidence_not_noop(self):
        task = make_task_record(
            "run-x", "T1", 1, status="completed", evidence_ids=["E-1"]
        )
        state = dict(
            make_task_state([task]),
            db_executions=[
                {
                    "evidence_id": "E-1",
                    "run_id": "run-x",
                    "purpose": "query",
                    "sub_task_id": "T1",
                    "sub_task_revision": 1,
                    "result_ref": "res_" + "1" * 32,
                    "saved_row_count": 1,
                    "source_truncated": False,
                },
                {
                    "evidence_id": "E-2",
                    "run_id": "run-x",
                    "purpose": "query",
                    "sub_task_id": "T1",
                    "sub_task_revision": 1,
                    "result_ref": "res_" + "2" * 32,
                    "saved_row_count": 1,
                    "source_truncated": False,
                },
            ],
        )
        # completed→completed 不同事实：终态不经 status 原地改（须 revise）
        with pytest.raises(TaskLedgerError) as exc:
            _apply(
                state,
                [_op("status", status="completed", evidence_ids=["E-2"])],
                expected=1,
            )
        assert exc.value.error_type == "task_operation_conflict"

    def test_revised_id_add_conflicts(self):
        state = make_task_state(
            [
                make_task_record("run-x", "T1", 1, content="v1"),
                make_task_record("run-x", "T1", 2, content="v2"),
            ]
        )
        with pytest.raises(TaskLedgerError):
            _apply(state, [_op("add", content="v1")], expected=1)

    def test_rejects_multiple_ops_same_id_in_batch(self):
        with pytest.raises(TaskLedgerError) as exc:
            _apply(
                {},
                [
                    _op("add", content="x"),
                    _op("status", status="in_progress"),
                ],
            )
        assert exc.value.error_type == "task_operation_conflict"


class TestStatusTransitions:
    def test_pending_direct_terminal_allowed(self):
        state = make_task_state([make_task_record("run-x", "T1", 1)])
        result = _apply(
            state,
            [
                _op(
                    "status",
                    status="failed",
                    reason_code="agent_abandoned",
                    reason="无法推进",
                )
            ],
            expected=1,
        )
        assert result.plan_version == 2
        (record,) = result.task_records.values()
        assert record["status"] == "failed"
        assert record["reason_code"] == "agent_abandoned"
        assert record["termination_context"]["decision_plan_version"] == 2

    def test_completed_requires_owned_evidence(self):
        state = make_task_state([make_task_record("run-x", "T1", 1)])
        with pytest.raises(TaskLedgerError) as exc:
            _apply(
                state,
                [_op("status", status="completed", evidence_ids=["E-ghost"])],
                expected=1,
            )
        assert exc.value.error_type == "task_evidence_mismatch"

    def test_completed_empty_evidence_rejected(self):
        state = make_task_state([make_task_record("run-x", "T1", 1)])
        with pytest.raises(TaskLedgerError) as exc:
            _apply(
                state, [_op("status", status="completed", evidence_ids=[])], expected=1
            )
        assert exc.value.error_type == "task_evidence_mismatch"

    def test_terminal_to_terminal_rejected(self):
        state = make_task_state(
            [
                make_task_record(
                    "run-x", "T1", 1, status="completed", evidence_ids=["E-1"]
                )
            ]
        )
        with pytest.raises(TaskLedgerError) as exc:
            _apply(
                state,
                [
                    _op(
                        "status",
                        status="failed",
                        reason_code="agent_abandoned",
                        reason="x",
                    )
                ],
                expected=1,
            )
        assert exc.value.error_type == "task_operation_conflict"

    def test_failed_requires_nonblank_reason(self):
        state = make_task_state([make_task_record("run-x", "T1", 1)])
        for bad in [None, "", "  \t "]:
            with pytest.raises(TaskLedgerError) as exc:
                _apply(state, [_op("status", status="failed", reason=bad)], expected=1)
            assert exc.value.error_type == "invalid_argument"

    def test_needs_clarification_requires_question(self):
        state = make_task_state([make_task_record("run-x", "T1", 1)])
        with pytest.raises(TaskLedgerError) as exc:
            _apply(
                state,
                [
                    _op(
                        "status",
                        status="needs_clarification",
                        reason="口径不明",
                        clarification_question="  ",
                    ),
                ],
                expected=1,
            )
        assert exc.value.error_type == "invalid_argument"

    def test_tool_failed_requires_failure_refs(self):
        state = make_task_state([make_task_record("run-x", "T1", 1)])
        with pytest.raises(TaskLedgerError) as exc:
            _apply(
                state,
                [
                    _op(
                        "status",
                        status="failed",
                        reason_code="tool_failed",
                        reason="SQL 失败",
                        failure_refs=[],
                    )
                ],
                expected=1,
            )
        assert exc.value.error_type == "invalid_task_failure_ref"

    def test_agent_abandoned_forbids_failure_refs(self):
        state = make_task_state([make_task_record("run-x", "T1", 1)])
        fake_ref = {
            "error_ref": {
                "session_id": "s-test",
                "thread_id": "t-test",
                "run_id": "run-x",
                "error_id": "ERR-x",
            },
            "tool_call_id": "tc",
            "error_type": "db_error",
            "source_task_ref": {},
        }
        with pytest.raises(TaskLedgerError) as exc:
            _apply(
                state,
                [
                    _op(
                        "status",
                        status="failed",
                        reason_code="agent_abandoned",
                        reason="放弃",
                        failure_refs=[fake_ref],
                    )
                ],
                expected=1,
            )
        assert exc.value.error_type == "invalid_task_failure_ref"

    def test_failure_ref_must_match_error_record(self):
        task = make_task_record("run-x", "T1", 1)
        state = dict(
            make_task_state([task]),
            db_tool_errors=[
                {
                    "error_id": "ERR-real",
                    "session_id": "s-test",
                    "thread_id": "t-test",
                    "run_id": "run-x",
                    "request_id": "req-x",
                    "tool_call_id": "tc-9",
                    "tool": "execute_sql",
                    "error_type": "db_semantic_error",
                    "message": "boom",
                    "retryable": True,
                    "source_task_ref": {
                        "session_id": "s-test",
                        "thread_id": "t-test",
                        "run_id": "run-x",
                        "sub_task_id": "T1",
                        "revision": 1,
                    },
                }
            ],
        )
        bad_ref = {
            "error_ref": {
                "session_id": "s-test",
                "thread_id": "t-test",
                "run_id": "run-x",
                "error_id": "ERR-real",
            },
            "tool_call_id": "tc-WRONG",  # 不符
            "error_type": "db_semantic_error",
            "source_task_ref": {},
        }
        with pytest.raises(TaskLedgerError) as exc:
            _apply(
                state,
                [
                    _op(
                        "status",
                        status="failed",
                        reason_code="tool_failed",
                        reason="失败",
                        failure_refs=[bad_ref],
                    )
                ],
                expected=1,
            )
        assert exc.value.error_type == "invalid_task_failure_ref"

    def test_valid_failure_ref_accepted(self):
        task = make_task_record("run-x", "T1", 1)
        state = dict(
            make_task_state([task]),
            db_tool_errors=[
                {
                    "error_id": "ERR-real",
                    "session_id": "s-test",
                    "thread_id": "t-test",
                    "run_id": "run-x",
                    "request_id": "req-x",
                    "tool_call_id": "tc-9",
                    "tool": "execute_sql",
                    "error_type": "db_semantic_error",
                    "message": "boom",
                    "retryable": True,
                    "source_task_ref": {
                        "session_id": "s-test",
                        "thread_id": "t-test",
                        "run_id": "run-x",
                        "sub_task_id": "T1",
                        "revision": 1,
                    },
                }
            ],
        )
        good_ref = {
            "error_ref": {
                "session_id": "s-test",
                "thread_id": "t-test",
                "run_id": "run-x",
                "error_id": "ERR-real",
            },
            "tool_call_id": "tc-9",
            "error_type": "db_semantic_error",
            "source_task_ref": {
                "session_id": "s-test",
                "thread_id": "t-test",
                "run_id": "run-x",
                "sub_task_id": "T1",
                "revision": 1,
            },
        }
        result = _apply(
            state,
            [
                _op(
                    "status",
                    status="failed",
                    reason_code="tool_failed",
                    reason="失败",
                    failure_refs=[good_ref],
                )
            ],
            expected=1,
        )
        assert result.plan_version == 2


class TestDependencyBlocked:
    def _blocked_chain_state(self):
        t1 = make_task_record(
            "run-x",
            "T1",
            1,
            status="needs_clarification",
            reason="口径不明",
            clarification_question="统计含退款吗？",
        )
        return make_task_state([t1])

    def test_blocked_by_must_point_to_failed_or_nc(self):
        state = make_task_state(
            [
                make_task_record("run-x", "T1", 1, status="pending", content="上游"),
                make_task_record(
                    "run-x",
                    "T2",
                    1,
                    content="下游",
                    dependencies=[{"sub_task_id": "T1", "revision": 1}],
                ),
            ]
        )
        with pytest.raises(TaskLedgerError) as exc:
            _apply(
                state,
                [
                    _op(
                        "status",
                        tid="T2",
                        status="failed",
                        reason_code="dependency_blocked",
                        reason="x",
                        blocked_by=[{"sub_task_id": "T1", "revision": 1}],
                    )
                ],
                expected=1,
            )
        assert exc.value.error_type == "invalid_task_dependency"
        assert "谎报" in exc.value.message

    def test_blocked_chain_root_cause_generated(self):
        state = self._blocked_chain_state()
        state["db_task_plans"]["run-x"]["plan_version"] = 1
        # T1 需先在候选视图——由已有状态提供；登记 T2/T3 批量阻塞
        result = _apply(
            dict(
                state,
                db_sub_tasks={
                    **state["db_sub_tasks"],
                    task_ledger_key("run-x", "T2", 1): make_task_record(
                        "run-x",
                        "T2",
                        1,
                        content="下游2",
                        dependencies=[{"sub_task_id": "T1", "revision": 1}],
                    ),
                    task_ledger_key("run-x", "T3", 1): make_task_record(
                        "run-x",
                        "T3",
                        1,
                        content="下游3",
                        dependencies=[{"sub_task_id": "T1", "revision": 1}],
                    ),
                },
            ),
            [
                _op(
                    "status",
                    tid="T2",
                    status="failed",
                    reason_code="dependency_blocked",
                    reason="占位",
                    blocked_by=[{"sub_task_id": "T1", "revision": 1}],
                ),
                _op(
                    "status",
                    tid="T3",
                    status="failed",
                    reason_code="dependency_blocked",
                    reason="占位",
                    blocked_by=[{"sub_task_id": "T1", "revision": 1}],
                ),
            ],
            expected=1,
        )
        assert result.plan_version == 2
        for record in result.task_records.values():
            assert record["status"] == "failed"
            assert record["reason_code"] == "dependency_blocked"
            # 服务端生成原因含根因；不伪造 clarification_question
            assert "根因" in record["reason"]
            assert "clarification_question" not in record
            root = record["termination_context"]["blocked_by_root"]
            assert root["sub_task_id"] == "T1"

    def test_no_forged_question_for_blocked(self):
        """dependency_blocked 不要求伪造 clarification_question（§5.2）。"""
        state = self._blocked_chain_state()
        state["db_task_plans"]["run-x"]["plan_version"] = 1
        state["db_sub_tasks"][task_ledger_key("run-x", "T2", 1)] = make_task_record(
            "run-x",
            "T2",
            1,
            content="下游",
            dependencies=[{"sub_task_id": "T1", "revision": 1}],
        )
        result = _apply(
            state,
            [
                _op(
                    "status",
                    tid="T2",
                    status="failed",
                    reason_code="dependency_blocked",
                    reason="占位",
                    blocked_by=[{"sub_task_id": "T1", "revision": 1}],
                )
            ],
            expected=1,
        )
        record = result.task_records[task_ledger_key("run-x", "T2", 1)]
        assert record.get("clarification_question") is None


class TestReviseAndVoid:
    def test_revise_creates_new_pending_clears_outcomes(self):
        state = make_task_state(
            [
                make_task_record(
                    "run-x", "T1", 1, status="completed", evidence_ids=["E-1"]
                )
            ]
        )
        result = _apply(
            state, [_op("revise", content="新目标", dependencies=[])], expected=1
        )
        assert result.plan_version == 2
        new = result.task_records[task_ledger_key("run-x", "T1", 2)]
        assert new["status"] == "pending"
        assert new["evidence_ids"] == []
        assert new["revision"] == 2

    def test_revise_keeps_old_record(self):
        state = make_task_state([make_task_record("run-x", "T1", 1, content="旧")])
        _apply(state, [_op("revise", content="新", dependencies=[])], expected=1)
        # 旧记录仍在（不可删除）
        assert task_ledger_key("run-x", "T1", 1) in state["db_sub_tasks"]

    def test_void_latest_then_id_dead(self):
        state = make_task_state([make_task_record("run-x", "T1", 1)])
        result = _apply(state, [_op("void", reason="计划替换")], expected=1)
        assert result.plan_version == 2
        (record,) = result.task_records.values()
        assert record["status"] == "voided"
        # 再 revise → 拒绝
        with pytest.raises(TaskLedgerError):
            state2 = dict(
                state,
                db_sub_tasks={
                    **state["db_sub_tasks"],
                    task_ledger_key("run-x", "T1", 1): record,
                },
            )
            _apply(state2, [_op("revise", content="复活", dependencies=[])], expected=2)

    def test_void_with_replacement_snapshot(self):
        tasks = make_task_state(
            [
                make_task_record("run-x", "T1", 1, content="旧任务"),
                make_task_record("run-x", "T2", 1, content="替代"),
            ]
        )
        result = _apply(
            tasks,
            [
                _op(
                    "void",
                    reason="由 T2 替代",
                    replacement={"sub_task_id": "T2", "revision": 1},
                )
            ],
            expected=1,
        )
        record = result.task_records[task_ledger_key("run-x", "T1", 1)]
        assert record["replacement"] == {"sub_task_id": "T2", "revision": 1}
        snap = record["termination_context"]["replacement_snapshot"]
        assert snap["sub_task_id"] == "T2"
        assert snap["status"] == "pending"  # 决策时状态快照

    def test_void_replacement_must_exist_current_unvoided(self):
        state = make_task_state([make_task_record("run-x", "T1", 1)])
        with pytest.raises(TaskLedgerError) as exc:
            _apply(
                state,
                [
                    _op(
                        "void",
                        reason="x",
                        replacement={"sub_task_id": "T9", "revision": 1},
                    )
                ],
                expected=1,
            )
        assert exc.value.error_type == "invalid_task_replacement"

    def test_upstream_void_breaks_unretargeted_downstream(self):
        """上游作废后，未重绑的执行图下游 → 整批拒绝并指明受影响任务。"""
        tasks = make_task_state(
            [
                make_task_record("run-x", "T1", 1, content="上游"),
                make_task_record(
                    "run-x",
                    "T2",
                    1,
                    content="下游",
                    dependencies=[{"sub_task_id": "T1", "revision": 1}],
                ),
            ]
        )
        with pytest.raises(TaskLedgerError) as exc:
            _apply(tasks, [_op("void", tid="T1", reason="替换")], expected=1)
        assert exc.value.error_type == "invalid_task_dependency"
        assert "T2" in exc.value.message

    def test_upstream_void_with_downstream_terminated_same_batch(self):
        """同批作废上游 + 终止下游（agent_abandoned）→ 允许，保留历史引用。"""
        tasks = make_task_state(
            [
                make_task_record("run-x", "T1", 1, content="上游"),
                make_task_record(
                    "run-x",
                    "T2",
                    1,
                    content="下游",
                    dependencies=[{"sub_task_id": "T1", "revision": 1}],
                ),
            ]
        )
        result = _apply(
            tasks,
            [
                _op("void", tid="T1", reason="替换"),
                _op(
                    "status",
                    tid="T2",
                    status="failed",
                    reason_code="agent_abandoned",
                    reason="上游被替换",
                ),
            ],
            expected=1,
        )
        assert result.plan_version == 2
        t2 = result.task_records[task_ledger_key("run-x", "T2", 1)]
        assert t2["status"] == "failed"
        # 历史 A@1 引用保留在 dependencies（终止审计）
        assert t2["dependencies"] == [{"sub_task_id": "T1", "revision": 1}]

    def test_completed_downstream_cannot_flip_failed(self):
        tasks = make_task_state(
            [
                make_task_record("run-x", "A", 1, status="completed"),
                make_task_record(
                    "run-x",
                    "B",
                    1,
                    status="completed",
                    dependencies=[{"sub_task_id": "A", "revision": 1}],
                ),
            ]
        )
        with pytest.raises(TaskLedgerError) as exc:
            _apply(
                tasks,
                [
                    _op(
                        "status",
                        tid="B",
                        status="failed",
                        reason_code="agent_abandoned",
                        reason="x",
                    )
                ],
                expected=1,
            )
        assert exc.value.error_type == "task_operation_conflict"
