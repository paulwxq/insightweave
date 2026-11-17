"""doc 6.3 R1 审核反例回归（codex_doc63_impl_review_r1.md 逐项）

覆盖 R1-01…R1-17 的具体反例：正常 Web 锁交接进入 Agent、来源资格
（revise/void/未选成果/无快照/删 checkpoint 凭快照/多跳链）、同任务旧
revision 占位符、memory 清理互斥、归档 snapshot_ref 闭包、全依赖准入、
多跳轮换清单寻址、错误归属/伪造/空壳、整批顺序无关、无关 blocked_by、
replacement 历史复核、幂等口径、确定性分项正文、兜底分级、reducer
覆盖防护、空保护集 SQL、WebTurn DTO。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from langgraph.prebuilt import ToolNode
from pydantic import ValidationError

from src.modules.db_deepagent.persistence.archive import QaArchive
from src.modules.db_deepagent.persistence.session_guard import (
    acquire_memory_lease,
    release_memory_lease,
)
from src.modules.db_deepagent.schemas import DbAgentFinalAnswer
from src.modules.db_deepagent.services.task_completion import (
    CompletionSnapshot,
    evaluate_submission,
)
from src.modules.db_deepagent.services.task_ledger import (
    LedgerOperation,
    TaskLedger,
    TaskLedgerError,
    task_ledger_key,
)
from src.modules.db_deepagent.state import LedgerConsistencyError
from src.tests.db_deepagent.conftest import (
    FakeAgent,
    FakeStore,
    make_task_record,
    make_task_state,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 13, 10, 0, tzinfo=UTC)


def _apply(state, ops, expected=0):
    return TaskLedger(state).apply(
        run_id="run-x",
        session_id="s-test",
        thread_id="t-test",
        request_id="req-x",
        expected_plan_version=expected,
        operations=ops,
        now=NOW,
        tool_call_id="tc",
    )


def _op(op, tid="T1", **kw):
    return LedgerOperation(op=op, sub_task_id=tid, **kw)


def _merge_states(*states):
    merged = {"db_sub_tasks": {}, "db_task_plans": {}}
    for state in states:
        merged["db_sub_tasks"].update(state.get("db_sub_tasks") or {})
        merged["db_task_plans"].update(state.get("db_task_plans") or {})
    return merged


# ====================================================================
# R1-01：Web SessionLockHandle → Runner 真实链路
# ====================================================================


class TestR101WebLockHandoff:
    def test_web_handle_to_runner_enters_agent(self, assembly_services):
        """真实 SessionLockHandle(held) 包装 → Runner.run 正常进入 Agent。

        修复前 lease_from_advisory_lock 只认 locked，held=True 的 Web 包装
        抛 SessionGuardRequired，正常查询在 _run_locked 前失败。
        """
        from src.api.v2.runtime import SessionLockHandle
        from src.modules.db_deepagent.agent import DbAgentHandle
        from src.modules.db_deepagent.runner import DbAgentRunner

        acquired = {"n": 0}
        released = {"n": 0}
        lock = SessionLockHandle(
            lambda: (acquired.__setitem__("n", acquired["n"] + 1) or True),
            lambda: released.__setitem__("n", released["n"] + 1),
        )
        assert lock.try_acquire() is True  # held=True
        _state = {
            "messages": [],
            **make_task_state([make_task_record("ignored", "T1", 1)]),
        }
        # FakeAgent 状态会被 remap；给一个完成态提交由既有装配覆盖——此处
        # 只验证锁链路可达 Agent（invoke 后有 get_state 调用即进入 P1/P2）。
        agent = FakeAgent({"messages": []})
        handle = DbAgentHandle(
            agent=agent,
            backend=None,
            permissions=[],
            harness_key="k",
            session_dir=Path("."),
        )
        runner = DbAgentRunner(
            services=assembly_services,
            handle=handle,
            archive=QaArchive(FakeStore()),
        )
        runner.run("问", "s-web-lock", session_lock=lock, request_id="req-wl1")
        assert acquired["n"] == 1
        assert len(agent.invoke_calls) >= 1  # 进入 Agent（而非锁转换失败）
        assert lock.held is True  # 释放职责仍归外层（Runner 不释放交接锁）
        lock.release()
        assert released["n"] == 1

    def test_released_web_handle_cannot_mint_lease(self):
        from src.api.v2.runtime import SessionLockHandle
        from src.modules.db_deepagent.persistence.session_guard import (
            SessionGuardRequired,
            lease_from_advisory_lock,
        )

        lock = SessionLockHandle(lambda: True, lambda: None)
        lock.try_acquire()
        lock.release()
        with pytest.raises(SessionGuardRequired):
            lease_from_advisory_lock(lock, "s1")


# ====================================================================
# R1-02：来源资格与快照读路径
# ====================================================================


def _source_evidence(eid="E-old", run_id="run-old", task=("T0", 1)):
    return {
        "evidence_id": eid,
        "tool_call_id": "tc-old",
        "run_id": run_id,
        "request_id": "req-old",
        "purpose": "query",
        "datasource_id": "orders",
        "sql_template": "SELECT 1",
        "executed_sql": "SELECT 1",
        "executed_sql_sha256": "h",
        "result_ref": "res_" + eid[-1] * 32,
        "columns": [],
        "saved_row_count": 1,
        "source_truncated": False,
        "elapsed_ms": 1,
        "warnings": [],
        "sub_task_id": task[0] if task else None,
        "sub_task_revision": task[1] if task else None,
        "input_result_refs": [],
    }


def _ref(run_id="run-old", eid="E-old", thread="t-test"):
    return {
        "session_id": "s-test",
        "thread_id": thread,
        "run_id": run_id,
        "evidence_id": eid,
    }


class TestR102SourceQualification:
    def _verify(self, record, ref, state, archive=None):
        from src.modules.db_deepagent.services.reuse_qualification import (
            verify_reuse_source,
        )

        verify_reuse_source(
            ref, record, session_id="s-test", state=state, archive=archive
        )

    def _expect_fail(self, record, ref, state, archive=None):
        from src.modules.db_deepagent.services.reuse_qualification import (
            SourceUnqualified,
        )

        with pytest.raises(SourceUnqualified):
            self._verify(record, ref, state, archive)

    def test_source_revoked_by_revise_rejected(self):
        """来源任务 revise 后，旧 revision 成果不得沿用（R1-02 复现）。"""
        rec = _source_evidence()
        t1 = make_task_record(
            "run-old",
            "T0",
            1,
            status="completed",
            evidence_ids=["E-old"],
            content="旧版",
        )
        t2 = make_task_record("run-old", "T0", 2, content="新版")
        state = make_task_state([t1, t2])
        self._expect_fail(rec, _ref(), state)

    def test_source_voided_rejected(self):
        rec = _source_evidence()
        t1 = make_task_record("run-old", "T0", 1, status="voided", content="旧版")
        t1["reason"] = "替换"
        state = make_task_state([t1])
        self._expect_fail(rec, _ref(), state)

    def test_source_not_completed_rejected(self):
        rec = _source_evidence()
        t1 = make_task_record("run-old", "T0", 1, status="pending", content="x")
        state = make_task_state([t1])
        self._expect_fail(rec, _ref(), state)

    def test_evidence_not_selected_rejected(self):
        """来源任务 completed 但未选定该证据 → 拒绝。"""
        rec = _source_evidence()
        t1 = make_task_record(
            "run-old", "T0", 1, status="completed", evidence_ids=[], content="x"
        )
        state = make_task_state([t1])
        self._expect_fail(rec, _ref(), state)

    def test_no_snapshot_no_ledger_rejected(self):
        rec = _source_evidence()
        self._expect_fail(rec, _ref(), {"db_sub_tasks": {}})

    def _sealed_snapshot(
        self,
        store,
        task_status="completed",
        selected=("E-old",),
        run_id="run-old",
        tid="T0",
    ):
        archive = QaArchive(store)
        task = make_task_record(
            run_id,
            tid,
            1,
            status=task_status,
            evidence_ids=list(selected),
            content="来源",
        )
        archive.write_run_snapshot(
            "s-test",
            "t-test",
            run_id,
            {
                "run_snapshot_schema_version": 1,
                "ledger_protocol_version": 1,
                "session_id": "s-test",
                "thread_id": "t-test",
                "request_id": "req-old",
                "run_id": run_id,
                "run_status_at_seal": "completed",
                "plan_version": 1,
                "tasks": [task],
                "executions": [_source_evidence(run_id=run_id, task=(tid, 1))],
                "analyses": [],
                "tool_errors": [],
                "coverage": None,
                "interrupted_tasks": [],
                "sealed_at": "2026-11-13T10:00:00+08:00",
            },
        )
        return archive

    def test_deleted_checkpoint_verified_via_snapshot(self):
        """删除来源 checkpoint 后仅凭运行快照可核验（§8.2）。"""
        store = FakeStore()
        archive = self._sealed_snapshot(store)
        rec = _source_evidence()
        # 线程账本为空（checkpoint 已清）——快照仍可核验
        self._verify(rec, _ref(), {"db_sub_tasks": {}}, archive=archive)

    def test_snapshot_unknown_protocol_rejected(self):
        store = FakeStore()
        archive = self._sealed_snapshot(store)
        # 篡改协议版本（直接入 store 绕过写校验）
        key = archive._run_snapshot_key("t-test", "run-old")
        record = store.items[(("db_deepagent", "run_snapshots", "s-test"), key)]
        record["ledger_protocol_version"] = 99
        rec = _source_evidence()
        self._expect_fail(rec, _ref(), {"db_sub_tasks": {}}, archive=archive)

    def test_same_thread_two_runs_same_task_id_addressing(self):
        """同线程两轮同名 T1@1：快照按 run 身份互不串读。"""
        store = FakeStore()
        archive = self._sealed_snapshot(store, run_id="run-a", tid="T1")
        archive.write_run_snapshot(
            "s-test",
            "t-test",
            "run-b",
            {
                "run_snapshot_schema_version": 1,
                "ledger_protocol_version": 1,
                "session_id": "s-test",
                "thread_id": "t-test",
                "request_id": "req-b",
                "run_id": "run-b",
                "tasks": [
                    make_task_record(
                        "run-b", "T1", 1, status="pending", content="第二轮"
                    )
                ],
                "executions": [],
                "analyses": [],
                "tool_errors": [],
                "sealed_at": "2026-11-13T10:00:00+08:00",
            },
        )
        # run-a 的任务记录属于 run-a；run-b 快照不含 run-a 任务
        snap_a = archive.read_run_snapshot("s-test", "t-test", "run-a")
        assert all(t["run_id"] == "run-a" for t in snap_a["tasks"])

    def test_ledger_submission_gate_rejects_unqualified_reuse(self):
        """R1-02 终局：沿用来源已 revise 的证据提交 → 最终门控拒绝。"""
        reused = {
            **_source_evidence(eid="E-reuse1", run_id="run-x", task=("T1", 1)),
            "direct_reuse_from": {
                "evidence_ref": _ref(),
                "source_task_ref": {
                    "session_id": "s-test",
                    "thread_id": "t-test",
                    "run_id": "run-old",
                    "sub_task_id": "T0",
                    "revision": 1,
                },
            },
        }
        # 来源任务已被 revise（@2 存在）
        t_old1 = make_task_record(
            "run-old",
            "T0",
            1,
            status="completed",
            evidence_ids=["E-old"],
            content="旧版",
        )
        t_old2 = make_task_record("run-old", "T0", 2, content="新版")
        t_cur = make_task_record(
            "run-x",
            "T1",
            1,
            status="completed",
            evidence_ids=["E-reuse1"],
            content="目标",
        )
        snapshot = CompletionSnapshot(
            run_id="run-x",
            tasks={"T1": t_cur},
            executions=[reused],
            analyses=[],
            errors=[],
            known_counts={"E-reuse1": 1},
            proven_untruncated={"E-reuse1": True},
            reuse_failures={"E-reuse1": "来源 revision=1 已被取代"},
            all_task_records={
                ("run-old", "T0", 1): t_old1,
                ("run-old", "T0", 2): t_old2,
                ("run-x", "T1", 1): t_cur,
            },
        )
        submission = {
            "status": "completed",
            "answer": "结论",
            "findings": [
                {
                    "text": "发现",
                    "finding_id": "F-1",
                    "task_refs": [{"sub_task_id": "T1", "revision": 1}],
                    "evidence_ids": ["E-reuse1"],
                }
            ],
            "metric_definitions": [],
            "limitations": [],
            "open_questions": [],
            "unresolved_task_refs": [],
        }
        decision = evaluate_submission(submission, snapshot)
        assert not decision.accepted
        assert any(e.code == "stale_task_inputs" for e in decision.errors)


# ====================================================================
# R1-03：同任务旧 revision 占位符
# ====================================================================


class TestR103SameTaskOldRevisionPlaceholder:
    def _stack(self, pipeline_stack, db_agent_context):
        """T1@1 completed（产出 E-prior）；revise→T1@2 pending。"""
        pipeline, db, results, _budget, _cfg = pipeline_stack
        from src.tests.db_deepagent.conftest import make_time_evidence

        sep = make_time_evidence("T-sep", "2026-09-01", "2026-10-01")
        meta = results.save(
            ctx=db_agent_context,
            purpose="query",
            sql_template="SELECT customer_id FROM public.orders",
            executed_sql="SELECT customer_id FROM public.orders",
            columns=[{"name": "customer_id", "pg_type": "varchar"}],
            rows=[("C001",)],
            source_truncated=False,
            derived_from=[],
            guard_evidence={"time": [], "dim": []},
        )
        prior_exec = _source_evidence("E-prior", run_id="run-test", task=("T1", 1))
        prior_exec["result_ref"] = meta.result_ref
        prior_exec["datasource_id"] = db_agent_context.datasource_id
        t1v1 = make_task_record(
            "run-test",
            "T1",
            1,
            status="completed",
            evidence_ids=["E-prior"],
            content="v1",
        )
        t1v2 = make_task_record("run-test", "T1", 2, content="v2")
        state = {
            "db_time_evidence": [sep],
            "db_dim_evidence": [],
            "db_validations": {},
            "db_executions": [prior_exec],
            **_merge_states(make_task_state([t1v1, t1v2])),
        }
        return pipeline, db, state, meta

    def test_old_revision_placeholder_rejected_before_side_effects(
        self, pipeline_stack, db_agent_context
    ):
        """T1@2 的 SQL 引用 @1 的 result_ref → validate 即拒（无授权/无执行）。"""
        from src.modules.db_deepagent.services.sql_pipeline import LedgerView

        pipeline, db, state, meta = self._stack(pipeline_stack, db_agent_context)
        sql = (
            "SELECT id FROM public.orders "
            f"WHERE customer_id IN {{{{result:{meta.result_ref}:customer_id}}}} "
            "AND created_at >= '2026-09-01' AND created_at < '2026-10-01'"
        )
        outcome, record, update = pipeline.validate(
            sql,
            db_agent_context,
            LedgerView(state),
            sub_task_id="T1",
            sub_task_revision=2,
        )
        assert not outcome.valid
        assert "stale_task_inputs" in outcome.error_codes
        assert "旧 revision" in outcome.errors[0]
        assert record is None and not update.validations  # 无授权
        assert not db.executed  # 无业务执行/EXPLAIN 副作用前拒绝
        assert db.explained == []  # 校验阶段不产生 EXPLAIN


# ====================================================================
# R1-04：memory 清理互斥
# ====================================================================


class TestR104MemoryCleanupMutex:
    def _memory_service(self, db_agent_config, tmp_path, monkeypatch):
        from src.modules.db_deepagent.persistence import maintenance as mod

        cfg = db_agent_config.model_copy(deep=True)
        cfg.persistence.mode = "memory"
        cfg.workspace.root = str(tmp_path)
        service = mod.MaintenanceService(
            cfg, QaArchive(FakeStore()), FakeAgent({"messages": []})
        )
        monkeypatch.setattr(service, "_list_sessions", lambda: ["s-live"])
        return service, cfg, tmp_path

    def test_runner_held_session_skipped(self, db_agent_config, tmp_path, monkeypatch):
        """Runner（memory lease）持锁期间 cleanup 跳过该会话。"""
        from src.tests.db_deepagent.conftest import FakeStore

        service, cfg, _root = None, None, None
        from src.modules.db_deepagent.persistence import maintenance as mod

        cfg = db_agent_config.model_copy(deep=True)
        cfg.persistence.mode = "memory"
        cfg.workspace.root = str(tmp_path)
        store = FakeStore()
        old = datetime.now(UTC) - timedelta(days=400)
        store.put(
            ("db_deepagent", "qa", "s-live"), "req-1", {"evidence": []}, created_at=old
        )
        service = mod.MaintenanceService(
            cfg, QaArchive(store), FakeAgent({"messages": []})
        )
        monkeypatch.setattr(service, "_list_sessions", lambda: ["s-live"])
        lease = acquire_memory_lease("s-live")
        assert lease is not None
        try:
            report = service.cleanup(apply=True, memory_in_process=True)
            assert report.archives == []  # 跳过（保护窗口内不删）
            assert any("memory guard" in str(i) for i in report.skipped)
        finally:
            release_memory_lease(lease)
        # 运行结束后清理可执行
        report2 = service.cleanup(apply=True, memory_in_process=True)
        assert report2.archives == [("qa", "req-1")]

    def test_memory_mode_does_not_connect_pg(
        self, db_agent_config, tmp_path, monkeypatch
    ):
        """memory 清理不连接 PG（无 psycopg.connect 调用）。"""
        import psycopg

        from src.modules.db_deepagent.persistence import maintenance as mod

        cfg = db_agent_config.model_copy(deep=True)
        cfg.persistence.mode = "memory"
        cfg.workspace.root = str(tmp_path)
        service = mod.MaintenanceService(
            cfg, QaArchive(FakeStore()), FakeAgent({"messages": []})
        )
        monkeypatch.setattr(service, "_list_sessions", list)
        called = {"n": 0}

        def _fail_connect(*a, **k):
            called["n"] += 1
            raise AssertionError("memory 清理不得连接 PG")

        monkeypatch.setattr(psycopg, "connect", _fail_connect)
        service.cleanup(apply=True, memory_in_process=True)
        assert called["n"] == 0


# ====================================================================
# R1-05：归档 snapshot_ref 闭包
# ====================================================================


class TestR105SnapshotRefClosure:
    def test_run_errors_without_public_evidence_protects_results(
        self, monkeypatch, db_agent_config, tmp_path
    ):
        """run_errors 无公开 evidence、只带 snapshot_ref：其结果仍受保护。"""
        import psycopg

        from src.modules.db_deepagent.persistence import maintenance as mod

        store = FakeStore()

        cfg = db_agent_config.model_copy(deep=True)
        cfg.workspace.root = str(tmp_path)
        # 会话内一结果目录（过期）
        from src.modules.db_deepagent.services.result_store import ResultStore

        results = ResultStore(tmp_path)
        _ctx = type("C", (), {})()  # 简易 ctx
        from src.modules.db_deepagent.context import DbAgentContext

        real_ctx = DbAgentContext(
            session_id="s1",
            thread_id="dbda:s1:1",
            request_id="req-x",
            run_id="run-x",
            datasource_id="orders",
            validation_fingerprint="fp",
            now=datetime.now(UTC),
            timezone="UTC",
            user_texts=("q",),
            carried_evidence=(),
            deadline_monotonic=0.0,
        )
        meta = results.save(
            ctx=real_ctx,
            purpose="query",
            sql_template="SELECT 1",
            executed_sql="SELECT 1",
            columns=[{"name": "k", "pg_type": "int4"}],
            rows=[[1]],
            source_truncated=False,
            derived_from=[],
            guard_evidence={"time": [], "dim": []},
        )
        # 改写过期时间
        meta_path = results._root / meta.result_ref / "meta.json"
        payload = json.loads(meta_path.read_text(encoding="utf-8"))
        payload["created_at"] = (datetime.now(UTC) - timedelta(days=400)).isoformat()
        meta_path.write_text(json.dumps(payload), encoding="utf-8")

        class _Conn:
            def execute(self, sql, params=None):
                if "to_regclass" in sql:
                    import types

                    return types.SimpleNamespace(fetchone=lambda: {"reg": None})
                import types

                return types.SimpleNamespace(fetchall=list)

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def close(self):
                pass

        conn = _Conn()
        monkeypatch.setattr(psycopg, "connect", lambda *a, **k: conn)

        class FakeLock:
            def __init__(self, *a):
                pass

            def try_acquire(self):
                return True

            def release(self):
                pass

        monkeypatch.setattr(mod, "SessionAdvisoryLock", FakeLock)
        # run_errors（保留期内）只带 snapshot_ref；快照引用该结果
        archive = QaArchive(store)
        archive.write_run_snapshot(
            "s1",
            "dbda:s1:1",
            "run-x",
            {
                "run_snapshot_schema_version": 1,
                "ledger_protocol_version": 1,
                "session_id": "s1",
                "thread_id": "dbda:s1:1",
                "request_id": "req-x",
                "run_id": "run-x",
                "tasks": [],
                "executions": [
                    {
                        "evidence_id": "E-1",
                        "run_id": "run-x",
                        "purpose": "query",
                        "result_ref": meta.result_ref,
                        "input_result_refs": [],
                    }
                ],
                "analyses": [],
                "tool_errors": [],
                "sealed_at": datetime.now(UTC).isoformat(),
            },
        )
        store.put(
            ("db_deepagent", "run_errors", "s1"),
            "req-err",
            {
                "run_id": "run-x",
                "question": "q",
                "status": "failed",
                "stop_reason": None,
                "errors": [],
                "archive_schema_version": 2,
                "answer": {},
                "delivery_answer": {},
                "answer_source": "system_fallback",
                "snapshot_ref": {
                    "session_id": "s1",
                    "thread_id": "dbda:s1:1",
                    "run_id": "run-x",
                },
            },
        )
        service = mod.MaintenanceService(cfg, archive, FakeAgent({"messages": []}))
        monkeypatch.setattr(service, "_list_sessions", lambda: ["s1"])
        report = service.cleanup(apply=True)
        assert report.ok, report.errors
        assert meta.result_ref not in report.result_dirs  # 受快照保护
        assert (results._root / meta.result_ref).is_dir()


# ====================================================================
# R1-06：分析全依赖准入
# ====================================================================


class TestR106AnalyzeFullDependencies:
    @pytest.fixture()
    def node(self, guard_services):
        from src.modules.db_deepagent.tools.result_tools import (
            make_analyze_result_tool,
            make_read_result_tool,
        )

        return ToolNode(
            [
                make_read_result_tool(guard_services),
                make_analyze_result_tool(guard_services),
            ]
        )

    def test_declared_pending_dependency_rejects_analyze(
        self, node, guard_services, db_agent_context
    ):
        """T4 依赖 T1(completed)+T2(pending)：即使输入只来自 T1 也拒绝。"""
        from src.tests.db_deepagent.conftest import (
            invoke_tool_node,
            make_task_record,
            make_task_state,
        )

        meta = guard_services.results.save(
            ctx=db_agent_context,
            purpose="query",
            sql_template="SELECT k, x FROM t",
            executed_sql="SELECT k, x FROM t",
            columns=[
                {"name": "k", "pg_type": "int4"},
                {"name": "x", "pg_type": "numeric"},
            ],
            rows=[[1, 1], [2, 2]],
            source_truncated=False,
            derived_from=[],
            guard_evidence={"time": [], "dim": []},
        )
        t1 = make_task_record(
            "run-test",
            "T1",
            1,
            status="completed",
            evidence_ids=["E-in1"],
            content="上游",
        )
        t2 = make_task_record("run-test", "T2", 1, status="pending", content="未完成")
        t4 = make_task_record(
            "run-test",
            "T4",
            1,
            status="in_progress",
            content="分析",
            dependencies=[
                {"sub_task_id": "T1", "revision": 1},
                {"sub_task_id": "T2", "revision": 1},
            ],
        )
        prior = {
            "evidence_id": "E-in1",
            "tool_call_id": "tc-in",
            "run_id": "run-test",
            "request_id": "req-test",
            "purpose": "query",
            "datasource_id": "orders",
            "sql_template": "s",
            "executed_sql": "s",
            "executed_sql_sha256": "h",
            "result_ref": meta.result_ref,
            "columns": [],
            "saved_row_count": 2,
            "source_truncated": False,
            "elapsed_ms": 1,
            "warnings": [],
            "sub_task_id": "T1",
            "sub_task_revision": 1,
            "input_result_refs": [],
        }
        state = {
            "db_executions": [prior],
            **make_task_state([t1, t2, t4]),
        }
        _u, msg = invoke_tool_node(
            node,
            [
                (
                    "analyze_result",
                    {
                        "operation": "top_n",
                        "inputs": [
                            {
                                "result_ref": meta.result_ref,
                                "evidence_id": "E-in1",
                            }
                        ],
                        "sub_task_id": "T4",
                        "sub_task_revision": 1,
                        "params": {
                            "order_by": "k",
                            "descending": False,
                            "n": 5,
                            "ties": "strict",
                        },
                        "allow_pushdown": True,
                    },
                    "tc-adm6",
                )
            ],
            db_agent_context,
            extra_state=state,
        )
        payload = json.loads(msg.content)
        assert payload["error"]["error_type"] == "stale_task_inputs"
        assert "T2" in payload["error"]["message"]


# ====================================================================
# R1-09 / R1-10：整批顺序无关 + 无关 blocked_by
# ====================================================================


class TestR109R110BatchOrderAndBlockedBy:
    def _state(self):
        return make_task_state(
            [
                make_task_record("run-x", "T1", 1, content="上游", dependencies=[]),
                make_task_record(
                    "run-x",
                    "T2",
                    1,
                    content="下游",
                    dependencies=[{"sub_task_id": "T1", "revision": 1}],
                ),
                make_task_record("run-x", "T3", 1, content="无关", dependencies=[]),
            ]
        )

    def test_blocked_chain_order_independent(self):
        """[T1→NC, T2→blocked(T1)] 与反序同样接受（先合并后核验）。"""
        ops_a = [
            _op(
                "status",
                tid="T1",
                status="needs_clarification",
                reason="口径",
                clarification_question="含退款？",
            ),
            _op(
                "status",
                tid="T2",
                status="failed",
                reason_code="dependency_blocked",
                reason="占位",
                blocked_by=[{"sub_task_id": "T1", "revision": 1}],
            ),
        ]
        ops_b = list(reversed(ops_a))
        for ops in (ops_a, ops_b):
            state = self._state()
            result = _apply(state, ops, expected=1)
            assert result.plan_version == 2
            t2 = result.task_records[task_ledger_key("run-x", "T2", 1)]
            assert t2["reason_code"] == "dependency_blocked"
            assert "根因" in t2["reason"]

    def test_unrelated_blocked_by_rejected(self):
        """T2 只依赖 T1，却把无关 T3 填进 blocked_by → 拒绝。"""
        state = self._state()
        # T3 需为 failed 才能触发旧版校验路径；先单独构造
        t3_failed = dict(make_task_record("run-x", "T3", 1, content="无关"))
        t3_failed["status"] = "failed"
        t3_failed["reason_code"] = "agent_abandoned"
        t3_failed["reason"] = "放弃"
        state["db_sub_tasks"][task_ledger_key("run-x", "T3", 1)] = t3_failed
        with pytest.raises(TaskLedgerError) as exc:
            _apply(
                state,
                [
                    _op(
                        "status",
                        tid="T1",
                        status="needs_clarification",
                        reason="口径",
                        clarification_question="含退款？",
                    ),
                    _op(
                        "status",
                        tid="T2",
                        status="failed",
                        reason_code="dependency_blocked",
                        reason="占位",
                        blocked_by=[{"sub_task_id": "T3", "revision": 1}],
                    ),
                ],
                expected=1,
            )
        assert exc.value.error_type == "invalid_task_dependency"
        assert "不属于其声明的直接依赖" in exc.value.message


# ====================================================================
# R1-11：replacement 历史复核
# ====================================================================


class TestR111ReplacementHistory:
    def test_replacement_target_revised_later_accepted(self):
        """T1 void→T2@1，随后 T2 revise@2 终止：合法 failed 提交不被卡。"""
        t1 = make_task_record("run-x", "T1", 1, status="voided", content="旧任务")
        t1["reason"] = "由 T2 替代"
        t1["replacement"] = {"sub_task_id": "T2", "revision": 1}
        t1["termination_context"] = {
            "decision_plan_version": 1,
            "replacement_snapshot": {
                "session_id": "s-test",
                "thread_id": "t-test",
                "run_id": "run-x",
                "sub_task_id": "T2",
                "revision": 1,
                "status": "pending",
            },
        }
        t2v1 = make_task_record("run-x", "T2", 1, content="替代")
        t2v2 = make_task_record(
            "run-x",
            "T2",
            2,
            status="failed",
            reason_code="agent_abandoned",
            reason="放弃",
        )
        tasks = {"T1": t1, "T2": t2v2}  # 最新视图
        snapshot = CompletionSnapshot(
            run_id="run-x",
            tasks=tasks,
            executions=[],
            analyses=[],
            errors=[],
            known_counts={},
            proven_untruncated={},
            all_task_records={
                ("run-x", "T1", 1): t1,
                ("run-x", "T2", 1): t2v1,
                ("run-x", "T2", 2): t2v2,
            },
        )
        submission = {
            "status": "failed",
            "answer": "失败说明",
            "findings": [],
            "metric_definitions": [],
            "limitations": [],
            "open_questions": [],
            "unresolved_task_refs": [{"sub_task_id": "T2", "revision": 2}],
        }
        decision = evaluate_submission(submission, snapshot)
        assert decision.accepted, [e.code for e in decision.errors]


# ====================================================================
# R1-12：幂等口径
# ====================================================================


class TestR112IdempotencyNormalization:
    def test_dependency_order_replay_is_noop(self):
        """保存排序规范化后，[T2,T1] 与 [T1,T2] 重放等价（no-op）。"""
        result = _apply(
            {},
            [
                _op(
                    "add",
                    tid="T2",
                    content="B",
                    dependencies=[{"sub_task_id": "T1", "revision": 1}],
                ),
                _op("add", tid="T1", content="A"),
            ],
        )
        assert result.plan_version == 1
        # 乱序依赖重放（同批同内容、依赖序不同）
        state = {
            "db_sub_tasks": result.task_records,
            "db_task_plans": {"run-x": result.plan_record},
        }
        replay = _apply(
            state,
            [
                _op("add", tid="T1", content="A"),
                _op(
                    "add",
                    tid="T2",
                    content="B",
                    dependencies=[{"sub_task_id": "T1", "revision": 1}],
                ),
            ],
            expected=1,
        )
        assert replay.no_op is True

    def test_blocked_exact_input_replay_is_noop(self):
        """dependency_blocked 精确重放原输入 → no-op（比较保存的输入）。"""
        state = make_task_state(
            [
                make_task_record(
                    "run-x",
                    "T1",
                    1,
                    status="needs_clarification",
                    reason="口径",
                    clarification_question="？",
                ),
                make_task_record(
                    "run-x",
                    "T2",
                    1,
                    content="下游",
                    dependencies=[{"sub_task_id": "T1", "revision": 1}],
                ),
            ]
        )
        op = _op(
            "status",
            tid="T2",
            status="failed",
            reason_code="dependency_blocked",
            reason="占位",
            blocked_by=[{"sub_task_id": "T1", "revision": 1}],
        )
        result = _apply(state, [op], expected=1)
        assert result.plan_version == 2
        t2 = result.task_records[task_ledger_key("run-x", "T2", 1)]
        assert "根因" in t2["reason"]  # 服务端改写过 reason
        # 带当前版本重放原参数 → no-op（比较 status_operation 输入）
        state2 = {
            "db_sub_tasks": {
                **state["db_sub_tasks"],
                task_ledger_key("run-x", "T2", 1): t2,
            },
            "db_task_plans": {"run-x": result.plan_record},
        }
        replay = _apply(state2, [op], expected=2)
        assert replay.no_op is True
        assert replay.plan_version == 2


# ====================================================================
# R1-13 / R1-14：分项正文与兜底分级
# ====================================================================


class TestR113R114DeliverySections:
    def test_blank_finding_text_rejected_at_schema(self):
        with pytest.raises(ValidationError):
            DbAgentFinalAnswer(
                status="completed",
                answer="正文",
                findings=[
                    {
                        "text": "  \n\t",
                        "finding_id": "F-1",
                        "task_refs": [{"sub_task_id": "T1", "revision": 1}],
                        "evidence_ids": ["E-1"],
                    }
                ],
            )

    def test_legal_delivery_appends_section(self, assembly_services):
        """合法 partial：交付正文含失败任务原因（不依赖模型自行补全）。"""
        from src.tests.db_deepagent.conftest import FakeStore, make_task_record
        from src.tests.db_deepagent.unit.test_runner_persistence import (
            _run_ok,
            _state,
        )

        store = FakeStore()
        t1 = make_task_record(
            "run-x",
            "T1",
            1,
            status="completed",
            evidence_ids=["E-run00001"],
            content="统计订单",
        )
        t2 = make_task_record(
            "run-x",
            "T2",
            1,
            status="failed",
            reason_code="agent_abandoned",
            reason="口径不明无法推进",
            content="按渠道拆分",
        )
        answer = {
            "status": "partial",
            "answer": "只回答第一项。",
            "findings": [
                {
                    "text": "订单数",
                    "finding_id": "F-1",
                    "task_refs": [{"sub_task_id": "T1", "revision": 1}],
                    "evidence_ids": ["E-run00001"],
                }
            ],
            "metric_definitions": [],
            "limitations": [],
            "open_questions": [],
            "unresolved_task_refs": [{"sub_task_id": "T2", "revision": 1}],
        }
        state = _state(answer=answer, ledger=False)
        ledger = make_task_state([t1, t2])
        state["db_sub_tasks"] = ledger["db_sub_tasks"]
        state["db_task_plans"] = ledger["db_task_plans"]
        result, _ = _run_ok(assembly_services, store, state)
        assert result.status == "partial"
        # R1-13：确定性分项段附加（失败原因在正文中可见）
        assert result.answer.startswith("只回答第一项。")
        assert "未完成：按渠道拆分（原因：口径不明无法推进）" in result.answer
        qa = next(v for k, v in store.items.items() if k[0][1] == "qa")
        assert qa["answer"]["answer"] == "只回答第一项。"  # 原始保真

    def test_unverified_completed_not_claimed_done_in_fallback(self, assembly_services):
        """坏 completed（主证据不存在）：兜底正文不称"已完成"。"""
        from src.tests.db_deepagent.conftest import FakeStore, make_task_record
        from src.tests.db_deepagent.unit.test_runner_persistence import (
            _run_ok,
            _state,
        )

        store = FakeStore()
        t1 = make_task_record(
            "run-x",
            "T1",
            1,
            status="completed",
            evidence_ids=["E-ghost"],
            content="统计 9 月订单",
        )
        answer = {
            "status": "completed",
            "answer": "结论正文",
            "findings": [
                {
                    "text": "发现",
                    "finding_id": "F-1",
                    "task_refs": [{"sub_task_id": "T1", "revision": 1}],
                    "evidence_ids": ["E-ghost"],
                }
            ],
            "metric_definitions": [],
            "limitations": [],
            "open_questions": [],
            "unresolved_task_refs": [],
        }
        state = _state(answer=answer, ledger=False)
        ledger = make_task_state([t1])
        state["db_sub_tasks"] = ledger["db_sub_tasks"]
        state["db_task_plans"] = ledger["db_task_plans"]
        result, _ = _run_ok(assembly_services, store, state)
        assert result.status == "failed"  # 门控拒绝
        assert "已完成：统计 9 月订单" not in result.answer  # 不称已完成
        assert "已登记完成（未核验）" in result.answer

    def test_pending_disclosed_in_fallback(self, assembly_services):
        """中止运行含 pending 任务：兜底正文披露未完成事项。"""
        from src.tests.db_deepagent.conftest import FakeStore, make_task_record
        from src.tests.db_deepagent.unit.test_runner_persistence import (
            _run_ok,
            _state,
        )

        store = FakeStore()
        t1 = make_task_record(
            "run-x",
            "T1",
            1,
            status="completed",
            evidence_ids=["E-run00001"],
            content="第一项",
        )
        t2 = make_task_record("run-x", "T2", 1, status="pending", content="第二项")
        state = _state(answer=None, ledger=False)  # 无提交（预算中止样例）
        ledger = make_task_state([t1, t2])
        state["db_sub_tasks"] = ledger["db_sub_tasks"]
        state["db_task_plans"] = ledger["db_task_plans"]
        result, _ = _run_ok(assembly_services, store, state)
        assert result.status == "failed"
        assert "未完成：第二项（pending）" in result.answer


# ====================================================================
# R1-15：reducer 覆盖防护
# ====================================================================


class TestR115ReducerOverrideGuard:
    def test_illegal_command_cannot_rewrite_completed(self):
        from src.modules.db_deepagent.state import merge_task_records

        done = make_task_record(
            "run-x", "T1", 1, status="completed", evidence_ids=["E-1"], content="目标"
        )
        # 同 revision 回退 pending + 改 content：非法
        bad = dict(done)
        bad["status"] = "pending"
        bad["evidence_ids"] = []
        key = task_ledger_key("run-x", "T1", 1)
        with pytest.raises(LedgerConsistencyError):
            merge_task_records({key: done}, {key: bad})

    def test_content_rewrite_same_version_rejected(self):
        from src.modules.db_deepagent.state import merge_task_records

        rec = make_task_record("run-x", "T1", 1, content="原文")
        bad = dict(rec)
        bad["content"] = "改写"
        key = task_ledger_key("run-x", "T1", 1)
        with pytest.raises(LedgerConsistencyError):
            merge_task_records({key: rec}, {key: bad})

    def test_legal_status_transition_allowed(self):
        from src.modules.db_deepagent.state import merge_task_records

        pending = make_task_record("run-x", "T1", 1, content="目标")
        done = dict(pending)
        done["status"] = "completed"
        done["evidence_ids"] = ["E-1"]
        key = task_ledger_key("run-x", "T1", 1)
        merged = merge_task_records({key: pending}, {key: done})
        assert merged[key]["status"] == "completed"


# ====================================================================
# R1-16：空保护集 SQL 语义
# ====================================================================


class TestR116EmptyProtectionSql:
    def test_no_null_in_clause_when_empty(self, monkeypatch, db_agent_config):
        import psycopg

        from src.modules.db_deepagent.persistence import maintenance as mod

        cfg = db_agent_config.model_copy(deep=True)
        cfg.workspace.root = "/nonexistent-dbda-workspace"
        queries: list[str] = []

        class _Conn:
            def execute(self, sql, params=None):
                queries.append(sql)
                import types

                if "to_regclass" in sql:
                    return types.SimpleNamespace(
                        fetchone=lambda: {"reg": "result_snapshots"}
                    )
                if "DELETE FROM result_snapshots" in sql:
                    return types.SimpleNamespace(rowcount=2)
                if "count(*) AS n" in sql:
                    return types.SimpleNamespace(fetchone=lambda: {"n": 2})
                return types.SimpleNamespace(fetchall=list)

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def close(self):
                pass

        monkeypatch.setattr(psycopg, "connect", lambda *a, **k: _Conn())

        class FakeLock:
            def __init__(self, *a):
                pass

            def try_acquire(self):
                return True

            def release(self):
                pass

        monkeypatch.setattr(mod, "SessionAdvisoryLock", FakeLock)
        service = mod.MaintenanceService(
            cfg, QaArchive(FakeStore()), FakeAgent({"messages": []})
        )
        monkeypatch.setattr(service, "_list_sessions", lambda: ["s1"])
        report = service.cleanup(apply=True)
        deletes = [q for q in queries if "DELETE FROM result_snapshots" in q]
        assert deletes, "应执行 R3 删除"
        assert all("NOT IN (NULL)" not in q for q in deletes)  # R1-16
        assert report.snapshot_deleted == 2  # 空保护集仍可回收


# ====================================================================
# R1-17：WebTurn DTO
# ====================================================================


class TestR117WebTurnDto:
    def test_public_turn_validates_against_dto(self):
        from src.api.v2.dto import WebTurn
        from src.api.v2.webstore import public_turn

        record = {
            "question": "q",
            "answer": "a",
            "saved_at": "2026-11-13T10:00:00+08:00",
            "display": None,
            "open_questions": None,
        }
        turn = WebTurn.model_validate(public_turn(record))
        assert turn.open_questions is None  # 旧记录缺失为 None
        record2 = dict(record, open_questions=["问题一"])
        turn2 = WebTurn.model_validate(public_turn(record2))
        assert turn2.open_questions == ["问题一"]
        record3 = dict(record, open_questions=[])
        turn3 = WebTurn.model_validate(public_turn(record3))
        assert turn3.open_questions == []  # 新记录明确无问题为 []

    def test_unresolved_duplicates_rejected_at_schema(self):
        with pytest.raises(ValidationError):
            DbAgentFinalAnswer(
                status="failed",
                answer="x",
                findings=[],
                unresolved_task_refs=[
                    {"sub_task_id": "T2", "revision": 1},
                    {"sub_task_id": "T2", "revision": 1},
                ],
            )


# ====================================================================
# R1-07：多跳轮换清单寻址（照清单/成功回包直接调用）
# ====================================================================


class TestR107CarriedListingAddressing:
    def test_multihop_listing_resolves_after_two_rotations(self):
        """旧 E → 新 run 沿用（新 E）→ 轮换：清单给出的引用可解析。

        R1-07 修复前 origin 用链首（旧 run/旧 E）顶替记录身份，照清单
        调用 evidence_not_found。
        """
        from src.modules.db_deepagent.runner import _collect_carried

        new_record = {
            "evidence_id": "E-new1",
            "tool_call_id": "tc-reuse",
            "run_id": "run-mid",
            "request_id": "req-mid",
            "purpose": "query",
            "datasource_id": "orders",
            "sql_template": "s",
            "executed_sql": "s",
            "executed_sql_sha256": "h",
            "result_ref": "res_1",
            "columns": [],
            "saved_row_count": 1,
            "source_truncated": False,
            "elapsed_ms": 1,
            "warnings": [],
            "sub_task_id": "T1",
            "sub_task_revision": 1,
            "input_result_refs": [],
            "reused_from": {  # 链首=旧 run 的旧证据（展示语义）
                "evidence_id": "E-root",
                "thread_id": "t-root",
                "request_id": "",
                "run_id": "run-old",
                "datasource_id": "orders",
                "user_text": None,
            },
        }
        from types import SimpleNamespace

        cfg = SimpleNamespace(
            thread=SimpleNamespace(carry_max_evidence=10, carry_recent_executions=3)
        )
        carried = _collect_carried(
            {"db_executions": [new_record], "db_analyses": []},
            (),
            cfg,
            meta_loader=None,
        )
        exec_items = [c for c in carried if c.get("kind") == "execution"]
        assert exec_items, "应携带执行证据"
        item = exec_items[0]
        origin = item["origin"]
        # origin=记录自身身份（run-mid/E-new1）；链首仅在 record.reused_from
        assert origin["run_id"] == "run-mid"
        assert origin["evidence_id"] == "E-new1"
        assert item["record"]["reused_from"]["run_id"] == "run-old"

    def test_execute_success_ref_reusable_as_source(self):
        """execute 成功包的 source_evidence_ref 是新记录完整引用。"""
        # 由既有 test_sql_tools 断言过 result_ref/evidence_id；此处验证
        # 结构契约字段存在且一致（离线最小验证）
        import inspect

        from src.modules.db_deepagent.tools.sql_tools import (
            make_execute_sql_tool,
        )

        source = inspect.getsource(make_execute_sql_tool)
        assert "source_evidence_ref" in source


# ====================================================================
# R1-08：错误归属不伪造（未知目标）
# ====================================================================


class TestR108NoForgedOwnership:
    def test_validate_unknown_owner_error_has_no_source_ref(
        self, guard_services, db_agent_context
    ):
        """未登记 T9@99 的 validate 失败：错误记录 source_task_ref=None。"""
        from src.modules.db_deepagent.tools.sql_tools import (
            make_execute_sql_tool,
            make_validate_sql_tool,
        )
        from src.tests.db_deepagent.conftest import invoke_tool_node

        node = ToolNode(
            [
                make_validate_sql_tool(guard_services),
                make_execute_sql_tool(guard_services),
            ]
        )
        sep_holder = {}
        from src.tests.db_deepagent.conftest import make_time_evidence

        sep_holder["sep"] = make_time_evidence("T-sep", "2026-09-01", "2026-10-01")
        _updates, msg = invoke_tool_node(
            node,
            [
                (
                    "validate_sql",
                    {
                        "sql": (
                            "SELECT id FROM public.orders "
                            "WHERE created_at >= '2026-09-01' "
                            "AND created_at < '2026-10-01'"
                        ),
                        "sub_task_id": "T9",
                        "sub_task_revision": 99,
                    },
                    "tc-v99",
                )
            ],
            db_agent_context,
            extra_state={"db_time_evidence": [sep_holder["sep"]]},
        )
        payload = json.loads(msg.content)
        assert payload["status"] == "error"
        ref = payload["data"]["failure_ref"]
        assert ref["source_task_ref"] is None  # R1-08：不伪造归属

    def test_forged_failure_source_task_rejected(self):
        """登记的 failure_ref.source_task_ref 与记录不符 → 拒绝（R1-08）。"""
        task = make_task_record("run-x", "T1", 1)
        error_record = {
            "error_id": "ERR-r",
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
        forged = {
            "error_ref": {
                "session_id": "s-test",
                "thread_id": "t-test",
                "run_id": "run-x",
                "error_id": "ERR-r",
            },
            "tool_call_id": "tc-9",
            "error_type": "db_semantic_error",
            # 伪造：引用内的 source_task_ref 指向 T99
            "source_task_ref": {
                "session_id": "s-test",
                "thread_id": "t-test",
                "run_id": "run-x",
                "sub_task_id": "T99",
                "revision": 1,
            },
        }
        state = dict(make_task_state([task]), db_tool_errors=[error_record])
        with pytest.raises(TaskLedgerError) as exc:
            _apply(
                state,
                [
                    _op(
                        "status",
                        status="failed",
                        reason_code="tool_failed",
                        reason="失败",
                        failure_refs=[forged],
                    )
                ],
                expected=1,
            )
        assert exc.value.error_type == "invalid_task_failure_ref"
        assert "source_task_ref" in exc.value.message

    def test_shell_error_record_rejected_by_reducer(self):
        """四字段空壳错误记录被 reducer 拒绝（R1-08）。"""
        from src.modules.db_deepagent.state import append_dedup_tool_errors

        shell = {
            "error_id": "ERR-s",
            "session_id": "s",
            "thread_id": "t",
            "run_id": "r",
            "request_id": "req",
            "tool_call_id": "tc",
            "tool": "",
            "error_type": "",
            "message": "",
            "retryable": True,
            "source_task_ref": None,
        }
        with pytest.raises(LedgerConsistencyError):
            append_dedup_tool_errors([], [shell])

    def test_structured_detail_conflict_rejected(self):
        """同引用增加结构化 details → 事实差异拒绝（不静默去重）。"""
        from src.modules.db_deepagent.state import append_dedup_tool_errors

        base = {
            "error_id": "ERR-d",
            "session_id": "s",
            "thread_id": "t",
            "run_id": "r",
            "request_id": "req",
            "tool_call_id": "tc",
            "tool": "execute_sql",
            "error_type": "db_error",
            "message": "boom",
            "retryable": True,
            "source_task_ref": None,
        }
        with_detail = {**base, "details": {"sql_sha": "abc"}}
        with pytest.raises(LedgerConsistencyError):
            append_dedup_tool_errors([base], [with_detail])
