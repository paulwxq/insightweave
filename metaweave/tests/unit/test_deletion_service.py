"""doc 8.1 §5/§7 删除服务状态机：FakeSession + 注入清理器（无真库/真锁）。"""

import uuid
from copy import deepcopy
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from backend.deletion import service, task_lock
from backend.deletion.errors import (
    DeletionInProgressError,
    DeletionStepError,
    DeletionTargetsChangedError,
    ResourceHasUnfinishedRunsError,
    TaskPendingClaimError,
)
from backend.persistence.models import (
    CollectionJobInfo,
    CollectionRunInfo,
    DbSourceInfo,
    PublishJobInfo,
    ResourceDeletionTask,
    WorkspaceInfo,
)
from backend.persistence.platform_db import PlatformSettings

SETTINGS = PlatformSettings(
    host="platform", port=5432, name="metaweave", user="admin", password="secret"
)
NOW = datetime(2026, 10, 29, 8, 0, 0, tzinfo=timezone.utc)
SOURCE_ID = "db_src_1"
WS_ID = "ws_1"


class ServiceFakeSession:
    """覆盖提交/执行/重试/扫描所需的读取、行锁与更新语义。"""

    def __init__(self):
        self.db_now = NOW
        self.source = self._source()
        self.workspaces = [self._workspace(WS_ID)]
        self.collection_jobs = [
            SimpleNamespace(
                collection_job_id="cj_1",
                workspace_id=WS_ID,
                job_name="采集A",
                deleted_at=None,
            )
        ]
        self.publish_jobs = [
            SimpleNamespace(
                publish_job_id="pj_1",
                workspace_id=WS_ID,
                job_name="发布A",
                deleted_at=None,
            )
        ]
        self.collection_runs = []
        self.publish_runs = []
        self.tasks = []
        self.commits = 0

    @staticmethod
    def _source():
        return SimpleNamespace(
            source_id=SOURCE_ID,
            display_name="源",
            description="说明",
            db_type="postgresql",
            host="h",
            port=5432,
            db_name="db",
            username="u",
            password_ciphertext="cipher",
            sslmode=None,
            db_version="18",
            selected_schemas=["public"],
            is_active=True,
            deleted_at=None,
            deletion_state="active",
            created_at=NOW,
            updated_at=NOW,
        )

    @staticmethod
    def _workspace(ws_id):
        return SimpleNamespace(
            workspace_id=ws_id,
            source_id=SOURCE_ID,
            deleted_at=None,
            deletion_state="active",
        )

    def _pool(self, model):
        from backend.persistence.models import PublishRunInfo

        return {
            DbSourceInfo: [self.source] if self.source else [],
            WorkspaceInfo: self.workspaces,
            CollectionJobInfo: self.collection_jobs,
            PublishJobInfo: self.publish_jobs,
            CollectionRunInfo: self.collection_runs,
            PublishRunInfo: self.publish_runs,
            ResourceDeletionTask: self.tasks,
        }[model]

    def get(self, model, key, **_kwargs):
        pk_attr = {
            DbSourceInfo: "source_id",
            WorkspaceInfo: "workspace_id",
            CollectionJobInfo: "collection_job_id",
            PublishJobInfo: "publish_job_id",
            CollectionRunInfo: "collection_run_id",
            ResourceDeletionTask: "task_id",
        }[model]
        if isinstance(key, str) and model is ResourceDeletionTask:
            # 真库按 UUID 类型强转字符串主键
            from uuid import UUID as _UUID

            try:
                key = _UUID(key)
            except ValueError:
                return None
        for row in self._pool(model):
            if getattr(row, pk_attr) == key:
                return row
        return None

    def add(self, row):
        if type(row) is ResourceDeletionTask:
            # 模拟 server default：created_at/queued_at 由数据库时间生成
            if row.created_at is None:
                row.created_at = self.db_now
            if row.queued_at is None:
                row.queued_at = self.db_now
        self._pool(type(row)).append(row)

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass

    def scalar(self, _statement):
        return self.db_now

    def scalars(self, statement):
        from tests.unit.deletion_fakes import scalars_from_pool

        columns = [c["entity"] for c in statement.column_descriptions]
        return scalars_from_pool(self._pool(columns[0]), statement)

    def execute(self, statement):
        name = statement.__visit_name__
        if name == "update":
            where = {
                item.left.key: getattr(item.right, "value", item.right)
                for item in statement.whereclause.clauses
            }
            matched = [
                row
                for row in self.tasks
                if all(getattr(row, k) == v for k, v in where.items())
            ]
            values = {}
            for col, value in statement._values.items():
                if getattr(value, "__visit_name__", None) == "function":
                    values[col.key] = self.db_now
                elif hasattr(value, "value"):
                    values[col.key] = value.value
                else:
                    values[col.key] = value
            for row in matched:
                for key, value in values.items():
                    setattr(row, key, value)
            return SimpleNamespace(rowcount=len(matched))
        if name == "select":
            columns = [c["entity"] for c in statement.column_descriptions]
            rows = list(self._pool(columns[0])) if columns else []
            return SimpleNamespace(fetchall=lambda: rows, all=lambda: rows)
        raise AssertionError(name)


