"""账本 reducer 与会话守卫测试（doc 6.3 §4.1/§5.1/§5.3/§6.5、§13.7；§11）

覆盖：任务记录键解码与身份校验、计划版本严格单调（重放幂等/旧版本拒绝）、
db_tool_errors 专用去重（同键同事实保留首条、异事实拒绝、单侧内部校验、
事实字段含 tool_call_id）、执行/分析事实字段扩展（任务归属/结构化血缘/
直接沿用来源）、LedgerView 版本选择（先最大版再判 void）、session guard
（PG lease 复用锁、memory guard 排他与释放、凭据校验）、QaArchive 运行
快照（幂等写、冲突拒绝、按完整身份读取）。
"""

from __future__ import annotations

import pytest

from src.modules.db_deepagent.persistence.archive import (
    QaArchive,
    RUN_SNAPSHOT_SCHEMA_VERSION,
)
from src.modules.db_deepagent.persistence.session_guard import (
    InProcessSessionGuard,
    SessionGuardLease,
    SessionGuardRequired,
    validate_lease,
)
from src.modules.db_deepagent.services.sql_pipeline import LedgerView
from src.modules.db_deepagent.state import (
    LedgerConsistencyError,
    append_dedup_tool_errors,
    merge_task_plans,
    merge_task_records,
    parse_task_ledger_key,
    task_ledger_key,
)
from src.tests.db_deepagent.conftest import FakeStore, make_task_record

pytestmark = pytest.mark.unit


def _task(run_id="run-x", tid="T1", rev=1, **kw):
    return make_task_record(run_id, tid, rev, **kw)


class TestTaskRecordReducer:
    def test_key_roundtrip(self):
        key = task_ledger_key("run-x", "T1", 2)
        assert parse_task_ledger_key(key) == ("run-x", "T1", 2)
        assert parse_task_ledger_key("not-json") is None
        assert parse_task_ledger_key('["run-x","T1","2"]') is None

    def test_identity_mismatch_rejected(self):
        record = _task()
        with pytest.raises(LedgerConsistencyError):
            merge_task_records({}, {task_ledger_key("run-x", "T1", 9): record})

    def test_undecodable_key_rejected(self):
        with pytest.raises(LedgerConsistencyError):
            merge_task_records({}, {"??": _task()})

    def test_status_update_same_key_allowed(self):
        """status/void 更新同键记录：右值覆盖（写入准入在服务层把关）。"""
        pending = _task()
        done = {**pending, "status": "completed", "evidence_ids": ["E-1"]}
        merged = merge_task_records(
            {task_ledger_key("run-x", "T1", 1): pending},
            {task_ledger_key("run-x", "T1", 1): done},
        )
        assert merged[task_ledger_key("run-x", "T1", 1)]["status"] == "completed"

    def test_replay_identical_idempotent(self):
        record = _task()
        merged = merge_task_records(
            {task_ledger_key("run-x", "T1", 1): record},
            {task_ledger_key("run-x", "T1", 1): dict(record)},
        )
        assert merged[task_ledger_key("run-x", "T1", 1)] == record


class TestTaskPlanReducer:
    def _plan(self, version):
        return {
            "session_id": "s",
            "thread_id": "t",
            "request_id": "r",
            "run_id": "run-x",
            "plan_version": version,
            "updated_at": "now",
            "tool_call_id": "tc",
        }

    def test_first_write_and_increment(self):
        merged = merge_task_plans({}, {"run-x": self._plan(1)})
        assert merged["run-x"]["plan_version"] == 1
        merged = merge_task_plans(merged, {"run-x": self._plan(2)})
        assert merged["run-x"]["plan_version"] == 2

    def test_identical_replay_idempotent(self):
        merged = merge_task_plans({"run-x": self._plan(2)}, {"run-x": self._plan(2)})
        assert merged["run-x"]["plan_version"] == 2

    def test_stale_version_rejected(self):
        with pytest.raises(LedgerConsistencyError):
            merge_task_plans({"run-x": self._plan(2)}, {"run-x": self._plan(1)})

    def test_skip_version_rejected(self):
        with pytest.raises(LedgerConsistencyError):
            merge_task_plans({"run-x": self._plan(1)}, {"run-x": self._plan(3)})

    def test_identity_mismatch_rejected(self):
        with pytest.raises(LedgerConsistencyError):
            merge_task_plans({}, {"run-other": self._plan(1)})


