"""doc 6.3 R3 审核反例回归（codex_doc63_impl_review_r3.md 逐项）

以 R3-01…R3-05 为验收清单：每项先复现反例，再验证合法成功路径与相邻
场景。使用真实 ToolNode / 业务工具 / TaskLedger / reducer / 分析服务 /
内存 Store / 实际清理控制流，不以预设拒绝结果或手工拼最终状态代替。

业务 DB 为 FakeExecDb，清理仅 dry-run，不连真实 PG、不调用真实模型、
不删除真实结果。故障注入的畸形历史来源明确标注为损坏账本边界。
"""

from __future__ import annotations

import copy
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar

import pytest
from langgraph.store.memory import InMemoryStore

from src.modules.db_deepagent.persistence.archive import QaArchive
from src.modules.db_deepagent.persistence.maintenance import (
    CleanupReport,
    MaintenanceService,
)
from src.modules.db_deepagent.runner import DbAgentRunner
from src.modules.db_deepagent.services.task_completion import (
    derive_decision_root,
    evaluate_submission,
    root_matches,
    terminal_facts,
    validate_blocked_decision,
)
from src.modules.db_deepagent.services.task_ledger import (
    task_ledger_key,
)
from src.modules.db_deepagent.state import (
    merge_task_records,
)
from src.tests.db_deepagent.conftest import (
    FakeAgent,
    FakeStore,
    make_task_record,
)
from src.tests.db_deepagent.unit.test_doc63_r2_regressions import (
    Harness,
    _apply,
    _evidence,
    _finding,
    _merge,
    _ref,
    _snapshot_of,
    _submission,
)
from src.tests.db_deepagent.unit.test_doc63_r2_regressions import (
    stack as _r2_stack,
)

pytestmark = pytest.mark.unit


@pytest.fixture()
def stack(db_agent_context, tmp_path):
    """复用 R2 的真实工具栈装配（避免复制构造代码）。

    pytest 9 的 FixtureFunctionDefinition 不允许直接调用，取其 __wrapped__
    原函数执行；本文件内名称仍是 stack，测试参数按同名 fixture 解析。
    """
    return _r2_stack.__wrapped__(db_agent_context, tmp_path)


# ======================================================================
# 共用小工具
# ======================================================================


def _sealed(archive, session, thread, run, *, tasks, executions, analyses=()):
    archive.write_run_snapshot(
        session,
        thread,
        run,
        {
            "run_snapshot_schema_version": 1,
            "ledger_protocol_version": 1,
            "session_id": session,
            "thread_id": thread,
            "run_id": run,
            "tasks": list(tasks),
            "executions": list(executions),
            "analyses": list(analyses),
            "tool_errors": [],
        },
    )


def _task_ref(run, tid="T1", rev=1, thread="t-test", session="s-test"):
    return {
        "session_id": session,
        "thread_id": thread,
        "run_id": run,
        "sub_task_id": tid,
        "revision": rev,
    }


def _input_edge(result_ref, ref, task_ref):
    return {
        "result_ref": result_ref,
        "evidence_ref": ref,
        "source_task_ref": task_ref,
    }


def _cleanup_service(root, archive):
    return MaintenanceService(
        SimpleNamespace(workspace=SimpleNamespace(root=str(root))), archive, None
    )


def _dry_run(svc, now, *, archive_before=None):
    report = CleanupReport(dry_run=True)
    svc._cleanup_session_resources(
        "s1",
        result_metas={},
        result_retention=timedelta(days=30),
        archive_before=archive_before or (now - timedelta(days=365)),
        now=now,
        apply=False,
        report=report,
        conn=None,
    )
    return report


def _old_result_dirs(root, ref, *, days=400):
    path = Path(root) / "results" / ref
    path.mkdir(parents=True, exist_ok=True)
    (path / "meta.json").write_text(
        json.dumps(
            {
                "session_id": "s1",
                "created_at": (datetime.now(UTC) - timedelta(days=days)).isoformat(),
            }
        ),
        encoding="utf-8",
    )
    return path


# ======================================================================
# R3-01 · P1 · 合法分析成果沿用（继承输入的 consumer scope）
# ======================================================================


