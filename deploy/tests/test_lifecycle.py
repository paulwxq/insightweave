"""deploy/runtime/lifecycle.py 部署门闩语义测试（doc 13 §7.5、§9.2、任务 2A）。

运行：仓库根目录 PYTHONPATH=. 下用 NL2SQL 测试环境执行（doc 13 §7.2）。
"""

from __future__ import annotations

import json
import threading

import pytest
from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from deploy.runtime.lifecycle import (
    CONTROL_TOKEN_HEADER,
    DeploymentGate,
    install_deployment_gate,
    load_contract_version,
)

TOKEN = "test-token"


def make_gate(
    tmp_path, *, marker: bool = False, health_paths=("/healthz",)
) -> DeploymentGate:
    marker_path = tmp_path / "maintenance"
    if marker:
        marker_path.write_text("", encoding="utf-8")
    return DeploymentGate(
        token=TOKEN,
        marker_path=marker_path,
        health_paths=health_paths,
        contract_version=load_contract_version(),
    )


def make_app(gate: DeploymentGate):
    async def ping(request):
        return JSONResponse({"ok": True})

    app = Starlette(routes=[Route("/work", ping), Route("/healthz", ping)])
    app.add_middleware(gate.middleware_class, control_prefix="/api/_deploy", gate=gate)
    return app


def client_for(app) -> TestClient:
    return TestClient(app)


def auth(**extra):
    return {CONTROL_TOKEN_HEADER: TOKEN, **extra}


class TestControlAuth:
    def test_state_without_token_is_403(self, tmp_path):
        gate = make_gate(tmp_path)
        client = client_for(make_app(gate))
        resp = client.get("/api/_deploy/state")
        assert resp.status_code == 403
        assert resp.json()["error"] == "deploy_control_forbidden"

    def test_state_with_wrong_token_is_403(self, tmp_path):
        gate = make_gate(tmp_path)
        client = client_for(make_app(gate))
        resp = client.get("/api/_deploy/state", headers={CONTROL_TOKEN_HEADER: "bad"})
        assert resp.status_code == 403

    def test_state_returns_contract_version_and_zero_active(self, tmp_path):
        gate = make_gate(tmp_path)
        client = client_for(make_app(gate))
        resp = client.get("/api/_deploy/state", headers=auth())
        assert resp.status_code == 200
        body = resp.json()
        assert body["deployment_contract_version"] == 1
        assert body["maintaining"] is False
        assert body["active"] == 0

    def test_state_rejects_post(self, tmp_path):
        gate = make_gate(tmp_path)
        client = client_for(make_app(gate))
        resp = client.post("/api/_deploy/state", headers=auth())
        assert resp.status_code == 405

    def test_enter_rejects_get(self, tmp_path):
        gate = make_gate(tmp_path)
        client = client_for(make_app(gate))
        resp = client.get("/api/_deploy/enter", headers=auth())
        assert resp.status_code == 405

    def test_unknown_control_path_is_404(self, tmp_path):
        gate = make_gate(tmp_path)
        client = client_for(make_app(gate))
        resp = client.get("/api/_deploy/nope", headers=auth())
        assert resp.status_code == 404

    def test_control_state_does_not_count(self, tmp_path):
        gate = make_gate(tmp_path)
        client = client_for(make_app(gate))
        for _ in range(3):
            client.get("/api/_deploy/state", headers=auth())
        assert gate.state()["active"] == 0