class FakeLock:
    """task_lock 假件：记录持锁，可预占一把钥匙模拟“仍在执行”。"""

    def __init__(self):
        self.held = set()
        self.pre_held = set()

    def connect(self, _settings):
        return SimpleNamespace(closed=False)

    def try_acquire(self, _conn, key):
        if key in self.pre_held or key in self.held:
            return False
        self.held.add(key)
        return True

    def release(self, conn, key):
        self.held.discard(key)
        conn.closed = True


class FakeDeps:
    """注入的清理器：记录调用顺序，可让某一步失败。"""

    def __init__(self, fail_step=None):
        self.calls = []
        self.fail_step = fail_step

    def milvus_ops_factory(self):
        self.calls.append("milvus_connect")
        return SimpleNamespace(close=lambda: None)

    def neo4j_ops_factory(self):
        self.calls.append("neo4j_connect")
        return SimpleNamespace(close=lambda: None)

    def pg_purge(self, session, source_id):
        self.calls.append("pg")

    def workspace_tree(self, source_id, workspace_id):
        self.calls.append("workspace_tree")

    def source_tree(self, source_id):
        self.calls.append("source_tree")
        if self.fail_step == "files":
            raise DeletionStepError("目录删除失败")
        return {"status": "removed"}

    def _as_deps(self):
        return {
            "milvus_ops_factory": self.milvus_ops_factory,
            "neo4j_ops_factory": self.neo4j_ops_factory,
            "pg_purge": self.pg_purge,
            "workspace_tree": self.workspace_tree,
            "source_tree": self.source_tree,
        }


@pytest.fixture()
def env(monkeypatch, tmp_path):
    session = ServiceFakeSession()
    lock = FakeLock()

    @contextmanager
    def fake_platform_session(_settings):
        yield session

    monkeypatch.setattr(service, "platform_session", fake_platform_session)
    monkeypatch.setattr(task_lock, "connect_task_lock", lock.connect)
    monkeypatch.setattr(task_lock, "try_acquire", lock.try_acquire)
    monkeypatch.setattr(task_lock, "release", lock.release)
    # 锁连接探测默认通过（专用连接由 FakeLock 管理）；失锁场景单独覆写
    monkeypatch.setattr(task_lock, "ensure_lock_connection_alive", lambda _conn: None)

    config = {
        "vector_database": {
            "active": "milvus",
            "providers": {
                "milvus": {"host": "m", "port": 19530, "database": "rehearsal"}
            },
        },
        "loaders": {
            "table_schema_loader": {"collection_name": "table_schema_embeddings"},
            "dim_loader": {"collection_name": "dim_value_embeddings"},
            "sql_loader": {"collection_name": "sql_example_embeddings"},
            "cql_loader": {
                "neo4j": {
                    "uri": "bolt://g:7687",
                    "user": "neo4j",
                    "password": "p",
                    "database": "neo4j",
                }
            },
        },
    }
    monkeypatch.setattr(service, "load_main_config_snapshot", lambda _path: config)
    monkeypatch.setattr(service, "clean_milvus_source", lambda ops, sid, cols: {})
    monkeypatch.setattr(service, "clean_neo4j_source", lambda ops, sid: {})
    return SimpleNamespace(session=session, lock=lock, config=config)