def _err(error_id="ERR-a", tool_call_id="tc-1", message="boom", **extra):
    record = {
        "error_id": error_id,
        "session_id": "s",
        "thread_id": "t",
        "run_id": "run-x",
        "request_id": "req",
        "tool_call_id": tool_call_id,
        "tool": "execute_sql",
        "error_type": "db_error",
        "message": message,
        "retryable": True,
        "source_task_ref": None,
    }
    record.update(extra)
    return record


class TestToolErrorReducer:
    def test_same_ref_same_fact_dedup_keeps_first(self):
        left = [_err()]
        right = [_err()]
        merged = append_dedup_tool_errors(left, right)
        assert len(merged) == 1

    def test_different_ids_coexist(self):
        merged = append_dedup_tool_errors([_err("ERR-a")], [_err("ERR-b")])
        assert [r["error_id"] for r in merged] == ["ERR-a", "ERR-b"]

    def test_conflicting_fact_rejected(self):
        with pytest.raises(LedgerConsistencyError):
            append_dedup_tool_errors([_err()], [_err(message="other")])
        with pytest.raises(LedgerConsistencyError):
            append_dedup_tool_errors([_err()], [_err(tool_call_id="tc-2")])

    def test_tool_call_id_is_fact(self):
        """与执行 reducer 不同：tool_call_id 属错误事实（§5.1/§13.7）。"""
        with pytest.raises(LedgerConsistencyError):
            append_dedup_tool_errors([_err()], [_err(tool_call_id="tc-9")])

    def test_missing_identity_rejected(self):
        bad = _err()
        del bad["error_id"]
        with pytest.raises(LedgerConsistencyError):
            append_dedup_tool_errors([], [bad])

    def test_single_side_internal_duplicate_validated(self):
        """单侧内部同引用异事实也拒绝；不因另一侧为空跳过校验。"""
        with pytest.raises(LedgerConsistencyError):
            append_dedup_tool_errors([_err(), _err(message="diff")], [])
        with pytest.raises(LedgerConsistencyError):
            append_dedup_tool_errors([], [_err(), _err(message="diff")])

    def test_single_side_internal_same_fact_dedup(self):
        merged = append_dedup_tool_errors([_err(), dict(_err())], [])
        assert len(merged) == 1


class TestLedgerViewVersionSelection:
    def test_max_revision_then_void_check(self):
        """先取最大 revision 再判 void（§4.2）：最新版作废后旧版不复活。"""
        state = {
            "db_sub_tasks": {
                task_ledger_key("run-x", "T1", 1): _task(status="completed"),
                task_ledger_key("run-x", "T1", 2): _task(rev=2, status="voided"),
            }
        }
        state["db_sub_tasks"][task_ledger_key("run-x", "T1", 2)]["reason"] = "替换"
        view = LedgerView(state)
        tasks = view.current_tasks("run-x")
        assert tasks["T1"]["status"] == "voided"
        assert view.current_tasks("run-x", include_voided=False) == {}

    def test_require_current_task_rejects_old_revision(self):
        from src.modules.db_deepagent.services.sql_pipeline import TaskAccessError

        state = {
            "db_sub_tasks": {
                task_ledger_key("run-x", "T1", 1): _task(),
                task_ledger_key("run-x", "T1", 2): _task(rev=2),
            }
        }
        view = LedgerView(state)
        with pytest.raises(TaskAccessError) as exc:
            view.require_current_task("run-x", "T1", 1)
        assert exc.value.error_type == "task_revision_mismatch"
        assert view.require_current_task("run-x", "T1", 2)["revision"] == 2

    def test_require_writable_rejects_terminal(self):
        from src.modules.db_deepagent.services.sql_pipeline import TaskAccessError

        state = {
            "db_sub_tasks": {
                task_ledger_key("run-x", "T1", 1): _task(status="completed"),
            }
        }
        view = LedgerView(state)
        with pytest.raises(TaskAccessError) as exc:
            view.require_writable_task("run-x", "T1", 1)
        assert exc.value.error_type == "task_not_writable"

    def test_error_by_ref_duplicate_rejected(self):
        from src.modules.db_deepagent.services.sql_pipeline import TaskAccessError

        state = {"db_tool_errors": [_err(), dict(_err())]}
        view = LedgerView(state)
        with pytest.raises(TaskAccessError):
            view.error_by_ref(
                {
                    "session_id": "s",
                    "thread_id": "t",
                    "run_id": "run-x",
                    "error_id": "ERR-a",
                }
            )

    def test_error_by_ref_found(self):
        view = LedgerView({"db_tool_errors": [_err()]})
        record = view.error_by_ref(
            {
                "session_id": "s",
                "thread_id": "t",
                "run_id": "run-x",
                "error_id": "ERR-a",
            }
        )
        assert record["error_id"] == "ERR-a"


