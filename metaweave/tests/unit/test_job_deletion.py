"""doc 8.1 §5.2 作业逻辑删除：锁内校验未结束运行、写 deleted_at、409/410。"""

from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

from backend.deletion.errors import (
    ResourceDeletedError,
    ResourceHasUnfinishedRunsError,
)
from backend.jobs import job_service
from backend.jobs.job_repository import COLLECTION_JOB, PUBLISH_JOB
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
    host="p", port=5432, name="metaweave", user="u", password="s"
)
NOW = datetime(2026, 10, 29, tzinfo=timezone.utc)


class JobFakeSession:
    def __init__(self):
        self.db_now = NOW
        self.source = SimpleNamespace(
            source_id="db_src_1", deleted_at=None, deletion_state="active"
        )
        self.workspaces = [
            SimpleNamespace(
                workspace_id="ws_1",
                source_id="db_src_1",
                deleted_at=None,
                deletion_state="active",
            )
        ]
        self.collection_jobs = [
            SimpleNamespace(
                collection_job_id="coll_job_1",
                workspace_id="ws_1",
                job_name="采集",
                deleted_at=None,
            )
        ]
        self.publish_jobs = [
            SimpleNamespace(
                publish_job_id="pub_job_1",
                workspace_id="ws_1",
                job_name="发布",
                deleted_at=None,
            )
        ]
        self.collection_runs = []
        self.publish_runs = []
        self.tasks = []
        self.commits = 0

    def _pool(self, model):
        return {
            DbSourceInfo: [self.source],
            WorkspaceInfo: self.workspaces,
            CollectionJobInfo: self.collection_jobs,
            PublishJobInfo: self.publish_jobs,
            CollectionRunInfo: self.collection_runs,
            ResourceDeletionTask: self.tasks,
        }.get(model, [])

    def get(self, model, key, **_kwargs):
        pk = {
            DbSourceInfo: "source_id",
            WorkspaceInfo: "workspace_id",
            CollectionJobInfo: "collection_job_id",
            PublishJobInfo: "publish_job_id",
        }.get(model, "task_id")
        for row in self._pool(model):
            if getattr(row, pk, None) == key:
                return row
        return None

    def scalars(self, statement):
        from tests.unit.deletion_fakes import scalars_from_pool

        columns = [c["entity"] for c in statement.column_descriptions]
        return scalars_from_pool(self._pool(columns[0]), statement)

    def scalar(self, _statement):
        return self.db_now

    def execute(self, statement):
        columns = [c["entity"] for c in statement.column_descriptions]
        rows = list(self._pool(columns[0])) if columns else []
        return SimpleNamespace(fetchall=lambda: rows, all=lambda: rows)

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass

    def add(self, _row):
        pass


@pytest.fixture()
def env(monkeypatch):
    from contextlib import contextmanager

    session = JobFakeSession()

    @contextmanager
    def fake_ps(_settings):
        yield session

    monkeypatch.setattr(job_service, "platform_session", fake_ps)
    return session


def test_delete_collection_job_sets_deleted_at(env):
    job_service.delete_job(SETTINGS, "collection", "coll_job_1")
    assert env.collection_jobs[0].deleted_at == NOW


def test_delete_job_blocked_by_unfinished_run(env):
    env.collection_runs.append(
        SimpleNamespace(
            collection_run_id=uuid4(),
            collection_job_id="coll_job_1",
            status="pending",
            created_at=NOW,
            started_at=None,
        )
    )
    with pytest.raises(ResourceHasUnfinishedRunsError):
        job_service.delete_job(SETTINGS, "collection", "coll_job_1")
    assert env.collection_jobs[0].deleted_at is None


def test_delete_deleted_job_returns_410(env):
    env.collection_jobs[0].deleted_at = NOW
    with pytest.raises(ResourceDeletedError):
        job_service.delete_job(SETTINGS, "collection", "coll_job_1")


def test_delete_publish_job_sets_deleted_at(env):
    job_service.delete_job(SETTINGS, "publish", "pub_job_1")
    assert env.publish_jobs[0].deleted_at == NOW