def test_submit_source_deletion_sets_deleting_and_creates_task(env):
    result = service.submit_source_deletion(SETTINGS, SOURCE_ID)
    assert result["action"] == "delete_source"
    assert result["status"] == "pending"
    assert env.session.source.deletion_state == "deleting"
    assert len(env.session.tasks) == 1


def test_submit_rejects_unfinished_runs(env):
    env.session.collection_runs.append(
        SimpleNamespace(
            collection_run_id=uuid.UUID(int=1),
            collection_job_id="cj_1",
            status="running",
            created_at=NOW,
            started_at=NOW,
        )
    )
    with pytest.raises(ResourceHasUnfinishedRunsError) as excinfo:
        service.submit_source_deletion(SETTINGS, SOURCE_ID)
    assert excinfo.value.payload["blocking_runs"]
    assert env.session.source.deletion_state == "active"


def test_submit_same_target_returns_existing_task_and_different_action_conflicts(env):
    first = service.submit_source_deletion(SETTINGS, SOURCE_ID)
    again = service.submit_source_deletion(SETTINGS, SOURCE_ID)
    assert again["task_id"] == first["task_id"]
    assert len(env.session.tasks) == 1
    with pytest.raises(DeletionInProgressError):
        service.submit_knowledge_clear(SETTINGS, SOURCE_ID)


def test_submit_workspace_deletion_marks_workspace_only(env):
    result = service.submit_workspace_deletion(SETTINGS, WS_ID)
    assert result["action"] == "delete_workspace"
    assert env.session.workspaces[0].deletion_state == "deleting"
    assert env.session.source.deletion_state == "active"


def test_submit_knowledge_clear_keeps_source_active(env):
    result = service.submit_knowledge_clear(SETTINGS, SOURCE_ID)
    assert result["action"] == "clear_source_knowledge"
    assert env.session.source.deletion_state == "active"


def _submit(env, action):
    if action == "delete_source":
        return service.submit_source_deletion(SETTINGS, SOURCE_ID)
    if action == "delete_workspace":
        return service.submit_workspace_deletion(SETTINGS, WS_ID)
    return service.submit_knowledge_clear(SETTINGS, SOURCE_ID)


def test_execute_source_deletion_full_flow(env):
    submitted = _submit(env, "delete_source")
    deps = FakeDeps()
    result = service.execute_task(SETTINGS, submitted["task_id"], deps=deps._as_deps())
    assert result["status"] == "success"
    # §5.3 顺序：Milvus → Neo4j → PG → 文件
    assert deps.calls == ["milvus_connect", "neo4j_connect", "pg", "source_tree"]
    source = env.session.source
    assert source.deletion_state == "deleted" and source.deleted_at == NOW
    # §3.5 字段清单：连接字段清空、is_active false、显示名保留
    assert source.host is None and source.password_ciphertext is None
    assert source.selected_schemas is None and source.description is None
    assert source.is_active is False
    assert source.display_name == "源"
    assert env.session.workspaces[0].deletion_state == "deleted"
    assert env.session.workspaces[0].deleted_at == NOW
    assert env.session.collection_jobs[0].deleted_at == NOW
    assert env.session.publish_jobs[0].deleted_at == NOW


def test_execute_workspace_deletion_marks_workspace_and_jobs(env):
    submitted = _submit(env, "delete_workspace")
    deps = FakeDeps()
    result = service.execute_task(SETTINGS, submitted["task_id"], deps=deps._as_deps())
    assert result["status"] == "success"
    assert deps.calls == ["workspace_tree"]
    ws = env.session.workspaces[0]
    assert ws.deletion_state == "deleted" and ws.deleted_at == NOW
    assert env.session.collection_jobs[0].deleted_at == NOW
    assert env.session.source.deletion_state == "active"


