"""doc 8.1 §3.3/§7.3：运行创建必须经完整层级守卫（审查修复 4）。

已删除作业/父级 → 410；删除中/失败 → 409；兄弟工作区不受影响；
拒绝时不留 pending 运行。
"""

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from backend.deletion.errors import (
    ParentResourceUnavailableError,
    ResourceDeletedError,
)
from backend.jobs.collection_run import create_collection_run
from backend.persistence.models import (
    CollectionJobInfo,
    CollectionJobRevision,
    CollectionRunInfo,
    DbSourceInfo,
    PublishJobInfo,
    PublishJobRevision,
    PublishRunInfo,
    ResourceDeletionTask,
    WorkspaceInfo,
)
from backend.persistence.platform_db import PlatformSettings
from backend.publishing.publish_service import create_publish_run

SETTINGS = PlatformSettings(
    host="p", port=5432, name="metaweave", user="u", password="s"
)
NOW = datetime(2026, 10, 29, tzinfo=timezone.utc)


class RunGuardSession:
    def __init__(self, *, job_deleted=False, ws_state="active", source=None):
        self.source = source or SimpleNamespace(
            source_id="db_src_1",
            deleted_at=None,
            deletion_state="active",
            is_active=True,
            db_name="orders",
            updated_at=NOW,
            selected_schemas=["public"],
        )
        self.workspaces = [
            SimpleNamespace(
                workspace_id="ws_1",
                source_id="db_src_1",
                deleted_at=None,
                deletion_state=ws_state,
            ),
            SimpleNamespace(
                workspace_id="ws_2",
                source_id="db_src_1",
                deleted_at=None,
                deletion_state="active",
            ),
        ]
        self.collection_jobs = [
            SimpleNamespace(
                collection_job_id="coll_job_1",
                workspace_id="ws_1",
                job_name="采集",
                deleted_at=NOW if job_deleted else None,
            ),
            SimpleNamespace(
                collection_job_id="coll_job_2",
                workspace_id="ws_2",
                job_name="兄弟",
                deleted_at=None,
            ),
        ]
        self.publish_jobs = [
            SimpleNamespace(
                publish_job_id="pub_job_1",
                workspace_id="ws_1",
                job_name="发布",
                deleted_at=NOW if job_deleted else None,
            ),
        ]
        self.revisions = [
            SimpleNamespace(
                collection_job_id="coll_job_1",
                revision_no=1,
                steps=["ddl"],
                step_config=None,
            ),
            SimpleNamespace(
                collection_job_id="coll_job_2",
                revision_no=1,
                steps=["ddl"],
                step_config=None,
            ),
            SimpleNamespace(
                publish_job_id="pub_job_1",
                revision_no=1,
                steps=["db"],
                step_config=None,
            ),
        ]
        self.collection_runs = []
        self.publish_runs = []
        self.publish_jobs_pool = self.publish_jobs
        self.tasks = []
        self.commits = 0

    def _pool(self, model):
        return {
            DbSourceInfo: [self.source],
            WorkspaceInfo: self.workspaces,
            CollectionJobInfo: self.collection_jobs,
            PublishJobInfo: self.publish_jobs_pool,
            CollectionRunInfo: self.collection_runs,
            PublishRunInfo: self.publish_runs,
            ResourceDeletionTask: self.tasks,
        }.get(model, [])

    def get(self, model, key, **_kwargs):
        pk = {
            DbSourceInfo: "source_id",
            WorkspaceInfo: "workspace_id",
            CollectionJobInfo: "collection_job_id",
            PublishJobInfo: "publish_job_id",
            CollectionRunInfo: "collection_run_id",
        }.get(model)
        for row in self._pool(model):
            if pk and getattr(row, pk) == key:
                return row
        return None

    def execute(self, statement):
        from sqlalchemy.sql.elements import TextClause

        if isinstance(statement, TextClause):
            # doc 10.2：准入 advisory lock，假件不建模
            return SimpleNamespace(scalar_one_or_none=lambda: None)
        if getattr(statement, "_setup_joins", None):
            # 含 doc 10.2 §5.2 准入查询（.first()）：无活动运行
            return SimpleNamespace(
                scalar_one_or_none=lambda: None,
                first=lambda: None,
                all=lambda: [],
                scalar_one=lambda: 0,
            )
        columns = [c["entity"] for c in statement.column_descriptions]
        if columns and columns[0] in (CollectionJobRevision, PublishJobRevision):
            params = statement.compile().params
            job_id = params.get("collection_job_id_1") or params.get("publish_job_id_1")
            revision_no = params.get("revision_no_1")
            hit = next(
                (
                    r
                    for r in self.revisions
                    if getattr(
                        r, "collection_job_id", getattr(r, "publish_job_id", None)
                    )
                    == job_id
                    and r.revision_no == revision_no
                ),
                None,
            )
            return SimpleNamespace(scalar_one_or_none=lambda: hit)
        rows = list(self._pool(columns[0])) if columns else []
        return SimpleNamespace(
            fetchall=lambda: rows,
            all=lambda: rows,
            scalar_one_or_none=lambda: rows[0] if rows else None,
        )

    def scalars(self, statement):
        columns = [c["entity"] for c in statement.column_descriptions]
        return iter(list(self._pool(columns[0])) if columns else [])

    def scalar(self, _statement):
        return NOW

    def add(self, row):
        pool = self._pool(type(row))
        if pool is not None:
            pool.append(row)

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass


