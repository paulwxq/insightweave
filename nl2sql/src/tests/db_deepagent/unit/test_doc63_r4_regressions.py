"""doc 6.3 R4 审核反例回归（codex_doc63_impl_review_r4.md 逐项）

以 R4-01…R4-03 与 M1 为验收清单：每项同时覆盖「原反例按新语义处理」与
「合法成功路径通过」，并补相邻场景与完整路径：

- R4-01：真实 Runner.run() + Store run_snapshots GET 注入持久化/未知故障，
  校验受控异常边界、persistence 最高优先级、系统兜底、证据保留、archived；
  确定性坏来源仍为受控业务门控拒绝。
- R4-02：真实 TaskLedger→reducer→submit→Runner 的完整路径，多个独立
  阻塞根被接受；C/D 伪造与历史 void 正例保持。
- R4-03：真实 InMemoryStore 的实际清理控制流，连续两次清理，依赖一致的
  孤儿快照删除集合；坏协议/缺源/环仍保守停止。
- M1：共享祖先访问次数、角色转换、逐边 expected 约束的回归。

业务 DB 为 FakeExecDb，清理仅使用隔离临时目录 + 内存 Store 快照删除，
不连真实 PG、不调用真实模型、不删除真实资源。
"""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime, timedelta
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

import psycopg
import pytest
from langgraph.prebuilt import ToolNode
from langgraph.store.memory import InMemoryStore
from psycopg_pool import PoolTimeout

from src.modules.db_deepagent.persistence.archive import QaArchive
from src.modules.db_deepagent.persistence.maintenance import (
    CleanupReport,
    MaintenanceService,
)
from src.modules.db_deepagent.persistence.session_guard import (
    acquire_memory_lease,
    release_memory_lease,
)
from src.modules.db_deepagent.runner import DbAgentRunner
from src.modules.db_deepagent.services.reuse_qualification import (
    EvidenceGraphVerifier,
    SourceUnqualified,
)
from src.modules.db_deepagent.services.task_completion import evaluate_submission
from src.modules.db_deepagent.services.task_ledger import task_ledger_key
from src.modules.db_deepagent.tools.final_answer import (
    build_completion_snapshot,
    make_submit_final_answer_tool,
)
from src.tests.db_deepagent.conftest import (
    FakeAgent,
    FakeStore,
    invoke_tool_node,
    make_task_record,
    make_task_state,
)
from src.tests.db_deepagent.unit.test_doc63_r2_regressions import (
    Harness,
    _apply,
    _evidence,
    _merge,
    _ref,
    _snapshot_of,
    _submission,
)
from src.tests.db_deepagent.unit.test_doc63_r2_regressions import (
    stack as _r2_stack,
)
from src.tests.db_deepagent.unit.test_runner_persistence import (
    _exec,
    _runner,
    _state,
)

pytestmark = pytest.mark.unit


@pytest.fixture()
def stack(db_agent_context, tmp_path):
    """复用 R2 的真实工具栈装配。"""
    return _r2_stack.__wrapped__(db_agent_context, tmp_path)


# ======================================================================
# 共用故障注入与状态构造
# ======================================================================


class _SnapshotReadFaultStore(FakeStore):
    """仅 run_snapshots 命名空间的 GET 抛持久化故障；P0 会话/线程读取正常。"""

    fault: BaseException | None = None

    def get(self, namespace, key):
        if len(namespace) > 1 and namespace[1] == "run_snapshots":
            raise self.fault or psycopg.OperationalError(
                "injected run_snapshot read unavailable"
            )
        return super().get(namespace, key)


def _fault_store(exc: BaseException) -> _SnapshotReadFaultStore:
    store = _SnapshotReadFaultStore()
    store.fault = exc
    return store


def _run_with_fault(assembly_services, exc, *, reuse=False):
    """真实 Runner.run()：正常 FakeAgent + QaArchive，仅 Store GET 注入故障。"""
    executions = [_exec(reused=True)] if reuse else None
    state = _state(executions=executions)
    store = _fault_store(exc)
    services = assembly_services
    services.store = store
    agent = FakeAgent(state)
    result = _runner(services, agent, store, state).run(
        "查询销售额", "s1", request_id="req-r4-io"
    )
    return result, agent, store


def _assert_persistence_closeout(result):
    """R4-01 要求 2/3/4：persistence_error、primary=persistence、系统兜底、
    证据保留、archived=False。"""
    assert result.status == "persistence_error", result.status
    primaries = [e for e in result.errors if e.primary]
    assert primaries and primaries[0].kind == "persistence", [
        (e.kind, e.message) for e in result.errors
    ]
    assert result.answer.startswith("系统兜底说明：")
    assert "本轮出现保存异常" in result.answer
    assert result.evidence, "已取得的证据必须保留"
    assert result.archived is False, "不得对同一失败后端继续正常归档"