def test_execute_knowledge_clear_keeps_source_active(env):
    submitted = _submit(env, "clear_source_knowledge")
    deps = FakeDeps()
    result = service.execute_task(SETTINGS, submitted["task_id"], deps=deps._as_deps())
    assert result["status"] == "success"
    assert deps.calls == ["milvus_connect", "neo4j_connect", "pg"]
    assert env.session.source.deletion_state == "active"
    assert env.session.workspaces[0].deletion_state == "active"


def test_execute_step_failure_marks_failed_and_delete_failed(env):
    submitted = _submit(env, "delete_source")
    deps = FakeDeps(fail_step="files")
    result = service.execute_task(SETTINGS, submitted["task_id"], deps=deps._as_deps())
    assert result["status"] == "failed"
    assert "目录删除失败" in result["error"]
    assert env.session.source.deletion_state == "delete_failed"
    task = env.session.tasks[0]
    assert task.status == "failed"
    assert env.session.workspaces[0].deletion_state == "active"


def test_execute_rejects_fingerprint_change(env):
    submitted = _submit(env, "clear_source_knowledge")
    deps = FakeDeps()
    service.execute_task(SETTINGS, submitted["task_id"], deps=deps._as_deps())
    # 重新入队后再执行时配置已变（指纹不同）→ 明确报错
    task = env.session.tasks[0]
    task.status = "failed"
    service.retry_task(SETTINGS, task.task_id)
    env.config["vector_database"]["providers"]["milvus"]["database"] = "other"
    result = service.execute_task(SETTINGS, task.task_id, deps=deps._as_deps())
    assert result["status"] == "failed"
    assert "目标存储配置已变化" in result["error"]


def test_retry_respects_pending_protection_period(env):
    submitted = _submit(env, "delete_source")
    # 刚入队 5 秒 → 保护期内拒绝
    env.session.db_now = NOW + timedelta(seconds=5)
    with pytest.raises(TaskPendingClaimError):
        service.retry_task(SETTINGS, submitted["task_id"])


def test_retry_failed_task_requeues_and_restores_deleting(env):
    _submit(env, "delete_source")
    task = env.session.tasks[0]
    task.status = "failed"
    env.session.source.deletion_state = "delete_failed"
    env.session.db_now = NOW + timedelta(minutes=5)
    result = service.retry_task(SETTINGS, task.task_id)
    assert result["status"] == "pending"
    assert task.queued_at == env.session.db_now
    assert env.session.source.deletion_state == "deleting"


def _failed_task_with_changed_target(env, action="delete_source"):
    _submit(env, action)
    task = env.session.tasks[0]
    task.status = "failed"
    task.error_message = "milvus 连接失败"
    task.step_results = {
        "_targets": service.resolve_deletion_targets(env.config),
        "milvus": {"old_collection": {"status": "success", "deleted": 2}},
    }
    if action == "delete_source":
        env.session.source.deletion_state = "delete_failed"
    env.config["vector_database"]["providers"]["milvus"]["host"] = "new-milvus"
    return task


def test_retry_changed_target_requires_confirmation_without_mutation(env):
    task = _failed_task_with_changed_target(env)
    previous_steps = deepcopy(task.step_results)
    commits = env.session.commits
    with pytest.raises(DeletionTargetsChangedError) as caught:
        service.retry_task(SETTINGS, task.task_id)
    assert caught.value.payload["previous"] == previous_steps["_targets"]
    assert caught.value.payload["current"]["milvus"]["host"] == "new-milvus"
    assert "password" not in str(caught.value.payload)
    assert task.status == "failed"
    assert task.error_message == "milvus 连接失败"
    assert task.step_results == previous_steps
    assert env.session.source.deletion_state == "delete_failed"
    assert env.session.commits == commits
    assert env.lock.held == set()