class TestExecutionFactFields:
    """执行/分析 reducer 事实字段含任务归属与结构化血缘（§6）。"""

    def _exec(self, eid="E-1", **kw):
        base = {
            "evidence_id": eid,
            "tool_call_id": "tc",
            "run_id": "run-x",
            "request_id": "req",
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
        }
        base.update(kw)
        return base

    def test_task_ownership_is_fact(self):
        from src.modules.db_deepagent.state import append_dedup_execution

        with pytest.raises(LedgerConsistencyError):
            append_dedup_execution([self._exec()], [self._exec(sub_task_id="T2")])

    def test_input_lineage_is_fact(self):
        from src.modules.db_deepagent.state import append_dedup_execution

        lineage = [{"result_ref": "res_9", "evidence_ref": {}, "source_task_ref": {}}]
        with pytest.raises(LedgerConsistencyError):
            append_dedup_execution(
                [self._exec()], [self._exec(input_result_refs=lineage)]
            )

    def test_direct_reuse_is_fact(self):
        from src.modules.db_deepagent.state import append_dedup_execution

        direct = {"evidence_ref": {}, "source_task_ref": {}}
        with pytest.raises(LedgerConsistencyError):
            append_dedup_execution(
                [self._exec()], [self._exec(direct_reuse_from=direct)]
            )


class TestSessionGuard:
    def test_memory_guard_exclusive_and_release(self):
        guard = InProcessSessionGuard()
        lease = guard.try_acquire("s1")
        assert lease is not None
        assert guard.try_acquire("s1") is None  # 排他
        other = guard.try_acquire("s2")
        assert other is not None  # 不同会话独立
        guard.release(lease)
        assert guard.try_acquire("s1") is not None

    def test_validate_lease_scope_and_validity(self):
        guard = InProcessSessionGuard()
        lease = guard.try_acquire("s1")
        assert validate_lease(lease, "s1") is lease
        with pytest.raises(SessionGuardRequired):
            validate_lease(lease, "s2")
        guard.release(lease)
        with pytest.raises(SessionGuardRequired):
            validate_lease(lease, "s1")  # 已释放

    def test_invalid_lease_type_rejected(self):
        with pytest.raises(SessionGuardRequired):
            validate_lease(None, "s1")
        with pytest.raises(SessionGuardRequired):
            validate_lease("not-a-lease", "s1")

    def test_pg_lease_reflects_lock_state(self):
        from src.modules.db_deepagent.persistence.session_guard import (
            lease_from_advisory_lock,
        )

        class FakeLock:
            locked = True

            def release(self):
                self.locked = False

        lock = FakeLock()
        lease = lease_from_advisory_lock(lock, "s1")
        assert validate_lease(lease, "s1") is lease
        lock.locked = False
        with pytest.raises(SessionGuardRequired):
            validate_lease(lease, "s1")  # 锁已释放 → 凭据失效