class TestR301AnalysisReuseScope:
    def _source_run(self, h):
        """真实工具链：T1 query E → 同任务分析 A → completed 选 A。"""
        h.fresh("run-root")
        h.task([{"op": "add", "sub_task_id": "T1", "content": "来源分析"}])
        _, q, _ = h.query("SELECT * FROM public.orders")
        # top_n 保留来源列（含 amount），便于后续下游继续消费沿用 A
        a, _ = h.call(
            "analyze_result",
            {
                "operation": "top_n",
                "inputs": [
                    {
                        "result_ref": q["data"]["result_ref"],
                        "evidence_id": q["data"]["evidence_id"],
                    }
                ],
                "params": {
                    "order_by": "amount",
                    "descending": False,
                    "n": 1,
                    "ties": "strict",
                },
                "allow_pushdown": False,
                "sub_task_id": "T1",
                "sub_task_revision": 1,
            },
        )
        assert a["status"] == "ok", a
        done, _ = h.task(
            [
                {
                    "op": "status",
                    "sub_task_id": "T1",
                    "status": "completed",
                    "evidence_ids": [a["data"]["evidence_id"]],
                }
            ]
        )
        assert done["status"] == "ok", done
        return q["data"], a["data"]

    def test_query_analysis_reuse_completed_submit_runner_full_path(self, stack):
        """query→A→completed→reuse A→completed→submit→Runner 全部成功。"""
        h = Harness(stack)
        _q, a = self._source_run(h)
        source_inputs = next(
            r for r in h.state["db_analyses"] if r["evidence_id"] == a["evidence_id"]
        )["input_result_refs"]
        assert source_inputs, "来源 A 必须保留结构化输入血缘（§6.3）"

        h.fresh("run-target")
        h.task([{"op": "add", "sub_task_id": "T1", "content": "沿用分析"}])
        p, _ = h.call(
            "reuse_evidence",
            {
                "source_evidence_refs": [a["source_evidence_ref"]],
                "sub_task_id": "T1",
                "sub_task_revision": 1,
            },
        )
        assert p["status"] == "ok", p
        new_a = p["data"]["mapping"][0]
        reused = next(
            r for r in h.state["db_analyses"] if r["evidence_id"] == new_a["new_id"]
        )
        # 沿用记录不删除血缘，继承输入仍指向原 run 的 E
        assert reused["input_result_refs"] == source_inputs
        assert reused["direct_reuse_from"]["evidence_ref"]["run_id"] == "run-root"

        done, _ = h.task(
            [
                {
                    "op": "status",
                    "sub_task_id": "T1",
                    "status": "completed",
                    "evidence_ids": [new_a["new_id"]],
                }
            ]
        )
        assert done["status"] == "ok", done

        submission = _submission(
            "completed",
            [_finding("F-1", "沿用分析结论", "T1", 1, [new_a["new_id"]])],
        )
        fin, _ = h.call("submit_final_answer", submission)
        assert fin == "已接收最终答案", fin
        assert h.ctx.run_id in (h.state.get("db_final_answers") or {})

        from src.modules.db_deepagent.tools.final_answer import (
            build_completion_snapshot,
        )

        snap, err = build_completion_snapshot(
            h.state,
            h.ctx.run_id,
            services=stack.services,
            session_id=h.ctx.session_id,
            thread_id=h.ctx.thread_id,
            carried=tuple(h.ctx.carried_evidence or ()),
        )
        assert err is None and snap.reuse_failures == {}, (
            err,
            snap.reuse_failures if snap else None,
        )

        runner = DbAgentRunner.__new__(DbAgentRunner)
        runner._services = stack.services
        runner._archive = QaArchive(stack.services.store)
        decision = runner._task_completion_decision(
            h.state,
            h.ctx.run_id,
            submission,
            {},
            session_id=h.ctx.session_id,
            thread_id=h.ctx.thread_id,
        )
        assert decision.accepted, [e.code for e in decision.errors]

    def test_a_reuse_real_runner_finalize_delivery_archive(self, stack):
        """真实 Runner 收尾：A 沿用提交经实际 _finalize/交付/归档，非纯判定。"""
        from src.tests.db_deepagent.unit.test_runner_persistence import _runner

        h = Harness(stack)
        _q, a = self._source_run(h)
        # 目标 run 用占位 run-x，供 FakeAgent._remap_run_id 对齐真实 run_id
        h.fresh("run-x")
        h.task([{"op": "add", "sub_task_id": "T1", "content": "沿用分析"}])
        p, _ = h.call(
            "reuse_evidence",
            {
                "source_evidence_refs": [a["source_evidence_ref"]],
                "sub_task_id": "T1",
                "sub_task_revision": 1,
            },
        )
        assert p["status"] == "ok", p
        eid = p["data"]["mapping"][0]["new_id"]
        done, _ = h.task(
            [
                {
                    "op": "status",
                    "sub_task_id": "T1",
                    "status": "completed",
                    "evidence_ids": [eid],
                }
            ]
        )
        assert done["status"] == "ok", done
        h.call(
            "submit_final_answer",
            _submission(
                "completed",
                [_finding("F-1", "沿用分析结论：销售额123万元", "T1", 1, [eid])],
            ),
        )
        stack.services.config.persistence.mode = "memory"
        stack.services.source_id = "db_src_1"  # P0 短 ID 校验（离线装配）
        stack.db.datasource_id = "pg:test"  # Runner ctx 数据源属性
        # ToolNode 阶段持有 db_agent_context 的 memory guard；真实 Runner
        # 要自行取同会话锁，先释放工具侧凭据（fixture teardown 二次释放安全）。
        from src.modules.db_deepagent.persistence.session_guard import (
            release_memory_lease,
        )

        release_memory_lease(stack.ctx.session_guard_lease)
        store = FakeStore()
        result = _runner(stack.services, FakeAgent(h.state), store, h.state).run(
            "问", "s-test", request_id="req-a-reuse"
        )
        assert result.status == "completed", (
            result.status,
            [e.message for e in result.errors],
        )
        # R2-07：合法 finding 的实际结论进入确定性交付正文
        assert "沿用分析结论：销售额123万元" in result.answer
        assert result.archived is True
        assert any(
            ns[1] == "qa" for (ns, _k) in store.items
        ), "成功轮次应进 qa 而非 run_errors"

    def test_continue_consuming_reused_a_via_downstream_task(self, stack):
        """继续消费沿用 A：下游 T2 声明依赖 T1，分析沿用 A 成功。"""
        h = Harness(stack)
        _q, a = self._source_run(h)
        h.fresh("run-target")
        h.task(
            [
                {"op": "add", "sub_task_id": "T1", "content": "沿用分析"},
                {
                    "op": "add",
                    "sub_task_id": "T2",
                    "content": "下游消费",
                    "dependencies": [{"sub_task_id": "T1", "revision": 1}],
                },
            ]
        )
        p, _ = h.call(
            "reuse_evidence",
            {
                "source_evidence_refs": [a["source_evidence_ref"]],
                "sub_task_id": "T1",
                "sub_task_revision": 1,
            },
        )
        assert p["status"] == "ok", p
        new_a = p["data"]["mapping"][0]
        done, _ = h.task(
            [
                {
                    "op": "status",
                    "sub_task_id": "T1",
                    "status": "completed",
                    "evidence_ids": [new_a["new_id"]],
                }
            ]
        )
        assert done["status"] == "ok", done
        consumed, updates = h.call(
            "analyze_result",
            {
                "operation": "describe",
                "inputs": [
                    {
                        "result_ref": new_a["result_ref"],
                        "evidence_id": new_a["new_id"],
                    }
                ],
                "params": {"columns": ["amount"]},
                "allow_pushdown": False,
                "sub_task_id": "T2",
                "sub_task_revision": 1,
            },
        )
        assert consumed["status"] == "ok", consumed
        assert updates.get("db_analyses")

    def test_cross_thread_a_reuse_via_full_snapshot(self, stack):
        """跨线程完整快照：来源 A（含输入 E）封存后新线程沿用成功。"""
        h = Harness(stack)
        h.fresh("run-root", "t-old")
        _q, a = self._source_run(h)
        a_rec = next(
            r for r in h.state["db_analyses"] if r["evidence_id"] == a["evidence_id"]
        )
        e_rec = next(r for r in h.state["db_executions"] if r["run_id"] == "run-root")
        tasks = [
            t for t in h.state["db_sub_tasks"].values() if t["run_id"] == "run-root"
        ]
        archive = QaArchive(stack.services.store)
        _sealed(
            archive,
            h.ctx.session_id,
            "t-old",
            "run-root",
            tasks=tasks,
            executions=[e_rec],
            analyses=[a_rec],
        )
        carried = (
            {
                "kind": "analysis",
                "record": a_rec,
                "origin": {
                    "session_id": h.ctx.session_id,
                    "thread_id": "t-old",
                    "run_id": "run-root",
                    "evidence_id": a_rec["evidence_id"],
                },
            },
        )
        h.state = {}
        h.fresh("run-cross", "t-new", carried_evidence=carried)
        h.task([{"op": "add", "sub_task_id": "T1", "content": "跨线程沿用"}])
        p, _ = h.call(
            "reuse_evidence",
            {
                "source_evidence_refs": [
                    {
                        "thread_id": "t-old",
                        "run_id": "run-root",
                        "evidence_id": a_rec["evidence_id"],
                    }
                ],
                "sub_task_id": "T1",
                "sub_task_revision": 1,
            },
        )
        assert p["status"] == "ok", p
        eid = p["data"]["mapping"][0]["new_id"]
        done, _ = h.task(
            [
                {
                    "op": "status",
                    "sub_task_id": "T1",
                    "status": "completed",
                    "evidence_ids": [eid],
                }
            ]
        )
        assert done["status"] == "ok", done

    def test_multi_hop_a_reuse_via_snapshots(self, stack):
        """多跳沿用（A→A2→A3）仅凭完整快照仍可核验。"""
        archive = QaArchive(stack.services.store)
        root_e = _evidence("E-root", "run-a", "T1", 1)
        root_a = {
            **_evidence("A-root", "run-a", "T1", 1),
            "output_result_ref": "res_" + "a" * 32,
            "input_result_refs": [
                _input_edge(
                    root_e["result_ref"],
                    _ref("run-a", "E-root"),
                    _task_ref("run-a"),
                )
            ],
        }
        task_a = make_task_record(
            "run-a", "T1", 1, status="completed", evidence_ids=["A-root"]
        )
        _sealed(
            archive,
            "s-test",
            "t-test",
            "run-a",
            tasks=[task_a],
            executions=[root_e],
            analyses=[root_a],
        )
        a2 = {
            **root_a,
            "evidence_id": "A-mid",
            "run_id": "run-b",
            "output_result_ref": "res_" + "b" * 32,
            "direct_reuse_from": {
                "evidence_ref": _ref("run-a", "A-root"),
                "source_task_ref": _task_ref("run-a"),
            },
        }
        task_b = make_task_record(
            "run-b", "T1", 1, status="completed", evidence_ids=["A-mid"]
        )
        _sealed(
            archive,
            "s-test",
            "t-test",
            "run-b",
            tasks=[task_b],
            executions=[],
            analyses=[a2],
        )
        from src.modules.db_deepagent.services.reuse_qualification import (
            verify_reuse_source,
        )

        verify_reuse_source(
            _ref("run-b", "A-mid"),
            a2,
            session_id="s-test",
            state={"db_sub_tasks": {}, "db_executions": [], "db_analyses": []},
            archive=archive,
        )

    def test_missing_inherited_input_source_in_snapshot_rejected(self, stack):
        """快照含 A/任务但缺其结构化输入 E 原记录 → 沿用拒绝（不回退 live）。"""
        from src.modules.db_deepagent.services.reuse_qualification import (
            SourceUnqualified,
            verify_reuse_source,
        )

        archive = QaArchive(stack.services.store)
        root_e = _evidence("E-root", "run-a", "T1", 1)
        root_a = {
            **_evidence("A-root", "run-a", "T1", 1),
            "output_result_ref": "res_" + "a" * 32,
            "input_result_refs": [
                _input_edge(
                    root_e["result_ref"],
                    _ref("run-a", "E-root"),
                    _task_ref("run-a"),
                )
            ],
        }
        task_a = make_task_record(
            "run-a", "T1", 1, status="completed", evidence_ids=["A-root"]
        )
        _sealed(
            archive,
            "s-test",
            "t-test",
            "run-a",
            tasks=[task_a],
            executions=[],  # 缺输入 E 原始记录
            analyses=[root_a],
        )
        state = {
            "db_sub_tasks": {},
            "db_executions": [root_e],  # live 有副本，不得掩盖快照缺源
            "db_analyses": [],
        }
        with pytest.raises(SourceUnqualified):
            verify_reuse_source(
                _ref("run-a", "A-root"),
                root_a,
                session_id="s-test",
                state=state,
                archive=archive,
            )

    def test_input_bearing_sql_result_reuse_succeeds(self, stack):
        """带 inputs 的 SQL 成果（占位符派生 E）沿用采用同一规则，不被误拒。"""
        h = Harness(stack)
        h.fresh("run-root")
        h.task([{"op": "add", "sub_task_id": "T1", "content": "派生 SQL 来源"}])
        _, q, _ = h.query("SELECT * FROM public.orders")
        sql = (
            "SELECT orders.id FROM public.orders WHERE orders.id IN "
            "{{result:" + q["data"]["result_ref"] + ":id}}"
        )
        v, _ = h.call(
            "validate_sql",
            {"sql": sql, "sub_task_id": "T1", "sub_task_revision": 1},
        )
        e2, _ = h.call(
            "execute_sql",
            {"sql": sql, "sub_task_id": "T1", "sub_task_revision": 1},
        )
        assert v["status"] == "ok" and e2["status"] == "ok", (v, e2)
        derived = e2["data"]
        derived_rec = next(
            r
            for r in h.state["db_executions"]
            if r["evidence_id"] == derived["evidence_id"]
        )
        assert derived_rec["input_result_refs"], "派生 E 必须保存结构化输入血缘"
        done, _ = h.task(
            [
                {
                    "op": "status",
                    "sub_task_id": "T1",
                    "status": "completed",
                    "evidence_ids": [derived["evidence_id"]],
                }
            ]
        )
        assert done["status"] == "ok", done
        h.fresh("run-target")
        h.task([{"op": "add", "sub_task_id": "T1", "content": "沿用派生 E"}])
        p, _ = h.call(
            "reuse_evidence",
            {
                "source_evidence_refs": [derived["source_evidence_ref"]],
                "sub_task_id": "T1",
                "sub_task_revision": 1,
            },
        )
        assert p["status"] == "ok", p
        eid = p["data"]["mapping"][0]["new_id"]
        done2, _ = h.task(
            [
                {
                    "op": "status",
                    "sub_task_id": "T1",
                    "status": "completed",
                    "evidence_ids": [eid],
                }
            ]
        )
        assert done2["status"] == "ok", done2

    def test_forged_inherited_inputs_rejected(self, stack):
        """伪造继承输入：沿用记录的 input_result_refs 与权威来源不一致 → 拒绝。"""
        from src.modules.db_deepagent.services.reuse_qualification import (
            unqualified_reuse_evidence,
        )

        h = Harness(stack)
        _q, a = self._source_run(h)
        h.fresh("run-target")
        h.task([{"op": "add", "sub_task_id": "T1", "content": "沿用分析"}])
        p, _ = h.call(
            "reuse_evidence",
            {
                "source_evidence_refs": [a["source_evidence_ref"]],
                "sub_task_id": "T1",
                "sub_task_revision": 1,
            },
        )
        assert p["status"] == "ok", p
        eid = p["data"]["mapping"][0]["new_id"]
        # 故障注入：篡改沿用记录继承的输入血缘
        for rec in h.state["db_analyses"]:
            if rec["evidence_id"] == eid:
                rec["input_result_refs"] = [
                    _input_edge(
                        "res_" + "f" * 32,
                        _ref("run-target", "E-forged"),
                        _task_ref("run-target"),
                    )
                ]
        failures = unqualified_reuse_evidence(
            [r for r in h.state["db_analyses"] if r["run_id"] == "run-target"],
            session_id=h.ctx.session_id,
            thread_id=h.ctx.thread_id,
            run_id="run-target",
            state=h.state,
            archive=QaArchive(stack.services.store),
        )
        assert eid in failures, failures

    def test_source_invalidated_after_reuse_rejected(self, stack):
        """来源后续 revise/void → 沿用成果在最终门控被拒绝。"""
        from src.modules.db_deepagent.services.reuse_qualification import (
            unqualified_reuse_evidence,
        )

        h = Harness(stack)
        _q, a = self._source_run(h)
        h.fresh("run-root")  # 回到来源 run 做修订
        h.task([{"op": "revise", "sub_task_id": "T1", "content": "新版来源"}])
        h.fresh("run-target")
        h.task([{"op": "add", "sub_task_id": "T1", "content": "沿用分析"}])
        p, _ = h.call(
            "reuse_evidence",
            {
                "source_evidence_refs": [a["source_evidence_ref"]],
                "sub_task_id": "T1",
                "sub_task_revision": 1,
            },
        )
        assert p.get("error", {}).get("error_type") == "stale_task_inputs", p
        # 直接装配层也必须拒绝（防御复核）
        failures = unqualified_reuse_evidence(
            list(h.state.get("db_analyses") or []),
            session_id=h.ctx.session_id,
            thread_id=h.ctx.thread_id,
            run_id="run-target",
            state=h.state,
            archive=QaArchive(stack.services.store),
        )
        assert not failures or all("来源" in msg for msg in failures.values())