@pytest.mark.parametrize("action", ["delete_source", "clear_source_knowledge"])
def test_confirm_changed_target_requeues_archives_and_cleans_new_target(env, action):
    task = _failed_task_with_changed_target(env, action)
    old_steps = deepcopy(task.step_results)
    current = service.resolve_deletion_targets(env.config)
    result = service.retry_task(
        SETTINGS, task.task_id,
        target_confirmation={
            "previous_fingerprint": old_steps["_targets"]["fingerprint"],
            "current_fingerprint": current["fingerprint"],
        },
    )
    assert result["status"] == "pending"
    assert task.error_message is None
    assert task.step_results["_targets"] == current
    assert "milvus" not in task.step_results
    history = task.step_results["_target_changes"]
    assert history[0]["previous"] == old_steps["_targets"]
    assert history[0]["previous_steps"] == {"milvus": old_steps["milvus"]}
    assert history[0]["confirmed_at"] == NOW.isoformat()
    deps = FakeDeps()
    second = service.execute_task(SETTINGS, task.task_id, deps=deps._as_deps())
    assert second["status"] == "success"
    assert deps.calls[:3] == ["milvus_connect", "neo4j_connect", "pg"]
    assert env.session.source.deletion_state == ("deleted" if action == "delete_source" else "active")


@pytest.mark.parametrize("changed_again", ["configuration", "task"])
def test_stale_target_confirmation_is_rejected(env, changed_again):
    task = _failed_task_with_changed_target(env)
    confirmation = {
        "previous_fingerprint": task.step_results["_targets"]["fingerprint"],
        "current_fingerprint": service.resolve_deletion_targets(env.config)["fingerprint"],
    }
    if changed_again == "configuration":
        env.config["loaders"]["cql_loader"]["neo4j"]["database"] = "another"
    else:
        task.step_results["_targets"]["fingerprint"] = "f" * 16
    before = deepcopy(task.step_results)
    with pytest.raises(DeletionTargetsChangedError):
        service.retry_task(SETTINGS, task.task_id, target_confirmation=confirmation)
    assert task.step_results == before
    assert task.status == "failed"


def test_configuration_change_after_confirmation_stops_before_cleaning(env):
    task = _failed_task_with_changed_target(env)
    service.retry_task(SETTINGS, task.task_id, target_confirmation={
        "previous_fingerprint": task.step_results["_targets"]["fingerprint"],
        "current_fingerprint": service.resolve_deletion_targets(env.config)["fingerprint"],
    })
    env.config["vector_database"]["providers"]["milvus"]["host"] = "third-milvus"
    deps = FakeDeps()
    result = service.execute_task(SETTINGS, task.task_id, deps=deps._as_deps())
    assert result["status"] == "failed"
    assert "目标存储配置已变化" in result["error"]
    assert deps.calls == []


def test_retry_workspace_does_not_read_target_configuration(env, monkeypatch):
    _submit(env, "delete_workspace")
    task = env.session.tasks[0]
    task.status = "failed"
    def forbidden_load(_path):
        raise AssertionError("工作区重试不读取存储配置")
    monkeypatch.setattr(service, "load_main_config_snapshot", forbidden_load)
    assert service.retry_task(SETTINGS, task.task_id)["status"] == "pending"


def _retry_api_client(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from backend.api.errors import register_exception_handlers
    from backend.api.routers.data_sources import get_platform_settings
    from backend.api.routers.workspaces import router
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_platform_settings] = lambda: SETTINGS
    register_exception_handlers(app)
    calls = []
    monkeypatch.setattr(service, "execute_task", lambda settings, task_id: calls.append(task_id))
    return TestClient(app), calls


def test_retry_api_prompts_then_only_dispatches_after_confirmation(env, monkeypatch):
    task = _failed_task_with_changed_target(env)
    client, calls = _retry_api_client(monkeypatch)
    path = f"/api/deletion-tasks/{task.task_id}/retry"
    prompt = client.post(path)
    assert prompt.status_code == 409
    assert prompt.json()["code"] == "deletion_targets_changed"
    assert calls == []
    assert task.status == "failed"
    details = prompt.json()["details"]
    confirmed = client.post(path, json={"target_confirmation": {
        "previous_fingerprint": details["previous"]["fingerprint"],
        "current_fingerprint": details["current"]["fingerprint"],
    }})
    assert confirmed.status_code == 202
    assert confirmed.json()["error_message"] is None
    assert len(calls) == 1


