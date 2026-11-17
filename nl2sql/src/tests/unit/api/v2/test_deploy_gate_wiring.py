"""src.api.main 的部署门闩接线（doc 13 §7.5、任务 2A 切片 2）。

用子进程按需导入，避免门闩状态污染同进程的其他 API 测试；
默认关闭时零额外依赖（不导入 deploy.*）。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

NL2SQL_DIR = Path(__file__).resolve().parents[5]
REPO_ROOT = Path(__file__).resolve().parents[6]

WIRING_PROBE = """
import sys
import src.api.main as m
mw = m.app.user_middleware
names = [x.cls.__name__ for x in mw]
from src.api.v2 import runtime as r
enabled = names[0] == "DeploymentGateMiddleware"
assert enabled == __EXPECT__, names
assert (r._future_tracker is not None) == __EXPECT__, "tracker 接线状态不符"
if enabled:
    from starlette.testclient import TestClient
    c = TestClient(m.app)
    resp = c.get("/api/v2/_deploy/state")
    assert resp.status_code == 403, resp.status_code
    resp = c.get(
        "/api/v2/_deploy/state",
        headers={"x-insightweave-deploy-token": "probe-token"},
    )
    assert resp.status_code == 200, resp.status_code
    assert resp.json()["deployment_contract_version"] == 1
print("WIRING_OK")
"""


def _run_probe(tmp_path, *, enabled: bool) -> subprocess.CompletedProcess:
    env = {
        **os.environ,
        "PYTHONPATH": f"{REPO_ROOT}{os.pathsep}{NL2SQL_DIR}",
    }
    if enabled:
        env.update(
            DEPLOY_CONTROL_ENABLED="1",
            DEPLOY_CONTROL_TOKEN="probe-token",
            DEPLOY_CONTROL_DIR=str(tmp_path),
        )
    else:
        env.pop("DEPLOY_CONTROL_ENABLED", None)
    return subprocess.run(
        [sys.executable, "-c", WIRING_PROBE.replace("__EXPECT__", str(enabled))],
        env=env,
        cwd=str(NL2SQL_DIR),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def test_gate_wiring_disabled_by_default(tmp_path):
    result = _run_probe(tmp_path, enabled=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "WIRING_OK" in result.stdout


def test_gate_wiring_enabled_installs_gate_and_tracker(tmp_path):
    result = _run_probe(tmp_path, enabled=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "WIRING_OK" in result.stdout