# ======================================================================
# R4-01 · P1 · 来源快照 I/O 故障穿透 Runner 收尾
# ======================================================================


class TestR401RunnerIOBoundary:
    def test_plain_query_snapshot_io_fault_persistence_error(self, assembly_services):
        """普通查询：snapshot GET OperationalError 走真实 Runner.run 不穿透。"""
        result, agent, store = _run_with_fault(
            assembly_services, psycopg.OperationalError("snapshot get down")
        )
        _assert_persistence_closeout(result)
        # 不追加模型调用；invoke 仍只发生一次
        assert len(agent.invoke_calls) == 1
        # 不对同一失败后端继续正常归档（无 qa/run_errors 写入）
        assert not any(ns[1] in ("qa", "run_errors") for (ns, _k) in store.items)

    def test_reuse_snapshot_io_fault_persistence_error(self, assembly_services):
        """历史沿用：同样注入 snapshot GET 故障，仍返回持久化兜底。"""
        result, agent, _store = _run_with_fault(
            assembly_services,
            psycopg.OperationalError("snapshot get down"),
            reuse=True,
        )
        _assert_persistence_closeout(result)
        assert len(agent.invoke_calls) == 1

    def test_pool_timeout_classified_as_persistence(self, assembly_services):
        """PoolTimeout 也按已识别持久化故障处理，不吞成业务拒绝。"""
        result, _agent, _store = _run_with_fault(
            assembly_services, PoolTimeout("pool exhausted")
        )
        _assert_persistence_closeout(result)

    def test_unknown_runtime_error_converges_to_failed_fallback(
        self, assembly_services, monkeypatch
    ):
        """未知运行错误按既有 runtime 规则收敛为 failed + 兜底，不穿透破坏 6.2。"""
        state = _state()
        store = FakeStore()
        agent = FakeAgent(state)

        def _boom(self, *a, **kw):
            raise RuntimeError("unexpected bug in source assembly")

        monkeypatch.setattr(DbAgentRunner, "_task_completion_decision", _boom)
        result = _runner(assembly_services, agent, store, state).run(
            "问", "s1", request_id="req-r4-runtime"
        )
        assert result.status == "failed", result.status
        assert result.answer.startswith("系统兜底说明：")
        assert any(e.kind == "runtime" for e in result.errors)
        # runtime 不是后端故障：按 6.2 既有 failed 轮次正常归档到 run_errors
        assert result.archived is True

    def test_deterministic_bad_source_is_gate_failure_not_persistence(
        self, assembly_services
    ):
        """确定性坏来源：受控业务门控拒绝（非 persistence_error、非穿透）。"""
        state = _state()
        # 损坏血缘：旧协议裸字符串 input_result_refs → SourceUnqualified →
        # 共享核验转成 reuse_failures → 受控门控拒绝。
        state["db_executions"][0]["input_result_refs"] = ["res_old_string_protocol"]
        store = FakeStore()
        result = _runner(assembly_services, FakeAgent(state), store, state).run(
            "问", "s1", request_id="req-r4-deterministic"
        )
        assert result.status != "persistence_error", result.status
        assert result.answer.startswith("系统兜底说明：")
        assert any(e.kind == "task_completion_rejected" for e in result.errors)

    def test_build_completion_snapshot_reraises_persistence(self):
        """提交门控的广义 except 不把持久化故障伪装成 task_ledger_corrupt。"""
        state = _state()
        services = SimpleNamespace(store=_fault_store(psycopg.OperationalError("down")))
        with pytest.raises(psycopg.OperationalError):
            build_completion_snapshot(
                state,
                "run-x",
                services=services,
                session_id="s1",
                thread_id="t1",
            )

    def test_build_completion_snapshot_keeps_deterministic_business_rejection(self):
        """确定性账本损坏仍返回受控业务拒绝（不抛、不升级为持久化）。"""
        state = _state()
        state["db_sub_tasks"] = {"bad-key": {"status": "completed"}}
        services = SimpleNamespace(store=FakeStore())
        snap, err = build_completion_snapshot(
            state, "run-x", services=services, session_id="s1", thread_id="t1"
        )
        assert snap is None and err is not None  # 受控拒绝，不穿透

    def test_original_p0_model_exception_budget_behavior_preserved(
        self, assembly_services
    ):
        """原 P0 持久化跳过 invoke / 模型异常优先级保持（相邻场景）。"""
        store = FakeStore()
        store.get_side_effect = psycopg.OperationalError("p0 down")
        agent = FakeAgent(_state())
        p0 = _runner(assembly_services, agent, store, _state()).run("问", "s1")
        assert p0.status == "persistence_error"
        assert not agent.invoke_calls  # P0 故障不启动 invoke
        assert p0.archived is False

        store2 = FakeStore()
        agent2 = FakeAgent(_state())
        agent2.invoke_side_effect = RuntimeError("model boom")
        model_fail = _runner(assembly_services, agent2, store2, _state()).run(
            "问", "s1"
        )
        assert model_fail.status == "failed"
        assert model_fail.answer.startswith("系统兜底说明：")