def test_retry_api_without_body_remains_supported(env, monkeypatch):
    _submit(env, "delete_source")
    task = env.session.tasks[0]
    task.status = "failed"
    client, calls = _retry_api_client(monkeypatch)
    assert client.post(f"/api/deletion-tasks/{task.task_id}/retry").status_code == 202
    assert len(calls) == 1


@pytest.mark.parametrize("body", [
    {"target_confirmation": {"current_fingerprint": "a" * 16}},
    {"target_confirmation": {"previous_fingerprint": "a" * 16, "current_fingerprint": "invalid"}},
    {"target_confirmation": {"previous_fingerprint": "a" * 16, "current_fingerprint": "b" * 16, "host": "injected"}},
    {"force": True},
])
def test_retry_api_rejects_invalid_confirmation_without_dispatch(env, monkeypatch, body):
    task = _failed_task_with_changed_target(env)
    client, calls = _retry_api_client(monkeypatch)
    assert client.post(f"/api/deletion-tasks/{task.task_id}/retry", json=body).status_code == 422
    assert task.status == "failed"
    assert calls == []


def test_startup_scan_interrupts_stale_running_and_expired_pending(env):
    _submit(env, "delete_source")
    task = env.session.tasks[0]
    # pending 未过期 → 跳过
    report = service.startup_scan(SETTINGS)
    assert report == {"interrupted": 0}
    # 过期 pending → 标 failed（认领等待超时）
    env.session.db_now = NOW + timedelta(seconds=30)
    report = service.startup_scan(SETTINGS)
    assert report["interrupted"] == 1
    assert task.status == "failed"
    assert "认领等待超时" in task.error_message
    assert env.session.source.deletion_state == "delete_failed"
    # running 遗留 → 标 failed（执行已中断）
    service.retry_task(SETTINGS, task.task_id)
    task.status = "running"
    env.session.source.deletion_state = "deleting"
    report = service.startup_scan(SETTINGS)
    assert report["interrupted"] == 1
    assert task.status == "failed"
    assert "执行已中断" in task.error_message


def test_task_detail_can_retry_matrix(env):
    _submit(env, "delete_source")
    task = env.session.tasks[0]
    detail = service.task_detail(SETTINGS, task.task_id)
    assert detail["can_retry"] is False and detail["retry_reason"] == "正在排队"

    env.session.db_now = NOW + timedelta(seconds=30)
    detail = service.task_detail(SETTINGS, task.task_id)
    assert detail["can_retry"] is True

    task.status = "running"
    detail = service.task_detail(SETTINGS, task.task_id)
    assert detail["can_retry"] is True and detail["retry_reason"] == "检查并重试"

    task.status = "success"
    detail = service.task_detail(SETTINGS, task.task_id)
    assert detail["can_retry"] is False


def test_preview_source_deletion_counts_and_runs(env):
    preview = service.preview_source_deletion(SETTINGS, SOURCE_ID)
    assert preview["workspace_count"] == 1
    assert preview["collection_job_count"] == 1
    assert preview["publish_job_count"] == 1
    assert preview["blocking_runs"] == []
    assert preview["targets"]["milvus"]["database"] == "rehearsal"


def test_execute_reads_config_once_for_fingerprint_and_connections(env, monkeypatch):
    """doc 8.1 §7（审查收紧）：指纹与连接共用一次配置读取。"""
    calls = {"count": 0}

    def counting_load(_path):
        calls["count"] += 1
        return env.config

    monkeypatch.setattr(service, "load_main_config_snapshot", counting_load)
    submitted = _submit(env, "clear_source_knowledge")
    deps = FakeDeps()
    result = service.execute_task(SETTINGS, submitted["task_id"], deps=deps._as_deps())
    assert result["status"] == "success"
    assert calls["count"] == 1  # 全程一次：指纹 + 连接工厂共用