# ======================================================================
# R3-02 · P2 · SQL 占位符副作用前核验
# ======================================================================


class TestR302PlaceholderPrecheck:
    def _two_queries(self, h):
        h.fresh("run-ph")
        h.task([{"op": "add", "sub_task_id": "T1", "content": "上游"}])
        _, q1, _ = h.query("SELECT * FROM public.orders")
        _, q2, _ = h.query("SELECT * FROM public.orders WHERE orders.id > 1")
        return q1["data"], q2["data"]

    def _counters(self, stack, h):
        return (
            len(stack.db.explained),
            len(stack.db.executed),
            len(h.state.get("db_validations") or {}),
            len(h.state.get("db_executions") or []),
        )

    def test_unselected_placeholder_rejected_before_any_side_effect(self, stack):
        """未选成果占位符：validate/execute 均拒绝，四类副作用增量为 0。"""
        h = Harness(stack)
        q1, q2 = self._two_queries(h)
        done, _ = h.task(
            [
                {
                    "op": "status",
                    "sub_task_id": "T1",
                    "status": "completed",
                    "evidence_ids": [q2["evidence_id"]],
                }
            ]
        )
        assert done["status"] == "ok", done
        h.task(
            [
                {
                    "op": "add",
                    "sub_task_id": "T2",
                    "content": "下游",
                    "dependencies": [{"sub_task_id": "T1", "revision": 1}],
                }
            ]
        )
        before = self._counters(stack, h)
        sql = (
            "SELECT orders.id FROM public.orders WHERE orders.id IN "
            "{{result:" + q1["result_ref"] + ":id}}"
        )
        v, _ = h.call(
            "validate_sql",
            {"sql": sql, "sub_task_id": "T2", "sub_task_revision": 1},
        )
        e, _ = h.call(
            "execute_sql",
            {"sql": sql, "sub_task_id": "T2", "sub_task_revision": 1},
        )
        assert v["status"] == "error" and e["status"] == "error", (v, e)
        assert "stale_task_inputs" in json.dumps(v, ensure_ascii=False)
        after = self._counters(stack, h)
        assert after == before, (before, after)

    def test_selected_upstream_placeholder_succeeds(self, stack):
        """合法成功路径：占位符消费上游已选定成果 → validate/execute 成功。"""
        h = Harness(stack)
        _q1, q2 = self._two_queries(h)
        done, _ = h.task(
            [
                {
                    "op": "status",
                    "sub_task_id": "T1",
                    "status": "completed",
                    "evidence_ids": [q2["evidence_id"]],
                }
            ]
        )
        assert done["status"] == "ok", done
        h.task(
            [
                {
                    "op": "add",
                    "sub_task_id": "T2",
                    "content": "下游",
                    "dependencies": [{"sub_task_id": "T1", "revision": 1}],
                }
            ]
        )
        sql = (
            "SELECT orders.id FROM public.orders WHERE orders.id IN "
            "{{result:" + q2["result_ref"] + ":id}}"
        )
        v, _ = h.call(
            "validate_sql",
            {"sql": sql, "sub_task_id": "T2", "sub_task_revision": 1},
        )
        e, u = h.call(
            "execute_sql",
            {"sql": sql, "sub_task_id": "T2", "sub_task_revision": 1},
        )
        assert v["status"] == "ok" and e["status"] == "ok", (v, e)
        assert u.get("db_executions")

    def test_same_task_exploration_placeholder_succeeds(self, stack):
        """合法成功路径：同任务探索输入仍可合法使用。"""
        h = Harness(stack)
        q1, _q2 = self._two_queries(h)
        sql = (
            "SELECT orders.id FROM public.orders WHERE orders.id IN "
            "{{result:" + q1["result_ref"] + ":id}}"
        )
        v, _ = h.call(
            "validate_sql",
            {"sql": sql, "sub_task_id": "T1", "sub_task_revision": 1},
        )
        e, _ = h.call(
            "execute_sql",
            {"sql": sql, "sub_task_id": "T1", "sub_task_revision": 1},
        )
        assert v["status"] == "ok" and e["status"] == "ok", (v, e)

    def test_invalid_ancestor_placeholder_rejected_before_side_effect(self, stack):
        """失效祖先（已选成果的结构化输入缺源）→ validate/execute 副作用前拒绝。"""
        h = Harness(stack)
        h.fresh("run-ph")
        h.task([{"op": "add", "sub_task_id": "T1", "content": "上游"}])
        _, q, _ = h.query("SELECT * FROM public.orders")
        eid = q["data"]["evidence_id"]
        # 故障注入：该成果声称有一个不可核验的祖先输入
        for rec in h.state["db_executions"]:
            if rec["evidence_id"] == eid:
                rec["input_result_refs"] = [
                    _input_edge(
                        "res_" + "d" * 32,
                        _ref("run-ph", "E-ghost"),
                        _task_ref("run-ph"),
                    )
                ]
        done, _ = h.task(
            [
                {
                    "op": "status",
                    "sub_task_id": "T1",
                    "status": "completed",
                    "evidence_ids": [eid],
                }
            ]
        )
        # completed 登记本身的防御复核也会拒绝坏血缘；这里仍继续验证下游
        # SQL 占位符在副作用前拒绝（注入绕过登记后的防御路径）
        assert done["status"] == "error", done
        h.task(
            [
                {
                    "op": "add",
                    "sub_task_id": "T2",
                    "content": "下游",
                    "dependencies": [{"sub_task_id": "T1", "revision": 1}],
                }
            ]
        )
        before = self._counters(stack, h)
        sql = (
            "SELECT orders.id FROM public.orders WHERE orders.id IN "
            "{{result:" + q["data"]["result_ref"] + ":id}}"
        )
        v, _ = h.call(
            "validate_sql",
            {"sql": sql, "sub_task_id": "T2", "sub_task_revision": 1},
        )
        e, _ = h.call(
            "execute_sql",
            {"sql": sql, "sub_task_id": "T2", "sub_task_revision": 1},
        )
        assert v["status"] == "error" and e["status"] == "error", (v, e)
        assert self._counters(stack, h) == before


