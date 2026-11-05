"""doc 8.1 §5.1.1：两处步骤认领必须严格按“源 → 工作区”加锁。

此前先锁工作区再锁源，与删除路径（源 → 工作区）并发会形成反向等待。
本文件用记录加锁序列的假会话锁定该顺序；真实并发验证只在 PG 演练库做。
"""

from types import SimpleNamespace
from uuid import uuid4

import pytest

from backend.jobs.collection_run import _claim_step
from backend.persistence.models import DbSourceInfo, WorkspaceInfo
from backend.persistence.platform_db import PlatformSettings
from backend.publishing.publish_service import _claim_publish_step

SETTINGS = PlatformSettings(
    host="p", port=5432, name="metaweave", user="u", password="s"
)
SOURCE_ID = "db_src_1"
WORKSPACE_ID = "ws_1"


class LockOrderSession:
    """记录 get 调用序列；冲突查询返回无冲突。"""

    def __init__(self):
        self.get_calls = []
        self.commits = 0
        self.rollbacks = 0
        self.workspace_row = SimpleNamespace(
            workspace_id=WORKSPACE_ID,
            source_id=SOURCE_ID,
            deleted_at=None,
            deletion_state="active",
        )
        self.source_row = SimpleNamespace(
            source_id=SOURCE_ID,
            deleted_at=None,
            deletion_state="active",
        )

    def get(self, model, key, **kwargs):
        self.get_calls.append(
            (model.__name__, key, bool(kwargs.get("with_for_update", False)))
        )
        if model is WorkspaceInfo:
            return self.workspace_row if key == WORKSPACE_ID else None
        if model is DbSourceInfo:
            return self.source_row if key == SOURCE_ID else None
        return None

    def execute(self, statement):
        # 两处认领的三表 join 冲突查询：无冲突
        return SimpleNamespace(scalar_one_or_none=lambda: None)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


@pytest.fixture()
def session_env(monkeypatch):
    from contextlib import contextmanager

    session = LockOrderSession()

    @contextmanager
    def fake_ps(_settings):
        yield session

    import backend.jobs.collection_run as collection_run
    import backend.publishing.publish_service as publish_service

    monkeypatch.setattr(collection_run, "platform_session", fake_ps)
    monkeypatch.setattr(publish_service, "platform_session", fake_ps)
    # publish 侧在函数体内 from ... import mark_step_running，补丁打在源模块
    import backend.jobs.run_repository as run_repository

    monkeypatch.setattr(collection_run, "mark_step_running", lambda *a, **k: None)
    monkeypatch.setattr(run_repository, "mark_step_running", lambda *a, **k: None)
    return session


def _locked_sequence(session):
    return [(model, key) for model, key, locked in session.get_calls if locked]


def test_collection_claim_locks_source_before_workspace(session_env):
    result = _claim_step(SETTINGS, WORKSPACE_ID, "ddl", uuid4())
    assert result is None
    assert _locked_sequence(session_env) == [
        ("DbSourceInfo", SOURCE_ID),
        ("WorkspaceInfo", WORKSPACE_ID),
    ]
    assert session_env.commits == 1


def test_publish_claim_locks_source_before_workspace(session_env):
    result = _claim_publish_step(SETTINGS, WORKSPACE_ID, "db", uuid4())
    assert result is None
    assert _locked_sequence(session_env) == [
        ("DbSourceInfo", SOURCE_ID),
        ("WorkspaceInfo", WORKSPACE_ID),
    ]
    assert session_env.commits == 1


def test_claims_probe_workspace_without_lock_first(session_env):
    _claim_step(SETTINGS, WORKSPACE_ID, "ddl", uuid4())
    first = session_env.get_calls[0]
    # §5.1.1：普通查询只用于定位父级，不在此处加锁、不作准入结论
    assert first == ("WorkspaceInfo", WORKSPACE_ID, False)