# ======================================================================
# R4-02 · P2 · 多个独立阻塞根
# ======================================================================


def _mutate(s, ops):
    p = s.get("db_task_plans", {}).get("r", {}).get("plan_version", 0)
    return _merge(s, _apply(s, ops, p))


def _multi_root_state(t1, t2):
    """真实 TaskLedger→reducer：T1/T2 独立终止，T3 声明依赖两者并登记
    blocked_by=[T1@1, T2@1]（一次批量，服务端生成快照与首项根因）。"""
    s = _mutate(
        {},
        [
            ("add", {"sub_task_id": "T1", "content": "根1"}),
            ("add", {"sub_task_id": "T2", "content": "根2"}),
            (
                "add",
                {
                    "sub_task_id": "T3",
                    "content": "比较/汇总",
                    "dependencies": [
                        {"sub_task_id": "T1", "revision": 1},
                        {"sub_task_id": "T2", "revision": 1},
                    ],
                },
            ),
        ],
    )
    return _mutate(
        s,
        [
            ("status", {"sub_task_id": "T1", **t1}),
            ("status", {"sub_task_id": "T2", **t2}),
            (
                "status",
                {
                    "sub_task_id": "T3",
                    "status": "failed",
                    "reason_code": "dependency_blocked",
                    "reason": "两项上游都失败",
                    "blocked_by": [
                        {"sub_task_id": "T1", "revision": 1},
                        {"sub_task_id": "T2", "revision": 1},
                    ],
                },
            ),
        ],
    )


def _failed(reason):
    return {
        "status": "failed",
        "reason_code": "agent_abandoned",
        "reason": reason,
    }


def _nc(reason):
    return {
        "status": "needs_clarification",
        "reason": reason,
        "clarification_question": "请明确口径",
    }


def _full_path(state, payload):
    """真实 submit 工具 + 共享门控 + Runner 共享判定，返回 (decision, submit, runner)。"""
    snap = _snapshot_of(state, "r", session_id="s", thread_id="th")
    decision = evaluate_submission(payload, snap)
    ctx = SimpleNamespace(
        session_id="s",
        thread_id="th",
        run_id="r",
        request_id="req",
        carried_evidence=(),
    )
    updates, _message = invoke_tool_node(
        ToolNode([make_submit_final_answer_tool()]),
        [("submit_final_answer", payload, "tc-submit")],
        ctx,
        state,
    )
    runner = DbAgentRunner.__new__(DbAgentRunner)
    runner._services = SimpleNamespace(store=None)
    runner_decision = runner._task_completion_decision(
        state, "r", payload, {}, session_id="s", thread_id="th"
    )
    return decision, bool(updates.get("db_final_answers")), runner_decision


