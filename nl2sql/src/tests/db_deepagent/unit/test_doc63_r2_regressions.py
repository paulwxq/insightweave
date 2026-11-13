"""doc 6.3 R2 审核反例回归（codex_doc63_impl_review_r2.md 逐项）

以 R2-01…R2-13 为验收清单：每项同时覆盖「反例必须被拒绝」与「合法
成功路径必须通过」，并补完整路径成功/失败用例：
- reuse → completed → submit → Runner（R2-01）；
- TaskLedger 输出 → 实际 reducer 应用（R2-04）；
- 实际 cleanup 控制流（R2-03）。

不手工拼最终状态或预设拒绝结果证明修复：工具路径用真实 ToolNode、
真实服务栈与内存 Store/FakeExecDb；业务数据库为 FakeExecDb，不连真实
PG、不调用真实模型、不删除真实结果。
"""

from __future__ import annotations

import copy
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from langgraph.prebuilt import ToolNode
from langgraph.store.memory import InMemoryStore

from src.modules.db_deepagent.config import load_db_agent_config
from src.modules.db_deepagent.persistence.archive import QaArchive
from src.modules.db_deepagent.persistence.maintenance import (
    CleanupReport,
    MaintenanceService,
)
from src.modules.db_deepagent.runner import DbAgentRunner
from src.modules.db_deepagent.schemas import DbAgentFinalAnswer
from src.modules.db_deepagent.services import DbAgentServices
from src.modules.db_deepagent.services.dimension_probe import DimensionProbeBuilder
from src.modules.db_deepagent.services.result_analysis import (
    AnalysisError,
    ResultAnalysisService,
)
from src.modules.db_deepagent.services.result_store import ResultStore
from src.modules.db_deepagent.services.run_budget import BudgetLimits, RunBudgetLedger
from src.modules.db_deepagent.services.sql_guard import SqlEvidenceGuard
from src.modules.db_deepagent.services.sql_pipeline import SqlPipeline
from src.modules.db_deepagent.services.sql_validation import DbSqlValidator
from src.modules.db_deepagent.services.task_completion import (
    CompletionSnapshot,
    build_delivery_answer_section,
    evaluate_submission,
)
from src.modules.db_deepagent.services.task_ledger import (
    LedgerOperation,
    TaskLedger,
    TaskLedgerError,
    current_task_view,
    task_ledger_key,
)
from src.modules.db_deepagent.state import (
    LedgerConsistencyError,
    merge_task_plans,
    merge_task_records,
)
from src.modules.db_deepagent.tools.final_answer import (
    build_completion_snapshot,
    make_submit_final_answer_tool,
)
from src.modules.db_deepagent.tools.guard_tools import make_reuse_evidence_tool
from src.modules.db_deepagent.tools.result_tools import make_analyze_result_tool
from src.modules.db_deepagent.tools.sql_tools import (
    make_execute_sql_tool,
    make_validate_sql_tool,
)
from src.modules.db_deepagent.tools.task_tools import make_record_sub_tasks_tool
from src.tests.db_deepagent.conftest import (
    FakeExecDb,
    FakeStore,
    invoke_tool_node,
    make_task_record,
    make_task_state,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 13, 10, 0, tzinfo=UTC)


# ======================================================================
# 真实工具栈（guard 关闭以便聚焦任务/证据门控；业务库为 FakeExecDb）
# ======================================================================


@pytest.fixture()
def stack(db_agent_context, tmp_path):
    cfg = load_db_agent_config()
    cfg.validation.guard.enabled = False
    cfg.workspace.root = str(tmp_path)
    results = ResultStore(Path(tmp_path))
    db = FakeExecDb()
    budget = RunBudgetLedger()
    guard = SqlEvidenceGuard(cfg.validation.guard, db, DimensionProbeBuilder())
    pipeline = SqlPipeline(
        config=cfg,
        validator=DbSqlValidator(cfg.validation, db, explain_timeout_s=1),
        guard=guard,
        db=db,
        results=results,
        budget=budget,
    )
    services = DbAgentServices(
        config=cfg,
        business_db=db,
        embedding=None,
        milvus=None,
        catalog=None,
        join_graph=None,
        embedding_release={},
        source_id="orders",
        pipeline=pipeline,
        results=results,
        analysis=ResultAnalysisService(cfg, results),
        budget=budget,
    )
    services.store = InMemoryStore()
    node = ToolNode(
        [
            make_record_sub_tasks_tool(services),
            make_validate_sql_tool(services),
            make_execute_sql_tool(services),
            make_analyze_result_tool(services),
            make_reuse_evidence_tool(services),
            make_submit_final_answer_tool(services),
        ]
    )
    return SimpleNamespace(
        cfg=cfg,
        ctx=db_agent_context,
        results=results,
        db=db,
        budget=budget,
        services=services,
        node=node,
    )


class Harness:
    """累积真实 ToolNode 的 Command 更新；不手工拼最终状态。"""

    def __init__(self, stack):
        self.stack = stack
        self.ctx = stack.ctx
        self.state: dict = {}
        self.n = 0

    def call(self, name, args, context=None):
        self.n += 1
        updates, message = invoke_tool_node(
            self.stack.node,
            [(name, args, "probe-" + str(self.n))],
            context or self.ctx,
            self.state,
        )
        for key, value in updates.items():
            if key == "messages":
                continue
            if isinstance(value, dict):
                self.state[key] = {**self.state.get(key, {}), **value}
            elif isinstance(value, list):
                self.state[key] = self.state.get(key, []) + value
            else:
                self.state[key] = value
        try:
            payload = json.loads(message.content)
        except (ValueError, TypeError):
            payload = message.content
        return payload, updates

    def fresh(self, run_id, thread_id=None, **kw):
        self.ctx = replace(
            self.ctx,
            run_id=run_id,
            request_id="req-" + run_id,
            thread_id=thread_id or self.ctx.thread_id,
            **kw,
        )
        self.stack.budget.open_run(run_id, BudgetLimits(100, 100, 100), 1e12)

    def task(self, ops):
        run = self.ctx.run_id
        ver = self.state.get("db_task_plans", {}).get(run, {}).get("plan_version", 0)
        return self.call(
            "record_sub_tasks",
            {"expected_plan_version": ver, "operations": ops},
        )

    def query(self, sql, tid="T1", rev=1):
        a, _ = self.call(
            "validate_sql",
            {"sql": sql, "sub_task_id": tid, "sub_task_revision": rev},
        )
        b, u = self.call(
            "execute_sql",
            {"sql": sql, "sub_task_id": tid, "sub_task_revision": rev},
        )
        return a, b, u


def _evidence(
    eid,
    run_id,
    tid="T1",
    rev=1,
    *,
    result_ref=None,
    session_id=None,
    thread_id=None,
    **extra,
):
    # 合成记录默认不写内部 scope，让装配层用显式传入的可信 ctx 身份；
    # 需要验证 R2-10 携带 scope 时才显式给出。
    rec = {
        "evidence_id": eid,
        "tool_call_id": "tc-" + eid,
        "run_id": run_id,
        "request_id": "req-" + run_id,
        "purpose": "query",
        "datasource_id": "orders",
        "sql_template": "SELECT 1",
        "executed_sql": "SELECT 1",
        "executed_sql_sha256": "h",
        "result_ref": result_ref or ("res_" + eid[-1] * 32),
        "columns": [],
        "saved_row_count": 1,
        "source_truncated": False,
        "elapsed_ms": 1,
        "warnings": [],
        "sub_task_id": tid,
        "sub_task_revision": rev,
        "input_result_refs": [],
    }
    if session_id is not None:
        rec["session_id"] = session_id
    if thread_id is not None:
        rec["thread_id"] = thread_id
    rec.update(extra)
    return rec


def _ref(run_id, eid, thread="t-test", session="s-test"):
    return {
        "session_id": session,
        "thread_id": thread,
        "run_id": run_id,
        "evidence_id": eid,
    }


def _submission(status, findings, unresolved=(), answer="已处理"):
    return DbAgentFinalAnswer(
        status=status,
        answer=answer,
        findings=findings,
        metric_definitions=[],
        limitations=[],
        open_questions=[],
        unresolved_task_refs=list(unresolved),
    ).model_dump()