# ======================================================================
# R3-03 · P2 · 畸形血缘严格拒绝（工具/登记/提交/Runner/清理一致）
# ======================================================================


class TestR303StrictLineage:
    MALFORMED: ClassVar[list[dict]] = [
        {"input_result_refs": ["res_old_string_protocol"]},
        {"input_result_refs": [{"result_ref": "res_missing_input"}]},
        {"direct_reuse_from": {"source_task_ref": _task_ref("run-bad", "MISSING")}},
    ]

    @pytest.mark.parametrize("changes", MALFORMED)
    def test_malformed_sealed_source_rejected_by_real_tool_path(self, stack, changes):
        """三类损坏历史来源经真实 ToolNode：reuse 拒绝，不写沿用成果。"""
        h = Harness(stack)
        h.fresh("run-source")
        h.task([{"op": "add", "sub_task_id": "T1", "content": "来源"}])
        _, q, _ = h.query("SELECT * FROM public.orders")
        done, _ = h.task(
            [
                {
                    "op": "status",
                    "sub_task_id": "T1",
                    "status": "completed",
                    "evidence_ids": [q["data"]["evidence_id"]],
                }
            ]
        )
        assert done["status"] == "ok", done
        source = next(
            r for r in h.state["db_executions"] if r["run_id"] == "run-source"
        )
        source_task = next(
            t for t in h.state["db_sub_tasks"].values() if t["run_id"] == "run-source"
        )
        run = "run-bad"
        rec = {**source, **changes, "run_id": run}
        t = {**source_task, "run_id": run}
        archive = QaArchive(stack.services.store)
        _sealed(
            archive,
            h.ctx.session_id,
            h.ctx.thread_id,
            run,
            tasks=[t],
            executions=[rec],
        )
        h.state = {}
        h.fresh("run-target")
        h.task([{"op": "add", "sub_task_id": "T1", "content": "坏来源目标"}])
        h.ctx = replace(
            h.ctx,
            carried_evidence=(
                {
                    "kind": "execution",
                    "record": rec,
                    "origin": {
                        "thread_id": h.ctx.thread_id,
                        "run_id": run,
                        "evidence_id": rec["evidence_id"],
                    },
                },
            ),
        )
        p, updates = h.call(
            "reuse_evidence",
            {
                "source_evidence_refs": [
                    {
                        "thread_id": h.ctx.thread_id,
                        "run_id": run,
                        "evidence_id": rec["evidence_id"],
                    }
                ],
                "sub_task_id": "T1",
                "sub_task_revision": 1,
            },
        )
        assert p.get("error") is not None, p
        assert not updates.get("db_executions") and not updates.get("db_analyses")

    def test_wrong_container_and_missing_task_ref_rejected(self):
        """错误容器类型与缺任务引用的输入边明确拒绝。"""
        from src.modules.db_deepagent.services.reuse_qualification import (
            SourceUnqualified,
            evidence_edges,
        )

        with pytest.raises(SourceUnqualified, match="容器"):
            evidence_edges({"input_result_refs": {"result_ref": "res_x"}})
        with pytest.raises(SourceUnqualified, match="source_task_ref"):
            evidence_edges(
                {
                    "input_result_refs": [
                        {
                            "result_ref": "res_x",
                            "evidence_ref": _ref("run-x", "E-x"),
                        }
                    ]
                }
            )
        with pytest.raises(SourceUnqualified, match="不是映射"):
            evidence_edges({"input_result_refs": [None]})

    def test_four_entries_reject_malformed_outcome(self, stack):
        """畸形血缘在当前轮成果上：completed 登记/提交/Runner 一致拒绝。"""
        h = Harness(stack)
        h.fresh("run-bad")
        h.task([{"op": "add", "sub_task_id": "T1", "content": "畸形血缘成果"}])
        _, q, _ = h.query("SELECT * FROM public.orders")
        eid = q["data"]["evidence_id"]
        # 故障注入：把合法成果的结构化输入改成旧协议裸字符串
        for rec in h.state["db_executions"]:
            if rec["evidence_id"] == eid:
                rec["input_result_refs"] = ["res_old_string_protocol"]
        done, _ = h.task(
            [
                {
                    "op": "status",
                    "sub_task_id": "T1",
                    "status": "completed",
                    "evidence_ids": [eid],
                }
            ]
        )
        assert done["status"] == "error" and "stale_task_inputs" in json.dumps(
            done, ensure_ascii=False
        ), done

        from src.modules.db_deepagent.services.reuse_qualification import (
            unqualified_reuse_evidence,
        )

        failures = unqualified_reuse_evidence(
            [r for r in h.state["db_executions"] if r["run_id"] == "run-bad"],
            session_id=h.ctx.session_id,
            thread_id=h.ctx.thread_id,
            run_id="run-bad",
            state=h.state,
            archive=QaArchive(stack.services.store),
        )
        assert eid in failures, failures

        from src.modules.db_deepagent.tools.final_answer import (
            build_completion_snapshot,
        )

        snap, err = build_completion_snapshot(
            h.state,
            "run-bad",
            services=stack.services,
            session_id=h.ctx.session_id,
            thread_id=h.ctx.thread_id,
        )
        assert err is None and eid in snap.reuse_failures, (err, snap.reuse_failures)

        runner = DbAgentRunner.__new__(DbAgentRunner)
        runner._services = stack.services
        runner._archive = QaArchive(stack.services.store)
        decision = runner._task_completion_decision(
            h.state,
            "run-bad",
            _submission("completed", [_finding("F-1", "结论", "T1", 1, [eid])]),
            {},
            session_id=h.ctx.session_id,
            thread_id=h.ctx.thread_id,
        )
        assert not decision.accepted

    def test_normal_empty_and_full_edges_accepted(self):
        """正常空输入/无 direct 来源与合法完整边仍成功解析。"""
        from src.modules.db_deepagent.services.reuse_qualification import (
            evidence_edges,
        )

        assert evidence_edges({"input_result_refs": []}) == []
        assert evidence_edges({}) == []
        edges = evidence_edges(
            {
                "input_result_refs": [
                    _input_edge(
                        "res_" + "1" * 32,
                        _ref("run-x", "E-x"),
                        _task_ref("run-x"),
                    )
                ]
            },
            session_id="s-test",
        )
        assert len(edges) == 1 and edges[0]["kind"] == "input"

    def test_malformed_edge_stop_actual_cleanup(self, tmp_path):
        """清理控制流：解析失败报告问题并保守停止，不降级删除。"""
        now = datetime.now(UTC)
        ref = "res_" + "a" * 32
        _old_result_dirs(tmp_path, ref)
        archive = QaArchive(InMemoryStore())
        _sealed(
            archive,
            "s1",
            "dbda:s1:1",
            "r1",
            tasks=[],
            executions=[
                {
                    "evidence_id": "E-bad",
                    "run_id": "r1",
                    "result_ref": ref,
                    "input_result_refs": ["res_old_string_protocol"],
                }
            ],
        )
        report = _dry_run(_cleanup_service(tmp_path, archive), now)
        assert report.result_dirs == [], report.result_dirs
        assert any("血缘解析失败" in item["reason"] for item in report.skipped)
        assert any("跳过该会话全部" in item["reason"] for item in report.skipped)

    def test_malformed_edge_does_not_break_runner_fallback(self, assembly_services):
        """畸形血缘不穿透 Runner 破坏 6.2 兜底：受控门控失败 + 系统兜底。"""
        from src.tests.db_deepagent.unit.test_runner_persistence import (
            _run_ok,
            _state,
        )

        state = _state()
        state["db_executions"][0]["input_result_refs"] = ["res_old_string_protocol"]
        result, _ = _run_ok(assembly_services, FakeStore(), state)
        assert result.status == "failed"
        assert result.answer.startswith("系统兜底说明")
        assert any("服务端兜底" in w for w in result.warnings)
        assert not result.findings


