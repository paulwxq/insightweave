"""doc 8.1 §7 任务仓储与准入查询：FakeSession 语义假件（无真库）。"""

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

from backend.deletion import repository as repo
from backend.persistence.models import (
    CollectionJobInfo,
    CollectionRunInfo,
    DbSourceInfo,
    PublishJobInfo,
    PublishRunInfo,
    ResourceDeletionTask,
    WorkspaceInfo,
)

NOW = datetime(2026, 10, 29, 8, 0, 0, tzinfo=timezone.utc)


class FakeDeletionSession:
    """覆盖任务表 Update/Select 与四类主表读取的最小假会话。"""

    def __init__(self):
        self.db_now = NOW
        self.source = self._source()
        self.workspaces = [
            SimpleNamespace(
                workspace_id="ws_1", source_id="db_src_1", deleted_at=None
            ),
            SimpleNamespace(
                workspace_id="ws_2", source_id="db_src_1", deleted_at=None
            ),
            SimpleNamespace(
                workspace_id="ws_9", source_id="db_src_2", deleted_at=None
            ),
        ]
        self.collection_jobs = [
            SimpleNamespace(
                collection_job_id="cj_1",
                workspace_id="ws_1",
                job_name="采集A",
                deleted_at=None,
            ),
        ]
        self.publish_jobs = [
            SimpleNamespace(
                publish_job_id="pj_1",
                workspace_id="ws_1",
                job_name="发布A",
                deleted_at=None,
            ),
        ]
        self.collection_runs = [
            SimpleNamespace(
                collection_run_id=uuid.UUID(int=1),
                collection_job_id="cj_1",
                status="success",
                created_at=NOW,
                started_at=NOW,
            ),
            SimpleNamespace(
                collection_run_id=uuid.UUID(int=2),
                collection_job_id="cj_1",
                status="pending",
                created_at=NOW,
                started_at=None,
            ),
        ]
        self.publish_runs = [
            SimpleNamespace(
                publish_run_id=uuid.UUID(int=3),
                publish_job_id="pj_1",
                status="running",
                created_at=NOW,
                started_at=NOW,
            ),
        ]
        self.tasks = []
        self.commits = 0
        self.rollbacks = 0

    @staticmethod
    def _source():
        return SimpleNamespace(
            source_id="db_src_1",
            deleted_at=None,
            deletion_state="active",
            is_active=True,
            display_name="源",
        )

    # ---- Session 接口 ----

    def _pool(self, model):
        return {
            DbSourceInfo: [self.source],
            WorkspaceInfo: self.workspaces,
            CollectionJobInfo: self.collection_jobs,
            PublishJobInfo: self.publish_jobs,
            CollectionRunInfo: self.collection_runs,
            PublishRunInfo: self.publish_runs,
            ResourceDeletionTask: self.tasks,
        }[model]

    def get(self, model, key, **_kwargs):
        pool = self._pool(model)
        for row in pool:
            pk = getattr(row, "task_id", None) or getattr(row, "source_id", None)
            if pk == key:
                return row
        return None

    def add(self, row):
        self._pool(type(row)).append(row)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def scalar(self, statement):
        # select(func.now()) → 假数据库时钟
        return self.db_now

    def scalars(self, statement):
        from tests.unit.deletion_fakes import scalars_from_pool

        columns = [c["entity"] for c in statement.column_descriptions]
        return scalars_from_pool(self._pool(columns[0]), statement)

    def execute(self, statement):
        name = statement.__visit_name__
        if name == "update":
            table = statement.table.name
            where = dict(_where_pairs(statement))
            pool = {
                "resource_deletion_task": self.tasks,
            }[table]
            matched = [
                row
                for row in pool
                if all(getattr(row, k) == v for k, v in where.items())
            ]
            values = {}
            for col, value in statement._values.items():
                key = col.key
                if getattr(value, "__visit_name__", None) == "function":
                    values[key] = self.db_now  # func.now()
                elif hasattr(value, "value"):
                    values[key] = value.value  # BindParameter
                else:
                    values[key] = value  # 字面量（str/None）
            for row in matched:
                for key, value in values.items():
                    setattr(row, key, value)
            return SimpleNamespace(rowcount=len(matched))
        if name == "select":
            # 全池返回，过滤交给仓储的 Python 侧判断（假件不解析 where）。
            columns = [c["entity"] for c in statement.column_descriptions]
            model = columns[0] if columns else None
            rows = list(self._pool(model)) if model else []
            return SimpleNamespace(fetchall=lambda: rows, all=lambda: rows)
        raise AssertionError(name)


def _where_pairs(statement):
    clause = statement.whereclause
    for item in clause.clauses:
        value = item.right
        yield item.left.key, getattr(value, "value", value)