def _finding(fid, text, tid, rev, eids):
    return {
        "finding_id": fid,
        "text": text,
        "task_refs": [{"sub_task_id": tid, "revision": rev}],
        "evidence_ids": list(eids),
    }


def _snapshot_of(state, run_id, *, session_id="s-test", thread_id="t-test"):
    snap, err = build_completion_snapshot(
        state,
        run_id,
        session_id=session_id,
        thread_id=thread_id,
    )
    assert err is None, err
    return snap


# ======================================================================
# R2-01 / R2-02：完整路径 reuse → completed → submit → Runner
# ======================================================================


class TestR201FullPath:
    def test_legal_reuse_completed_submit_runner_same_thread(self, stack):
        """真实工具链：旧 run 查询并 completed → 新 run 沿用 → completed →
        submit → Runner 判定全部成功（R2-01 反例转正例）。"""
        h = Harness(stack)
        h.fresh("run-origin")
        h.task([{"op": "add", "sub_task_id": "T1", "content": "来源任务"}])
        _, q, _ = h.query("SELECT * FROM public.orders")
        root = q["data"]
        h.task(
            [
                {
                    "op": "status",
                    "sub_task_id": "T1",
                    "status": "completed",
                    "evidence_ids": [root["evidence_id"]],
                }
            ]
        )

        h.fresh("run-reuse")
        h.task([{"op": "add", "sub_task_id": "T1", "content": "沿用任务"}])
        p, _ = h.call(
            "reuse_evidence",
            {
                "source_evidence_refs": [root["source_evidence_ref"]],
                "sub_task_id": "T1",
                "sub_task_revision": 1,
            },
        )
        assert p["status"] == "ok", p
        eid = p["data"]["mapping"][0]["new_id"]
        completed, _ = h.task(
            [
                {
                    "op": "status",
                    "sub_task_id": "T1",
                    "status": "completed",
                    "evidence_ids": [eid],
                }
            ]
        )
        assert completed["status"] == "ok", completed

        # 提交快照显式使用可信 ctx 会话身份，不再出现「来源引用不属于当前会话」
        snap, err = build_completion_snapshot(
            h.state,
            h.ctx.run_id,
            services=stack.services,
            session_id=h.ctx.session_id,
            thread_id=h.ctx.thread_id,
            carried=tuple(h.ctx.carried_evidence or ()),
        )
        assert err is None
        assert snap.reuse_failures == {}, snap.reuse_failures

        submit, _ = h.call(
            "submit_final_answer",
            _submission("completed", [_finding("F-1", "沿用结论", "T1", 1, [eid])]),
        )
        assert submit == "已接收最终答案", submit
        assert h.ctx.run_id in (h.state.get("db_final_answers") or {})

        # Runner 从最终 checkpoint 使用同一共享规则再判定
        decision = evaluate_submission(
            _submission("completed", [_finding("F-1", "沿用结论", "T1", 1, [eid])]),
            snap,
        )
        assert decision.accepted, [e.code for e in decision.errors]
        runner = DbAgentRunner.__new__(DbAgentRunner)
        runner._services = stack.services
        runner._archive = QaArchive(stack.services.store)
        runner_decision = runner._task_completion_decision(
            h.state,
            h.ctx.run_id,
            _submission("completed", [_finding("F-1", "沿用结论", "T1", 1, [eid])]),
            {},
            session_id=h.ctx.session_id,
            thread_id=h.ctx.thread_id,
        )
        assert runner_decision.accepted, [e.code for e in runner_decision.errors]

    def test_legal_reuse_cross_thread_snapshot(self, stack):
        """跨线程沿用：来源封存在旧线程快照，新线程按完整身份沿用成功。"""
        h = Harness(stack)
        h.fresh("run-origin", "t-old")
        h.task([{"op": "add", "sub_task_id": "T1", "content": "来源"}])
        _, q, _ = h.query("SELECT * FROM public.orders")
        root = q["data"]
        h.task(
            [
                {
                    "op": "status",
                    "sub_task_id": "T1",
                    "status": "completed",
                    "evidence_ids": [root["evidence_id"]],
                }
            ]
        )
        source_rec = next(
            r for r in h.state["db_executions"] if r["run_id"] == "run-origin"
        )
        source_task = next(
            t for t in h.state["db_sub_tasks"].values() if t["run_id"] == "run-origin"
        )
        archive = QaArchive(stack.services.store)
        archive.write_run_snapshot(
            h.ctx.session_id,
            "t-old",
            "run-origin",
            {
                "run_snapshot_schema_version": 1,
                "ledger_protocol_version": 1,
                "session_id": h.ctx.session_id,
                "thread_id": "t-old",
                "run_id": "run-origin",
                "tasks": [source_task],
                "executions": [source_rec],
                "analyses": [],
                "tool_errors": [],
            },
        )
        carried = (
            {
                "kind": "execution",
                "record": source_rec,
                "origin": {
                    "session_id": h.ctx.session_id,
                    "thread_id": "t-old",
                    "run_id": "run-origin",
                    "evidence_id": source_rec["evidence_id"],
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
                    _ref(
                        "run-origin",
                        source_rec["evidence_id"],
                        thread="t-old",
                        session=h.ctx.session_id,
                    )
                ],
                "sub_task_id": "T1",
                "sub_task_revision": 1,
            },
        )
        assert p["status"] == "ok", p
        eid = p["data"]["mapping"][0]["new_id"]
        completed, _ = h.task(
            [
                {
                    "op": "status",
                    "sub_task_id": "T1",
                    "status": "completed",
                    "evidence_ids": [eid],
                }
            ]
        )
        assert completed["status"] == "ok", completed

    def test_source_revised_reuse_rejected(self, stack):
        """反例：来源任务后续 revise → 沿用必须拒绝（旧成果不可沿用）。"""
        h = Harness(stack)
        h.fresh("run-origin")
        h.task([{"op": "add", "sub_task_id": "T1", "content": "来源"}])
        _, q, _ = h.query("SELECT * FROM public.orders")
        root = q["data"]
        h.task(
            [
                {
                    "op": "status",
                    "sub_task_id": "T1",
                    "status": "completed",
                    "evidence_ids": [root["evidence_id"]],
                }
            ]
        )
        h.task([{"op": "revise", "sub_task_id": "T1", "content": "新版"}])
        h.fresh("run-reuse")
        h.task([{"op": "add", "sub_task_id": "T1", "content": "沿用"}])
        p, _ = h.call(
            "reuse_evidence",
            {
                "source_evidence_refs": [root["source_evidence_ref"]],
                "sub_task_id": "T1",
                "sub_task_revision": 1,
            },
        )
        assert p.get("error", {}).get("error_type") == "stale_task_inputs", p