# ======================================================================
# R3-04 · P2 · 阻塞决策生成/复核共用决策时事实
# ======================================================================


class TestR304HistoricalBlockedContract:
    def _blocked_state(self):
        s = _merge(
            {},
            _apply(
                {},
                [
                    ("add", {"sub_task_id": "T1", "content": "时间口径"}),
                    (
                        "add",
                        {
                            "sub_task_id": "T2",
                            "content": "统计销售额",
                            "dependencies": [{"sub_task_id": "T1", "revision": 1}],
                        },
                    ),
                ],
                0,
            ),
        )
        return _merge(
            s,
            _apply(
                s,
                [
                    (
                        "status",
                        {
                            "sub_task_id": "T1",
                            "status": "needs_clarification",
                            "reason": "时间不明确",
                            "clarification_question": "哪一年？",
                        },
                    ),
                    (
                        "status",
                        {
                            "sub_task_id": "T2",
                            "status": "failed",
                            "reason_code": "dependency_blocked",
                            "reason": "上游阻塞",
                            "blocked_by": [{"sub_task_id": "T1", "revision": 1}],
                        },
                    ),
                ],
                1,
            ),
        )

    def _decide(self, state, status="failed"):
        snap = _snapshot_of(state, "r")
        unresolved = [
            {"sub_task_id": tid, "revision": rec["revision"]}
            for tid, rec in snap.tasks.items()
            if rec.get("status") in ("failed", "needs_clarification")
        ]
        return evaluate_submission(_submission(status, [], unresolved=unresolved), snap)

    def test_a_upstream_later_void_keeps_decision_and_accepts(self):
        """A：同 revision 后来 void，旧决策上下文不变，最终仍接受。"""
        s = self._blocked_state()
        key = task_ledger_key("r", "T2", 1)
        before = copy.deepcopy(s["db_sub_tasks"][key]["termination_context"])
        s = _merge(
            s,
            _apply(
                s,
                [("void", {"sub_task_id": "T1", "reason": "本轮取消时间口径任务"})],
                2,
            ),
        )
        assert s["db_sub_tasks"][key]["termination_context"] == before
        t1 = s["db_sub_tasks"][task_ledger_key("r", "T1", 1)]
        assert t1["status"] == "voided"
        assert t1["pre_void_terminal"]["status"] == "needs_clarification"
        assert t1["pre_void_terminal"]["reason"] == "时间不明确"
        d = self._decide(s)
        assert d.accepted, [e.message for e in d.errors]

    def test_b_new_downstream_traverses_fixed_root(self):
        """B：新下游沿旧阻塞链追根，保存 T1@1 而非追到 T1@2/NEW_ROOT。"""
        s = self._blocked_state()
        s = _merge(
            s,
            _apply(s, [("revise", {"sub_task_id": "T1", "content": "新时间口径"})], 2),
        )
        s = _merge(
            s,
            _apply(
                s,
                [
                    (
                        "status",
                        {
                            "sub_task_id": "T1",
                            "status": "failed",
                            "reason_code": "agent_abandoned",
                            "reason": "NEW_ROOT",
                        },
                    )
                ],
                3,
            ),
        )
        s = _merge(
            s,
            _apply(
                s,
                [
                    (
                        "add",
                        {
                            "sub_task_id": "T3",
                            "content": "新下游统计",
                            "dependencies": [{"sub_task_id": "T2", "revision": 1}],
                        },
                    )
                ],
                4,
            ),
        )
        s = _merge(
            s,
            _apply(
                s,
                [
                    (
                        "status",
                        {
                            "sub_task_id": "T3",
                            "status": "failed",
                            "reason_code": "dependency_blocked",
                            "reason": "上游阻塞",
                            "blocked_by": [{"sub_task_id": "T2", "revision": 1}],
                        },
                    )
                ],
                5,
            ),
        )
        t3_root = s["db_sub_tasks"][task_ledger_key("r", "T3", 1)][
            "termination_context"
        ]["blocked_by_root"]
        t2_root = s["db_sub_tasks"][task_ledger_key("r", "T2", 1)][
            "termination_context"
        ]["blocked_by_root"]
        assert t3_root == t2_root
        assert t3_root["revision"] == 1 and "NEW_ROOT" not in json.dumps(t3_root)
        d = self._decide(s)
        assert d.accepted, [e.message for e in d.errors]

    def test_c_empty_snapshots_and_forged_plan_rejected(self):
        """C：dependency_snapshots 为空 + decision_plan_version 伪造 → 拒绝。"""
        s = self._blocked_state()
        ctx = s["db_sub_tasks"][task_ledger_key("r", "T2", 1)]["termination_context"]
        ctx["dependency_snapshots"] = []
        ctx["decision_plan_version"] = "forged"
        d = self._decide(s, "needs_clarification")
        assert not d.accepted
        assert any("decision_plan_version" in e.message for e in d.errors)
        assert any("dependency_snapshots" in e.message for e in d.errors)

    def test_d_forged_second_blocked_by_rejected(self):
        """D：追加未经原决策覆盖的第二个阻塞来源 → 拒绝。"""
        s = _merge(
            {},
            _apply(
                {},
                [
                    ("add", {"sub_task_id": "T1", "content": "根"}),
                    ("add", {"sub_task_id": "T2", "content": "另一根"}),
                    (
                        "add",
                        {
                            "sub_task_id": "T3",
                            "content": "下游",
                            "dependencies": [
                                {"sub_task_id": "T1", "revision": 1},
                                {"sub_task_id": "T2", "revision": 1},
                            ],
                        },
                    ),
                ],
                0,
            ),
        )
        s = _merge(
            s,
            _apply(
                s,
                [
                    (
                        "status",
                        {
                            "sub_task_id": "T1",
                            "status": "failed",
                            "reason_code": "agent_abandoned",
                            "reason": "ROOT",
                        },
                    ),
                    (
                        "status",
                        {
                            "sub_task_id": "T3",
                            "status": "failed",
                            "reason_code": "dependency_blocked",
                            "reason": "占位",
                            "blocked_by": [{"sub_task_id": "T1", "revision": 1}],
                        },
                    ),
                ],
                1,
            ),
        )
        s = _merge(
            s,
            _apply(
                s,
                [
                    (
                        "status",
                        {
                            "sub_task_id": "T2",
                            "status": "failed",
                            "reason_code": "agent_abandoned",
                            "reason": "later fail",
                        },
                    )
                ],
                2,
            ),
        )
        key = task_ledger_key("r", "T3", 1)
        # 故障注入：追加未被原决策依据覆盖的第二个 blocked_by
        s["db_sub_tasks"][key]["blocked_by"].append(
            {"sub_task_id": "T2", "revision": 1}
        )
        d = self._decide(s)
        assert not d.accepted

    def test_shared_helpers_used_by_generation_and_review(self):
        """生成与复核共用同一 root 推导；非法决策版本逐项拒绝。"""
        s = self._blocked_state()

        def lookup(tid, rev):
            return s["db_sub_tasks"].get(task_ledger_key("r", tid, rev))

        root = derive_decision_root(lookup, ("T1", 1))
        t2 = s["db_sub_tasks"][task_ledger_key("r", "T2", 1)]
        assert root_matches(t2["termination_context"]["blocked_by_root"], root)
        assert (
            validate_blocked_decision(t2, lookup=lookup, plan_version=2, run_id="r")
            == []
        )
        bad = copy.deepcopy(t2)
        bad["termination_context"]["decision_plan_version"] = 999
        assert validate_blocked_decision(bad, lookup=lookup, plan_version=2, run_id="r")
        assert (
            terminal_facts(
                {
                    "status": "voided",
                    "pre_void_terminal": {"status": "failed", "reason": "x"},
                }
            )["status"]
            == "failed"
        )

    @pytest.mark.parametrize("reverse", [False, True])
    def test_batch_order_independent_and_reducer_applied(self, reverse):
        """正反序批量终止结果一致，且经真实 reducer 应用。"""
        ops = [
            ("add", {"sub_task_id": "T1", "content": "根上游"}),
            (
                "add",
                {
                    "sub_task_id": "T2",
                    "content": "中游",
                    "dependencies": [{"sub_task_id": "T1", "revision": 1}],
                },
            ),
            (
                "add",
                {
                    "sub_task_id": "T3",
                    "content": "下游",
                    "dependencies": [{"sub_task_id": "T2", "revision": 1}],
                },
            ),
        ]
        terminal = [
            (
                "status",
                {
                    "sub_task_id": "T1",
                    "status": "failed",
                    "reason_code": "agent_abandoned",
                    "reason": "ROOT",
                },
            ),
            (
                "status",
                {
                    "sub_task_id": "T2",
                    "status": "failed",
                    "reason_code": "dependency_blocked",
                    "reason": "PLACEHOLDER",
                    "blocked_by": [{"sub_task_id": "T1", "revision": 1}],
                },
            ),
            (
                "status",
                {
                    "sub_task_id": "T3",
                    "status": "failed",
                    "reason_code": "dependency_blocked",
                    "reason": "PLACEHOLDER",
                    "blocked_by": [{"sub_task_id": "T2", "revision": 1}],
                },
            ),
        ]
        if reverse:
            terminal = list(reversed(terminal))
        state = _merge({}, _apply({}, ops, 0))
        out = _apply(state, terminal, 1)
        merged = merge_task_records(state["db_sub_tasks"], out.task_records)
        t3 = merged[task_ledger_key("r", "T3", 1)]
        assert "ROOT" in t3["reason"] and "PLACEHOLDER" not in t3["reason"]
        assert (
            t3["termination_context"]["dependency_snapshots"][0]["status"] == "failed"
        )