class TestR402MultipleIndependentRoots:
    def _assert_accepted(self, state, status="failed"):
        snap = _snapshot_of(state, "r", session_id="s", thread_id="th")
        unresolved = [
            {"sub_task_id": tid, "revision": rec["revision"]}
            for tid, rec in snap.tasks.items()
            if rec.get("status") in ("failed", "needs_clarification")
        ]
        payload = _submission(status, [], unresolved=unresolved)
        decision, written, runner_decision = _full_path(state, payload)
        assert decision.accepted, [e.message for e in decision.errors]
        assert written is True, "submit 工具应写入最终记录"
        assert runner_decision.accepted, [e.message for e in runner_decision.errors]
        return state

    def test_two_independent_failed_roots_accepted(self):
        """两个独立 failed 根：登记/submit/Runner 全部接受（R4-02 反例转正例）。"""
        s = _multi_root_state(_failed("A库无法访问"), _failed("B口径不明"))
        self._assert_accepted(s)
        # blocked_by_root 仍为首项主要根因摘要，不新增多根展示协议
        root = s["db_sub_tasks"][task_ledger_key("r", "T3", 1)]["termination_context"][
            "blocked_by_root"
        ]
        assert root["sub_task_id"] == "T1"
        assert set(
            s["db_sub_tasks"][task_ledger_key("r", "T3", 1)]["termination_context"]
        ) >= {"blocked_by_root", "dependency_snapshots", "decision_plan_version"}

    def test_failed_and_needs_clarification_two_roots_accepted(self):
        """failed + needs_clarification 两根：业务状态 needs_clarification，同样接受。"""
        s = _multi_root_state(_failed("A库无法访问"), _nc("时间口径不明"))
        self._assert_accepted(s, status="needs_clarification")

    def test_same_root_multiple_branches_accepted(self):
        """同一根的多个分支：其它项与首项同根也应接受（不强制等于首项时仍通过）。"""
        s = _multi_root_state(_failed("ROOT"), _failed("ROOT"))
        self._assert_accepted(s)

    def test_forged_root_still_rejected(self):
        """伪造 blocked_by_root 根因 → 仍按首项绑定拒绝。"""
        s = _multi_root_state(_failed("A库无法访问"), _failed("B口径不明"))
        key = task_ledger_key("r", "T3", 1)
        s["db_sub_tasks"][key]["termination_context"]["blocked_by_root"][
            "reason"
        ] = "伪造根因"
        payload = _submission(
            "failed",
            [],
            unresolved=[{"sub_task_id": t, "revision": 1} for t in ("T1", "T2", "T3")],
        )
        decision, written, runner_decision = _full_path(s, payload)
        assert not decision.accepted
        assert written is False
        assert not runner_decision.accepted
        assert any("blocked_by_root" in e.message for e in decision.errors)

    def test_empty_snapshots_and_forged_plan_rejected(self):
        """旧 C 防御反例：缺快照 + 错误计划版本仍拒绝。"""
        s = _multi_root_state(_failed("A库无法访问"), _failed("B口径不明"))
        ctx = s["db_sub_tasks"][task_ledger_key("r", "T3", 1)]["termination_context"]
        ctx["dependency_snapshots"] = []
        ctx["decision_plan_version"] = "forged"
        payload = _submission(
            "failed",
            [],
            unresolved=[{"sub_task_id": t, "revision": 1} for t in ("T1", "T2", "T3")],
        )
        decision, written, runner_decision = _full_path(s, payload)
        assert not decision.accepted
        assert not written
        assert not runner_decision.accepted
        assert any("decision_plan_version" in e.message for e in decision.errors)
        assert any("dependency_snapshots" in e.message for e in decision.errors)

    def test_appended_forged_second_blocked_by_rejected(self):
        """旧 D 防御反例：后来追加未经原决策覆盖的 blocked_by 仍拒绝。"""
        s = _mutate(
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
        )
        # 决策时 T2 仍 pending；仅 T1 failed、T3 blocked_by=[T1@1]
        s = _mutate(
            s,
            [
                ("status", {"sub_task_id": "T1", **_failed("ROOT")}),
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
        )
        # 后续批次 T2 才失败
        s = _mutate(s, [("status", {"sub_task_id": "T2", **_failed("later fail")})])
        key = task_ledger_key("r", "T3", 1)
        # 故障注入：追加未经原决策覆盖的第二个 blocked_by
        s["db_sub_tasks"][key]["blocked_by"].append(
            {"sub_task_id": "T2", "revision": 1}
        )
        payload = _submission(
            "failed",
            [],
            unresolved=[{"sub_task_id": t, "revision": 1} for t in ("T1", "T2", "T3")],
        )
        decision, written, runner_decision = _full_path(s, payload)
        assert not decision.accepted, [e.message for e in decision.errors]
        assert not written
        assert not runner_decision.accepted

    def test_historical_void_positive_kept(self):
        """历史 revise/void 正例：同 revision 后来 void 不改写旧决策依据。"""
        s = _multi_root_state(_failed("A库无法访问"), _failed("B口径不明"))
        key = task_ledger_key("r", "T3", 1)
        before = copy.deepcopy(s["db_sub_tasks"][key]["termination_context"])
        s = _mutate(s, [("void", {"sub_task_id": "T1", "reason": "计划调整"})])
        assert s["db_sub_tasks"][key]["termination_context"] == before
        self._assert_accepted(s)

    def test_real_runner_run_multi_root_delivery(self, stack):
        """真实 Runner.run()：多根 failed 提交经收尾/交付/归档，非纯判定。"""
        h = Harness(stack)  # 真实 ToolNode，构建 run-x 状态
        h.fresh("run-x")
        h.task(
            [
                {"op": "add", "sub_task_id": "T1", "content": "根1"},
                {"op": "add", "sub_task_id": "T2", "content": "根2"},
                {
                    "op": "add",
                    "sub_task_id": "T3",
                    "content": "汇总",
                    "dependencies": [
                        {"sub_task_id": "T1", "revision": 1},
                        {"sub_task_id": "T2", "revision": 1},
                    ],
                },
            ]
        )
        h.task(
            [
                {
                    "op": "status",
                    "sub_task_id": "T1",
                    **_failed("A库无法访问"),
                },
                {
                    "op": "status",
                    "sub_task_id": "T2",
                    **_failed("B口径不明"),
                },
                {
                    "op": "status",
                    "sub_task_id": "T3",
                    "status": "failed",
                    "reason_code": "dependency_blocked",
                    "reason": "两项上游都失败",
                    "blocked_by": [
                        {"sub_task_id": "T1", "revision": 1},
                        {"sub_task_id": "T2", "revision": 1},
                    ],
                },
            ]
        )
        payload = _submission(
            "failed",
            [],
            unresolved=[{"sub_task_id": t, "revision": 1} for t in ("T1", "T2", "T3")],
            answer="两项上游均失败，已交代",
        )
        _submitted, _updates = h.call("submit_final_answer", payload)
        assert "run-x" in (
            h.state.get("db_final_answers") or {}
        ), "submit 工具应写入最终答案记录"
        stack.services.config.persistence.mode = "memory"
        stack.services.source_id = "db_src_1"
        stack.db.datasource_id = "pg:test"
        release_memory_lease(stack.ctx.session_guard_lease)
        store = FakeStore()
        result = _runner(stack.services, FakeAgent(h.state), store, h.state).run(
            "问", "s-test", request_id="req-r4-multiroot"
        )
        # 合法 failed 提交按业务状态交付（不是被门控误拒后的系统兜底）
        assert "两项上游均失败" in result.answer, result.answer
        assert not result.answer.startswith("系统兜底说明：")
        assert result.archived is True
        assert not any(e.kind == "invalid_task_dependency" for e in result.errors)


# ======================================================================
# R4-03 · P2 · 跨周期清理制造缺源
# ======================================================================


def _cleanup_service(root, archive):
    return MaintenanceService(
        SimpleNamespace(workspace=SimpleNamespace(root=str(root))), archive, None
    )


def _sealed_snapshot(archive, session, thread, run, *, executions, sealed_at):
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
            "sealed_at": sealed_at.isoformat(),
            "tasks": [],
            "executions": list(executions),
            "analyses": [],
        },
    )