@pytest.fixture()
def wire(monkeypatch):
    from contextlib import contextmanager

    import backend.jobs.collection_run as collection_run
    import backend.publishing.publish_service as publish_service

    holder = SimpleNamespace(session=None)

    @contextmanager
    def fake_ps(_settings):
        yield holder.session

    monkeypatch.setattr(collection_run, "platform_session", fake_ps)
    monkeypatch.setattr(publish_service, "platform_session", fake_ps)
    # 采集执行入口的目录/marker 探测与运行创建打桩：守卫拒绝时不落库
    monkeypatch.setattr(
        collection_run,
        "create_run",
        lambda *a, **k: SimpleNamespace(collection_run_id=None),
    )
    return holder


def test_deleted_job_cannot_create_collection_run_and_no_rows(wire):
    wire.session = RunGuardSession(job_deleted=True)
    with pytest.raises(ResourceDeletedError):
        create_collection_run(
            SETTINGS,
            "coll_job_1",
            revision_no=1,
            mode="default",
            steps=None,
            step_config=None,
        )
    assert wire.session.collection_runs == []


def test_deleted_job_cannot_create_publish_run(wire):
    wire.session = RunGuardSession(job_deleted=True)
    with pytest.raises(ResourceDeletedError):
        create_publish_run(
            SETTINGS,
            "pub_job_1",
            revision_no=1,
            mode="default",
            steps=None,
            step_config=None,
        )
    assert wire.session.publish_runs == []


def test_deleted_workspace_blocks_run_of_same_workspace_only(wire):
    wire.session = RunGuardSession(ws_state="deleting")
    # 父工作区删除中 → 本工作区作业 409
    with pytest.raises(ParentResourceUnavailableError):
        create_collection_run(
            SETTINGS,
            "coll_job_1",
            revision_no=1,
            mode="default",
            steps=None,
            step_config=None,
        )
    # 兄弟工作区 B 的作业不受影响（守卫通过，进入后续请求解析）
    wire.session2 = None
    result = create_collection_run(
        SETTINGS,
        "coll_job_2",
        revision_no=1,
        mode="default",
        steps=None,
        step_config=None,
    )
    assert result is not None


def test_deleted_source_blocks_run_creation(wire):
    source = SimpleNamespace(
        source_id="db_src_1",
        deleted_at=NOW,
        deletion_state="deleted",
        is_active=True,
        db_name="orders",
        updated_at=NOW,
        selected_schemas=["public"],
    )
    wire.session = RunGuardSession(source=source)
    with pytest.raises(ResourceDeletedError):
        create_collection_run(
            SETTINGS,
            "coll_job_1",
            revision_no=1,
            mode="default",
            steps=None,
            step_config=None,
        )