class TestMaintenanceGate:
    def test_enter_blocks_business_with_503_retry_after(self, tmp_path):
        gate = make_gate(tmp_path)
        client = client_for(make_app(gate))
        assert client.get("/work").status_code == 200
        resp = client.post("/api/_deploy/enter", headers=auth())
        assert resp.status_code == 200
        blocked = client.get("/work")
        assert blocked.status_code == 503
        assert blocked.headers["retry-after"].isdigit()
        assert "maintenance" in blocked.json()["error"]
        # 被拒绝的请求不计入活动数
        assert gate.state()["active"] == 0

    def test_health_path_bypasses_maintenance_and_counting(self, tmp_path):
        gate = make_gate(tmp_path)
        client = client_for(make_app(gate))
        client.post("/api/_deploy/enter", headers=auth())
        assert client.get("/healthz").status_code == 200
        assert gate.state()["active"] == 0

    def test_tokened_get_passes_during_maintenance_and_counts_inflight(self, tmp_path):
        gate = make_gate(tmp_path)
        started = threading.Event()
        release = threading.Event()

        async def slow(request):
            return JSONResponse(
                {"ok": True},
                background=BackgroundTask(lambda: (started.set(), release.wait(5))),
            )

        app = Starlette(routes=[Route("/work", slow), Route("/healthz", slow)])
        app.add_middleware(
            gate.middleware_class, control_prefix="/api/_deploy", gate=gate
        )
        client = TestClient(app)
        client.post("/api/_deploy/enter", headers=auth())

        result = {}

        def call():
            result["resp"] = client.get("/work", headers=auth())

        t = threading.Thread(target=call)
        t.start()
        assert started.wait(5), "业务请求未开始"
        # 响应已发出但后台任务未结束：仍计入活动数
        assert gate.state()["active"] == 1
        release.set()
        t.join(5)
        assert result["resp"].status_code == 200
        assert gate.state()["active"] == 0

    def test_leave_reopens_business(self, tmp_path):
        gate = make_gate(tmp_path)
        client = client_for(make_app(gate))
        client.post("/api/_deploy/enter", headers=auth())
        assert client.get("/work").status_code == 503
        resp = client.post("/api/_deploy/leave", headers=auth())
        assert resp.status_code == 200
        assert client.get("/work").status_code == 200

    def test_token_does_not_open_writes_during_maintenance(self, tmp_path):
        gate = make_gate(tmp_path)
        client = client_for(make_app(gate))
        client.post("/api/_deploy/enter", headers=auth())
        resp = client.post("/work", headers=auth())
        assert resp.status_code == 503

    def test_marker_file_at_init_starts_maintenance(self, tmp_path):
        gate = make_gate(tmp_path, marker=True)
        client = client_for(make_app(gate))
        assert gate.state()["maintaining"] is True
        assert client.get("/work").status_code == 503


class TestTryAdmit:
    """同一把锁内完成维护判定 + 计数登记（doc 13 §7.5、审核 M1）。"""

    def test_open_admits_and_counts(self, tmp_path):
        gate = make_gate(tmp_path)
        assert gate.try_admit() is True
        assert gate.state()["active"] == 1
        gate._release()
        assert gate.state()["active"] == 0

    def test_maintaining_rejects_without_counting(self, tmp_path):
        gate = make_gate(tmp_path)
        gate.enter()
        assert gate.try_admit() is False
        assert gate.state()["active"] == 0

    def test_read_smoke_admits_during_maintenance_and_counts(self, tmp_path):
        gate = make_gate(tmp_path)
        gate.enter()
        assert gate.try_admit(during_maintenance=True) is True
        assert gate.state()["active"] == 1
        gate._release()
        assert gate.state()["active"] == 0


class TestFutureTracking:
    def test_submit_tracked_counts_until_future_done(self, tmp_path):
        import concurrent.futures

        gate = make_gate(tmp_path)
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        started = threading.Event()
        release = threading.Event()

        def job():
            started.set()
            release.wait(5)
            return 42

        future = gate.submit_tracked(pool.submit, job)
        assert started.wait(5)
        assert gate.state()["active"] == 1
        release.set()
        assert future.result(timeout=5) == 42
        assert gate.state()["active"] == 0
        pool.shutdown(wait=True)

    def test_submit_tracked_rolls_back_on_submit_error(self, tmp_path):
        gate = make_gate(tmp_path)

        def failing_submit(_fn):
            raise RuntimeError("executor closed")

        with pytest.raises(RuntimeError):
            gate.submit_tracked(failing_submit, lambda: None)
        assert gate.state()["active"] == 0


class TestInstall:
    def test_install_fails_without_token_env(self, tmp_path, monkeypatch):
        from starlette.applications import Starlette

        monkeypatch.delenv("DEPLOY_CONTROL_TOKEN", raising=False)
        monkeypatch.setenv("DEPLOY_CONTROL_ENABLED", "1")
        monkeypatch.setenv("DEPLOY_CONTROL_DIR", str(tmp_path))
        app = Starlette()
        with pytest.raises(RuntimeError, match="DEPLOY_CONTROL_TOKEN"):
            install_deployment_gate(app, control_prefix="/api/_deploy")

    def test_install_adds_gate_and_uses_marker_dir(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DEPLOY_CONTROL_TOKEN", TOKEN)
        monkeypatch.setenv("DEPLOY_CONTROL_DIR", str(tmp_path))
        gate = install_deployment_gate(
            Starlette(), control_prefix="/api/_deploy", health_paths=("/healthz",)
        )
        assert gate.state()["deployment_contract_version"] == 1
        assert gate.state()["maintaining"] is False
        (tmp_path / "maintenance").write_text("", encoding="utf-8")
        again = install_deployment_gate(
            Starlette(), control_prefix="/api/_deploy", health_paths=()
        )
        assert again.state()["maintaining"] is True


class TestContract:
    def test_contract_json_declares_version_1(self):
        from pathlib import Path

        contract = Path(__file__).resolve().parents[1] / "contract.json"
        data = json.loads(contract.read_text(encoding="utf-8"))
        assert data["deployment_contract_version"] == 1
        assert load_contract_version() == 1