def _source_record(run, eid, result_ref):
    return {
        "session_id": "s1",
        "thread_id": "dbda:s1:1",
        "run_id": run,
        "evidence_id": eid,
        "result_ref": result_ref,
        "sub_task_id": "T1",
        "sub_task_revision": 1,
        "input_result_refs": [],
    }


def _direct_child(run, eid, result_ref, source_run, source_eid, source_ref):
    return {
        **_source_record(run, eid, result_ref),
        "direct_reuse_from": {
            "evidence_ref": {
                "session_id": "s1",
                "thread_id": "dbda:s1:1",
                "run_id": source_run,
                "evidence_id": source_eid,
            },
            "source_task_ref": {
                "session_id": "s1",
                "thread_id": "dbda:s1:1",
                "run_id": source_run,
                "sub_task_id": "T1",
                "revision": 1,
            },
        },
    }


def _cleanup(archive, moment, *, apply=True):
    """真实 _cleanup_session_resources，apply 时用真实内存 Store 快照删除；
    文件 rmtree 由 patch 禁止，避免删除真实资源。"""
    with TemporaryDirectory(prefix="doc63-r4-") as root:
        svc = _cleanup_service(root, archive)
        report = CleanupReport(dry_run=False)
        lease = acquire_memory_lease("s1")
        try:
            with patch(
                "src.modules.db_deepagent.persistence.maintenance.shutil.rmtree",
                side_effect=AssertionError("real file deletion forbidden"),
            ):
                svc._cleanup_session_resources(
                    "s1",
                    result_metas={},
                    result_retention=timedelta(days=30),
                    archive_before=moment - timedelta(days=7),
                    now=moment,
                    apply=apply,
                    report=report,
                    conn=None,
                )
        finally:
            release_memory_lease(lease)
        return report


