"""reuse_evidence 执行／分析沿用测试（docs/v4/5 §7.12.1；doc 6.3 §6.4）

E/A 沿用改为完整来源引用（source_evidence_refs={thread,run,evidence}）+
目标任务版本；本文件验证新契约：完整键寻址与冲突拒绝、裸 ID/混合模式
拒绝、目标版本化新 ID（多目标不冲突）、direct_reuse_from 保留链首
reused_from、可读性/保留期/数据源检查、无归属来源拒绝、缺会话凭据拒绝、
T/D 裸 ID 歧义拒绝。
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

from src.tests.db_deepagent.conftest import (
    invoke_tool_node,
    make_task_record,
    make_task_state,
)

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


def _exec_record(
    meta,
    *,
    evidence_id="E-old",
    run_id="run-old",
    reused_from=None,
    sub_task=("T0", 1),
):
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
        "sub_task_id": sub_task[0] if sub_task else None,
        "sub_task_revision": sub_task[1] if sub_task else None,
        "input_result_refs": [],
        **({"reused_from": reused_from} if reused_from else {}),
    }


def _analysis_record(
    meta,
    *,
    evidence_id="A-old",
    run_id="run-old",
    reused_from=None,
    sub_task=("T0", 1),
):
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
        "sub_task_id": sub_task[0] if sub_task else None,
        "sub_task_revision": sub_task[1] if sub_task else None,
        **({"reused_from": reused_from} if reused_from else {}),
    }


def _source_task_state(run_id="run-old", evidence_id="E-old", analysis=False):
    """来源 run 中 T0@1 completed 且选定该证据（R1-02 来源资格）。"""
    task = make_task_record(
        run_id,
        "T0",
        1,
        status="completed",
        evidence_ids=[evidence_id],
        content="来源任务",
        session_id="s-test",
        thread_id="t-test",
        request_id="req-src",
    )
    return make_task_state([task], plan_version=1)


def _merge_states(*states):
    """按段合并多个 make_task_state 结果（db_sub_tasks/db_task_plans 键级合并）。"""
    merged = {"db_sub_tasks": {}, "db_task_plans": {}}
    for state in states:
        merged["db_sub_tasks"].update(state.get("db_sub_tasks") or {})
        merged["db_task_plans"].update(state.get("db_task_plans") or {})
    return merged


def _target_task(run_id="run-test", status="pending"):
    task = make_task_record(
        run_id, "T1", 1, status=status, evidence_ids=[], content="沿用目标任务"
    )
    return make_task_state([task])


def _reuse_ea(node, refs, ctx, extra_state, target=("T1", 1)):
    args = {
        "source_evidence_refs": refs,
        "sub_task_id": target[0],
        "sub_task_revision": target[1],
    }
    return invoke_tool_node(
        node, [("reuse_evidence", args, "tc-reuse")], ctx, extra_state
    )


def _reuse_td(node, ids, ctx, extra_state):
    return invoke_tool_node(
        node,
        [("reuse_evidence", {"evidence_ids": ids}, "tc-reuse")],
        ctx,
        extra_state,
    )


def _expected_new_id(ctx, source_ref: dict, target=("T1", 1), prefix="E-") -> str:
    from src.modules.db_deepagent.services.task_ledger import canonical_json

    direct = {
        "session_id": ctx.session_id,
        "thread_id": source_ref["thread_id"],
        "run_id": source_ref["run_id"],
        "evidence_id": source_ref["evidence_id"],
    }
    digest = hashlib.sha256(
        canonical_json([ctx.run_id, target[0], target[1], direct]).encode()
    ).hexdigest()[:8]
    return prefix + digest


def _full_ref(ctx, evidence_id, run_id="run-old", thread_id="t-test"):
    return {
        "thread_id": thread_id,
        "run_id": run_id,
        "evidence_id": evidence_id,
    }


class TestEAReuseByFullRef:
    def test_execution_from_ledger(self, guard_services, db_agent_context):
        meta = _save_query_result(guard_services, db_agent_context)
        node = _node(guard_services, _clock(FROZEN))
        source = _exec_record(meta)
        ref = _full_ref(db_agent_context, "E-old")
        updates, msg = _reuse_ea(
            node,
            [ref],
            db_agent_context,
            {
                **_merge_states(_target_task(), _source_task_state()),
                "db_executions": [source],
            },
        )
        payload = json.loads(msg.content)
        assert payload["status"] == "ok", payload
        record = updates["db_executions"][0]
        assert record["evidence_id"] == _expected_new_id(db_agent_context, ref)
        assert record["run_id"] == db_agent_context.run_id
        assert record["purpose"] == "query"
        # 链首 reused_from 展示语义保留；直接来源另存（doc 6.3 §6.4）
        assert record["reused_from"]["evidence_id"] == "E-old"
        assert record["reused_from"]["datasource_id"] == meta.datasource_id
        assert record["direct_reuse_from"]["evidence_ref"]["evidence_id"] == "E-old"
        assert record["direct_reuse_from"]["source_task_ref"]["sub_task_id"] == "T0"
        # 沿用成果归属目标任务版本
        assert record["sub_task_id"] == "T1"
        assert record["sub_task_revision"] == 1
        assert record["result_generated_at"] == meta.created_at
        entry = payload["data"]["mapping"][0]
        assert entry["kind"] == "execution"
        # R1-07：source_evidence_ref=新记录引用；origin 保留直接来源
        assert entry["source_evidence_ref"]["evidence_id"] == record["evidence_id"]
        assert entry["source_evidence_ref"]["run_id"] == db_agent_context.run_id
        assert entry["origin_evidence_ref"]["evidence_id"] == "E-old"
        assert entry["sub_task_id"] == "T1"

    def test_analysis_from_state(self, guard_services, db_agent_context):
        meta = _save_query_result(guard_services, db_agent_context)
        node = _node(guard_services, _clock(FROZEN))
        source = _analysis_record(meta)
        ref = _full_ref(db_agent_context, "A-old")
        updates, msg = _reuse_ea(
            node,
            [ref],
            db_agent_context,
            {
                **_merge_states(
                    _target_task(), _source_task_state(evidence_id="A-old")
                ),
                "db_analyses": [source],
            },
        )
        payload = json.loads(msg.content)
        assert payload["status"] == "ok", payload
        record = updates["db_analyses"][0]
        assert record["evidence_id"].startswith("A-")
        assert record["sub_task_id"] == "T1"

    def test_bare_ea_id_rejected(self, guard_services, db_agent_context):
        meta = _save_query_result(guard_services, db_agent_context)
        node = _node(guard_services, _clock(FROZEN))
        _updates, msg = _reuse_td(
            node,
            ["E-old"],
            db_agent_context,
            {
                **_merge_states(_target_task(), _source_task_state()),
                "db_executions": [_exec_record(meta)],
            },
        )
        payload = json.loads(msg.content)
        assert payload["error"]["error_type"] == "invalid_argument"
        assert "source_evidence_refs" in payload["error"]["message"]

    def test_mixed_modes_rejected(self, guard_services, db_agent_context):
        meta = _save_query_result(guard_services, db_agent_context)
        node = _node(guard_services, _clock(FROZEN))
        _updates, msg = invoke_tool_node(
            node,
            [
                (
                    "reuse_evidence",
                    {
                        "evidence_ids": ["T-x1"],
                        "source_evidence_refs": [_full_ref(db_agent_context, "E-old")],
                        "sub_task_id": "T1",
                        "sub_task_revision": 1,
                    },
                    "tc-reuse",
                )
            ],
            db_agent_context,
            {
                **_merge_states(_target_task(), _source_task_state()),
                "db_executions": [_exec_record(meta)],
            },
        )
        payload = json.loads(msg.content)
        assert payload["error"]["error_type"] == "invalid_argument"

    def test_td_mode_with_target_rejected(self, guard_services, db_agent_context):
        node = _node(guard_services, _clock(FROZEN))
        _updates, msg = invoke_tool_node(
            node,
            [
                (
                    "reuse_evidence",
                    {
                        "evidence_ids": ["T-x1"],
                        "sub_task_id": "T1",
                        "sub_task_revision": 1,
                    },
                    "tc-reuse",
                )
            ],
            db_agent_context,
            {**_target_task()},
        )
        payload = json.loads(msg.content)
        assert payload["error"]["error_type"] == "invalid_argument"

    def test_same_evidence_id_different_runs_selectable(
        self, guard_services, db_agent_context
    ):
        """不同历史 run 的同名 evidence_id 各自可用完整引用选择，不互相覆盖。"""
        node = _node(guard_services, _clock(FROZEN))
        ctx_old1 = replace(db_agent_context, run_id="run-old1")
        ctx_old2 = replace(db_agent_context, run_id="run-old2")
        meta1 = _save_query_result(guard_services, ctx_old1)
        meta2 = _save_query_result(guard_services, ctx_old2)
        rec1 = _exec_record(meta1, evidence_id="E-same", run_id="run-old1")
        rec2 = _exec_record(meta2, evidence_id="E-same", run_id="run-old2")
        state = _merge_states(
            _target_task(),
            _source_task_state(run_id="run-old1", evidence_id="E-same"),
        )
        state["db_executions"] = [rec1, rec2]
        # run-old2 也有自己的来源任务（同会话另一 run）
        t_old2 = make_task_record(
            "run-old2",
            "T0",
            1,
            status="completed",
            evidence_ids=["E-same"],
            content="来源任务",
            session_id="s-test",
            thread_id="t-test",
            request_id="req-src2",
        )
        state["db_sub_tasks"].update(make_task_state([t_old2])["db_sub_tasks"])
        # 选 run-old2
        ref2 = _full_ref(db_agent_context, "E-same", run_id="run-old2")
        updates, msg = _reuse_ea(node, [ref2], db_agent_context, state)
        payload = json.loads(msg.content)
        assert payload["status"] == "ok", payload
        record = updates["db_executions"][0]
        assert record["result_ref"] == meta2.result_ref
        assert record["direct_reuse_from"]["evidence_ref"]["run_id"] == "run-old2"
        # 选 run-old1（同目标会撞 ID？——同来源+同目标=同 ID，reducer 幂等；
        # 不同来源到同目标不互相覆盖见下例）
        ref1 = _full_ref(db_agent_context, "E-same", run_id="run-old1")
        updates2, msg2 = _reuse_ea(node, [ref1], db_agent_context, state)
        payload2 = json.loads(msg2.content)
        assert payload2["status"] == "ok", payload2
        record2 = updates2["db_executions"][0]
        assert record2["result_ref"] == meta1.result_ref
        assert record2["evidence_id"] != record["evidence_id"]

    def test_same_full_key_conflict_rejected(self, guard_services, db_agent_context):
        """同完整键不同事实 → evidence_source_conflict（不字典覆盖）。"""
        meta = _save_query_result(guard_services, db_agent_context)
        rec1 = _exec_record(meta, evidence_id="E-dup", run_id="run-old")
        rec2 = dict(rec1)
        rec2["saved_row_count"] = 99
        node = _node(guard_services, _clock(FROZEN))
        _updates, msg = _reuse_ea(
            node,
            [_full_ref(db_agent_context, "E-dup")],
            db_agent_context,
            {"db_executions": [rec1, rec2], **_target_task()},
        )
        payload = json.loads(msg.content)
        assert payload["error"]["error_type"] == "evidence_source_conflict"

    def test_out_of_catalog_rejected(self, guard_services, db_agent_context):
        """知道完整身份也不行：不在本线程历史/轮换携带清单中的来源拒绝。"""
        meta = _save_query_result(guard_services, db_agent_context)
        node = _node(guard_services, _clock(FROZEN))
        ghost = _full_ref(db_agent_context, "E-ghost", run_id="run-elsewhere")
        _updates, msg = _reuse_ea(
            node,
            [ghost],
            db_agent_context,
            {**_target_task()},
        )
        payload = json.loads(msg.content)
        assert payload["error"]["error_type"] == "evidence_not_found"

    def test_source_without_task_owner_rejected(self, guard_services, db_agent_context):
        """旧协议来源（无任务归属）不能计入新门控成果（§6.4 表）。"""
        meta = _save_query_result(guard_services, db_agent_context)
        node = _node(guard_services, _clock(FROZEN))
        source = _exec_record(meta, sub_task=None)
        _updates, msg = _reuse_ea(
            node,
            [_full_ref(db_agent_context, "E-old")],
            db_agent_context,
            {
                **_merge_states(_target_task(), _source_task_state()),
                "db_executions": [source],
            },
        )
        payload = json.loads(msg.content)
        assert payload["error"]["error_type"] == "stale_task_inputs"

    def test_terminal_target_rejected(self, guard_services, db_agent_context):
        """终态目标任务不能产生新沿用成果（task_not_writable）。"""
        meta = _save_query_result(guard_services, db_agent_context)
        node = _node(guard_services, _clock(FROZEN))
        _updates, msg = _reuse_ea(
            node,
            [_full_ref(db_agent_context, "E-old")],
            db_agent_context,
            {
                "db_executions": [_exec_record(meta)],
                **_target_task(status="completed"),
            },
        )
        payload = json.loads(msg.content)
        assert payload["error"]["error_type"] == "task_not_writable"

    def test_two_targets_same_source_no_id_collision(
        self, guard_services, db_agent_context
    ):
        """同一来源到多个目标任务：新 ID 含目标版本，不互相覆盖。"""
        meta = _save_query_result(guard_services, db_agent_context)
        node = _node(guard_services, _clock(FROZEN))
        t1 = make_task_record("run-test", "T1", 1, content="目标一")
        t2 = make_task_record("run-test", "T2", 1, content="目标二")
        ref = _full_ref(db_agent_context, "E-old")
        state = _merge_states(make_task_state([t1, t2]), _source_task_state())
        state["db_executions"] = [_exec_record(meta)]
        _u1, msg1 = _reuse_ea(node, [ref], db_agent_context, state, target=("T1", 1))
        _u2, msg2 = _reuse_ea(node, [ref], db_agent_context, state, target=("T2", 1))
        id1 = json.loads(msg1.content)["data"]["mapping"][0]["new_id"]
        id2 = json.loads(msg2.content)["data"]["mapping"][0]["new_id"]
        assert id1 != id2
        assert id1 == _expected_new_id(db_agent_context, ref, target=("T1", 1))
        assert id2 == _expected_new_id(db_agent_context, ref, target=("T2", 1))

    def test_missing_lease_rejected(self, guard_services, db_agent_context):
        """缺少有效运行持锁凭据 → session_guard_required（§6.5）。"""
        meta = _save_query_result(guard_services, db_agent_context)
        ctx = replace(db_agent_context, session_guard_lease=None)
        node = _node(guard_services, _clock(FROZEN))
        _updates, msg = _reuse_ea(
            node,
            [_full_ref(db_agent_context, "E-old")],
            ctx,
            {
                **_merge_states(_target_task(), _source_task_state()),
                "db_executions": [_exec_record(meta)],
            },
        )
        payload = json.loads(msg.content)
        assert payload["error"]["error_type"] == "session_guard_required"

    def test_chain_head_reused_from_not_rewritten(
        self, guard_services, db_agent_context
    ):
        """多跳沿用：链首 reused_from 不改写，direct_reuse_from 记直接来源。"""
        meta = _save_query_result(guard_services, db_agent_context)
        head = {
            "evidence_id": "E-root",
            "thread_id": "t-root",
            "request_id": "",
            "run_id": "run-root",
            "datasource_id": "orders",
            "user_text": None,
        }
        mid = _exec_record(
            meta,
            evidence_id="E-mid",
            run_id="run-mid",
            reused_from=head,
        )
        node = _node(guard_services, _clock(FROZEN))
        ref = _full_ref(db_agent_context, "E-mid", run_id="run-mid")
        updates, msg = _reuse_ea(
            node,
            [ref],
            db_agent_context,
            {
                **_merge_states(
                    _target_task(),
                    _source_task_state(run_id="run-mid", evidence_id="E-mid"),
                ),
                "db_executions": [mid],
            },
        )
        payload = json.loads(msg.content)
        assert payload["status"] == "ok", payload
        record = updates["db_executions"][0]
        assert record["reused_from"] == head  # 链首保留
        assert record["direct_reuse_from"]["evidence_ref"]["evidence_id"] == "E-mid"


class TestEAReuseVerification:
    def _ctx(self, base, **kw):
        return replace(base, **kw)

    def test_result_not_readable_rejected(self, guard_services, db_agent_context):
        meta = _save_query_result(guard_services, db_agent_context)
        guard_services.results.delete(
            meta.result_ref,
            session_id=db_agent_context.session_id,
            lease=db_agent_context.session_guard_lease,
        )
        node = _node(guard_services, _clock(FROZEN))
        _updates, msg = _reuse_ea(
            node,
            [_full_ref(db_agent_context, "E-old")],
            db_agent_context,
            {
                **_merge_states(_target_task(), _source_task_state()),
                "db_executions": [_exec_record(meta)],
            },
        )
        payload = json.loads(msg.content)
        assert payload["error"]["error_type"] == "result_not_found"

    def test_expired_retention_rejected(self, guard_services, db_agent_context):
        meta = _save_query_result(guard_services, db_agent_context)
        node = _node(guard_services, _clock(FROZEN + timedelta(days=400)))
        _updates, msg = _reuse_ea(
            node,
            [_full_ref(db_agent_context, "E-old")],
            db_agent_context,
            {
                **_merge_states(_target_task(), _source_task_state()),
                "db_executions": [_exec_record(meta)],
            },
        )
        payload = json.loads(msg.content)
        assert payload["error"]["error_type"] == "result_expired"

    def test_datasource_mismatch_rejected(self, guard_services, db_agent_context):
        other = replace(db_agent_context, datasource_id="other_db")
        meta = _save_query_result(guard_services, other)
        node = _node(guard_services, _clock(FROZEN))
        _updates, msg = _reuse_ea(
            node,
            [_full_ref(db_agent_context, "E-old")],
            db_agent_context,
            {
                **_merge_states(_target_task(), _source_task_state()),
                "db_executions": [_exec_record(meta)],
            },
        )
        payload = json.loads(msg.content)
        assert payload["error"]["error_type"] == "evidence_datasource_mismatch"


class TestTDAmbiguity:
    def test_td_bare_id_ambiguous_rejected(self, guard_services, db_agent_context):
        """T/D 裸 ID 在可见候选中不唯一 → evidence_source_ambiguous。"""
        from src.tests.db_deepagent.conftest import make_dim_evidence

        d1 = make_dim_evidence(
            "public.orders", "status", ["已支付"], evidence_id="D-dup", run_id="run-old"
        )
        d2 = make_dim_evidence(
            "public.orders", "channel", ["线上"], evidence_id="D-dup", run_id="run-old2"
        )
        node = _node(guard_services, _clock(FROZEN))
        _updates, msg = _reuse_td(
            node,
            ["D-dup"],
            db_agent_context,
            {"db_dim_evidence": [d1, d2]},
        )
        payload = json.loads(msg.content)
        assert payload["error"]["error_type"] == "evidence_source_ambiguous"