class TestR202EvidenceGraph:
    def test_unselected_cross_task_analysis_input_rejected(self, stack):
        """反例 1：上游只选定 E2，下游消费未选成果 E1 分析 → 工具拒绝。"""
        h = Harness(stack)
        h.fresh("run-sel")
        h.task([{"op": "add", "sub_task_id": "T1", "content": "上游"}])
        _, q1, _ = h.query("SELECT * FROM public.orders")
        _, q2, _ = h.query("SELECT * FROM public.orders WHERE orders.id > 1")
        h.task(
            [
                {
                    "op": "status",
                    "sub_task_id": "T1",
                    "status": "completed",
                    "evidence_ids": [q2["data"]["evidence_id"]],
                }
            ]
        )
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
        p, u = h.call(
            "analyze_result",
            {
                "operation": "top_n",
                "inputs": [
                    {
                        "result_ref": q1["data"]["result_ref"],
                        "evidence_id": q1["data"]["evidence_id"],
                    }
                ],
                "params": {
                    "order_by": "amount",
                    "descending": False,
                    "n": 1,
                    "ties": "strict",
                },
                "allow_pushdown": False,
                "sub_task_id": "T2",
                "sub_task_revision": 1,
            },
        )
        assert p["status"] != "ok", p
        assert not u.get("db_analyses")

    def test_selected_upstream_outcome_analysis_accepted(self, stack):
        """合法成功路径：下游消费上游**已选定**成果分析 → 成功。"""
        h = Harness(stack)
        h.fresh("run-sel2")
        h.task([{"op": "add", "sub_task_id": "T1", "content": "上游"}])
        _, q, _ = h.query("SELECT * FROM public.orders")
        h.task(
            [
                {
                    "op": "status",
                    "sub_task_id": "T1",
                    "status": "completed",
                    "evidence_ids": [q["data"]["evidence_id"]],
                }
            ]
        )
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
        p, u = h.call(
            "analyze_result",
            {
                "operation": "describe",
                "inputs": [
                    {
                        "result_ref": q["data"]["result_ref"],
                        "evidence_id": q["data"]["evidence_id"],
                    }
                ],
                "params": {"columns": ["amount"]},
                "allow_pushdown": False,
                "sub_task_id": "T2",
                "sub_task_revision": 1,
            },
        )
        assert p["status"] == "ok", p
        assert u.get("db_analyses")

    def test_same_task_exploration_input_accepted(self, stack):
        """合法成功路径：同任务内先 query 后 analysis（探索输入保留）。"""
        h = Harness(stack)
        h.fresh("run-same")
        h.task([{"op": "add", "sub_task_id": "T1", "content": "同任务分析"}])
        _, q, _ = h.query("SELECT * FROM public.orders")
        p, u = h.call(
            "analyze_result",
            {
                "operation": "describe",
                "inputs": [
                    {
                        "result_ref": q["data"]["result_ref"],
                        "evidence_id": q["data"]["evidence_id"],
                    }
                ],
                "params": {"columns": ["amount"]},
                "allow_pushdown": False,
                "sub_task_id": "T1",
                "sub_task_revision": 1,
            },
        )
        assert p["status"] == "ok", p
        assert u.get("db_analyses")

    def test_derived_from_unqualified_source_rejected(self):
        """反例 2：派生结果消费一个来源资格不成立的沿用记录 → 门控拒绝。

        祖先错误沿 input_result_refs 传播，不能被派生结果绕过。
        """
        from src.modules.db_deepagent.services.reuse_qualification import (
            unqualified_reuse_evidence,
        )

        run = "run-x"
        # 来源任务已 revise（@2）→ 旧成果不再是最终有效成果
        t1v1 = make_task_record(
            run, "T1", 1, status="completed", evidence_ids=["E-source"]
        )
        t1v2 = make_task_record(run, "T1", 2, content="新版")
        source_task = make_task_record(
            "run-old",
            "T0",
            1,
            status="completed",
            evidence_ids=["E-old"],
        )
        reused = _evidence(
            "E-reused",
            run,
            "T1",
            1,
            direct_reuse_from={
                "evidence_ref": _ref("run-old", "E-old"),
                "source_task_ref": {
                    "session_id": "s-test",
                    "thread_id": "t-test",
                    "run_id": "run-old",
                    "sub_task_id": "T0",
                    "revision": 1,
                },
            },
        )
        derived = _evidence("E-derived", run, "T1", 1)
        derived["input_result_refs"] = [
            {
                "result_ref": reused["result_ref"],
                "evidence_ref": _ref(run, "E-reused"),
                "source_task_ref": {
                    "session_id": "s-test",
                    "thread_id": "t-test",
                    "run_id": run,
                    "sub_task_id": "T1",
                    "revision": 1,
                },
            }
        ]
        state = make_task_state([t1v1, t1v2, source_task])
        state["db_executions"] = [derived, reused]
        failures = unqualified_reuse_evidence(
            [derived, reused],
            session_id="s-test",
            thread_id="t-test",
            run_id=run,
            state=state,
        )
        assert "E-derived" in failures, failures
        assert "E-reused" in failures, failures

    def test_snapshot_missing_structured_input_rejected(self, stack):
        """反例 3：快照含 A/任务但缺其结构化输入 E，且不回退 live。"""
        from src.modules.db_deepagent.services.reuse_qualification import (
            unqualified_reuse_evidence,
        )

        run = "run-a"
        analysis = {
            "evidence_id": "A-1",
            "run_id": run,
            "sub_task_id": "T1",
            "sub_task_revision": 1,
            "output_result_ref": "res_" + "a" * 32,
            "input_result_refs": [
                {
                    "result_ref": "res_" + "b" * 32,
                    "evidence_ref": _ref(run, "E-missing"),
                    "source_task_ref": {
                        "session_id": "s-test",
                        "thread_id": "t-test",
                        "run_id": run,
                        "sub_task_id": "T1",
                        "revision": 1,
                    },
                }
            ],
        }
        task = make_task_record(run, "T1", 1, status="completed", evidence_ids=["A-1"])
        archive = QaArchive(stack.services.store)
        archive.write_run_snapshot(
            "s-test",
            "t-test",
            run,
            {
                "run_snapshot_schema_version": 1,
                "ledger_protocol_version": 1,
                "session_id": "s-test",
                "thread_id": "t-test",
                "run_id": run,
                "tasks": [task],
                "executions": [],  # 缺输入 E 原始记录
                "analyses": [analysis],
                "tool_errors": [],
            },
        )
        # live 状态刻意放入 E-missing：已有不完整快照不得被 live 掩盖
        state = {
            "db_sub_tasks": {},
            "db_executions": [_evidence("E-missing", run, "T1", 1)],
            "db_analyses": [],
        }
        failures = unqualified_reuse_evidence(
            [analysis],
            session_id="s-test",
            thread_id="t-test",
            run_id=run,
            state=state,
            archive=archive,
        )
        assert "A-1" in failures, failures

    def test_wrong_thread_snapshot_reader_rejected(self):
        """反例 5：[thread1,run1] 键下注入 thread=WRONG 的记录读取被拒绝。"""
        store = FakeStore()
        archive = QaArchive(store)
        key = archive._run_snapshot_key("t-test", "run-x")
        store.put(
            ("db_deepagent", "run_snapshots", "s-test"),
            key,
            {
                "run_snapshot_schema_version": 1,
                "ledger_protocol_version": 1,
                "session_id": "s-test",
                "thread_id": "WRONG",
                "run_id": "run-x",
                "tasks": [],
                "executions": [],
                "analyses": [],
            },
        )
        with pytest.raises(ValueError, match="作用域"):
            archive.read_run_snapshot("s-test", "t-test", "run-x")

    def test_extra_ghost_selected_primary_rejected(self):
        """反例 4：completed.evidence_ids 注入未选 ghost 成果 → 门控拒绝。"""
        run = "run-x"
        e1 = _evidence("E-a", run)
        t1 = make_task_record(
            run, "T1", 1, status="completed", evidence_ids=["E-a", "E-ghost"]
        )
        state = make_task_state([t1])
        state["db_executions"] = [e1]
        snap = CompletionSnapshot(
            run_id=run,
            tasks=current_task_view(state["db_sub_tasks"], run),
            executions=[e1],
            analyses=[],
            errors=[],
            known_counts={"E-a": 1},
            proven_untruncated={"E-a": True},
            plan_version=1,
        )
        decision = evaluate_submission(
            _submission("completed", [_finding("F-1", "结论", "T1", 1, ["E-a"])]),
            snap,
        )
        assert not decision.accepted
        assert any(e.code == "task_evidence_mismatch" for e in decision.errors)

    def test_four_entry_points_agree_on_unqualified_source(self):
        """R2-02：沿用工具、completed 登记、提交门控、Runner 共享同一规则，
        对同一个不合法来源的结论一致（拒绝），且合法来源一致（接受）。"""
        from src.modules.db_deepagent.services.reuse_qualification import (
            SourceUnqualified,
            unqualified_reuse_evidence,
            verify_reuse_source,
        )

        run = "run-x"
        old_run = "run-old"
        source_task = make_task_record(
            old_run, "T0", 1, status="completed", evidence_ids=["E-old"]
        )
        old_e = _evidence("E-old", old_run, "T0", 1)
        reused = _evidence(
            "E-new",
            run,
            "T1",
            1,
            direct_reuse_from={
                "evidence_ref": _ref(old_run, "E-old"),
                "source_task_ref": {
                    "session_id": "s-test",
                    "thread_id": "t-test",
                    "run_id": old_run,
                    "sub_task_id": "T0",
                    "revision": 1,
                },
            },
        )
        t1 = make_task_record(run, "T1", 1, status="in_progress")
        state = make_task_state([source_task, t1])
        state["db_executions"] = [old_e, reused]

        # 1) 提交门控 / Runner 装配
        snap = _snapshot_of(state, run)
        assert snap.reuse_failures == {}, snap.reuse_failures
        # 2) 批量证据图核验
        assert (
            unqualified_reuse_evidence(
                [reused],
                session_id="s-test",
                thread_id="t-test",
                run_id=run,
                state=state,
            )
            == {}
        )
        # 3) 沿用工具入口
        verify_reuse_source(
            _ref(old_run, "E-old"),
            old_e,
            session_id="s-test",
            state=state,
        )
        # 4) completed 登记（真实 TaskLedger 输出 → reducer）
        # make_task_state 的 plan 键取首条记录 run_id（此处为 run-old），
        # 故当前 run-x 的计划版本为 0。
        out = TaskLedger(state).apply(
            run_id=run,
            session_id="s-test",
            thread_id="t-test",
            request_id="req",
            expected_plan_version=0,
            operations=[
                LedgerOperation(
                    op="status",
                    sub_task_id="T1",
                    status="completed",
                    evidence_ids=["E-new"],
                )
            ],
            now=NOW,
            tool_call_id="tc",
        )
        assert out.task_records  # 合法路径可达，不预设拒绝
        assert (
            merge_task_records(state["db_sub_tasks"], out.task_records)[
                task_ledger_key(run, "T1", 1)
            ]["status"]
            == "completed"
        )

        # 来源被 revise 后四入口一致拒绝
        source_v2 = make_task_record(old_run, "T0", 2, content="新版")
        bad_state = make_task_state([source_task, source_v2, t1])
        bad_state["db_executions"] = [old_e, reused]
        bad_snap = _snapshot_of(bad_state, run)
        assert "E-new" in bad_snap.reuse_failures, bad_snap.reuse_failures
        assert "E-new" in unqualified_reuse_evidence(
            [reused],
            session_id="s-test",
            thread_id="t-test",
            run_id=run,
            state=bad_state,
        )
        with pytest.raises(SourceUnqualified):
            verify_reuse_source(
                _ref(old_run, "E-old"),
                old_e,
                session_id="s-test",
                state=bad_state,
            )
        with pytest.raises(TaskLedgerError):
            TaskLedger(bad_state).apply(
                run_id=run,
                session_id="s-test",
                thread_id="t-test",
                request_id="req",
                expected_plan_version=0,
                operations=[
                    LedgerOperation(
                        op="status",
                        sub_task_id="T1",
                        status="completed",
                        evidence_ids=["E-new"],
                    )
                ],
                now=NOW,
                tool_call_id="tc",
            )

    def test_multi_hop_reuse_via_full_snapshot_accepted(self, stack):
        """合法成功路径：多跳沿用（A→B→C）仅凭完整快照可核验。"""
        from src.modules.db_deepagent.services.reuse_qualification import (
            verify_reuse_source,
        )

        archive = QaArchive(stack.services.store)
        # 第一跳：run-a 的 E-a（T0@1 completed 选定成果）
        task_a = make_task_record(
            "run-a", "T0", 1, status="completed", evidence_ids=["E-a"]
        )
        e_a = _evidence("E-a", "run-a", "T0", 1)
        archive.write_run_snapshot(
            "s-test",
            "t-test",
            "run-a",
            {
                "run_snapshot_schema_version": 1,
                "ledger_protocol_version": 1,
                "session_id": "s-test",
                "thread_id": "t-test",
                "run_id": "run-a",
                "tasks": [task_a],
                "executions": [e_a],
                "analyses": [],
                "tool_errors": [],
            },
        )
        # 第二跳：run-b 沿用 E-a 产生 E-b（T0@1 completed 选定成果）
        task_b = make_task_record(
            "run-b", "T0", 1, status="completed", evidence_ids=["E-b"]
        )
        e_b = _evidence(
            "E-b",
            "run-b",
            "T0",
            1,
            direct_reuse_from={
                "evidence_ref": _ref("run-a", "E-a"),
                "source_task_ref": {
                    "session_id": "s-test",
                    "thread_id": "t-test",
                    "run_id": "run-a",
                    "sub_task_id": "T0",
                    "revision": 1,
                },
            },
        )
        archive.write_run_snapshot(
            "s-test",
            "t-test",
            "run-b",
            {
                "run_snapshot_schema_version": 1,
                "ledger_protocol_version": 1,
                "session_id": "s-test",
                "thread_id": "t-test",
                "run_id": "run-b",
                "tasks": [task_b],
                "executions": [e_b],
                "analyses": [],
                "tool_errors": [],
            },
        )
        # 第三跳：从 E-b 沿用（递归核验 E-b → E-a）
        verify_reuse_source(
            _ref("run-b", "E-b"),
            e_b,
            session_id="s-test",
            state={"db_sub_tasks": {}, "db_executions": [], "db_analyses": []},
            archive=archive,
        )

    def test_full_snapshot_after_checkpoint_deletion_accepted(self, stack):
        """合法成功路径：删 checkpoint 后仅凭完整快照仍可核验多跳来源。"""
        from src.modules.db_deepagent.services.reuse_qualification import (
            verify_reuse_source,
        )

        archive = QaArchive(stack.services.store)
        old_task = make_task_record(
            "run-old", "T0", 1, status="completed", evidence_ids=["E-old"]
        )
        old_e = _evidence("E-old", "run-old", "T0", 1)
        archive.write_run_snapshot(
            "s-test",
            "t-test",
            "run-old",
            {
                "run_snapshot_schema_version": 1,
                "ledger_protocol_version": 1,
                "session_id": "s-test",
                "thread_id": "t-test",
                "run_id": "run-old",
                "tasks": [old_task],
                "executions": [old_e],
                "analyses": [],
                "tool_errors": [],
            },
        )
        # 线程账本为空：checkpoint 已删除
        verify_reuse_source(
            _ref("run-old", "E-old"),
            old_e,
            session_id="s-test",
            state={"db_sub_tasks": {}, "db_executions": [], "db_analyses": []},
            archive=archive,
        )