def test_default_deps_factories_use_passed_snapshot(env, monkeypatch):
    """连接工厂消费执行快照，不再自行读 YAML（检查 A、连接 A）。"""
    recorded = {}

    class RecordingMilvus:
        def __init__(self, providers):
            recorded["milvus"] = providers

    class RecordingNeo4j:
        def __init__(self, section):
            recorded["neo4j"] = section

    monkeypatch.setattr(service, "MilvusCleanerOps", RecordingMilvus)
    monkeypatch.setattr(service, "Neo4jCleanerOps", RecordingNeo4j)

    def forbidden_load(_path):
        raise AssertionError("连接工厂不得再次读取配置")

    monkeypatch.setattr(service, "load_main_config_snapshot", forbidden_load)

    deps = service.default_deps(env.config)
    deps["milvus_ops_factory"]()
    deps["neo4j_ops_factory"]()

    assert recorded["milvus"] is env.config["vector_database"]["providers"]["milvus"]
    assert recorded["neo4j"]["uri"] == "bolt://g:7687"
    assert recorded["neo4j"]["database"] == "neo4j"


def test_retry_target_change_rejects_before_any_side_effect(env):
    """重试时目标变化：在产生删除副作用之前拒绝（deps 无新增调用）。"""
    submitted = _submit(env, "clear_source_knowledge")
    deps = FakeDeps()
    service.execute_task(SETTINGS, submitted["task_id"], deps=deps._as_deps())
    calls_after_first = list(deps.calls)

    task = env.session.tasks[0]
    task.status = "failed"
    env.session.db_now = NOW + timedelta(minutes=5)
    service.retry_task(SETTINGS, task.task_id)
    env.config["vector_database"]["providers"]["milvus"]["database"] = "other"

    result = service.execute_task(SETTINGS, task.task_id, deps=deps._as_deps())
    assert result["status"] == "failed"
    assert "目标存储配置已变化" in result["error"]
    # 指纹核对失败发生在 milvus/neo4j/pg 任何清理之前
    assert deps.calls == calls_after_first


def test_lock_connection_loss_stops_all_followup_actions(env, monkeypatch):
    """§7.1：第一步后锁连接失效——后续步骤、终态标记、成败写入均不发生。"""
    from backend.deletion.task_lock import TaskLockLostError

    submitted = _submit(env, "delete_source")
    deps = FakeDeps()
    probes = {"count": 0}

    def flaky_ensure(_conn):
        probes["count"] += 1
        if probes["count"] >= 3:  # 第 1 步（milvus）完成并记录后、第 2 步前失效
            raise TaskLockLostError("任务执行锁连接已失效（OperationalError）")

    monkeypatch.setattr(task_lock, "ensure_lock_connection_alive", flaky_ensure)

    result = service.execute_task(SETTINGS, submitted["task_id"], deps=deps._as_deps())
    assert result["status"] == "lock_lost"
    assert "锁连接已失效" in result["error"]
    # 仅第 1 步执行；neo4j/pg/文件与最终删除标记全部未发生
    assert deps.calls == ["milvus_connect"]
    assert env.session.source.deletion_state == "deleting"
    assert env.session.workspaces[0].deletion_state == "active"
    # 旧执行者不得改写任务成败；连接锁已释放
    assert env.session.tasks[0].status == "running"
    assert env.lock.held == set()


def test_workspace_deletion_ignores_rag_config_entirely(env, monkeypatch):
    """§审查：纯目录删除不读 metadata_config——RAG 配置缺失/非法也能删。"""
    calls = {"load": 0}

    def forbidden_load(_path):
        calls["load"] += 1
        raise ValueError("Milvus/Neo4j 配置缺失，解析必失败")

    monkeypatch.setattr(service, "load_main_config_snapshot", forbidden_load)
    submitted = _submit(env, "delete_workspace")

    deps = FakeDeps()
    # 仅提供目录清理依赖（无 milvus/neo4j 工厂）
    result = service.execute_task(
        SETTINGS,
        submitted["task_id"],
        deps={"workspace_tree": deps.workspace_tree},
    )
    assert result["status"] == "success"
    assert deps.calls == ["workspace_tree"]
    assert calls["load"] == 0  # 未读 RAG 配置、未调用 RAG 连接工厂
    assert env.session.workspaces[0].deletion_state == "deleted"