# ======================================================================
# R3-05 · P2 · 无保留根证据链可回收
# ======================================================================


class TestR305CleanupRootSet:
    SOURCE = "res_" + "a" * 32
    DERIVED = "res_" + "b" * 32

    def _input_chain_archive(self):
        archive = QaArchive(InMemoryStore())
        source = {
            "session_id": "s1",
            "thread_id": "dbda:s1:1",
            "run_id": "run-source",
            "evidence_id": "E-source",
            "purpose": "query",
            "result_ref": self.SOURCE,
            "sub_task_id": "T1",
            "sub_task_revision": 1,
            "input_result_refs": [],
        }
        derived = {
            "session_id": "s1",
            "thread_id": "dbda:s1:1",
            "run_id": "run-source",
            "evidence_id": "A-derived",
            "output_result_ref": self.DERIVED,
            "sub_task_id": "T1",
            "sub_task_revision": 1,
            "input_result_refs": [
                _input_edge(
                    self.SOURCE,
                    {
                        "session_id": "s1",
                        "thread_id": "dbda:s1:1",
                        "run_id": "run-source",
                        "evidence_id": "E-source",
                    },
                    {
                        "session_id": "s1",
                        "thread_id": "dbda:s1:1",
                        "run_id": "run-source",
                        "sub_task_id": "T1",
                        "revision": 1,
                    },
                )
            ],
        }
        _sealed(
            archive,
            "s1",
            "dbda:s1:1",
            "run-source",
            tasks=[],
            executions=[source],
            analyses=[derived],
        )
        return archive

    def test_orphan_input_chain_is_recoverable(self, tmp_path):
        """无根 input 链：实际清理控制流产生两个到期删除候选。"""
        now = datetime.now(UTC)
        _old_result_dirs(tmp_path, self.SOURCE)
        _old_result_dirs(tmp_path, self.DERIVED)
        svc = _cleanup_service(tmp_path, self._input_chain_archive())
        roots, snaps, root_problems = svc._retained_snapshot_roots(
            "s1", now - timedelta(days=365)
        )
        protected, orphans, problems = svc._protected_closure("s1", roots, snaps)
        assert roots == set() and snaps == set()
        assert not root_problems and not problems, problems
        assert protected == set(), protected
        assert orphans, "无保留根时运行快照应成为孤儿"
        assert all("run-source" in key for key in orphans)
        report = _dry_run(svc, now)
        assert sorted(x.split()[0] for x in report.result_dirs) == sorted(
            [self.SOURCE, self.DERIVED]
        ), report.result_dirs

    def test_orphan_direct_chain_is_recoverable(self, tmp_path):
        """无根 direct 沿用链（两个 run）：同样可回收。"""
        now = datetime.now(UTC)
        _old_result_dirs(tmp_path, self.SOURCE)
        archive = QaArchive(InMemoryStore())
        source = {
            "session_id": "s1",
            "thread_id": "dbda:s1:1",
            "run_id": "run-source",
            "evidence_id": "E-source",
            "result_ref": self.SOURCE,
            "sub_task_id": "T1",
            "sub_task_revision": 1,
            "input_result_refs": [],
        }
        reused = {
            **source,
            "run_id": "run-reuse",
            "evidence_id": "E-reused",
            "direct_reuse_from": {
                "evidence_ref": {
                    "session_id": "s1",
                    "thread_id": "dbda:s1:1",
                    "run_id": "run-source",
                    "evidence_id": "E-source",
                },
                "source_task_ref": {
                    "session_id": "s1",
                    "thread_id": "dbda:s1:1",
                    "run_id": "run-source",
                    "sub_task_id": "T1",
                    "revision": 1,
                },
            },
        }
        _sealed(
            archive,
            "s1",
            "dbda:s1:1",
            "run-source",
            tasks=[],
            executions=[source],
        )
        _sealed(
            archive,
            "s1",
            "dbda:s1:1",
            "run-reuse",
            tasks=[],
            executions=[reused],
        )
        svc = _cleanup_service(tmp_path, archive)
        protected, _orphans, problems = svc._protected_closure("s1", set(), set())
        assert protected == set() and not problems, (protected, problems)
        report = _dry_run(svc, now)
        assert any(
            self.SOURCE in item for item in report.result_dirs
        ), report.result_dirs

    def test_rooted_chain_recursively_protected(self, tmp_path):
        """有保留根（QA 引用派生输出）时递归保护来源与快照。"""
        now = datetime.now(UTC)
        _old_result_dirs(tmp_path, self.SOURCE)
        _old_result_dirs(tmp_path, self.DERIVED)
        archive = self._input_chain_archive()
        archive.write_qa(
            "s1",
            "req-root",
            {
                "evidence": [
                    {
                        "evidence_id": "A-derived",
                        "output_result_ref": self.DERIVED,
                        "input_result_refs": [],
                    }
                ],
            },
        )
        svc = _cleanup_service(tmp_path, archive)
        roots, snaps, root_problems = svc._retained_snapshot_roots(
            "s1", now - timedelta(days=365)
        )
        assert self.DERIVED in roots and not root_problems
        protected, _orphans, problems = svc._protected_closure("s1", roots, snaps)
        assert not problems, problems
        assert {self.SOURCE, self.DERIVED} <= protected, protected
        report = _dry_run(svc, now)
        assert report.result_dirs == [], report.result_dirs

    def test_remove_last_root_then_recoverable(self, tmp_path):
        """移除最后一个保留根后重新清理即可回收。"""
        now = datetime.now(UTC)
        _old_result_dirs(tmp_path, self.SOURCE)
        _old_result_dirs(tmp_path, self.DERIVED)
        archive = self._input_chain_archive()
        archive.write_qa(
            "s1",
            "req-root",
            {
                "evidence": [
                    {
                        "evidence_id": "A-derived",
                        "output_result_ref": self.DERIVED,
                        "input_result_refs": [],
                    }
                ]
            },
        )
        svc = _cleanup_service(tmp_path, archive)
        assert _dry_run(svc, now).result_dirs == []
        # 移除最后一个根（模拟归档保留期到后清理掉 QA）
        archive._store.delete(("db_deepagent", "qa", "s1"), "req-root")
        report = _dry_run(svc, now)
        assert sorted(x.split()[0] for x in report.result_dirs) == sorted(
            [self.SOURCE, self.DERIVED]
        ), report.result_dirs

    def test_read_failure_and_bad_graph_still_fail_closed(self, tmp_path):
        """读取失败/未知协议/缺源/环仍报告并保守停止删除。"""
        now = datetime.now(UTC)
        _old_result_dirs(tmp_path, self.SOURCE)
        broken = QaArchive(InMemoryStore())
        broken.write_run_errors("s1", "req1", {"evidence": [123]})
        report = _dry_run(_cleanup_service(tmp_path, broken), now)
        assert report.result_dirs == []
        assert any("跳过该会话全部" in item["reason"] for item in report.skipped)

        bad = QaArchive(InMemoryStore())
        _sealed(
            bad,
            "s1",
            "dbda:s1:1",
            "r1",
            tasks=[],
            executions=[
                {
                    "evidence_id": "E-bad",
                    "run_id": "r1",
                    "result_ref": self.SOURCE,
                    "direct_reuse_from": {
                        "evidence_ref": {
                            "session_id": "s1",
                            "thread_id": "dbda:s1:1",
                            "run_id": "r2",
                            "evidence_id": "E-missing",
                        },
                        "source_task_ref": {
                            "session_id": "s1",
                            "thread_id": "dbda:s1:1",
                            "run_id": "r2",
                            "sub_task_id": "T1",
                            "revision": 1,
                        },
                    },
                }
            ],
        )
        report2 = _dry_run(_cleanup_service(tmp_path, bad), now)
        assert report2.result_dirs == []
        assert any("缺源" in item["reason"] for item in report2.skipped)


