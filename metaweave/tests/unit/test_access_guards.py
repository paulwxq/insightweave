"""doc 8.1 §7.3/§8.1 统一访问守卫：404/410/409 判定收口（FakeSession）。"""

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from backend.access_guards import require_job, require_source, require_workspace
from backend.deletion.errors import (
    ParentResourceUnavailableError,
    ResourceDeletedError,
    ResourceNotFoundError,
    SourceKnowledgeFrozenError,
)
from backend.persistence.models import (
    CollectionJobInfo,
    DbSourceInfo,
    PublishJobInfo,
    ResourceDeletionTask,
    WorkspaceInfo,
)

NOW = datetime(2026, 10, 29, tzinfo=timezone.utc)


def _source(**overrides):
    base = dict(
        source_id="db_src_1",
        deleted_at=None,
        deletion_state="active",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _workspace(**overrides):
    base = dict(
        workspace_id="ws_1",
        source_id="db_src_1",
        deleted_at=None,
        deletion_state="active",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _task(**overrides):
    base = dict(
        task_id=uuid.UUID(int=7),
        action="clear_source_knowledge",
        target_id="db_src_1",
        source_id="db_src_1",
        workspace_id=None,
        status="pending",
        error_message=None,
        created_at=NOW,
        queued_at=NOW,
        started_at=None,
        finished_at=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class GuardSession:
    def __init__(self, source=None, workspace=None, jobs=None, tasks=None):
        self.source = source
        self.workspace = workspace
        self.collection_jobs = jobs or []
        self.publish_jobs = []
        self.tasks = tasks or []
        self.workspaces = [workspace] if workspace else []

    def get(self, model, key, **_kwargs):
        pools = {
            DbSourceInfo: [self.source] if self.source else [],
            WorkspaceInfo: self.workspaces,
            CollectionJobInfo: self.collection_jobs,
            PublishJobInfo: self.publish_jobs,
        }
        for row in pools[model]:
            pk = (
                row.workspace_id
                if model is WorkspaceInfo
                else row.collection_job_id
                if model is CollectionJobInfo
                else row.publish_job_id
                if model is PublishJobInfo
                else row.source_id
            )
            if pk == key:
                return row
        return None

    def execute(self, statement):
        columns = [c["entity"] for c in statement.column_descriptions]
        rows = [
            {ResourceDeletionTask: self.tasks}.get(columns[0], []),
        ][0]
        return SimpleNamespace(fetchall=lambda: list(rows))

    def scalar(self, _statement):
        return NOW

    def scalars(self, statement):
        from tests.unit.deletion_fakes import scalars_from_pool

        columns = [c["entity"] for c in statement.column_descriptions]
        if columns[0] is ResourceDeletionTask:
            return scalars_from_pool(self.tasks, statement)
        return iter([])

    def add(self, _row):
        pass

    def commit(self):
        pass


def test_source_guard_404_and_410():
    with pytest.raises(ResourceNotFoundError):
        require_source(GuardSession(source=None), "db_src_1")
    deleted = _source(deleted_at=NOW, deletion_state="deleted")
    with pytest.raises(ResourceDeletedError):
        require_source(GuardSession(source=deleted), "db_src_1")


def test_source_write_guard_blocks_deleting_state_and_clear_task():
    deleting = _source(deletion_state="deleting")
    with pytest.raises(ParentResourceUnavailableError):
        require_source(GuardSession(source=deleting), "db_src_1", for_write=True)
    # 只读允许 deleting 状态（§8.1 只读守卫）
    assert require_source(GuardSession(source=deleting), "db_src_1") is deleting

    active = _source()
    frozen = GuardSession(
        source=active,
        tasks=[_task(status="running", action="clear_source_knowledge")],
    )
    with pytest.raises(SourceKnowledgeFrozenError):
        require_source(frozen, "db_src_1", for_write=True)
    # 独立知识清理失败待重试同样冻结（§7.3 规则 2）
    frozen_failed = GuardSession(
        source=active,
        tasks=[_task(status="failed", action="clear_source_knowledge")],
    )
    with pytest.raises(SourceKnowledgeFrozenError):
        require_source(frozen_failed, "db_src_1", for_write=True)


def test_workspace_task_does_not_freeze_source_write():
    """§7.3 规则 3：delete_workspace 任务不整源冻结。"""
    active = _source()
    session = GuardSession(
        source=active,
        workspace=_workspace(),
        tasks=[
            _task(
                action="delete_workspace",
                target_id="ws_2",
                workspace_id="ws_2",
            )
        ],
    )
    assert require_source(session, "db_src_1", for_write=True) is active


def test_workspace_guard_ancestor_states():
    deleted_ws = _workspace(deleted_at=NOW, deletion_state="deleted")
    with pytest.raises(ResourceDeletedError):
        require_workspace(
            GuardSession(source=_source(), workspace=deleted_ws), "ws_1"
        )
    ancestor_deleted = GuardSession(
        source=_source(deleted_at=NOW, deletion_state="deleted"),
        workspace=_workspace(),
    )
    with pytest.raises(ResourceDeletedError):
        require_workspace(ancestor_deleted, "ws_1")

    parent_deleting = GuardSession(
        source=_source(deletion_state="deleting"), workspace=_workspace()
    )
    with pytest.raises(ParentResourceUnavailableError):
        require_workspace(parent_deleting, "ws_1", for_write=True)

    ws_deleting = GuardSession(
        source=_source(), workspace=_workspace(deletion_state="deleting")
    )
    with pytest.raises(ParentResourceUnavailableError):
        require_workspace(ws_deleting, "ws_1", for_write=True)
    # 只读不受冻结影响
    assert require_workspace(ws_deleting, "ws_1") is ws_deleting.workspace


def test_job_guard_checks_job_workspace_source():
    job = SimpleNamespace(
        collection_job_id="cj_1",
        workspace_id="ws_1",
        deleted_at=None,
    )
    deleted_job = SimpleNamespace(
        collection_job_id="cj_1", workspace_id="ws_1", deleted_at=NOW
    )
    with pytest.raises(ResourceNotFoundError):
        require_job(GuardSession(source=_source(), workspace=_workspace()), "collection", "cj_1")
    with pytest.raises(ResourceDeletedError):
        require_job(
            GuardSession(
                source=_source(), workspace=_workspace(), jobs=[deleted_job]
            ),
            "collection",
            "cj_1",
        )
    session = GuardSession(source=_source(), workspace=_workspace(), jobs=[job])
    assert require_job(session, "collection", "cj_1") is job
    with pytest.raises(ValueError):
        require_job(session, "unknown_kind", "cj_1")