# ======================================================================
# R2-03：实际 cleanup 控制流
# ======================================================================


class TestR203CleanupControlFlow:
    def _service(self, archive, root):
        return MaintenanceService(
            SimpleNamespace(workspace=SimpleNamespace(root=str(root))),
            archive,
            None,
        )

    def test_root_read_failure_no_delete_candidates(self, tmp_path):
        """反例：run_errors.evidence=[123] 读取失败 → 该会话零实删候选。"""
        now = datetime.now(UTC)
        ref = "res_" + "a" * 32
        result_path = tmp_path / "results" / ref
        result_path.mkdir(parents=True)
        (result_path / "meta.json").write_text(
            json.dumps(
                {
                    "session_id": "s1",
                    "created_at": (now - timedelta(days=60)).isoformat(),
                }
            ),
            encoding="utf-8",
        )
        archive = QaArchive(InMemoryStore())
        archive.write_run_errors("s1", "req1", {"evidence": [123]})
        report = CleanupReport(dry_run=True)
        self._service(archive, tmp_path)._cleanup_session_resources(
            "s1",
            result_metas={},
            result_retention=timedelta(days=30),
            archive_before=now - timedelta(days=365),
            now=now,
            apply=False,
            report=report,
            conn=None,
        )
        assert result_path.is_dir()
        assert report.result_dirs == [], report.result_dirs
        assert any(
            "保护根读取失败" in item["reason"] for item in report.skipped
        ), report.skipped
        assert any("跳过该会话全部" in item["reason"] for item in report.skipped)

    def test_unknown_protocol_and_missing_source_reported(self):
        """反例：未知协议 + 缺 direct 来源 → 闭包 problems 非空。"""
        archive = QaArchive(InMemoryStore())
        archive.write_run_snapshot(
            "s1",
            "dbda:s1:1",
            "r1",
            {
                "run_snapshot_schema_version": 99,
                "ledger_protocol_version": 1,
                "session_id": "s1",
                "thread_id": "dbda:s1:1",
                "run_id": "r1",
                "executions": [
                    {
                        "result_ref": "res_x",
                        # R3-03：直接来源边须带完整 source_task_ref；本例
                        # 保持结构合法，只让目标来源证据缺失以测「缺源」。
                        "direct_reuse_from": {
                            "evidence_ref": {
                                "session_id": "s1",
                                "thread_id": "dbda:s1:9",
                                "run_id": "missing",
                                "evidence_id": "E-missing",
                            },
                            "source_task_ref": {
                                "session_id": "s1",
                                "thread_id": "dbda:s1:9",
                                "run_id": "missing",
                                "sub_task_id": "T9",
                                "revision": 1,
                            },
                        },
                    }
                ],
                "analyses": [],
            },
        )
        service = MaintenanceService(SimpleNamespace(), archive, None)
        _protected, _orphans, problems = service._protected_closure(
            "s1", set(), {'["dbda:s1:1","r1"]'}
        )
        assert any("协议版本" in p["reason"] for p in problems), problems
        assert any("缺源" in p["reason"] for p in problems), problems

    def test_missing_protection_root_reported(self):
        """反例：保护根快照不存在 → 明确报告，不按空集降级删除。"""
        service = MaintenanceService(
            SimpleNamespace(), QaArchive(InMemoryStore()), None
        )
        _protected, _orphans, problems = service._protected_closure(
            "s1", set(), {'["dbda:s1:1","missing-run"]'}
        )
        assert any("保护根快照缺失" in p["reason"] for p in problems), problems

    def test_cycle_in_source_chain_reported(self):
        """反例：direct 来源成环 → 闭包 problems 非空。"""
        archive = QaArchive(InMemoryStore())
        # R3-03：来源边结构合法（完整 task 引用），只让 direct 链成环。
        task_ref = {
            "session_id": "s1",
            "thread_id": "dbda:s1:1",
            "run_id": "r1",
            "sub_task_id": "T1",
            "revision": 1,
        }
        rec_a = {
            "evidence_id": "E-a",
            "run_id": "r1",
            "result_ref": "res_a",
            "direct_reuse_from": {
                "evidence_ref": {
                    "session_id": "s1",
                    "thread_id": "dbda:s1:1",
                    "run_id": "r1",
                    "evidence_id": "E-b",
                },
                "source_task_ref": task_ref,
            },
        }
        rec_b = {
            "evidence_id": "E-b",
            "run_id": "r1",
            "result_ref": "res_b",
            "direct_reuse_from": {
                "evidence_ref": {
                    "session_id": "s1",
                    "thread_id": "dbda:s1:1",
                    "run_id": "r1",
                    "evidence_id": "E-a",
                },
                "source_task_ref": task_ref,
            },
        }
        archive.write_run_snapshot(
            "s1",
            "dbda:s1:1",
            "r1",
            {
                "run_snapshot_schema_version": 1,
                "ledger_protocol_version": 1,
                "session_id": "s1",
                "thread_id": "dbda:s1:1",
                "run_id": "r1",
                "tasks": [],
                "executions": [rec_a, rec_b],
                "analyses": [],
            },
        )
        service = MaintenanceService(SimpleNamespace(), archive, None)
        _p, _o, problems = service._protected_closure(
            "s1", set(), {'["dbda:s1:1","r1"]'}
        )
        assert any("成环" in p["reason"] for p in problems), problems

    def test_bad_protocol_no_delete_candidates_via_control_flow(self, tmp_path):
        """反例（真实清理控制流）：快照协议非法 → 该会话零实删候选。"""
        now = datetime.now(UTC)
        ref = "res_" + "c" * 32
        result_path = tmp_path / "results" / ref
        result_path.mkdir(parents=True)
        (result_path / "meta.json").write_text(
            json.dumps(
                {
                    "session_id": "s1",
                    "created_at": (now - timedelta(days=60)).isoformat(),
                }
            ),
            encoding="utf-8",
        )
        archive = QaArchive(InMemoryStore())
        archive.write_run_snapshot(
            "s1",
            "dbda:s1:1",
            "r1",
            {
                "run_snapshot_schema_version": 99,
                "ledger_protocol_version": 1,
                "session_id": "s1",
                "thread_id": "dbda:s1:1",
                "run_id": "r1",
                "tasks": [],
                "executions": [],
                "analyses": [],
            },
        )
        report = CleanupReport(dry_run=True)
        self._service(archive, tmp_path)._cleanup_session_resources(
            "s1",
            result_metas={},
            result_retention=timedelta(days=30),
            archive_before=now - timedelta(days=365),
            now=now,
            apply=False,
            report=report,
            conn=None,
        )
        assert result_path.is_dir()
        assert report.result_dirs == [], report.result_dirs
        assert any(
            "协议版本" in item["reason"] for item in report.skipped
        ), report.skipped

    def test_normal_session_still_cleanable(self, tmp_path):
        """合法成功路径：正常会话（无闭包问题）仍产生到期删除候选。"""
        now = datetime.now(UTC)
        ref = "res_" + "b" * 32
        result_path = tmp_path / "results" / ref
        result_path.mkdir(parents=True)
        (result_path / "meta.json").write_text(
            json.dumps(
                {
                    "session_id": "s-ok",
                    "created_at": (now - timedelta(days=60)).isoformat(),
                }
            ),
            encoding="utf-8",
        )
        archive = QaArchive(InMemoryStore())
        report = CleanupReport(dry_run=True)
        self._service(archive, tmp_path)._cleanup_session_resources(
            "s-ok",
            result_metas={},
            result_retention=timedelta(days=30),
            archive_before=now - timedelta(days=365),
            now=now,
            apply=False,
            report=report,
            conn=None,
        )
        assert any(ref in item for item in report.result_dirs), report.result_dirs