class TestRunSnapshotArchive:
    def _record(self, run_id="run-r", version=1):
        return {
            "run_snapshot_schema_version": RUN_SNAPSHOT_SCHEMA_VERSION,
            "session_id": "s1",
            "thread_id": "dbda:s1:1",
            "run_id": run_id,
            "version_marker": version,
            "tasks": [],
            "executions": [],
            "analyses": [],
            "tool_errors": [],
        }

    def test_write_read_roundtrip(self):
        store = FakeStore()
        archive = QaArchive(store)
        archive.write_run_snapshot("s1", "dbda:s1:1", "run-r", self._record())
        back = archive.read_run_snapshot("s1", "dbda:s1:1", "run-r")
        assert back["run_id"] == "run-r"

    def test_idempotent_same_content(self):
        store = FakeStore()
        archive = QaArchive(store)
        archive.write_run_snapshot("s1", "dbda:s1:1", "run-r", self._record())
        archive.write_run_snapshot("s1", "dbda:s1:1", "run-r", self._record())
        assert len(archive.list_run_snapshots("s1")) == 1

    def test_conflict_rejected(self):
        store = FakeStore()
        archive = QaArchive(store)
        archive.write_run_snapshot("s1", "dbda:s1:1", "run-r", self._record())
        with pytest.raises(ValueError):
            archive.write_run_snapshot(
                "s1", "dbda:s1:1", "run-r", self._record(version=2)
            )

    def test_read_task_and_evidence_snapshot(self):
        store = FakeStore()
        archive = QaArchive(store)
        record = self._record()
        record["tasks"] = [
            make_task_record("run-r", "T1", 1, status="completed", evidence_ids=["E-1"])
        ]
        record["executions"] = [
            {
                "evidence_id": "E-1",
                "run_id": "run-r",
                "purpose": "query",
                "sub_task_id": "T1",
                "sub_task_revision": 1,
                "result_ref": "res_x",
                "input_result_refs": [],
            }
        ]
        archive.write_run_snapshot("s1", "dbda:s1:1", "run-r", record)
        snapshot = archive.read_run_snapshot("s1", "dbda:s1:1", "run-r")
        task = archive.read_task_snapshot(snapshot, "T1", 1)
        assert task["status"] == "completed"
        evidence = archive.read_evidence_snapshot(snapshot, "E-1")
        assert evidence["result_ref"] == "res_x"
        assert archive.read_evidence_snapshot(snapshot, "E-none") is None

    def test_unknown_version_rejected_on_read(self):
        store = FakeStore()
        archive = QaArchive(store)
        record = self._record()
        record["run_snapshot_schema_version"] = 99
        # 直接入 store（write 侧拒绝未知版本；读取侧同样防御）
        store.put(
            ("db_deepagent", "run_snapshots", "s1"),
            archive._run_snapshot_key("dbda:s1:1", "run-r"),
            record,
        )
        with pytest.raises(ValueError):
            archive.read_run_snapshot("s1", "dbda:s1:1", "run-r")


class TestMemoryGuardConcurrency:
    """doc 6.3 §11：真实同步边界（非两个无关 mock）——独立执行线程 +
    threading.Barrier 停在"来源校验通过、清理即将实删"处。"""

    def test_cleanup_skips_session_held_by_runner(self):
        """运行持锁期间清理跳过该会话；运行结束后清理可取锁。"""
        import threading

        guard = InProcessSessionGuard()
        runner_lease = guard.try_acquire("s-live")
        assert runner_lease is not None
        barrier = threading.Barrier(2, timeout=5)
        results: dict = {}

        def cleanup_worker():
            barrier.wait()  # 与运行方对齐后尝试取锁
            results["cleanup_lease"] = guard.try_acquire("s-live")

        thread = threading.Thread(target=cleanup_worker)
        thread.start()
        barrier.wait()  # 运行方仍在持锁（模拟沿用窗口）
        thread.join()
        assert results["cleanup_lease"] is None  # 跳过该会话
        guard.release(runner_lease)
        assert guard.try_acquire("s-live") is not None  # 运行结束后可清理

    def test_reverse_order_runner_busy_then_recheck(self):
        """清理先持锁：新运行按 busy 拒绝；清理结束后运行重新检查可进入。"""
        import threading

        guard = InProcessSessionGuard()
        cleanup_lease = guard.try_acquire("s-rev")
        assert cleanup_lease is not None
        assert guard.try_acquire("s-rev") is None  # 运行 busy
        guard.release(cleanup_lease)
        assert guard.try_acquire("s-rev") is not None  # 重新核验后进入
