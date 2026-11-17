"""backend.api.main.create_app 的部署门闩接线（doc 13 §7.5、任务 2A 切片 2）。

仅在 DEPLOY_CONTROL_ENABLED=1 时惰性导入 deploy.runtime.lifecycle；
默认关闭不依赖仓库根路径。仓库根路径仅为启用态测试注入。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.api.main import create_app
from backend.persistence.platform_db import PlatformSettings

REPO_ROOT = Path(__file__).resolve().parents[3]

SETTINGS = PlatformSettings(host="h", port=1, name="n", user="u", password="p")


def _gate_names(app) -> list[str]:
    return [mw.cls.__name__ for mw in app.user_middleware]


def test_default_create_app_has_no_gate(monkeypatch):
    monkeypatch.delenv("DEPLOY_CONTROL_ENABLED", raising=False)
    app = create_app(SETTINGS, cors_origins=[])
    assert "DeploymentGateMiddleware" not in _gate_names(app)


def test_enabled_create_app_installs_gate(monkeypatch, tmp_path):
    monkeypatch.setenv("DEPLOY_CONTROL_ENABLED", "1")
    monkeypatch.setenv("DEPLOY_CONTROL_TOKEN", "probe-token")
    monkeypatch.setenv("DEPLOY_CONTROL_DIR", str(tmp_path))
    monkeypatch.setenv("METAWEAVE_DELETION_STARTUP_SCAN", "0")
    monkeypatch.syspath_prepend(str(REPO_ROOT))

    app = create_app(SETTINGS, cors_origins=[])
    assert _gate_names(app)[0] == "DeploymentGateMiddleware"

    from starlette.testclient import TestClient

    client = TestClient(app)
    # 无令牌控制被拒；带令牌返回契约版本
    assert client.get("/api/_deploy/state").status_code == 403
    state = client.get(
        "/api/_deploy/state", headers={"x-insightweave-deploy-token": "probe-token"}
    )
    assert state.status_code == 200
    assert state.json()["deployment_contract_version"] == 1
    # 维护期：/openapi.json 健康路径放行；普通业务被门闩拦截
    assert (
        client.post(
            "/api/_deploy/enter", headers={"x-insightweave-deploy-token": "probe-token"}
        ).status_code
        == 200
    )
    assert client.get("/openapi.json").status_code == 200
    blocked = client.get("/api/data-sources")
    assert blocked.status_code == 503
    assert blocked.json()["error"] == "maintenance_mode"
    # 离开维护后业务恢复进入应用自身逻辑（fake settings → platform_unavailable 503）
    assert (
        client.post(
            "/api/_deploy/leave", headers={"x-insightweave-deploy-token": "probe-token"}
        ).status_code
        == 200
    )
    assert client.get("/api/data-sources").status_code == 503
    assert client.get("/api/data-sources").json()["code"] == "platform_unavailable"


def test_enabled_without_token_fails_fast(monkeypatch, tmp_path):
    monkeypatch.setenv("DEPLOY_CONTROL_ENABLED", "1")
    monkeypatch.delenv("DEPLOY_CONTROL_TOKEN", raising=False)
    monkeypatch.syspath_prepend(str(REPO_ROOT))
    with pytest.raises(RuntimeError, match="DEPLOY_CONTROL_TOKEN"):
        create_app(SETTINGS, cors_origins=[])