# ======================================================================
# R2-04：TaskLedger 输出 → 实际 reducer 应用
# ======================================================================


def _op(spec):
    name, kw = spec
    kw = dict(kw)
    tid = kw.pop("sub_task_id", "T1")
    return LedgerOperation(op=name, sub_task_id=tid, **kw)


def _apply(state, ops, expected=0):
    return TaskLedger(state).apply(
        run_id="r",
        session_id="s",
        thread_id="th",
        request_id="req",
        expected_plan_version=expected,
        operations=[_op(o) for o in ops],
        now=NOW,
        tool_call_id="tc",
    )


def _merge(state, out):
    return {
        **state,
        "db_sub_tasks": merge_task_records(state.get("db_sub_tasks"), out.task_records),
        "db_task_plans": (
            merge_task_plans(state.get("db_task_plans"), {"r": out.plan_record})
            if out.plan_record
            else state.get("db_task_plans", {})
        ),
    }


class TestR204ReducerVoid:
    @pytest.mark.parametrize(
        "status,setup",
        [
            (
                "failed",
                [("status", {"reason_code": "agent_abandoned", "reason": "放弃"})],
            ),
            (
                "needs_clarification",
                [
                    (
                        "status",
                        {
                            "reason": "不明确",
                            "clarification_question": "哪个时间？",
                        },
                    )
                ],
            ),
            ("completed", None),
        ],
    )
    def test_terminal_to_voided_applies_through_real_reducer(self, status, setup):
        """三个终态 → voided 经真实 TaskLedger 输出 + 真实 reducer 成功。"""
        state = {}
        state = _merge(state, _apply(state, [("add", {"content": "查询"})]))
        if status == "completed":
            state["db_executions"] = [_evidence("E-a", "r", "T1", 1)]
            state = _merge(
                state,
                _apply(
                    state,
                    [
                        (
                            "status",
                            {"status": "completed", "evidence_ids": ["E-a"]},
                        )
                    ],
                    expected=1,
                ),
            )
        else:
            op_kw = dict(setup[0][1])
            op_kw["status"] = status
            state = _merge(
                state,
                _apply(state, [("status", op_kw)], expected=1),
            )
        out = _apply(state, [("void", {"reason": "范围调整"})], expected=2)
        merged = _merge(state, out)
        rec = merged["db_sub_tasks"][task_ledger_key("r", "T1", 1)]
        assert rec["status"] == "voided"

    def test_completed_to_pending_still_rejected(self):
        """合法终态作废之外：completed→pending 与同 revision 改 content 仍拒绝。"""
        state = {}
        state = _merge(state, _apply(state, [("add", {"content": "查询"})]))
        state["db_executions"] = [_evidence("E-a", "r", "T1", 1)]
        state = _merge(
            state,
            _apply(
                state,
                [("status", {"status": "completed", "evidence_ids": ["E-a"]})],
                expected=1,
            ),
        )
        with pytest.raises(TaskLedgerError):
            _apply(state, [("status", {"status": "pending"})], expected=2)
        key = task_ledger_key("r", "T1", 1)
        changed = copy.deepcopy(state["db_sub_tasks"][key])
        changed["content"] = "改写"
        with pytest.raises(LedgerConsistencyError):
            merge_task_records(state["db_sub_tasks"], {key: changed})