def test_workspace_deletion_retry_survives_rag_config_change(env):
    """工作区删除失败后 RAG 配置被改：重试不被指纹拒绝，目录照常清理。"""
    submitted = _submit(env, "delete_workspace")
    boom = {"armed": True}

    def flaky_tree(source_id, workspace_id):
        if boom["armed"]:
            boom["armed"] = False
            raise RuntimeError("目录删除临时失败")
        return {"status": "removed"}

    deps = FakeDeps()
    first = service.execute_task(
        SETTINGS, submitted["task_id"], deps={"workspace_tree": flaky_tree}
    )
    assert first["status"] == "failed"

    env.session.db_now = NOW + timedelta(minutes=5)
    service.retry_task(SETTINGS, submitted["task_id"])
    # RAG 配置变化（对工作区删除无关，不应触发指纹拒绝）
    env.config["vector_database"]["providers"]["milvus"]["database"] = "changed"
    second = service.execute_task(
        SETTINGS, submitted["task_id"], deps={"workspace_tree": deps.workspace_tree}
    )
    assert second["status"] == "success"
    assert env.session.workspaces[0].deletion_state == "deleted"


def test_workspace_deletion_production_default_path(env, monkeypatch):
    """审查回归：不注入 deps 的生产调用——默认 clean_workspace_tree 生效。"""
    recorded = {}

    def recording_tree(source_id, workspace_id):
        recorded["args"] = (source_id, workspace_id)
        return {"status": "removed"}

    monkeypatch.setattr(service, "clean_workspace_tree", recording_tree)
    submitted = _submit(env, "delete_workspace")

    result = service.execute_task(SETTINGS, submitted["task_id"])  # 不传 deps

    assert result["status"] == "success"
    assert recorded["args"] == (SOURCE_ID, WS_ID)
    assert env.session.workspaces[0].deletion_state == "deleted"
    task = env.session.tasks[0]
    assert task.status == "success"
    assert task.step_results["files"] == {"status": "removed"}


def test_step_error_with_simultaneous_lock_loss_skips_fail_writes(env, monkeypatch):
    """§7.1：步骤报错与失锁同时发生——不 _fail、不改任务/对象状态。"""
    from backend.deletion.task_lock import TaskLockLostError

    submitted = _submit(env, "delete_source")
    probes = {"count": 0}

    def flaky_ensure(_conn):
        probes["count"] += 1
        if probes["count"] >= 2:  # 第 1 步执行中连接已死（步骤自身也报错）
            raise TaskLockLostError("任务执行锁连接已失效（OperationalError）")

    monkeypatch.setattr(task_lock, "ensure_lock_connection_alive", flaky_ensure)

    def exploding_milvus(*_a, **_k):
        raise RuntimeError("Milvus 清理故障")

    deps = FakeDeps()
    deps_dict = deps._as_deps()
    deps_dict["milvus_ops_factory"] = exploding_milvus

    result = service.execute_task(SETTINGS, submitted["task_id"], deps=deps_dict)
    assert result["status"] == "lock_lost"
    # 旧执行者未写失败：任务保持 running，源未被置 delete_failed
    assert env.session.tasks[0].status == "running"
    assert env.session.source.deletion_state == "deleting"
    assert env.session.tasks[0].error_message is None


def test_step_error_with_valid_lock_still_fails_normally(env):
    """锁连接有效时，同一失败路径正常收尾（任务 failed + 源 delete_failed）。"""
    submitted = _submit(env, "delete_source")

    def exploding_milvus(*_a, **_k):
        raise RuntimeError("Milvus 清理故障")

    deps = FakeDeps()._as_deps()
    deps["milvus_ops_factory"] = exploding_milvus

    result = service.execute_task(SETTINGS, submitted["task_id"], deps=deps)
    assert result["status"] == "failed"
    assert "Milvus 清理故障" in result["error"]
    assert env.session.tasks[0].status == "failed"
    assert env.session.source.deletion_state == "delete_failed"
