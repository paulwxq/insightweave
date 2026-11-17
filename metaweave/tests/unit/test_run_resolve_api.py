"""doc 11 §4.3：/resolve 端点行为（409 注册表拦截、400 校验、404、幂等 200）。"""

from contextlib import contextmanager
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from backend.api.main import create_app
from backend.api.routers import workspaces
from backend.jobs import active_runs
from backend.jobs.run_repository import COLLECTION_RUN, PUBLISH_RUN
from backend.persistence.platform_db import PlatformSettings

SETTINGS = PlatformSettings(
    host="platform", port=5432, name="metaweave", user="admin", password="secret"
)


class FakeSession:
    pass


@contextmanager
def fake_scope(_settings):
    yield FakeSession()


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(workspaces, "platform_session", fake_scope)
    monkeypatch.setattr(
        workspaces,
        "job_of_run",
        lambda spec, session, run_id: (
            None,
            SimpleNamespace(workspace_id="ws_1"),
        ),
    )
    return TestClient(create_app())


def apply_stub(result):
    calls = []

    def _apply(session, spec, run_id, reason, *, finalize_only=False, origin="离线收尾"):
        calls.append(
            {"spec": spec, "run_id": run_id, "reason": reason, "origin": origin}
        )
        return result

    return _apply, calls


def test_resolve_success_maps_fields_and_web_origin(client, monkeypatch):
    run_id = uuid4()
    _apply, calls = apply_stub(
        {
            "changed": True,
            "final_status": "partial_success",
            "steps": [{"step": "rel", "to": "failed"}],
            "note": "Web 终止：原状态 running；…",
            "source_id": "db_src_1",
            "job_id": "cj_1",
            "run_id": str(run_id),
        }
    )
    monkeypatch.setattr(workspaces, "apply_resolution", _apply)
    monkeypatch.setattr(workspaces, "get_platform_settings", lambda: SETTINGS)

    resp = client.post(
        f"/api/collection-runs/{run_id}/resolve",
        json={"reason": " 后端进程被回收 ", "confirm_executors_stopped": True},
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["changed"] is True
    assert body["final_status"] == "partial_success"
    assert body["steps"] == [{"step": "rel", "to": "failed"}]
    assert body["run_id"] == str(run_id)
    assert body["job_id"] == "cj_1"
    assert body["workspace_id"] == "ws_1"
    # reason 去除首尾空白；origin 固定 Web 终止
    assert calls == [
        {
            "spec": COLLECTION_RUN,
            "run_id": run_id,
            "reason": "后端进程被回收",
            "origin": "Web 终止",
        }
    ]


def test_resolve_publish_uses_publish_spec(client, monkeypatch):
    run_id = uuid4()
    _apply, calls = apply_stub(
        {
            "changed": True,
            "final_status": "failed",
            "steps": [],
            "note": "Web 终止：…",
            "source_id": "db_src_1",
            "job_id": "pj_1",
            "run_id": str(run_id),
        }
    )
    monkeypatch.setattr(workspaces, "apply_resolution", _apply)
    monkeypatch.setattr(workspaces, "get_platform_settings", lambda: SETTINGS)

    resp = client.post(
        f"/api/publish-runs/{run_id}/resolve",
        json={"reason": "中断收尾", "confirm_executors_stopped": True},
    )

    assert resp.status_code == 200, resp.text
    assert calls[0]["spec"] is PUBLISH_RUN


def test_resolve_empty_reason_is_400_without_apply(client, monkeypatch):
    _apply, calls = apply_stub({})
    monkeypatch.setattr(workspaces, "apply_resolution", _apply)
    monkeypatch.setattr(workspaces, "get_platform_settings", lambda: SETTINGS)

    resp = client.post(
        f"/api/collection-runs/{uuid4()}/resolve",
        json={"reason": "   ", "confirm_executors_stopped": True},
    )

    assert resp.status_code == 400
    assert resp.json()["code"] == "invalid_request"
    assert calls == []


def test_resolve_missing_confirmation_is_400(client, monkeypatch):
    _apply, calls = apply_stub({})
    monkeypatch.setattr(workspaces, "apply_resolution", _apply)
    monkeypatch.setattr(workspaces, "get_platform_settings", lambda: SETTINGS)

    resp = client.post(
        f"/api/collection-runs/{uuid4()}/resolve",
        json={"reason": "中断收尾", "confirm_executors_stopped": False},
    )

    assert resp.status_code == 400
    assert calls == []


def test_resolve_unknown_run_is_404(client, monkeypatch):
    monkeypatch.setattr(workspaces, "get_platform_settings", lambda: SETTINGS)
    _apply, _calls = apply_stub({"error": "运行不存在"})
    monkeypatch.setattr(workspaces, "apply_resolution", _apply)

    resp = client.post(
        f"/api/collection-runs/{uuid4()}/resolve",
        json={"reason": "中断收尾", "confirm_executors_stopped": True},
    )

    assert resp.status_code == 404
    assert resp.json()["code"] == "not_found"


def test_resolve_active_run_is_409(client, monkeypatch):
    run_id = uuid4()
    _apply, calls = apply_stub({})
    monkeypatch.setattr(workspaces, "apply_resolution", _apply)
    monkeypatch.setattr(workspaces, "get_platform_settings", lambda: SETTINGS)
    active_runs.register("collection", run_id)
    try:
        resp = client.post(
            f"/api/collection-runs/{run_id}/resolve",
            json={"reason": "中断收尾", "confirm_executors_stopped": True},
        )
    finally:
        active_runs.unregister("collection", run_id)

    assert resp.status_code == 409
    body = resp.json()
    assert body["code"] == "run_resolve_active"
    assert "正在本进程执行中" in body["message"]
    assert calls == []


def test_resolve_terminal_run_returns_changed_false(client, monkeypatch):
    _apply, _calls = apply_stub(
        {"changed": False, "message": "无需处理：运行已是终态 success"}
    )
    monkeypatch.setattr(workspaces, "apply_resolution", _apply)
    monkeypatch.setattr(workspaces, "get_platform_settings", lambda: SETTINGS)

    resp = client.post(
        f"/api/collection-runs/{uuid4()}/resolve",
        json={"reason": "中断收尾", "confirm_executors_stopped": True},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["changed"] is False
    assert "已是终态" in body["message"]