# ======================================================================
# R2-05：历史决策不重验、不原地改写；R2-06：固定 revision 与最终原因
# ======================================================================


class TestR205HistoricalDecisions:
    def _ledger(self):
        def apply(state, ops, expected):
            out = _apply(state, ops, expected=expected)
            return _merge(state, out), out

        return apply

    def test_void_replacement_then_revise_target_ok(self):
        """反例：void T1→T2@1 后正常 revise T2 成功（经真实登记/reducer）。"""
        apply = self._ledger()
        state, _ = apply(
            {},
            [
                ("add", {"sub_task_id": "T1", "content": "旧任务"}),
                ("add", {"sub_task_id": "T2", "content": "替代任务"}),
            ],
            0,
        )
        state, _ = apply(
            state,
            [
                (
                    "void",
                    {
                        "sub_task_id": "T1",
                        "reason": "改由T2",
                        "replacement": {"sub_task_id": "T2", "revision": 1},
                    },
                )
            ],
            1,
        )
        state, out = apply(
            state,
            [("revise", {"sub_task_id": "T2", "content": "新版替代"})],
            2,
        )
        assert task_ledger_key("r", "T2", 2) in out.task_records

    def test_blocked_then_revise_upstream_ok(self):
        """反例：T2 因 T1@1 阻塞后 revise T1 成功。"""
        apply = self._ledger()
        state, _ = apply(
            {},
            [
                ("add", {"sub_task_id": "T1", "content": "上游"}),
                (
                    "add",
                    {
                        "sub_task_id": "T2",
                        "content": "下游",
                        "dependencies": [{"sub_task_id": "T1", "revision": 1}],
                    },
                ),
            ],
            0,
        )
        state, _ = apply(
            state,
            [
                (
                    "status",
                    {
                        "sub_task_id": "T1",
                        "status": "needs_clarification",
                        "reason": "时间不明",
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
        )
        state, out = apply(
            state,
            [("revise", {"sub_task_id": "T1", "content": "新上游"})],
            2,
        )
        assert task_ledger_key("r", "T1", 2) in out.task_records

    def test_old_replacement_snapshot_not_mutated(self):
        """反例：无关更新不得原地改写既有历史快照，输入输出深比较相等。"""
        apply = self._ledger()
        state, _ = apply(
            {},
            [
                ("add", {"sub_task_id": "T1", "content": "旧任务"}),
                ("add", {"sub_task_id": "T2", "content": "替代任务"}),
            ],
            0,
        )
        state, _ = apply(
            state,
            [
                (
                    "void",
                    {
                        "sub_task_id": "T1",
                        "reason": "改由T2",
                        "replacement": {"sub_task_id": "T2", "revision": 1},
                    },
                )
            ],
            1,
        )
        key = task_ledger_key("r", "T1", 1)
        before = copy.deepcopy(state["db_sub_tasks"][key]["termination_context"])
        state_before = copy.deepcopy(state)
        state, _ = apply(
            state,
            [
                (
                    "status",
                    {
                        "sub_task_id": "T2",
                        "status": "failed",
                        "reason_code": "agent_abandoned",
                        "reason": "无法完成",
                    },
                )
            ],
            2,
        )
        after = state["db_sub_tasks"][key]["termination_context"]
        assert after == before, (before, after)
        assert state_before["db_sub_tasks"][key]["termination_context"] == before


class TestR206Snapshots:
    def test_old_version_dependency_snapshot_uses_historical_fact(self):
        """反例：A@1 completed，同批 revise A→@2 并终止 B：B 的依赖快照
        仍保存 A@1 completed，而非 A@1 pending。"""
        state = {}
        state = _merge(
            state,
            _apply(
                state,
                [
                    ("add", {"sub_task_id": "T1", "content": "上游"}),
                    (
                        "add",
                        {
                            "sub_task_id": "T2",
                            "content": "下游",
                            "dependencies": [{"sub_task_id": "T1", "revision": 1}],
                        },
                    ),
                ],
                0,
            ),
        )
        state["db_executions"] = [_evidence("E-a", "r", "T1", 1)]
        state = _merge(
            state,
            _apply(
                state,
                [
                    (
                        "status",
                        {
                            "sub_task_id": "T1",
                            "status": "completed",
                            "evidence_ids": ["E-a"],
                        },
                    )
                ],
                expected=1,
            ),
        )
        out = _apply(
            state,
            [
                ("revise", {"sub_task_id": "T1", "content": "新版上游"}),
                (
                    "status",
                    {
                        "sub_task_id": "T2",
                        "status": "failed",
                        "reason_code": "agent_abandoned",
                        "reason": "上游改版",
                    },
                ),
            ],
            expected=2,
        )
        snap = out.task_records[task_ledger_key("r", "T2", 1)]["termination_context"][
            "dependency_snapshots"
        ]
        assert snap == [
            {
                "sub_task_id": "T1",
                "revision": 1,
                "status": "completed",
            }
        ], snap

    @pytest.mark.parametrize("reverse", [False, True])
    def test_three_hop_batch_snapshots_order_independent(self, reverse):
        """反例：三跳同批终止，打乱操作顺序仍保存相同、真实的服务端原因。"""
        ops = [
            (
                "add",
                {"sub_task_id": "T1", "content": "根上游"},
            ),
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
        out = _apply(state, terminal, expected=1)
        t2 = out.task_records[task_ledger_key("r", "T2", 1)]
        t3 = out.task_records[task_ledger_key("r", "T3", 1)]
        assert "ROOT" in t2["reason"]
        assert "ROOT" in t3["reason"]
        t2_snap = t3["termination_context"]["dependency_snapshots"]
        assert t2_snap[0]["reason"] == t2["reason"], (t2_snap, t2["reason"])
        assert "PLACEHOLDER" not in t2_snap[0]["reason"]


# ======================================================================
# R2-07 / R2-08：交付正文与兜底可信度
# ======================================================================


class TestR207Delivery:
    def test_model_answer_without_result_still_shows_finding_text(self):
        """反例：模型 answer='已处理'，结果只在合法 finding 中 → 正文含结论。"""
        run = "r"
        e1 = _evidence("E-a", run)
        t1 = make_task_record(run, "T1", 1, status="completed", evidence_ids=["E-a"])
        state = make_task_state([t1])
        state["db_executions"] = [e1]
        snap = _snapshot_of(state, run)
        payload = _submission(
            "completed",
            [_finding("F-1", "销售额为123万元", "T1", 1, ["E-a"])],
            answer="已处理",
        )
        decision = evaluate_submission(payload, snap)
        assert decision.accepted, [e.code for e in decision.errors]
        section = build_delivery_answer_section(decision)
        assert "销售额为123万元" in section, section
        assert "已完成" in section

    def test_failed_and_clarification_reason_still_present(self):
        """失败/澄清说明继续存在（不因补 finding 段落丢失）。"""
        run = "r"
        t1 = make_task_record(
            run,
            "T1",
            1,
            status="failed",
            reason_code="agent_abandoned",
            reason="口径不明",
        )
        t2 = make_task_record(
            run,
            "T2",
            1,
            status="needs_clarification",
            reason="需要澄清",
            clarification_question="哪个时间？",
        )
        state = make_task_state([t1, t2])
        snap = _snapshot_of(state, run)
        decision = evaluate_submission(
            _submission(
                "needs_clarification",
                [],
                unresolved=[
                    {"sub_task_id": "T1", "revision": 1},
                    {"sub_task_id": "T2", "revision": 1},
                ],
            ),
            snap,
        )
        assert decision.accepted
        section = build_delivery_answer_section(decision)
        assert "口径不明" in section and "哪个时间？" in section


class TestR208Fallback:
    @pytest.mark.parametrize(
        "stop",
        ["no_submission", "budget_exhausted", "runtime_error"],
    )
    def test_unverified_completion_not_claimed_success(self, stop):
        """反例：无提交/预算停止/模型异常 + 缺证据 → 不得确认未核验任务成功。"""
        run = "r"
        t1 = make_task_record(
            run, "T1", 1, status="completed", evidence_ids=["E-ghost"]
        )
        state = make_task_state([t1])
        state["db_executions"] = []  # 选定成果不存在
        snap = _snapshot_of(state, run)
        decision = evaluate_submission(None, snap)
        assert not decision.accepted
        assert any(e.code == "task_evidence_mismatch" for e in decision.errors)
        assert decision.expected_status is None
        assert "T1" not in decision.verified_completed_refs
        section = DbAgentRunner._fallback_task_section(None, decision)
        assert "已登记完成（未核验）" in section
        assert "- 已完成：统计 9 月订单" not in section
        assert stop in ("no_submission", "budget_exhausted", "runtime_error")

    def test_verified_completion_can_be_shown(self):
        """合法成功路径：逐项核验通过的 completed 任务可显示已完成。"""
        run = "r"
        e1 = _evidence("E-a", run)
        t1 = make_task_record(run, "T1", 1, status="completed", evidence_ids=["E-a"])
        state = make_task_state([t1])
        state["db_executions"] = [e1]
        snap = _snapshot_of(state, run)
        decision = evaluate_submission(
            _submission("completed", [_finding("F-1", "结论", "T1", 1, ["E-a"])]),
            snap,
        )
        assert decision.accepted
        assert "T1" in decision.verified_completed_refs
        section = DbAgentRunner._fallback_task_section(None, decision)
        assert "- 已完成：业务任务" in section


# ======================================================================
# R2-09：本地分析工具/内部服务 guard
# ======================================================================


class TestR209Guard:
    def test_tool_without_lease_rejected_before_write(self, stack):
        """反例：真实 ToolNode 传 session_guard_lease=None → 拒绝且不写 A。"""
        h = Harness(stack)
        h.fresh("run-guard")
        h.task([{"op": "add", "sub_task_id": "T1", "content": "分析"}])
        _, q, _ = h.query("SELECT * FROM public.orders")
        no_lease = replace(h.ctx, session_guard_lease=None)
        p, u = h.call(
            "analyze_result",
            {
                "operation": "describe",
                "inputs": [
                    {
                        "result_ref": q["data"]["result_ref"],
                        "evidence_id": q["data"]["evidence_id"],
                    }
                ],
                "params": {"columns": ["amount"]},
                "allow_pushdown": False,
                "sub_task_id": "T1",
                "sub_task_revision": 1,
            },
            context=no_lease,
        )
        assert p["status"] != "ok", p
        assert not u.get("db_analyses")

    def test_internal_service_without_lease_rejected(self, stack):
        """反例：不经过 Runner 直接调用内部分析服务 → 绕行被拒。"""
        svc = ResultAnalysisService(stack.cfg, stack.results)
        ctx = replace(h_ctx(stack), session_guard_lease=None)
        with pytest.raises(AnalysisError) as exc:
            svc.analyze(
                operation="describe",
                params={"columns": ["x"]},
                input_refs=["res_x"],
                ctx=ctx,
                allow_pushdown=False,
                load_bundle=lambda r: None,
                run_pushdown=lambda *a, **k: None,
            )
        assert exc.value.error_type == "session_guard_required"

    def test_valid_lease_analysis_accepted(self, stack):
        """合法成功路径：有效运行持锁凭据下分析成功写入 A。"""
        h = Harness(stack)
        h.fresh("run-guard-ok")
        h.task([{"op": "add", "sub_task_id": "T1", "content": "分析"}])
        _, q, _ = h.query("SELECT * FROM public.orders")
        p, u = h.call(
            "analyze_result",
            {
                "operation": "describe",
                "inputs": [
                    {
                        "result_ref": q["data"]["result_ref"],
                        "evidence_id": q["data"]["evidence_id"],
                    }
                ],
                "params": {"columns": ["amount"]},
                "allow_pushdown": False,
                "sub_task_id": "T1",
                "sub_task_revision": 1,
            },
        )
        assert p["status"] == "ok", p
        assert u.get("db_analyses")


def h_ctx(stack):
    return stack.ctx


# ======================================================================
# R2-10：轮换携带记录自身完整 scope
# ======================================================================


class TestR210Carry:
    def test_two_rotations_carry_direct_source_identity(self, stack):
        """至少两次线程轮换：携带引用使用记录自身完整身份（新 thread）。"""
        from src.modules.db_deepagent.runner import _carried_listing, _collect_carried

        h = Harness(stack)
        h.fresh("run-origin", "t-probe")
        h.task([{"op": "add", "sub_task_id": "T1", "content": "来源"}])
        _, q, _ = h.query("SELECT * FROM public.orders")
        root = q["data"]
        h.task(
            [
                {
                    "op": "status",
                    "sub_task_id": "T1",
                    "status": "completed",
                    "evidence_ids": [root["evidence_id"]],
                }
            ]
        )
        source_rec = next(
            r for r in h.state["db_executions"] if r["run_id"] == "run-origin"
        )
        source_task = next(
            t for t in h.state["db_sub_tasks"].values() if t["run_id"] == "run-origin"
        )
        archive = QaArchive(stack.services.store)
        archive.write_run_snapshot(
            h.ctx.session_id,
            "t-probe",
            "run-origin",
            {
                "run_snapshot_schema_version": 1,
                "ledger_protocol_version": 1,
                "session_id": h.ctx.session_id,
                "thread_id": "t-probe",
                "run_id": "run-origin",
                "tasks": [source_task],
                "executions": [source_rec],
                "analyses": [],
                "tool_errors": [],
            },
        )
        carried = (
            {
                "kind": "execution",
                "record": source_rec,
                "origin": {
                    "session_id": h.ctx.session_id,
                    "thread_id": "t-probe",
                    "run_id": "run-origin",
                    "evidence_id": source_rec["evidence_id"],
                },
            },
        )
        h.state = {}
        h.fresh("run-mid", "t-middle", carried_evidence=carried)
        h.task([{"op": "add", "sub_task_id": "T1", "content": "第一次沿用"}])
        p, _ = h.call(
            "reuse_evidence",
            {
                "source_evidence_refs": [
                    _ref(
                        "run-origin",
                        source_rec["evidence_id"],
                        thread="t-probe",
                        session=h.ctx.session_id,
                    )
                ],
                "sub_task_id": "T1",
                "sub_task_revision": 1,
            },
        )
        assert p["status"] == "ok", p
        new_rec = next(r for r in h.state["db_executions"] if r["run_id"] == "run-mid")
        # 第二次轮换：使用生产同款 load_meta（原结果文件生成于 t-probe）
        carried2 = _collect_carried(
            {"db_executions": [new_rec]},
            (),
            stack.cfg,
            meta_loader=lambda result, run: stack.results.load_meta(
                result, session_id=h.ctx.session_id
            ),
        )
        origin = carried2[0]["origin"]
        assert origin["thread_id"] == "t-middle", origin
        assert origin["run_id"] == "run-mid", origin
        assert origin["evidence_id"] == new_rec["evidence_id"], origin
        listing = _carried_listing(carried2)
        assert "thread=t-middle" in listing, listing


# ======================================================================
# R2-11：沿用失败绑定已核验目标
# ======================================================================


class TestR211FailureBinding:
    def test_reuse_failure_ref_registers_tool_failed(self, stack):
        """反例：合法目标沿用时数据源不符 → failure_ref 原样登记 failed/tool_failed。"""
        h = Harness(stack)
        h.fresh("run-source")
        h.task([{"op": "add", "sub_task_id": "T1", "content": "来源"}])
        _, q, _ = h.query("SELECT * FROM public.orders")
        root = q["data"]
        h.task(
            [
                {
                    "op": "status",
                    "sub_task_id": "T1",
                    "status": "completed",
                    "evidence_ids": [root["evidence_id"]],
                }
            ]
        )
        h.fresh("run-target", datasource_id="different-source")
        h.task([{"op": "add", "sub_task_id": "T1", "content": "沿用失败目标"}])
        p, _ = h.call(
            "reuse_evidence",
            {
                "source_evidence_refs": [root["source_evidence_ref"]],
                "sub_task_id": "T1",
                "sub_task_revision": 1,
            },
        )
        fail_ref = p.get("data", {}).get("failure_ref")
        assert fail_ref is not None, p
        assert isinstance(fail_ref.get("source_task_ref"), dict), fail_ref
        registered, _ = h.task(
            [
                {
                    "op": "status",
                    "sub_task_id": "T1",
                    "status": "failed",
                    "reason_code": "tool_failed",
                    "reason": "沿用失败",
                    "failure_refs": [fail_ref],
                }
            ]
        )
        assert registered["status"] == "ok", registered

    def test_unknown_target_still_unowned(self, stack):
        """未知目标/probe 准入失败不伪造任务归属。"""
        h = Harness(stack)
        h.fresh("run-unknown")
        h.task([{"op": "add", "sub_task_id": "T1", "content": "已登记"}])
        p, _ = h.call(
            "reuse_evidence",
            {
                "source_evidence_refs": [_ref("run-x", "E-none")],
                "sub_task_id": "T9",
                "sub_task_revision": 99,
            },
        )
        # 未知目标在目标准入边界之前被拒绝：无 failure_ref 业务归属
        assert p.get("error") is not None, p
        fail_ref = p.get("data", {}).get("failure_ref")
        if fail_ref is not None:
            assert fail_ref.get("source_task_ref") is None, fail_ref


# ======================================================================
# R2-12：封存 interrupted_tasks 参数形状
# ======================================================================


class TestR212Interrupted:
    def _seal(self, tasks):
        runner = DbAgentRunner.__new__(DbAgentRunner)
        runner._archive = QaArchive(InMemoryStore())
        return runner._seal_run_snapshot(
            "s1",
            "dbda:s1:1",
            "r1",
            "req1",
            "budget_exhausted",
            {"db_sub_tasks": tasks},
            None,
        )

    def test_pending_task_recorded(self):
        """反例：pending 中止任务必须出现在 interrupted_tasks。"""
        tasks = {
            task_ledger_key("r1", "T1", 1): make_task_record(
                "r1", "T1", 1, status="pending", thread_id="dbda:s1:1"
            )
        }
        snap = self._seal(tasks)
        assert snap["interrupted_tasks"] == ["T1"], snap

    def test_in_progress_task_recorded(self):
        tasks = {
            task_ledger_key("r1", "T1", 1): make_task_record(
                "r1", "T1", 1, status="in_progress", thread_id="dbda:s1:1"
            )
        }
        assert self._seal(tasks)["interrupted_tasks"] == ["T1"]

    def test_no_interrupted_tasks(self):
        tasks = {
            task_ledger_key("r1", "T1", 1): make_task_record(
                "r1",
                "T1",
                1,
                status="completed",
                evidence_ids=["E-a"],
                thread_id="dbda:s1:1",
            )
        }
        assert self._seal(tasks)["interrupted_tasks"] == []

    def test_other_run_same_task_id_ignored(self):
        tasks = {
            task_ledger_key("r0", "T1", 1): make_task_record(
                "r0", "T1", 1, status="pending", thread_id="dbda:s1:1"
            ),
            task_ledger_key("r1", "T2", 1): make_task_record(
                "r1",
                "T2",
                1,
                status="completed",
                evidence_ids=["E-a"],
                thread_id="dbda:s1:1",
            ),
        }
        assert self._seal(tasks)["interrupted_tasks"] == []


# ======================================================================
# R2-13：dependency_blocked 完整阻塞依据
# ======================================================================


class TestR213BlockedAudit:
    def _blocked_state(
        self,
        *,
        drop_blocked_by=False,
        drop_ctx=False,
        forged_root=False,
        missing_old_version=False,
        upstream_revised=False,
    ):
        run = "r"
        t1 = make_task_record(
            run,
            "T1",
            1,
            status="failed",
            reason_code="agent_abandoned",
            reason="ROOT",
        )
        t2 = make_task_record(
            run,
            "T2",
            1,
            status="failed",
            reason_code="dependency_blocked",
            reason="依赖 T1@1 阻塞（根因：ROOT）",
            dependencies=[{"sub_task_id": "T1", "revision": 1}],
            extra={
                "blocked_by": [{"sub_task_id": "T1", "revision": 1}],
                "termination_context": {
                    "decision_plan_version": 1,
                    "dependency_snapshots": [
                        {
                            "sub_task_id": "T1",
                            "revision": 1,
                            "status": "failed",
                            "reason_code": "agent_abandoned",
                            "reason": "ROOT",
                        }
                    ],
                    "blocked_by_root": {
                        "sub_task_id": "T1",
                        "revision": 1,
                        "status": "failed",
                        "reason_code": "agent_abandoned",
                        "reason": "ROOT",
                    },
                },
            },
        )
        if drop_blocked_by:
            t2["blocked_by"] = []
        if drop_ctx:
            t2["termination_context"] = {"decision_plan_version": 1}
        if forged_root:
            t2["termination_context"]["blocked_by_root"]["reason"] = "伪造根因"
        if missing_old_version:
            t2["blocked_by"] = [{"sub_task_id": "T1", "revision": 9}]
        state = make_task_state([t1, t2])
        if upstream_revised:
            # 上游后来修订为终态（不能留 pending，否则普通提交本身被拒绝）；
            # 旧终止记录仍按固定历史版本复核，不因上游升级被否定。
            t1v2 = make_task_record(
                run,
                "T1",
                2,
                content="新版上游",
                status="failed",
                reason_code="agent_abandoned",
                reason="新版上游失败",
            )
            state = make_task_state([t1, t1v2, t2])
        return state

    def _decide(self, state):
        snap = _snapshot_of(state, "r")
        # all_task_records 供固定历史版本复核（Runner 装配同源）
        from src.modules.db_deepagent.services.task_completion import (
            all_task_versions,
        )

        snap.all_task_records = all_task_versions(state)
        tasks = current_task_view(state["db_sub_tasks"], "r")
        unresolved = [
            {"sub_task_id": tid, "revision": rec["revision"]}
            for tid, rec in tasks.items()
            if rec.get("status") in ("failed", "needs_clarification")
        ]
        return evaluate_submission(
            _submission("failed", [], unresolved=unresolved), snap
        )

    def test_missing_basis_rejected(self):
        assert not self._decide(
            self._blocked_state(drop_blocked_by=True, drop_ctx=True)
        ).accepted

    def test_missing_context_rejected(self):
        assert not self._decide(self._blocked_state(drop_ctx=True)).accepted

    def test_old_version_missing_rejected(self):
        assert not self._decide(self._blocked_state(missing_old_version=True)).accepted

    def test_forged_root_rejected(self):
        assert not self._decide(self._blocked_state(forged_root=True)).accepted

    def test_legal_historical_basis_with_upstream_revised_accepted(self):
        """合法成功路径：历史依据合法，上游后来 revise 仍可复核。"""
        decision = self._decide(self._blocked_state(upstream_revised=True))
        assert decision.accepted, [e.code for e in decision.errors]
