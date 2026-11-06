"""doc 11 §4.7：详情接口透出 executor_active（本进程执行者注册表读取）。"""

from contextlib import contextmanager
from types import SimpleNamespace
from uuid import uuid4

from backend.jobs import active_runs
from backend.jobs import run_detail


class FakeSession:
    def get(self, _model, _key):
        return SimpleNamespace(workspace_id="ws_1", job_name="作业")


@contextmanager
def fake_scope(_settings):
    yield FakeSession()


def _patch(monkeypatch, run_id):
    run = SimpleNamespace(
        collection_run_id=run_id,
        publish_run_id=run_id,
        collection_job_id="cj_1",
        publish_job_id="pj_1",
        revision_no=1,
        trigger_type="manual",
        status="running",
        message=None,
        created_at=None,
        started_at=None,
        finished_at=None,
    )
    monkeypatch.setattr(run_detail, "platform_session", fake_scope)
    monkeypatch.setattr(run_detail, "get_run", lambda spec, session, rid: run)
    monkeypatch.setattr(run_detail, "list_run_steps", lambda spec, session, rid: [])


def test_detail_executor_active_false_when_not_registered(monkeypatch):
    run_id = uuid4()
    _patch(monkeypatch, run_id)
    detail = run_detail.get_collection_run_detail(object(), run_id)
    assert detail["executor_active"] is False


def test_detail_executor_active_true_while_registered(monkeypatch):
    run_id = uuid4()
    _patch(monkeypatch, run_id)
    active_runs.register("collection", run_id)
    try:
        detail = run_detail.get_collection_run_detail(object(), run_id)
    finally:
        active_runs.unregister("collection", run_id)
    assert detail["executor_active"] is True


def test_publish_detail_executor_active_true_while_registered(monkeypatch):
    run_id = uuid4()
    _patch(monkeypatch, run_id)
    active_runs.register("publish", run_id)
    try:
        detail = run_detail.get_publish_run_detail(object(), run_id)
    finally:
        active_runs.unregister("publish", run_id)
    assert detail["executor_active"] is True
