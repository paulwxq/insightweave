"""账本去重 reducer 单元测试（docs/v4/5 §5.2、§7.12.1、doc 6 §18）

执行／分析记录分别去重；排除 tool_call_id；同 ID 异事实抛一致性错误。
"""

from __future__ import annotations

import pytest

from src.modules.db_deepagent.state import (
    LedgerConsistencyError,
    append_dedup_analysis,
    append_dedup_execution,
)

pytestmark = pytest.mark.unit


def _exec(evidence_id="E-1", tool_call_id="tc-1", **overrides):
    record = {
        "evidence_id": evidence_id,
        "tool_call_id": tool_call_id,
        "run_id": "run-x",
        "request_id": "req-x",
        "purpose": "query",
        "datasource_id": "ds",
        "sql_template": "SELECT 1",
        "executed_sql": "SELECT 1",
        "executed_sql_sha256": "sha",
        "result_ref": "res_" + "1" * 32,
        "columns": [],
        "saved_row_count": 3,
        "source_truncated": False,
        "elapsed_ms": 9,
        "warnings": [],
        "result_generated_at": "2026-10-01T00:00:00+08:00",
    }
    record.update(overrides)
    return record


def _ana(evidence_id="A-1", tool_call_id="tc-1", **overrides):
    record = {
        "evidence_id": evidence_id,
        "tool_call_id": tool_call_id,
        "run_id": "run-x",
        "request_id": "req-x",
        "pushdown_execution_id": None,
        "operation": {"name": "sum", "column": "amount"},
        "input_result_refs": ["res_" + "1" * 32],
        "output_result_ref": "res_" + "2" * 32,
        "executed_sql": None,
        "saved_row_count": 1,
        "source_truncated": False,
        "warnings": [],
        "result_generated_at": "2026-10-01T00:00:00+08:00",
    }
    record.update(overrides)
    return record


class TestExecutionDedup:
    def test_same_id_same_facts_keeps_first_tool_call_id(self):
        first = _exec(tool_call_id="tc-first")
        second = _exec(tool_call_id="tc-second")  # 并行重复登记：调用 ID 不同
        merged = append_dedup_execution([first], [second])
        assert len(merged) == 1 and merged[0]["tool_call_id"] == "tc-first"

    def test_different_id_appends(self):
        merged = append_dedup_execution([_exec("E-1")], [_exec("E-2")])
        assert [r["evidence_id"] for r in merged] == ["E-1", "E-2"]

    def test_fact_conflict_raises(self):
        first = _exec(saved_row_count=3)
        conflicting = _exec(saved_row_count=9)
        with pytest.raises(LedgerConsistencyError, match="saved_row_count"):
            append_dedup_execution([first], [conflicting])

    def test_reused_from_mismatch_raises(self):
        origin = {
            "evidence_id": "E-old",
            "thread_id": "",
            "request_id": "",
            "run_id": "run-old",
            "datasource_id": "ds",
            "user_text": None,
        }
        with pytest.raises(LedgerConsistencyError, match="reused_from"):
            append_dedup_execution(
                [_exec(reused_from=origin)],
                [_exec(reused_from={**origin, "evidence_id": "E-other"})],
            )

    def test_empty_sides(self):
        assert append_dedup_execution(None, [_exec()]) == [_exec()]
        assert append_dedup_execution([_exec()], None) == [_exec()]


class TestAnalysisDedup:
    def test_analysis_fields_compared(self):
        first = _ana()
        assert append_dedup_analysis([first], [_ana(tool_call_id="tc-2")]) == [first]
        with pytest.raises(LedgerConsistencyError, match="operation"):
            append_dedup_analysis([first], [_ana(operation={"name": "share"})])
        with pytest.raises(LedgerConsistencyError, match="output_result_ref"):
            append_dedup_analysis([first], [_ana(output_result_ref="res_" + "3" * 32)])

    def test_execution_only_fields_not_required(self):
        """分析记录没有 result_ref／sha／columns：比较清单不触及这些键。"""
        merged = append_dedup_analysis([_ana()], [_ana(tool_call_id="tc-x")])
        assert len(merged) == 1


class TestParallelIdempotence:
    def test_triple_registration_single_record(self):
        base = _exec()
        merged = append_dedup_execution(
            [base], [base, _exec(tool_call_id="tc-b"), _exec(tool_call_id="tc-c")]
        )
        assert len(merged) == 1