class TestR403DependencyConsistentDeletion:
    def _archive_direct(self, now):
        ref = "res_" + "a" * 32
        archive = QaArchive(InMemoryStore())
        source = _source_record("source", "E-source", ref)
        reused = _direct_child("reuse", "E-reused", ref, "source", "E-source", ref)
        # source 8 天（到期）、reuse 1 天（未到期）
        _sealed_snapshot(
            archive,
            "s1",
            "dbda:s1:1",
            "source",
            executions=[source],
            sealed_at=now - timedelta(days=8),
        )
        _sealed_snapshot(
            archive,
            "s1",
            "dbda:s1:1",
            "reuse",
            executions=[reused],
            sealed_at=now - timedelta(days=1),
        )
        return archive

    def test_two_cycles_defer_then_reclaim_both(self):
        """连续两次清理：第一次不制造悬空引用；全部到期后一起回收。"""
        now = datetime.now(UTC)
        archive = self._archive_direct(now)
        first = _cleanup(archive, now)
        rem1 = sorted(s["run_id"] for s in archive.list_run_snapshots("s1"))
        assert rem1 == ["reuse", "source"], rem1  # 来源被延迟删除
        assert any("延迟清理" in x["reason"] for x in first.skipped)
        second = _cleanup(archive, now + timedelta(days=8))
        rem2 = sorted(s["run_id"] for s in archive.list_run_snapshots("s1"))
        assert rem2 == [], rem2  # 全部到期一起回收
        assert not any("缺源" in x["reason"] for x in second.skipped)
        assert not any("跳过该会话全部" in x["reason"] for x in second.skipped)

    def test_input_multihop_deferral(self):
        """input 多跳：保留孤儿引用的整条到期来源链递归延迟。"""
        now = datetime.now(UTC)
        archive = QaArchive(InMemoryStore())
        r1, r2, r3 = ("res_" + c * 32 for c in ("a", "b", "c"))
        e1 = _source_record("run-1", "E-1", r1)
        e2 = {
            **_source_record("run-2", "E-2", r2),
            "input_result_refs": [
                {
                    "result_ref": r1,
                    "evidence_ref": _ref_of("run-1", "E-1"),
                    "source_task_ref": _task_ref_of("run-1"),
                }
            ],
        }
        e3 = {
            **_source_record("run-3", "E-3", r3),
            "input_result_refs": [
                {
                    "result_ref": r2,
                    "evidence_ref": _ref_of("run-2", "E-2"),
                    "source_task_ref": _task_ref_of("run-2"),
                }
            ],
        }
        ages = {"run-1": 9, "run-2": 8, "run-3": 1}
        for run, rec in [("run-1", e1), ("run-2", e2), ("run-3", e3)]:
            _sealed_snapshot(
                archive,
                "s1",
                "dbda:s1:1",
                run,
                executions=[rec],
                sealed_at=now - timedelta(days=ages[run]),
            )
        first = _cleanup(archive, now)
        remaining = sorted(s["run_id"] for s in archive.list_run_snapshots("s1"))
        # 未到期 run-3 引用 run-2，run-2 引用 run-1：整条到期链递归延迟
        assert remaining == ["run-1", "run-2", "run-3"], remaining
        assert sum("延迟清理" in x["reason"] for x in first.skipped) == 2
        second = _cleanup(archive, now + timedelta(days=8))
        assert archive.list_run_snapshots("s1") == []
        assert not any("缺源" in x["reason"] for x in second.skipped)

    def test_direct_multihop_deferral(self):
        """direct 多跳沿用链：同样递归延迟，不制造悬空引用。"""
        now = datetime.now(UTC)
        archive = QaArchive(InMemoryStore())
        r = "res_" + "a" * 32
        e1 = _source_record("run-a", "E-a", r)
        e2 = _direct_child("run-b", "E-b", r, "run-a", "E-a", r)
        e3 = _direct_child("run-c", "E-c", r, "run-b", "E-b", r)
        for run, rec, age in [("run-a", e1, 10), ("run-b", e2, 8), ("run-c", e3, 1)]:
            _sealed_snapshot(
                archive,
                "s1",
                "dbda:s1:1",
                run,
                executions=[rec],
                sealed_at=now - timedelta(days=age),
            )
        _cleanup(archive, now)
        assert sorted(s["run_id"] for s in archive.list_run_snapshots("s1")) == [
            "run-a",
            "run-b",
            "run-c",
        ]
        second = _cleanup(archive, now + timedelta(days=8))
        assert archive.list_run_snapshots("s1") == []
        assert not any("缺源" in x["reason"] for x in second.skipped)

    def test_rooted_protection_then_remove_last_root(self):
        """有根保护期间不删除；移除最后根后重新清理可回收。"""
        now = datetime.now(UTC)
        archive = self._archive_direct(now)
        # 引用 reuse 输出为 QA 根，递归保护 source
        archive.write_qa(
            "s1",
            "req-root",
            {
                "evidence": [
                    {
                        "evidence_id": "A-derived",
                        "output_result_ref": "res_" + "a" * 32,
                        "input_result_refs": [],
                    }
                ],
            },
        )
        first = _cleanup(archive, now)
        assert not any("孤儿快照已清理" in x["reason"] for x in first.skipped)
        assert len(archive.list_run_snapshots("s1")) == 2
        # 移除最后根（QA 过期）
        archive._store.delete(("db_deepagent", "qa", "s1"), "req-root")
        second = _cleanup(archive, now + timedelta(days=8))
        assert archive.list_run_snapshots("s1") == []
        assert not any("缺源" in x["reason"] for x in second.skipped)

    def test_bad_protocol_and_missing_source_still_fail_closed(self):
        """坏协议/真实缺源仍明确报告并保守停止删除。"""
        now = datetime.now(UTC)
        # 坏协议
        bad = QaArchive(InMemoryStore())
        _sealed_snapshot(
            bad,
            "s1",
            "dbda:s1:1",
            "run-bad",
            executions=[_source_record("run-bad", "E-bad", "res_" + "a" * 32)],
            sealed_at=now - timedelta(days=8),
        )
        snap = bad.read_run_snapshot("s1", "dbda:s1:1", "run-bad")
        snap["run_snapshot_schema_version"] = 999
        bad._store.put(
            ("db_deepagent", "run_snapshots", "s1"),
            json.dumps(["dbda:s1:1", "run-bad"], separators=(",", ":")),
            snap,
        )
        r1 = _cleanup(bad, now)
        assert len(bad.list_run_snapshots("s1")) == 1
        assert any("跳过该会话全部" in x["reason"] for x in r1.skipped)

        # 真实缺源（引用不存在的来源）
        missing = QaArchive(InMemoryStore())
        rec = _direct_child(
            "run-child",
            "E-child",
            "res_" + "b" * 32,
            "run-ghost",
            "E-ghost",
            "res_" + "a" * 32,
        )
        _sealed_snapshot(
            missing,
            "s1",
            "dbda:s1:1",
            "run-child",
            executions=[rec],
            sealed_at=now - timedelta(days=8),
        )
        r2 = _cleanup(missing, now)
        assert any("缺源" in x["reason"] for x in r2.skipped)
        assert len(missing.list_run_snapshots("s1")) == 1

    def test_cycle_still_fail_closed(self):
        """来源链成环仍明确报告并保守停止。"""
        now = datetime.now(UTC)
        archive = QaArchive(InMemoryStore())
        ra, rb = "res_" + "a" * 32, "res_" + "b" * 32
        ea = {
            **_source_record("run-a", "E-a", ra),
            "input_result_refs": [
                {
                    "result_ref": rb,
                    "evidence_ref": _ref_of("run-b", "E-b"),
                    "source_task_ref": _task_ref_of("run-b"),
                }
            ],
        }
        eb = {
            **_source_record("run-b", "E-b", rb),
            "input_result_refs": [
                {
                    "result_ref": ra,
                    "evidence_ref": _ref_of("run-a", "E-a"),
                    "source_task_ref": _task_ref_of("run-a"),
                }
            ],
        }
        _sealed_snapshot(
            archive,
            "s1",
            "dbda:s1:1",
            "run-a",
            executions=[ea],
            sealed_at=now - timedelta(days=8),
        )
        _sealed_snapshot(
            archive,
            "s1",
            "dbda:s1:1",
            "run-b",
            executions=[eb],
            sealed_at=now - timedelta(days=8),
        )
        report = _cleanup(archive, now)
        assert any("环" in x["reason"] for x in report.skipped)
        assert len(archive.list_run_snapshots("s1")) == 2


