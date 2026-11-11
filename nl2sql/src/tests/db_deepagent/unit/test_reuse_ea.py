"""reuse_evidence 执行／分析沿用测试（docs/v4/5 §7.12.1、§18.2）

覆盖：账本／携带两类来源、链首解析不改写、确定性新 ID、可读性／保留期／
数据源检查（整批拒绝）、探针与同运行记录不可沿用、同批去重。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from langgraph.prebuilt import ToolNode

from src.modules.db_deepagent.context import DbAgentContext
from src.modules.db_deepagent.services import DbAgentServices
from src.modules.db_deepagent.tools.guard_tools import make_reuse_evidence_tool

from src.tests.db_deepagent.conftest import invoke_tool_node

pytestmark = pytest.mark.unit


def _clock(now: datetime):
    return lambda: now


FROZEN = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)


def _node(services: DbAgentServices, clock=None) -> ToolNode:
    return ToolNode([make_reuse_evidence_tool(services, storage_clock=clock)])


def _save_query_result(services: DbAgentServices, ctx: DbAgentContext, **kwargs):
    return services.results.save(
        ctx=ctx,
        purpose=kwargs.pop("purpose", "query"),
        sql_template="SELECT 1",
        executed_sql="SELECT 1",
        columns=[{"name": "k", "pg_type": "int4"}],
        rows=[[1]],
        source_truncated=False,
        derived_from=[],
        guard_evidence={"time": [], "dim": []},
        **kwargs,
    )


def _exec_record(meta, *, evidence_id="E-old", run_id="run-old", reused_from=None):
    return {
        "evidence_id": evidence_id,
        "tool_call_id": "tc-old",
        "run_id": run_id,
        "request_id": "req-old",
        "purpose": "query",
        "datasource_id": meta.datasource_id,
        "sql_template": "SELECT 1",
        "executed_sql": "SELECT 1",
        "executed_sql_sha256": meta.executed_sql_sha256,
        "result_ref": meta.result_ref,
        "columns": meta.columns,
        "saved_row_count": meta.saved_row_count,
        "source_truncated": False,
        "elapsed_ms": 5,
        "warnings": [],
        **({"reused_from": reused_from} if reused_from else {}),
    }


def _analysis_record(meta, *, evidence_id="A-old", run_id="run-old", reused_from=None):
    return {
        "evidence_id": evidence_id,
        "tool_call_id": "tc-old-a",
        "run_id": run_id,
        "request_id": "req-old",
        "pushdown_execution_id": None,
        "operation": {"name": "aggregate"},
        "input_result_refs": [],
        "output_result_ref": meta.result_ref,
        "executed_sql": None,
        "saved_row_count": meta.saved_row_count,
        "source_truncated": False,
        "warnings": [],
        **({"reused_from": reused_from} if reused_from else {}),
    }


def _reuse(node, ids, ctx, extra_state=None):
    return invoke_tool_node(
        node,
        [("reuse_evidence", {"evidence_ids": ids}, "tc-reuse")],
        ctx,
        extra_state,
    )


def _expected_new_id(ctx, root_id: str, prefix: str) -> str:
    digest = hashlib.sha256(f"{ctx.run_id}:{root_id}".encode()).hexdigest()[:8]
    return prefix + digest


class TestExecutionAnalysisReuse:
    def test_execution_from_ledger(self, guard_services, db_agent_context):
        meta = _save_query_result(guard_services, db_agent_context)
        node = _node(guard_services, _clock(FROZEN))
        updates, msg = _reuse(
            node,
            ["E-old"],
            db_agent_context,
            {"db_executions": [_exec_record(meta)]},
        )
        payload = json.loads(msg.content)
        assert payload["status"] == "ok", payload
        record = updates["db_executions"][0]
        assert record["evidence_id"] == _expected_new_id(
            db_agent_context, "E-old", "E-"
        )
        assert record["run_id"] == db_agent_context.run_id
        assert record["request_id"] == db_agent_context.request_id
        assert record["purpose"] == "query"
        assert record["reused_from"]["evidence_id"] == "E-old"
        # 链首 origin 的身份字段以 ResultMeta 为准
        assert record["reused_from"]["datasource_id"] == meta.datasource_id
        assert record["reused_from"]["run_id"] == meta.run_id
        assert record["result_generated_at"] == meta.created_at
        entry = payload["data"]["mapping"][0]
        assert entry["kind"] == "execution"
        assert entry["result_ref"] == meta.result_ref
        assert entry["result_generated_at"] == meta.created_at

    def test_analysis_from_carried(self, guard_services, db_agent_context):
        meta = _save_query_result(guard_services, db_agent_context)
        ctx = replace(
            db_agent_context,
            carried_evidence=(
                {"kind": "analysis", "record": _analysis_record(meta), "origin": None},
            ),
        )
        node = _node(guard_services, _clock(FROZEN))
        updates, msg = _reuse(node, ["A-old"], ctx)
        payload = json.loads(msg.content)
        assert payload["status"] == "ok", payload
        record = updates["db_analyses"][0]
        assert record["evidence_id"] == _expected_new_id(ctx, "A-old", "A-")
        assert record["reused_from"]["evidence_id"] == "A-old"
        assert payload["data"]["mapping"][0]["kind"] == "analysis"

    def test_chain_head_not_rewritten(self, guard_services, db_agent_context):
        """沿用源本身是沿用登记：reused_from 保留链首，不改写为上一运行。"""
        meta = _save_query_result(guard_services, db_agent_context)
        chain_head = {
            "evidence_id": "E-root",
            "thread_id": "dbda:s9:1",
            "request_id": "req-root",
            "run_id": "run-root",
            "datasource_id": meta.datasource_id,
            "user_text": None,
        }
        node = _node(guard_services, _clock(FROZEN))
        updates, msg = _reuse(
            node,
            ["E-mid"],
            db_agent_context,
            {
                "db_executions": [
                    _exec_record(meta, evidence_id="E-mid", reused_from=chain_head)
                ]
            },
        )
        payload = json.loads(msg.content)
        assert payload["status"] == "ok", payload
        record = updates["db_executions"][0]
        assert record["reused_from"] == chain_head  # 链首原样
        # 新 ID 由链首原始 ID 决定
        assert record["evidence_id"] == _expected_new_id(
            db_agent_context, "E-root", "E-"
        )

    def test_deterministic_id_same_run(self, guard_services, db_agent_context):
        """同一运行重复沿用同一原始证据 → 同一登记 ID。"""
        meta = _save_query_result(guard_services, db_agent_context)
        node = _node(guard_services, _clock(FROZEN))
        state = {"db_executions": [_exec_record(meta)]}
        _u1, m1 = _reuse(node, ["E-old"], db_agent_context, state)
        _u2, m2 = _reuse(node, ["E-old"], db_agent_context, state)
        id1 = json.loads(m1.content)["data"]["mapping"][0]["new_id"]
        id2 = json.loads(m2.content)["data"]["mapping"][0]["new_id"]
        assert id1 == id2

    def test_batch_dedup(self, guard_services, db_agent_context):
        meta = _save_query_result(guard_services, db_agent_context)
        node = _node(guard_services, _clock(FROZEN))
        updates, msg = _reuse(
            node,
            ["E-old", "E-old"],
            db_agent_context,
            {"db_executions": [_exec_record(meta)]},
        )
        payload = json.loads(msg.content)
        assert len(payload["data"]["mapping"]) == 1
        assert len(updates["db_executions"]) == 1

    def test_large_ea_mapping_matches_ledger(self, guard_services, db_agent_context):
        """doc 9 §5：大型 E/A mapping（10+10，达到入参上限）逐项核对。

        消息是 new_id 的唯一返回入口：全部 20 条映射须完整出现在返回中，
        且每条 new_id/result_ref 与账本登记一致；E/A 沿用 warnings 恰两条。
        """
        exec_metas = [
            _save_query_result(guard_services, db_agent_context) for _ in range(10)
        ]
        analysis_metas = [
            _save_query_result(guard_services, db_agent_context) for _ in range(10)
        ]
        exec_records = [
            _exec_record(m, evidence_id=f"E-old{i:02d}")
            for i, m in enumerate(exec_metas)
        ]
        analysis_records = [
            _analysis_record(m, evidence_id=f"A-old{i:02d}")
            for i, m in enumerate(analysis_metas)
        ]
        ids = [r["evidence_id"] for r in exec_records] + [
            r["evidence_id"] for r in analysis_records
        ]
        node = _node(guard_services, _clock(FROZEN))
        updates, msg = _reuse(
            node,
            ids,
            db_agent_context,
            {"db_executions": exec_records, "db_analyses": analysis_records},
        )
        payload = json.loads(msg.content)
        assert payload["status"] == "ok", payload
        mapping = payload["data"]["mapping"]
        assert [m["old_id"] for m in mapping] == ids  # 20 条全在、顺序与输入一致
        assert payload["warnings"] == [
            "沿用口径须在回答中写明（原始表达与区间/取值）",
            "沿用历史结果须在回答中写明原结果生成时间与数据源",
        ]
        ledger_exec = {r["evidence_id"]: r for r in updates["db_executions"]}
        ledger_ana = {r["evidence_id"]: r for r in updates["db_analyses"]}
        assert len(ledger_exec) == 10 and len(ledger_ana) == 10
        meta_by_old = {
            **{f"E-old{i:02d}": m for i, m in enumerate(exec_metas)},
            **{f"A-old{i:02d}": m for i, m in enumerate(analysis_metas)},
        }
        for entry in mapping:
            record = ledger_exec.get(entry["new_id"]) or ledger_ana.get(entry["new_id"])
            assert record is not None, entry
            assert record["run_id"] == db_agent_context.run_id
            assert entry["result_ref"] == meta_by_old[entry["old_id"]].result_ref
            assert entry["kind"] == (
                "execution" if entry["old_id"].startswith("E-") else "analysis"
            )
            assert entry["new_id"] == _expected_new_id(
                db_agent_context, entry["old_id"], entry["new_id"][:2]
            )


class TestReuseChecks:
    def test_probe_execution_not_reusable(self, guard_services, db_agent_context):
        """探针／下推执行记录仅作轨迹，不可沿用（不在索引 → not_found）。"""
        meta = _save_query_result(guard_services, db_agent_context, purpose="probe")
        record = _exec_record(meta)
        record["purpose"] = "probe"
        node = _node(guard_services, _clock(FROZEN))
        _u, msg = _reuse(node, ["E-old"], db_agent_context, {"db_executions": [record]})
        payload = json.loads(msg.content)
        assert payload["error"]["error_type"] == "evidence_not_found"

    def test_same_run_excluded(self, guard_services, db_agent_context):
        meta = _save_query_result(guard_services, db_agent_context)
        record = _exec_record(meta, run_id=db_agent_context.run_id)
        node = _node(guard_services, _clock(FROZEN))
        _u, msg = _reuse(node, ["E-old"], db_agent_context, {"db_executions": [record]})
        payload = json.loads(msg.content)
        assert payload["error"]["error_type"] == "evidence_not_found"

    def test_result_not_found(self, guard_services, db_agent_context):
        meta = _save_query_result(guard_services, db_agent_context)
        record = _exec_record(meta)
        record["result_ref"] = "res_" + "0" * 32
        node = _node(guard_services, _clock(FROZEN))
        _u, msg = _reuse(node, ["E-old"], db_agent_context, {"db_executions": [record]})
        payload = json.loads(msg.content)
        assert payload["error"]["error_type"] == "result_not_found"

    def test_result_access_denied(self, guard_services, db_agent_context):
        other_ctx = replace(db_agent_context, session_id="s-other")
        meta = _save_query_result(guard_services, other_ctx)
        node = _node(guard_services, _clock(FROZEN))
        _u, msg = _reuse(
            node, ["E-old"], db_agent_context, {"db_executions": [_exec_record(meta)]}
        )
        payload = json.loads(msg.content)
        assert payload["error"]["error_type"] == "result_access_denied"

    def test_result_integrity_error(self, guard_services, db_agent_context):
        meta = _save_query_result(guard_services, db_agent_context)
        rows_path = guard_services.results._root / meta.result_ref / "rows.jsonl"
        rows_path.write_text("[999]\n", encoding="utf-8")
        node = _node(guard_services, _clock(FROZEN))
        _u, msg = _reuse(
            node, ["E-old"], db_agent_context, {"db_executions": [_exec_record(meta)]}
        )
        payload = json.loads(msg.content)
        assert payload["error"]["error_type"] == "result_integrity_error"

    def test_result_expired_with_injected_clock(self, guard_services, db_agent_context):
        """保留期用存储时钟（注入）判定；created_at + 保留期 + 1 秒即过期。"""
        meta = _save_query_result(guard_services, db_agent_context)
        created = datetime.fromisoformat(meta.created_at).astimezone(timezone.utc)
        retention_days = guard_services.config.thread.result_retention_days
        late_clock = _clock(created + timedelta(days=retention_days, seconds=1))
        node = _node(guard_services, late_clock)
        _u, msg = _reuse(
            node, ["E-old"], db_agent_context, {"db_executions": [_exec_record(meta)]}
        )
        payload = json.loads(msg.content)
        assert payload["error"]["error_type"] == "result_expired"

    def test_retention_boundary_not_expired(self, guard_services, db_agent_context):
        """恰在保留期内（差 1 秒）可沿用。"""
        meta = _save_query_result(guard_services, db_agent_context)
        created = datetime.fromisoformat(meta.created_at).astimezone(timezone.utc)
        retention_days = guard_services.config.thread.result_retention_days
        clock = _clock(created + timedelta(days=retention_days, seconds=-1))
        node = _node(guard_services, clock)
        _u, msg = _reuse(
            node, ["E-old"], db_agent_context, {"db_executions": [_exec_record(meta)]}
        )
        payload = json.loads(msg.content)
        assert payload["status"] == "ok", payload

    def test_datasource_mismatch(self, guard_services, db_agent_context):
        meta = _save_query_result(
            guard_services, db_agent_context, datasource_id="ds-other"
        )
        node = _node(guard_services, _clock(FROZEN))
        _u, msg = _reuse(
            node, ["E-old"], db_agent_context, {"db_executions": [_exec_record(meta)]}
        )
        payload = json.loads(msg.content)
        assert payload["error"]["error_type"] == "evidence_datasource_mismatch"

    def test_record_meta_inconsistency_rejected(self, guard_services, db_agent_context):
        """执行记录副本数据源与元数据不一致同样拒绝。"""
        meta = _save_query_result(guard_services, db_agent_context)
        record = _exec_record(meta)
        record["datasource_id"] = "ds-stale"
        node = _node(guard_services, _clock(FROZEN))
        _u, msg = _reuse(node, ["E-old"], db_agent_context, {"db_executions": [record]})
        payload = json.loads(msg.content)
        assert payload["error"]["error_type"] == "evidence_datasource_mismatch"

    def test_any_failure_rejects_batch(self, guard_services, db_agent_context):
        """任一失败整批拒绝：一个过期 + 一个正常 → 全部不登记。"""
        meta = _save_query_result(guard_services, db_agent_context)
        created = datetime.fromisoformat(meta.created_at).astimezone(timezone.utc)
        retention_days = guard_services.config.thread.result_retention_days
        late_clock = _clock(created + timedelta(days=retention_days, seconds=1))
        node = _node(guard_services, late_clock)
        updates, msg = _reuse(
            node,
            ["E-old", "A-old"],
            db_agent_context,
            {
                "db_executions": [_exec_record(meta)],
                "db_analyses": [_analysis_record(meta)],
            },
        )
        payload = json.loads(msg.content)
        assert payload["error"]["error_type"] == "result_expired"
        assert "db_executions" not in updates and "db_analyses" not in updates