# ======================================================================
# M1 · 实际预算/模型异常的 Runner 集成路径（R3 非阻塞建议）
# ======================================================================


class TestM1RunnerExceptionPaths:
    def test_model_exception_uses_fallback_and_priority(self, assembly_services):
        """真实 FakeAgent.invoke 异常 → 状态优先级/兜底/诊断/归档（6.2）。"""
        from src.tests.db_deepagent.unit.test_runner_persistence import (
            _runner,
            _state,
        )

        state = _state()
        agent = FakeAgent(state)
        agent.invoke_side_effect = RuntimeError("model down")
        store = FakeStore()
        result = _runner(assembly_services, agent, store, state).run("问", "s1")
        assert result.status == "failed"
        assert result.answer.startswith("系统兜底说明：")
        assert "未取得可确认的查询或分析结果" in result.answer
        assert any("服务端兜底" in w for w in result.warnings)
        assert not result.findings
        assert any(
            e.phase == "P1" and e.kind == "runtime" and e.primary for e in result.errors
        )
        # 6.2：运行诊断仍按 P2 账本归档（run_errors），不因异常丢失
        assert any(ns[1] == "run_errors" for (ns, _k) in store.items)

    def test_budget_stop_uses_fallback_and_status(self, assembly_services, monkeypatch):
        """真实 budget.close_run 返回 stop_reason → budget_exhausted + 兜底。"""
        from src.modules.db_deepagent.services.run_budget import BudgetSnapshot
        from src.tests.db_deepagent.unit.test_runner_persistence import (
            _runner,
            _state,
        )

        def fake_close_run(_run_id):
            return BudgetSnapshot(
                counters={"model_call": 40, "tool_call": 80, "db_execute": 0},
                stop_reason="model_call_limit",
            )

        monkeypatch.setattr(assembly_services.budget, "close_run", fake_close_run)
        state = _state(answer=None, executions=[])
        result = _runner(assembly_services, FakeAgent(state), FakeStore(), state).run(
            "问", "s1"
        )
        assert result.status == "budget_exhausted"
        assert "已达到运行次数或时间限制" in result.answer
        assert result.answer.startswith("系统兜底说明")

    def test_unverified_completion_not_claimed_under_exceptions(
        self, assembly_services, monkeypatch
    ):
        """模型异常 + 缺成果：兜底不把未核验 completed 说成成功。"""
        from src.modules.db_deepagent.services.run_budget import BudgetSnapshot
        from src.tests.db_deepagent.unit.test_runner_persistence import (
            _runner,
            _state,
        )

        def fake_close_run(_run_id):
            return BudgetSnapshot(stop_reason="model_call_limit")

        monkeypatch.setattr(assembly_services.budget, "close_run", fake_close_run)
        state = _state(answer=None)
        state["db_sub_tasks"][task_ledger_key("run-x", "T1", 1)]["evidence_ids"] = [
            "E-ghost"
        ]
        state["db_executions"] = []
        agent = FakeAgent(state)
        agent.invoke_side_effect = RuntimeError("model down")
        result = _runner(assembly_services, agent, FakeStore(), state).run("问", "s1")
        assert result.status in ("failed", "budget_exhausted")
        assert "已完成：统计 9 月订单" not in result.answer
        assert result.answer.startswith("系统兜底说明")