def _task(**overrides):
    base = dict(
        task_id=uuid.UUID(int=99),
        action="clear_source_knowledge",
        target_id="db_src_1",
        source_id="db_src_1",
        workspace_id=None,
        status="pending",
        step_results=None,
        error_message=None,
        created_at=NOW,
        queued_at=NOW,
        started_at=None,
        finished_at=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_insert_task_assigns_pending_status():
    session = FakeDeletionSession()
    task = repo.insert_task(
        session,
        action="delete_workspace",
        target_id="ws_1",
        source_id="db_src_1",
        workspace_id="ws_1",
    )
    assert task.status == "pending"
    assert task in session.tasks
    assert session.commits == 1


def test_claim_task_only_from_pending():
    session = FakeDeletionSession()
    task = _task(status="pending")
    session.tasks.append(task)
    assert repo.claim_task(session, task.task_id) is True
    assert task.status == "running"
    assert task.started_at == NOW
    # 已 running 再认领返回 False（原子条件更新语义）
    assert repo.claim_task(session, task.task_id) is False


def test_retry_transition_only_from_failed_and_refreshes_queued():
    session = FakeDeletionSession()
    task = _task(
        status="failed",
        started_at=NOW,
        finished_at=NOW,
        error_message="x",
    )
    session.tasks.append(task)
    session.db_now = datetime(2026, 10, 29, 9, 0, 0, tzinfo=timezone.utc)
    assert repo.retry_transition(session, task.task_id) is True
    assert task.status == "pending"
    assert task.queued_at == session.db_now
    assert task.started_at is None and task.finished_at is None
    # pending 状态下重试转换不生效
    assert repo.retry_transition(session, task.task_id) is False


def test_mark_task_terminal_and_interrupt():
    session = FakeDeletionSession()
    task = _task(status="running")
    session.tasks.append(task)
    assert repo.mark_task_success(session, task.task_id) is True
    assert task.status == "success" and task.finished_at == NOW

    task2 = _task(task_id=uuid.UUID(int=98), status="running")
    session.tasks.append(task2)
    assert (
        repo.interrupt_task(
            session, task2.task_id, reason="执行已中断，等待人工重试"
        )
        is True
    )
    assert task2.status == "failed"
    assert "执行已中断" in task2.error_message


def test_record_step_merges_step_results():
    session = FakeDeletionSession()
    task = _task(status="running", step_results={"_targets": {"fingerprint": "ab"}})
    session.tasks.append(task)
    repo.record_step(session, task.task_id, "milvus", {"deleted": 3})
    repo.record_step(session, task.task_id, "neo4j", {"nodes": 5})
    assert task.step_results["_targets"]["fingerprint"] == "ab"
    assert task.step_results["milvus"] == {"deleted": 3}
    assert task.step_results["neo4j"] == {"nodes": 5}


def test_unfinished_runs_in_scope_by_source_and_workspace():
    session = FakeDeletionSession()
    runs = repo.unfinished_runs_in_scope(session, source_id="db_src_1")
    # pending 采集运行 + running 发布运行（ws_1/cj_1、pj_1 属 db_src_1）
    assert {r["run_id"] for r in runs} == {uuid.UUID(int=2), uuid.UUID(int=3)}
    assert all(r["status"] in ("pending", "running") for r in runs)

    ws_runs = repo.unfinished_runs_in_scope(session, workspace_id="ws_2")
    assert ws_runs == []

    other = repo.unfinished_runs_in_scope(session, source_id="db_src_2")
    assert other == []


def test_unresolved_task_prefers_active_then_latest_failed():
    session = FakeDeletionSession()
    assert repo.unresolved_task_for_source(session, "db_src_1") is None

    failed = _task(
        task_id=uuid.UUID(int=1),
        status="failed",
        created_at=datetime(2026, 10, 28, tzinfo=timezone.utc),
    )
    session.tasks.append(failed)
    assert repo.unresolved_task_for_source(session, "db_src_1") is failed

    active = _task(
        task_id=uuid.UUID(int=2),
        status="running",
        action="delete_workspace",
        workspace_id="ws_1",
        created_at=datetime(2026, 10, 29, tzinfo=timezone.utc),
    )
    session.tasks.append(active)
    assert repo.unresolved_task_for_source(session, "db_src_1") is active

    # success 不算未解决
    session.tasks = [_task(status="success")]
    assert repo.unresolved_task_for_source(session, "db_src_1") is None


def test_task_summary_scoping_for_workspace():
    session = FakeDeletionSession()
    session.tasks = [
        _task(
            task_id=uuid.UUID(int=1),
            status="pending",
            action="delete_workspace",
            target_id="ws_1",
            workspace_id="ws_1",
        ),
        _task(
            task_id=uuid.UUID(int=2),
            status="success",
            action="delete_workspace",
            target_id="ws_2",
            workspace_id="ws_2",
        ),
    ]
    summary = repo.task_summary(session, source_id="db_src_1", workspace_id="ws_1")
    assert summary["task_id"] == uuid.UUID(int=1)
    assert summary["action"] == "delete_workspace"
    # 兄弟工作区 ws_2 的任务不得附给 ws_1
    assert repo.task_summary(session, source_id="db_src_1", workspace_id="ws_2") is None
    # 源级摘要返回同源未解决任务
    source_summary = repo.task_summary(session, source_id="db_src_1")
    assert source_summary["task_id"] == uuid.UUID(int=1)
    assert set(source_summary) == {
        "task_id",
        "action",
        "status",
        "source_id",
        "workspace_id",
        "target_id",
        "error_message",
    }
