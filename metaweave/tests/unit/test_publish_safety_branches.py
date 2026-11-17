"""发布并行安全分支直测（doc 10.2 §9.8/§9.14/§9.15，DeepSeek R1 #4）。

覆盖此前零覆盖的关键分支：_control_fault_wrapup 的终态尽力写回与
claim-skipped 跳过、claim/内容事务的 publish_lock_timeout 分类
（blocked vs failed）、CQLLoader 锁探测在每条约束语句前的传播。
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy.exc import SQLAlchemyError

import backend.publishing.publish_service as publish_service
from backend.publishing.publish_service import (
    _claim_publish_step,
    _control_fault_wrapup,
    _execute_step,
)
from backend.publishing.scheduling import STATE_UNKNOWN
from tests.unit.test_publish_service import (
    FakeSession,
    SETTINGS,
    WORKSPACE_ID,
    install,
    make_ready_db,
)


class _LockTimeoutError(SQLAlchemyError):
    """携带 57014 sqlstate 的伪锁超时（真实库由 statement_timeout 取消）。"""

    def __init__(self):
        super().__init__("canceling statement due to lock timeout")
        self.orig = SimpleNamespace(sqlstate="57014")


# ---- §9.8：_control_fault_wrapup 直测 ----


class TestControlFaultWrapup:
    def _make_session(self, monkeypatch, tmp_path, *, statuses):
        session = FakeSession(steps=tuple(statuses))
        install(monkeypatch, tmp_path, session)
        run_id = uuid4()
        from backend.jobs.run_repository import PUBLISH_RUN, create_run

        with publish_service.platform_session(SETTINGS) as hand:
            create_run(
                PUBLISH_RUN,
                hand,
                run_id=run_id,
                job_id="pub_job_wrapped",
                revision_no=1,
                steps=list(statuses),
                step_params={},
            )
            hand.commit()
        for step, status in statuses.items():
            row = next(
                r
                for r in session.run_steps
                if r.step_name == step and r.publish_run_id == run_id
            )
            row.status = status
        return session, run_id

    def test_running_to_failed_pending_to_blocked_and_skip_respected(
        self, monkeypatch, tmp_path
    ):
        session, run_id = self._make_session(
            monkeypatch, tmp_path,
            statuses={"db": "running", "ddl": "pending", "json": "running"},
        )
        # json 为认领跳过步骤（他人持有），收尾不得改写
        _control_fault_wrapup(
            SETTINGS,
            run_id,
            ["db", "ddl", "json"],
            skip_steps={"json"},
        )
        by_name = {row.step_name: row for row in session.run_steps}
        assert by_name["db"].status == "failed"
        assert "控制面故障" in by_name["db"].message
        assert by_name["ddl"].status == "blocked"
        assert by_name["json"].status == "running", "跳过步骤不得被改写"
        # 已终态步骤不动
        session2, run2 = self._make_session(
            monkeypatch, tmp_path, statuses={"db": "success"}
        )
        _control_fault_wrapup(SETTINGS, run2, ["db"])
        assert session2.run_steps[-1].status == "success"


# ---- §9.14：publish_lock_timeout 分类（claim=blocked，内容事务=failed） ----


class TestLockTimeoutClassification:
    def test_claim_timeout_records_blocked(self, monkeypatch, tmp_path):
        session = FakeSession(steps=("db",))
        install(monkeypatch, tmp_path, session)
        make_ready_db(tmp_path)
        from backend.jobs.run_repository import PUBLISH_RUN, create_run

        run_id = uuid4()
        with publish_service.platform_session(SETTINGS) as hand:
            create_run(
                PUBLISH_RUN,
                hand,
                run_id=run_id,
                job_id="pub_job_lt",
                revision_no=1,
                steps=["db"],
                step_params={},
            )
            hand.commit()
        # 复查通过；认领事务的第一条语句（SET LOCAL lock_timeout 位置）
        # 抛锁超时 → claim 的 except 分支转为 blocked 原因返回
        monkeypatch.setattr(
            publish_service, "_set_local_lock_timeout",
            lambda session: (_ for _ in ()).throw(_LockTimeoutError()),
        )
        outcome = _execute_step(SETTINGS, run_id, WORKSPACE_ID, "db")
        assert outcome == {"step": "db", "status": "blocked"}
        row = next(
            r
            for r in session.run_steps
            if r.step_name == "db" and r.publish_run_id == run_id
        )
        assert row.status == "blocked"
        assert "publish_lock_timeout" in row.message

    def test_content_txn_timeout_records_failed(self, monkeypatch, tmp_path):
        session = FakeSession(steps=("db",))
        install(monkeypatch, tmp_path, session)
        make_ready_db(tmp_path)
        from backend.jobs.run_repository import PUBLISH_RUN, create_run

        run_id = uuid4()
        with publish_service.platform_session(SETTINGS) as hand:
            create_run(
                PUBLISH_RUN,
                hand,
                run_id=run_id,
                job_id="pub_job_lt2",
                revision_no=1,
                steps=["db"],
                step_params={},
            )
            hand.commit()
        # 已认领（步骤置 running）；publisher 内容事务抛锁超时 → failed
        row = next(
            r
            for r in session.run_steps
            if r.step_name == "db" and r.publish_run_id == run_id
        )
        row.status = "running"
        monkeypatch.setattr(
            publish_service,
            "_claim_publish_step",
            lambda *a, **k: None,
        )

        def _timeout_publisher(settings, run_id, workspace_id, **kwargs):
            raise _LockTimeoutError()

        monkeypatch.setitem(
            publish_service._PUBLISHERS, "db", _timeout_publisher
        )
        outcome = _execute_step(SETTINGS, run_id, WORKSPACE_ID, "db")
        assert outcome == {"step": "db", "status": "failed"}
        row = next(
            r
            for r in session.run_steps
            if r.step_name == "db" and r.publish_run_id == run_id
        )
        assert row.status == "failed"
        assert "publish_lock_timeout" in row.message


# ---- §9.15：CQLLoader 约束语句锁探测传播 ----


class TestCqlLoaderLockProbe:
    def test_probe_runs_before_each_constraint_and_propagates_loss(self):
        from metaweave.core.loaders.cql_loader import CQLLoader, LockLostError

        loader = CQLLoader.__new__(CQLLoader)
        calls = {"probe": 0, "stmt": 0}

        def _probe():
            calls["probe"] += 1
            if calls["probe"] == 2:
                raise LockLostError("锁连接已失效（第 2 条语句前）")

        loader._lock_probe = _probe
        loader.neo4j_manager = SimpleNamespace(
            execute_write_transaction=lambda stmt: calls.__setitem__(
                "stmt", calls["stmt"] + 1
            )
        )
        loader._is_constraint_section = lambda section: True
        loader._fix_json_quotes = lambda cypher_text: cypher_text
        loader._split_statements = lambda cypher_text: ["S1;", "S2;", "S3;"]
        with pytest.raises(LockLostError):
            loader._apply_constraint_statements(
                [{"section_name": "constraints", "cypher": "ignored"}]
            )
        # 第 1 条执行成功；第 2 条探测失锁 → 停止，第 2/3 条不再执行
        assert calls["stmt"] == 1
        assert calls["probe"] == 2