def _ref_of(run, eid):
    return {
        "session_id": "s1",
        "thread_id": "dbda:s1:1",
        "run_id": run,
        "evidence_id": eid,
    }


def _task_ref_of(run):
    return {
        "session_id": "s1",
        "thread_id": "dbda:s1:1",
        "run_id": run,
        "sub_task_id": "T1",
        "revision": 1,
    }


# ======================================================================
# M1 · 非阻塞 · verifier 完成节点缓存
# ======================================================================


class TestM1VerifierCache:
    def _compare_dag(self, n):
        """构造同任务 compare DAG：每项消费前两项成果（共享祖先）。"""
        state = make_task_state(
            [make_task_record("run-dag", "T1", 1, status="in_progress")]
        )
        records = []
        for i in range(n):
            eid = f"A-{i}"
            rec = {
                "evidence_id": eid,
                "tool_call_id": "tc-" + eid,
                "run_id": "run-dag",
                "request_id": "req-run-dag",
                "session_id": "s-test",
                "thread_id": "t-test",
                "result_ref": "res_" + f"{i:032d}"[-32:],
                "sub_task_id": "T1",
                "sub_task_revision": 1,
                "input_result_refs": [],
            }
            if i >= 1:
                rec["input_result_refs"].append(
                    {
                        "result_ref": records[i - 1]["result_ref"],
                        "evidence_ref": _ref("run-dag", records[i - 1]["evidence_id"]),
                        "source_task_ref": {
                            "session_id": "s-test",
                            "thread_id": "t-test",
                            "run_id": "run-dag",
                            "sub_task_id": "T1",
                            "revision": 1,
                        },
                    }
                )
            if i >= 2:
                rec["input_result_refs"].append(
                    {
                        "result_ref": records[i - 2]["result_ref"],
                        "evidence_ref": _ref("run-dag", records[i - 2]["evidence_id"]),
                        "source_task_ref": {
                            "session_id": "s-test",
                            "thread_id": "t-test",
                            "run_id": "run-dag",
                            "sub_task_id": "T1",
                            "revision": 1,
                        },
                    }
                )
            records.append(rec)
        state["db_analyses"] = records
        return state

    def test_shared_ancestor_visit_count_bounded(self, monkeypatch):
        """共享祖先不再指数级重复遍历：10/14/18 节点调用次数近线性。"""
        from src.modules.db_deepagent.services import reuse_qualification as rq

        original = EvidenceGraphVerifier.verify_node
        for n in (10, 14, 18):
            state = self._compare_dag(n)
            counter = [0]

            def counted(self, *a, _counter=counter, _original=original, **kw):
                _counter[0] += 1
                return _original(self, *a, **kw)

            monkeypatch.setattr(EvidenceGraphVerifier, "verify_node", counted)
            failures = rq.unqualified_reuse_evidence(
                state["db_analyses"][-1:],
                session_id="s-test",
                thread_id="t-test",
                run_id="run-dag",
                state=state,
            )
            monkeypatch.undo()
            assert failures == {}, failures
            # 修复前 18 节点为 5167 次；缓存后为近线性（显著低于节点数平方）
            assert counter[0] <= 4 * n + 8, (n, counter[0])

    def test_role_transition_input_then_source_still_strict(self):
        """input 验证过不能跳过更严格的 source 资格检查（角色入键）。"""
        state = make_task_state([make_task_record("run-r", "T1", 1, status="failed")])
        rec = _evidence("E-1", "run-r", "T1", 1)
        state["db_executions"] = [rec]
        verifier = EvidenceGraphVerifier(session_id="s-test", state=state, archive=None)
        ref = _ref("run-r", "E-1", thread="t-test", session="s-test")
        # input/outcome 角色：failed 任务非 voided → 允许
        verifier.verify_node(ref, role="input")
        # 同一节点以 source 角色：要求 completed → 必须仍被拒绝
        with pytest.raises(SourceUnqualified) as exc:
            verifier.verify_node(ref, role="source")
        assert "未完成" in exc.value.reason or "不能沿用" in exc.value.reason

    def test_each_edge_still_validates_expected_constraints(self):
        """缓存命中后，后续边的 expected task/result 约束仍逐边核验。"""
        state = make_task_state(
            [
                make_task_record(
                    "run-e", "T1", 1, status="completed", evidence_ids=["E-1"]
                )
            ]
        )
        rec = _evidence("E-1", "run-e", "T1", 1, result_ref="res_real")
        state["db_executions"] = [rec]
        verifier = EvidenceGraphVerifier(session_id="s-test", state=state, archive=None)
        ref = _ref("run-e", "E-1")
        verifier.verify_node(ref, role="input", expected_result_ref="res_real")
        # 缓存已含 (身份,"input")；用伪造 expected_result_ref 再到达同一节点，
        # 仍必须在缓存短路之前被拒绝。
        with pytest.raises(SourceUnqualified) as exc:
            verifier.verify_node(ref, role="input", expected_result_ref="res_forged")
        assert "result_ref" in exc.value.reason

    def test_cache_is_per_view_not_global(self):
        """缓存局限当前一致视图：不同 verifier 实例不共享已完成节点。"""
        state = make_task_state(
            [make_task_record("run-v", "T1", 1, status="in_progress")]
        )
        rec = _evidence("E-1", "run-v", "T1", 1)
        state["db_executions"] = [rec]
        v1 = EvidenceGraphVerifier(session_id="s-test", state=state, archive=None)
        v2 = EvidenceGraphVerifier(session_id="s-test", state=state, archive=None)
        ref = _ref("run-v", "E-1")
        v1.verify_node(ref, role="input")
        assert (
            ("s-test", ref["thread_id"], "run-v", "E-1"),
            "input",
        ) in v1._verified_nodes
        assert v2._verified_nodes == set()
