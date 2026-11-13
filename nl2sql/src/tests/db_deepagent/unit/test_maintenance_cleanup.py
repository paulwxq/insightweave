"""清理判定单元测试（docs/v4/5 §13.5 + doc 7 §2、§18.3 清理粒度行）

FakeAgent/FakeStore/FakeConn：不连真库。覆盖 LIKE 转义与归属复核、
当前线程／待恢复保护、三来源 effective_at（含轨迹落后于 checkpoint）、
get_state 异常跳过、会话名相似与嵌套不互扰。
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from src.modules.db_deepagent.persistence.archive import QaArchive
from src.modules.db_deepagent.persistence.maintenance import MaintenanceService
from src.tests.db_deepagent.conftest import FakeAgent, FakeStore

pytestmark = pytest.mark.unit

NOW = datetime(2026, 10, 15, tzinfo=timezone.utc)
OLD = NOW - timedelta(days=90)
RECENT = NOW - timedelta(days=1)


class FakeConn:
    """记录 SQL；模拟 checkpoints 查询与 result_snapshots 维护 SQL。"""

    def __init__(
        self,
        thread_rows=None,
        surviving_rows=None,
        *,
        snapshot_table=True,
        snapshot_rows=0,
        snapshot_sessions=None,
    ):
        self.queries: list[tuple[str, tuple]] = []
        self.thread_rows = thread_rows or []
        self.surviving_rows = surviving_rows or []
        self.snapshot_table = snapshot_table
        self.snapshot_rows = snapshot_rows
        self.snapshot_sessions = snapshot_sessions or []
        self.snapshot_delete_rowcount = snapshot_rows
        self.snapshot_deletes: list[tuple] = []

    def execute(self, sql, params=None):
        self.queries.append((sql, params))
        if "to_regclass" in sql:
            return SimpleNamespace(
                fetchone=lambda: {
                    "reg": "result_snapshots" if self.snapshot_table else None
                }
            )
        if "DELETE FROM result_snapshots" in sql:
            self.snapshot_deletes.append(params or ())
            return SimpleNamespace(rowcount=self.snapshot_delete_rowcount)
        if "DISTINCT session_id FROM result_snapshots" in sql:
            return SimpleNamespace(
                fetchall=lambda: [{"session_id": s} for s in self.snapshot_sessions]
            )
        if "count(*) AS n FROM result_snapshots" in sql:
            return SimpleNamespace(fetchone=lambda: {"n": self.snapshot_rows})
        if "DISTINCT thread_id" in sql:
            if "LIKE" in sql:  # 会话内前缀枚举
                return SimpleNamespace(
                    fetchall=lambda: [{"thread_id": t} for t in self.thread_rows]
                )
            return SimpleNamespace(  # 清理后幸存线程（仅会话发现）
                fetchall=lambda: [{"thread_id": t} for t in self.surviving_rows]
            )
        if "checkpoint->>'ts'" in sql and "max(" in sql:

            def fetchone():
                return {"max_ts": None}

            return SimpleNamespace(fetchone=fetchone)
        return SimpleNamespace(fetchall=lambda: [])


@pytest.fixture()
def store():
    return FakeStore()


@pytest.fixture()
def svc(store, db_agent_config):
    agent = FakeAgent({"messages": []})
    agent.get_state_snapshot = SimpleNamespace(values={}, next=(), created_at=None)
    return MaintenanceService(db_agent_config, QaArchive(store), agent)


def _thread(
    store, session_id, thread_id, *, last_active=None, retired=False, retired_at=None
):
    store.put(
        ("db_deepagent", "threads", session_id),
        thread_id,
        {
            "thread_id": thread_id,
            "session_id": session_id,
            "seq": 1,
            "last_active_at": last_active,
            "retired": retired,
            "retired_at": retired_at,
        },
    )


def _session(store, session_id, current, seq=1):
    store.put(
        ("db_deepagent", "sessions"),
        session_id,
        {"session_id": session_id, "current_thread_id": current, "thread_seq": seq},
    )


class TestCandidateDiscovery:
    def test_like_escape_and_ownership(self, svc, store):
        conn = FakeConn(thread_rows=["dbda:s_1:1", "dbda:sA1:1", "dbda:s_1:x"])
        found = svc._checkpoint_thread_ids(conn, "s_1")
        # LIKE 转义：s_1 不匹配 sA1；归属解析：非正整数序号剔除
        assert found == {"dbda:s_1:1"}
        sql, params = conn.queries[0]
        assert "ESCAPE" in sql and params[0] == "dbda:s\\_1:%"

    def test_seq_derivation_plus_enumeration(self, svc, store):
        _session(store, "s1", "dbda:s1:3", seq=3)
        conn = FakeConn(thread_rows=["dbda:s1:5"])
        found = svc._candidates(conn, "s1")
        assert found == [
            "dbda:s1:1",
            "dbda:s1:2",
            "dbda:s1:3",
            "dbda:s1:5",
        ]


class TestResultsAndArchiveCleanup:
    """§13.5 第 4/5 点：结果目录（引用保护）与 qa/run_errors 保留期清理。"""

    def _install(
        self,
        monkeypatch,
        db_agent_config,
        store,
        *,
        surviving=(),
        sessions=(),
        lock_ok=True,
        agent=None,
        snapshot_table=True,
        snapshot_rows=0,
    ):
        import psycopg

        from src.modules.db_deepagent.persistence import maintenance as maintenance_mod

        captured: dict = {}

        class _Conn(FakeConn):
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def close(self):
                pass

        # thread_rows 服务会话内 LIKE 枚举；surviving_rows 服务全表 DISTINCT
        # （checkpoint 会话发现）——两分支同源即可
        conn = _Conn(
            thread_rows=list(surviving),
            surviving_rows=list(surviving),
            snapshot_table=snapshot_table,
            snapshot_rows=snapshot_rows,
            snapshot_sessions=list(sessions),
        )
        captured["conn"] = conn

        def fake_connect(conninfo, **kwargs):
            captured.update(kwargs)
            return conn

        monkeypatch.setattr(psycopg, "connect", fake_connect)

        class FakeLock:
            def __init__(self, conninfo, session_id):
                pass

            def try_acquire(self):
                return lock_ok

            def release(self):
                pass

        monkeypatch.setattr(maintenance_mod, "SessionAdvisoryLock", FakeLock)
        cfg = db_agent_config.model_copy(deep=True)
        service = maintenance_mod.MaintenanceService(
            cfg, QaArchive(store), agent or FakeAgent({"messages": []})
        )
        monkeypatch.setattr(service, "_list_sessions", lambda: list(sessions))
        return service, captured

    def _save_result(self, cfg, *, thread_id, session_id="s1", created_at=None):
        import json as _json

        from src.modules.db_deepagent.context import DbAgentContext
        from src.modules.db_deepagent.services.result_store import ResultStore

        ctx = DbAgentContext(
            session_id=session_id,
            thread_id=thread_id,
            request_id="req-x",
            run_id="run-x",
            datasource_id="orders",
            validation_fingerprint="fp",
            now=datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc),
            timezone="UTC",
            user_texts=("q",),
            carried_evidence=(),
            deadline_monotonic=0.0,
        )
        results = ResultStore(_workspace_root(cfg))
        meta = results.save(
            ctx=ctx,
            purpose="query",
            sql_template="SELECT 1",
            executed_sql="SELECT 1",
            columns=[{"name": "k", "pg_type": "int4"}],
            rows=[[1]],
            source_truncated=False,
            derived_from=[],
            guard_evidence={"time": [], "dim": []},
        )
        if created_at is not None:  # 改写 meta 的生成时间（保留期判定字段）
            meta_path = results._root / meta.result_ref / "meta.json"
            payload = _json.loads(meta_path.read_text(encoding="utf-8"))
            payload["created_at"] = created_at
            meta_path.write_text(
                _json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
            )
        return meta

    def test_results_cleanup_per_session_with_closure(
        self, monkeypatch, db_agent_config, store, tmp_path
    ):
        """doc 6.3 §6.5/§8.2：R2 按会话持锁清理；保留闭包以仍在保留期的
        QA/run_errors 与携带引用为根——过期目录不再全局无保护删除。"""
        from src.modules.db_deepagent.services.result_store import ResultStore

        cfg = db_agent_config.model_copy(deep=True)
        cfg.workspace.root = str(tmp_path)
        old = (datetime.now(timezone.utc) - timedelta(days=400)).isoformat()
        r_thread = self._save_result(cfg, thread_id="dbda:s1:1", created_at=old)
        r_qa = self._save_result(cfg, thread_id="dbda:s1:2", created_at=old)
        r_free = self._save_result(cfg, thread_id="dbda:s1:3", created_at=old)
        r_recent = self._save_result(cfg, thread_id="dbda:s1:4")
        store.put(
            ("db_deepagent", "qa", "s1"),
            "req-qa",
            {"evidence": [{"result_ref": r_qa.result_ref}]},
        )
        service, _captured = self._install(
            monkeypatch,
            cfg,
            store,
            surviving=["dbda:s1:1"],  # 线程存活不再是结果保护口径
            sessions=["s1"],
        )
        report = service.cleanup(apply=True)
        assert report.ok, report.errors
        # 过期且不在保留闭包内 → 删除；QA 引用的 r_qa 受保护
        assert set(report.result_dirs) == {r_thread.result_ref, r_free.result_ref}
        results = ResultStore(tmp_path)
        for ref in (r_thread.result_ref, r_free.result_ref):
            assert not (results._root / ref).exists(), ref
        assert (results._root / r_qa.result_ref).is_dir(), "QA 引用保护"
        assert (results._root / r_recent.result_ref).is_dir()

    def test_r2_future_created_at_kept_conservatively(
        self, monkeypatch, db_agent_config, store, tmp_path
    ):
        """doc 6.3 §6.5：meta.created_at 在未来/不可解析 → 年龄不可判，
        保守保留（不按目录 mtime 强删；坏元数据只报告）。"""
        from src.modules.db_deepagent.services.result_store import ResultStore

        cfg = db_agent_config.model_copy(deep=True)
        cfg.workspace.root = str(tmp_path)
        future = (datetime.now(timezone.utc) + timedelta(days=3650)).isoformat()
        result = self._save_result(cfg, thread_id="dbda:s1:1", created_at=future)
        result_dir = ResultStore(tmp_path)._root / result.result_ref
        old = datetime.now(timezone.utc) - timedelta(days=400)
        os.utime(result_dir, (old.timestamp(), old.timestamp()))

        service, _captured = self._install(monkeypatch, cfg, store, sessions=["s1"])
        report = service.cleanup(apply=True)

        assert report.ok, report.errors
        assert result.result_ref not in report.result_dirs
        assert result_dir.exists()

    def test_r2_proceeds_and_r3_skips_when_snapshot_table_missing(
        self, monkeypatch, db_agent_config, store, tmp_path
    ):
        """快照表不存在：R2（会话结果目录）不依赖该表、照常清理；R3 跳过。"""
        from src.modules.db_deepagent.services.result_store import ResultStore

        cfg = db_agent_config.model_copy(deep=True)
        cfg.workspace.root = str(tmp_path)
        old = (datetime.now(timezone.utc) - timedelta(days=400)).isoformat()
        r_old = self._save_result(cfg, thread_id="dbda:s1:1", created_at=old)
        service, captured = self._install(
            monkeypatch, cfg, store, snapshot_table=False, sessions=["s1"]
        )
        report = service.cleanup(apply=True)
        assert report.ok
        assert report.result_dirs == [r_old.result_ref]
        assert report.snapshot_deleted == 0
        assert captured["conn"].snapshot_deletes == []
        results = ResultStore(tmp_path)
        assert not (results._root / r_old.result_ref).is_dir()

    def test_lock_failure_skips_session_archives(
        self, monkeypatch, db_agent_config, store
    ):
        """取锁失败的会话：归档与结果目录一并跳过（三段同锁）。"""
        old = datetime.now(timezone.utc) - timedelta(days=400)
        store.put(
            ("db_deepagent", "qa", "s1"), "req-old", {"answer": "x"}, created_at=old
        )
        cfg = db_agent_config.model_copy(deep=True)
        cfg.workspace.root = "/nonexistent-dbda-workspace"
        service, _captured = self._install(
            monkeypatch, cfg, store, sessions=["s1"], lock_ok=False
        )
        report = service.cleanup(apply=True)
        assert report.ok
        assert report.archives == []
        assert any("跳过该会话全部清理" in str(i) for i in report.skipped)
        assert store.get(("db_deepagent", "qa", "s1"), "req-old") is not None

    def test_orphan_session_also_takes_lock(self, monkeypatch, db_agent_config, store):
        """孤儿会话（无 sessions 记录，仅归档/结果发现）也统一取锁：
        取锁失败同样跳过——sessions 缺失不代表没有运行（首请求未到 P3）。"""
        old = datetime.now(timezone.utc) - timedelta(days=400)
        store.put(
            ("db_deepagent", "qa", "s-orphan"),
            "req-old",
            {"answer": "x"},
            created_at=old,
        )
        cfg = db_agent_config.model_copy(deep=True)
        cfg.workspace.root = "/nonexistent-dbda-workspace"
        service, _captured = self._install(
            monkeypatch, cfg, store, sessions=[], lock_ok=False
        )
        report = service.cleanup(apply=True)
        assert report.ok
        assert report.archives == []
        assert any("s-orphan" in str(i) for i in report.skipped)
        assert store.get(("db_deepagent", "qa", "s-orphan"), "req-old") is not None

    def test_checkpoint_only_session_discovered(
        self, monkeypatch, db_agent_config, store
    ):
        """只有 checkpoint 的会话（首请求无结果＋P3 写入失败）也能被发现：
        从 checkpoint 线程标识补充会话，走同一套取锁流程。"""
        cfg = db_agent_config.model_copy(deep=True)
        cfg.workspace.root = "/nonexistent-dbda-workspace"
        # surviving_rows 即“全表 DISTINCT thread_id”（FakeConn 的无 LIKE 分支）
        service, _captured = self._install(
            monkeypatch,
            cfg,
            store,
            sessions=[],
            surviving=["dbda:s-ckpt-only:1"],
            lock_ok=False,  # 取锁失败 → 该会话全部清理跳过（证明其被发现）
        )
        report = service.cleanup(apply=False)
        assert report.ok
        assert any("s-ckpt-only" in str(i) for i in report.skipped), report.skipped

    def test_r2_protects_qa_analysis_input_refs(
        self, monkeypatch, db_agent_config, store, tmp_path
    ):
        """doc 6.3 §8.2：闭包保护分析/占位符输入——结构化与字符串条目都
        提取（refs_of_record），受保护输入不因目录过期被删。"""
        from src.modules.db_deepagent.services.result_store import ResultStore

        cfg = db_agent_config.model_copy(deep=True)
        cfg.workspace.root = str(tmp_path)
        old = (datetime.now(timezone.utc) - timedelta(days=400)).isoformat()
        r_input = self._save_result(cfg, thread_id="dbda:s1:1", created_at=old)
        r_free = self._save_result(cfg, thread_id="dbda:s1:2", created_at=old)
        store.put(
            ("db_deepagent", "qa", "s1"),
            "req-qa",
            {
                "evidence": [
                    {
                        "evidence_id": "A-1",
                        "output_result_ref": "res_" + "6" * 32,
                        "input_result_refs": [
                            {
                                "result_ref": r_input.result_ref,
                                "evidence_ref": {},
                                "source_task_ref": {},
                            }
                        ],
                    }
                ]
            },
        )
        service, _captured = self._install(monkeypatch, cfg, store, sessions=["s1"])
        report = service.cleanup(apply=True)
        assert report.ok, report.errors
        assert report.result_dirs == [r_free.result_ref]
        results = ResultStore(tmp_path)
        assert (results._root / r_input.result_ref).is_dir(), "分析输入受闭包保护"
        assert not (results._root / r_free.result_ref).exists()

    def test_session_archives_purged_under_lock(
        self, monkeypatch, db_agent_config, store, tmp_path
    ):
        """正常取锁会话：同锁内归档按保留期清理。"""
        old = datetime.now(timezone.utc) - timedelta(days=400)
        recent = datetime.now(timezone.utc)
        store.put(
            ("db_deepagent", "qa", "s1"), "req-old", {"answer": "x"}, created_at=old
        )
        store.put(
            ("db_deepagent", "qa", "s1"), "req-new", {"answer": "y"}, created_at=recent
        )
        store.put(
            ("db_deepagent", "run_errors", "s1"),
            "req-err-old",
            {"errors": []},
            created_at=old,
        )
        cfg = db_agent_config.model_copy(deep=True)
        cfg.workspace.root = str(tmp_path)
        service, _captured = self._install(monkeypatch, cfg, store, sessions=["s1"])
        report = service.cleanup(apply=True)
        assert report.ok
        assert sorted(report.archives) == [
            ("qa", "req-old"),
            ("run_errors", "req-err-old"),
        ]
        assert store.get(("db_deepagent", "qa", "s1"), "req-new") is not None
        assert store.get(("db_deepagent", "qa", "s1"), "req-old") is None

    def test_archives_purged_by_retention(self, monkeypatch, db_agent_config, store):
        old = datetime.now(timezone.utc) - timedelta(days=400)
        recent = datetime.now(timezone.utc)
        store.put(
            ("db_deepagent", "qa", "s1"), "req-old", {"answer": "x"}, created_at=old
        )
        store.put(
            ("db_deepagent", "qa", "s1"), "req-new", {"answer": "y"}, created_at=recent
        )
        store.put(
            ("db_deepagent", "run_errors", "s1"),
            "req-err-old",
            {"errors": []},
            created_at=old,
        )
        cfg = db_agent_config.model_copy(deep=True)
        service, _captured = self._install(monkeypatch, cfg, store)
        before = datetime.now(timezone.utc) - timedelta(
            days=cfg.thread.archive_retention_days
        )
        expected = [("qa", "req-old"), ("run_errors", "req-err-old")]
        targets = service._archive.purge_archives(
            session_id="s1", before=before, apply=False
        )
        assert sorted(targets) == expected
        deleted = service._archive.purge_archives(
            session_id="s1", before=before, apply=True
        )
        assert sorted(deleted) == expected
        assert store.get(("db_deepagent", "qa", "s1"), "req-new") is not None
        assert store.get(("db_deepagent", "qa", "s1"), "req-old") is None

    def test_results_cleanup_dry_run_lists_only(
        self, monkeypatch, db_agent_config, store, tmp_path
    ):
        from src.modules.db_deepagent.services.result_store import ResultStore

        cfg = db_agent_config.model_copy(deep=True)
        cfg.workspace.root = str(tmp_path)
        old = (datetime.now(timezone.utc) - timedelta(days=400)).isoformat()
        r_free = self._save_result(cfg, thread_id="dbda:s1:3", created_at=old)
        service, _captured = self._install(monkeypatch, cfg, store)
        report = service.cleanup(apply=False)
        assert report.result_dirs == [r_free.result_ref + " (dry-run)"]
        results = ResultStore(tmp_path)
        assert (results._root / r_free.result_ref).is_dir()  # dry-run 不删

    def test_r3_snapshot_cleanup_dry_run_and_apply(
        self, monkeypatch, db_agent_config, store
    ):
        """R3 按会话分组、同锁删除（排除受保护 refs）。"""
        cfg = db_agent_config.model_copy(deep=True)
        cfg.workspace.root = "/nonexistent-dbda-workspace"
        service, captured = self._install(
            monkeypatch, cfg, store, snapshot_rows=2, sessions=["s1"]
        )
        dry = service.cleanup(apply=False)
        assert dry.snapshot_deleted == 2
        assert captured["conn"].snapshot_deletes == []
        applied = service.cleanup(apply=True)
        assert applied.snapshot_deleted == 2
        assert len(captured["conn"].snapshot_deletes) == 1
        params = captured["conn"].snapshot_deletes[0]
        assert params[0] == "s1"  # 会话过滤参数在前

    def test_r3_skips_when_snapshot_table_missing(
        self, monkeypatch, db_agent_config, store
    ):
        cfg = db_agent_config.model_copy(deep=True)
        cfg.workspace.root = "/nonexistent-dbda-workspace"
        service, captured = self._install(
            monkeypatch,
            cfg,
            store,
            snapshot_table=False,
            snapshot_rows=2,
            sessions=["s1"],
        )
        report = service.cleanup(apply=True)
        assert report.snapshot_deleted == 0
        assert captured["conn"].snapshot_deletes == []

    def test_before_naive_does_not_crash_and_future_clamped(
        self, monkeypatch, db_agent_config, store
    ):
        """朴素 --before 不再与 UTC 比较报错；未来日期按当前时刻收紧。"""
        cfg = db_agent_config.model_copy(deep=True)
        cfg.workspace.root = "/nonexistent-dbda-workspace"
        service, _captured = self._install(monkeypatch, cfg, store)
        naive = datetime(2026, 10, 1)  # 无时区
        report = service.cleanup(apply=False, before=naive)
        assert report.ok
        future = datetime.now(timezone.utc) + timedelta(days=3650)
        report2 = service.cleanup(apply=False, before=future)
        assert report2.ok

    def test_connection_kwargs_full_spec(self, monkeypatch, db_agent_config, store):
        """§13.5 清理连接规范：autocommit＋prepare_threshold=0＋dict_row。"""
        cfg = db_agent_config.model_copy(deep=True)
        cfg.workspace.root = "/nonexistent-dbda-workspace"
        service, captured = self._install(monkeypatch, cfg, store)
        service.cleanup(apply=False)
        assert captured.get("autocommit") is True
        assert captured.get("prepare_threshold") == 0
        assert captured.get("row_factory") is not None  # dict_row


def _workspace_root(cfg):
    from pathlib import Path

    return Path(cfg.workspace.root)


class TestJudgement:
    def _judge(self, svc, store, session_id="s1", *, current=None, **kwargs):
        conn = FakeConn()
        _session(store, session_id, current) if current else None
        return svc.judge(
            conn,
            session_id,
            kwargs.pop("thread_id"),
            cutoff=NOW - timedelta(days=30),
            current_thread=current,
            **kwargs,
        )

    def test_current_thread_protected(self, svc, store):
        verdict = self._judge(svc, store, thread_id="dbda:s1:1", current="dbda:s1:1")
        assert verdict.action == "keep" and verdict.reason == "keep-current"

    def test_expired_inactive_deleted(self, svc, store):
        _thread(store, "s1", "dbda:s1:1", last_active=OLD.isoformat())
        verdict = self._judge(svc, store, thread_id="dbda:s1:1")
        assert verdict.action == "delete" and verdict.reason == "expired-inactive"

    def test_recent_activity_protected_even_if_retired(self, svc, store):
        # 两个月前退休、昨天又使用：max(last_active, retired_at) 保护
        _thread(
            store,
            "s1",
            "dbda:s1:1",
            last_active=RECENT.isoformat(),
            retired=True,
            retired_at=(NOW - timedelta(days=60)).isoformat(),
        )
        verdict = self._judge(svc, store, thread_id="dbda:s1:1")
        assert verdict.action == "keep"

    def test_checkpoint_time_beats_stale_track(self, svc, store, db_agent_config):
        # 轨迹落后于 checkpoint（P3 失败路径）：快照 created_at 新 → 不清理
        agent = FakeAgent({"messages": [object()]})
        agent.get_state = lambda config: SimpleNamespace(
            values={"messages": [object()]}, next=(), created_at=RECENT
        )
        service = MaintenanceService(db_agent_config, QaArchive(store), agent)
        _thread(store, "s1", "dbda:s1:1", last_active=OLD.isoformat())
        verdict = service.judge(
            FakeConn(),
            "s1",
            "dbda:s1:1",
            cutoff=NOW - timedelta(days=30),
            current_thread=None,
        )
        assert verdict.action == "keep" and verdict.reason == "keep-recent"

    def test_state_error_skips(self, svc, store, db_agent_config):
        agent = FakeAgent({"messages": []})
        agent.get_state_side_effect = RuntimeError("state down")
        service = MaintenanceService(db_agent_config, QaArchive(store), agent)
        verdict = service.judge(
            FakeConn(),
            "s1",
            "dbda:s1:1",
            cutoff=NOW - timedelta(days=30),
            current_thread=None,
        )
        assert verdict.action == "skip" and verdict.reason == "skip-state-error"

    def test_pending_resume_protected(self, svc, store, db_agent_config):
        agent = FakeAgent({"messages": []})
        agent.get_state = lambda config: SimpleNamespace(
            values={"messages": []}, next=[object()], created_at=OLD
        )
        service = MaintenanceService(db_agent_config, QaArchive(store), agent)
        verdict = service.judge(
            FakeConn(),
            "s1",
            "dbda:s1:1",
            cutoff=NOW - timedelta(days=30),
            current_thread=None,
        )
        assert verdict.action == "keep" and verdict.reason == "keep-pending"

    def test_undecidable_skipped(self, svc, store):
        _thread(store, "s1", "dbda:s1:1", last_active=None)
        verdict = self._judge(svc, store, thread_id="dbda:s1:1")
        assert verdict.action == "skip" and verdict.reason == "skip-undecidable"


# ============================================================================
# Web 命名空间清理（doc 2 §4.4）：dry-run 只读；apply 删除失败不按已删除计
# ============================================================================


def _web_registration(store, sid):
    from src.api.v2.webstore import WebStore

    webstore = WebStore(store)
    record = webstore.new_registration(
        session_id=sid,
        source_id="db_src_1",  # doc 5.1 §1 短 ID：归属校验要求合法 db_src_N
        source_fingerprint="fp",
        knowledge_snapshot={},
        creation_id="create_" + "a" * 27,
    )
    webstore.put_registration(sid, record)
    return webstore


def _web_turn(store, sid, key, *, saved_at, question="问", answer="答"):
    from src.api.v2.webstore import web_turns_ns

    store.put(
        web_turns_ns(sid),
        key,
        {
            "question": question,
            "answer": answer,
            "request_id": key,
            "run_id": "run_1",
            "status": "completed",
            "saved_at": saved_at,
        },
    )


OLD_KEY = "req_" + "1" * 28
NEW_KEY = "req_" + "2" * 28


class TestWebCleanup:
    SID = "s_" + "a" * 32

    def _run(self, svc, webstore, *, apply):
        from src.modules.db_deepagent.persistence.maintenance import CleanupReport

        report = CleanupReport(dry_run=not apply)
        svc._web_cleanup(
            webstore,
            self.SID,
            conn=FakeConn(),
            result_refs=[],
            archive_before=NOW - timedelta(days=30),
            apply=apply,
            report=report,
        )
        return report

    def _seed(self, store):
        webstore = _web_registration(store, self.SID)
        _web_turn(
            store, self.SID, OLD_KEY, saved_at=(NOW - timedelta(days=90)).isoformat()
        )
        _web_turn(store, self.SID, NEW_KEY, saved_at=NOW.isoformat())
        return webstore

    def test_dry_run_reports_only_no_write(self, svc, store):
        webstore = self._seed(store)
        report = self._run(svc, webstore, apply=False)
        assert report.web_turns_deleted == [f"{OLD_KEY} (dry-run)"]
        # 只读：轮次未删、登记未被校正/退避改写
        assert webstore.read_turn(self.SID, OLD_KEY) is not None
        rec = webstore.get_registration(self.SID)
        assert rec["turn_count"] == 0
        assert rec["metadata_reconciled_at"] is None
        assert rec["metadata_reconcile_retry_after"] is None
        assert report.web_reconciled == []

    def test_dry_run_bad_record_no_backoff_write(self, svc, store):
        webstore = _web_registration(store, self.SID)
        _web_turn(store, self.SID, OLD_KEY, saved_at=NOW.isoformat(), question=" ")
        report = self._run(svc, webstore, apply=False)
        assert any("损坏" in item["reason"] for item in report.web_reports)
        rec = webstore.get_registration(self.SID)
        assert rec["metadata_reconcile_retry_after"] is None  # dry-run 不写退避

    def test_apply_deletes_and_reconciles_survivors(self, svc, store):
        webstore = self._seed(store)
        report = self._run(svc, webstore, apply=True)
        assert report.web_turns_deleted == [OLD_KEY]
        assert webstore.read_turn(self.SID, OLD_KEY) is None
        rec = webstore.get_registration(self.SID)
        assert rec["turn_count"] == 1  # 幸存轮计数
        assert rec["metadata_reconciled_at"] is not None
        assert report.web_reconciled == [self.SID]

    def test_apply_delete_failure_kept_in_count(self, svc, store):
        from src.api.v2.webstore import StoreUnavailable

        webstore = self._seed(store)
        orig_delete = webstore.delete_turn

        def _flaky_delete(session_id, key):
            if key == OLD_KEY:
                raise StoreUnavailable("delete down (fake)")
            return orig_delete(session_id, key)

        webstore.delete_turn = _flaky_delete
        report = self._run(svc, webstore, apply=True)
        assert report.web_turns_deleted == []
        assert len(report.errors) == 1 and report.errors[0]["key"] == OLD_KEY
        # 删除失败的那条仍在库中，缓存按实际存活集校正（不按“已删除”计）
        assert webstore.read_turn(self.SID, OLD_KEY) is not None
        rec = webstore.get_registration(self.SID)
        assert rec["turn_count"] == 2
