"""发布 HTTP 契约（doc 6.2 §6.1/§6.3）：publish-check、202 运行创建与详情轮询。

路由层测试 monkeypatch workspaces 路由模块引用的服务函数；发布运行的
创建/执行语义在 test_publish_service.py 走真实服务链。
"""

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from backend.api.main import create_app
from backend.api.routers import workspaces as workspaces_router
from backend.jobs.run_detail import RunNotFoundError
from backend.persistence.platform_db import PlatformSettings
from backend.publishing.precheck import PublishPrecheckError
from backend.workspaces.service import WorkspaceNotFoundError

WORKSPACE_ID = "ws_1"
PUBLISH_JOB_ID = "pub_job_1"
RUN_ID = uuid4()
MISSING_RUN = uuid4()


def _client():
    settings = PlatformSettings(
        host="platform", port=5432, name="metaweave", user="admin", password="secret"
    )
    return TestClient(create_app(settings, cors_origins=[]))


@pytest.fixture
def executed(monkeypatch):
    """记录后台任务是否被排入并执行（202 之后同线程执行）。"""
    recorder = {"created": None, "executed": []}

    def fake_create(settings, publish_job_id, *, revision_no, mode, steps, step_config):
        recorder["created"] = {
            "publish_run_id": str(RUN_ID),
            "publish_job_id": publish_job_id,
            "revision_no": revision_no,
            "steps": steps or ["db"],
            "status": "pending",
        }
        return dict(recorder["created"])

    def fake_execute(settings, run_id):
        recorder["executed"].append(run_id)

    monkeypatch.setattr(workspaces_router, "create_publish_run", fake_create)
    monkeypatch.setattr(workspaces_router, "execute_publish_run", fake_execute)
    return recorder


# ---- GET /api/workspaces/{workspace_id}/publish-check ----


def test_publish_check_returns_fixed_eight_steps(monkeypatch):
    from backend.publishing.precheck import PUBLISH_STEP_ORDER

    def fake_check(_settings, workspace_id):
        return {
            "workspace_id": workspace_id,
            "source_id": "db_src_1",
            "steps": [
                {
                    "step_name": step,
                    "ready": step == "db",
                    "reason": None if step == "db" else "未就绪",
                }
                for step in PUBLISH_STEP_ORDER
            ],
            "input_directories": [
                {
                    "directory": "db",
                    "marker_status": "success",
                    "collection_run_id": str(RUN_ID),
                    "collected_at": "2026-10-21T15:00:00+08:00",
                    "error": None,
                }
            ],
        }

    monkeypatch.setattr(workspaces_router, "check_publication", fake_check)
    with _client() as client:
        response = client.get(f"/api/workspaces/{WORKSPACE_ID}/publish-check")
    assert response.status_code == 200
    body = response.json()
    assert body["workspace_id"] == WORKSPACE_ID
    assert [step["step_name"] for step in body["steps"]] == [
        "db",
        "ddl",
        "json",
        "domain",
        "table_schema",
        "dim_value",
        "rel",
        "sql",
    ]
    assert body["steps"][0] == {
        "step_name": "db",
        "ready": True,
        "reason": None,
    }
    assert body["input_directories"][0]["marker_status"] == "success"


def test_publish_check_missing_workspace_is_404(monkeypatch):
    def missing(_settings, _workspace_id):
        raise WorkspaceNotFoundError("工作区不存在")

    monkeypatch.setattr(workspaces_router, "check_publication", missing)
    with _client() as client:
        response = client.get(f"/api/workspaces/{WORKSPACE_ID}/publish-check")
    assert response.status_code == 404
    assert response.json() == {"code": "not_found", "message": "工作区不存在"}


def test_publish_check_does_not_accept_steps_parameter(monkeypatch):
    """§8.2.2：预检不提供 steps 参数，固定返回全部 8 步。"""
    monkeypatch.setattr(
        workspaces_router,
        "check_publication",
        lambda _s, ws: {
            "workspace_id": ws,
            "source_id": "db_src_1",
            "steps": [],
            "input_directories": [],
        },
    )
    with _client() as client:
        response = client.get(
            f"/api/workspaces/{WORKSPACE_ID}/publish-check",
            params={"steps": ["db"]},
        )
    assert response.status_code == 200
    assert response.json()["steps"] == []


# ---- POST /api/publish-jobs/{job_id}/runs（202 + 后台执行） ----


def test_create_publish_run_returns_202_and_schedules_execution(executed):
    with _client() as client:
        response = client.post(
            f"/api/publish-jobs/{PUBLISH_JOB_ID}/runs",
            json={"revision_no": 1, "mode": "default"},
        )
    assert response.status_code == 202
    body = response.json()
    assert body == {
        "publish_run_id": str(RUN_ID),
        "publish_job_id": PUBLISH_JOB_ID,
        "revision_no": 1,
        "steps": ["db"],
        "status": "pending",
    }
    # BackgroundTasks 在响应后同线程执行：只执行本次新建的那一个运行
    assert executed["executed"] == [RUN_ID]


def test_precheck_failure_is_400_with_step_details(monkeypatch):
    def rejected(*_args, **_kwargs):
        raise PublishPrecheckError(
            [
                {"step": "dim_value", "reason": "dim_tables.yaml 没有任何维度表"},
                {"step": "sql", "reason": "sql/ 目录没有采集 marker，产物不可用"},
            ]
        )

    executed = []
    monkeypatch.setattr(workspaces_router, "create_publish_run", rejected)
    monkeypatch.setattr(
        workspaces_router,
        "execute_publish_run",
        lambda *a, **k: executed.append(a),
    )
    with _client() as client:
        response = client.post(
            f"/api/publish-jobs/{PUBLISH_JOB_ID}/runs",
            json={"revision_no": 1, "mode": "default"},
        )
    assert response.status_code == 400
    body = response.json()
    assert body["code"] == "publish_precheck_failed"
    assert body["message"] == "发布预检未通过，本次请求未创建发布运行"
    assert body["details"] == [
        {"step": "dim_value", "reason": "dim_tables.yaml 没有任何维度表"},
        {"step": "sql", "reason": "sql/ 目录没有采集 marker，产物不可用"},
    ]
    assert executed == []  # 不启动后台任务


def test_invalid_run_request_is_400(executed):
    from backend.publishing.publish_service import PublishRunError

    def invalid(*_args, **_kwargs):
        raise PublishRunError(
            "default 模式不接受非空 step_config；需要临时参数请使用 custom 模式"
        )

    monkeypatch_holder = pytest.MonkeyPatch()
    monkeypatch_holder.setattr(workspaces_router, "create_publish_run", invalid)
    try:
        with _client() as client:
            response = client.post(
                f"/api/publish-jobs/{PUBLISH_JOB_ID}/runs",
                json={"revision_no": 1, "mode": "default", "step_config": {"db": {}}},
            )
    finally:
        monkeypatch_holder.undo()
    assert response.status_code == 400
    assert response.json()["code"] == "invalid_request"
    assert "step_config" in response.json()["message"]
    assert executed["executed"] == []


def test_malformed_body_is_422():
    with _client() as client:
        response = client.post(
            f"/api/publish-jobs/{PUBLISH_JOB_ID}/runs",
            json={"mode": "default"},  # 缺 revision_no
        )
    assert response.status_code == 422
    assert response.json() == {"code": "invalid_request", "message": "请求字段不合法"}


# ---- GET /api/publish-runs/{publish_run_id}（详情轮询模型） ----


def _detail_payload():
    return {
        "publish_run_id": str(RUN_ID),
        "job_id": PUBLISH_JOB_ID,
        "job_name": "整包发布",
        "workspace_id": WORKSPACE_ID,
        "revision_no": 1,
        "trigger_type": "manual",
        "status": "partial_success",
        "message": None,
        "created_at": "2026-10-21T15:00:00.000000Z",
        "started_at": "2026-10-21T15:00:01.000000Z",
        "finished_at": "2026-10-21T15:00:09.000000Z",
        "run_type": "publish",
        "steps": [
            {
                "step_name": "db",
                "status": "success",
                "message": "数据库画像发布完成",
                "step_config": None,
                "input_manifest": {
                    "files": [
                        {
                            "path": "db/database.json",
                            "collection_run_id": str(uuid4()),
                        }
                    ]
                },
                "output_manifest": None,
                "target_manifest": {
                    "targets": [
                        {
                            "type": "postgresql",
                            "database": "metaweave",
                            "schema": "public",
                            "table": "db_profile",
                        }
                    ]
                },
                "started_at": "2026-10-21T15:00:02.000000Z",
                "finished_at": "2026-10-21T15:00:03.000000Z",
            },
            {
                "step_name": "ddl",
                "status": "blocked",
                "message": "ddl/ 目录没有采集 marker，产物不可用",
                "step_config": None,
                "input_manifest": None,
                "output_manifest": None,
                "target_manifest": None,
                "started_at": None,
                "finished_at": "2026-10-21T15:00:04.000000Z",
            },
        ],
    }


def test_run_detail_shape(monkeypatch):
    monkeypatch.setattr(
        workspaces_router, "get_publish_run_detail", lambda _s, _rid: _detail_payload()
    )
    with _client() as client:
        response = client.get(f"/api/publish-runs/{RUN_ID}")
    assert response.status_code == 200
    body = response.json()
    assert body["publish_run_id"] == str(RUN_ID)
    assert body["status"] == "partial_success"
    assert body["run_type"] == "publish"
    assert [step["step_name"] for step in body["steps"]] == ["db", "ddl"]
    blocked = body["steps"][1]
    assert blocked["status"] == "blocked"
    assert blocked["started_at"] is None
    assert "marker" in blocked["message"]
    success = body["steps"][0]
    assert success["target_manifest"]["targets"][0]["table"] == "db_profile"


def test_run_detail_missing_is_404(monkeypatch):
    def missing(_settings, _run_id):
        raise RunNotFoundError("运行不存在")

    monkeypatch.setattr(workspaces_router, "get_publish_run_detail", missing)
    with _client() as client:
        response = client.get(f"/api/publish-runs/{MISSING_RUN}")
    assert response.status_code == 404
    assert response.json() == {"code": "not_found", "message": "运行不存在"}


# ---- 作业运行历史与工作区运行列表 ----


def test_publish_job_runs_listing(monkeypatch):
    payload = {
        "workspace_id": None,
        "job_id": PUBLISH_JOB_ID,
        "run_type": "publish",
        "total": 1,
        "limit": 20,
        "offset": 0,
        "items": [
            {
                "run_id": str(RUN_ID),
                "job_id": PUBLISH_JOB_ID,
                "job_name": "整包发布",
                "revision_no": 1,
                "status": "success",
                "trigger_type": "manual",
                "created_at": "2026-10-21T15:00:00.000000Z",
                "started_at": "2026-10-21T15:00:01.000000Z",
                "finished_at": "2026-10-21T15:00:09.000000Z",
                "run_type": "publish",
            }
        ],
    }
    monkeypatch.setattr(
        workspaces_router, "list_publish_job_runs", lambda *a, **k: payload
    )
    with _client() as client:
        response = client.get(f"/api/publish-jobs/{PUBLISH_JOB_ID}/runs")
    assert response.status_code == 200
    assert response.json() == payload


def test_workspace_publish_runs_listing(monkeypatch):
    payload = {
        "workspace_id": WORKSPACE_ID,
        "job_id": None,
        "run_type": "publish",
        "total": 0,
        "limit": 20,
        "offset": 0,
        "items": [],
    }
    monkeypatch.setattr(
        workspaces_router, "list_workspace_publish_runs", lambda *a, **k: payload
    )
    with _client() as client:
        response = client.get(f"/api/workspaces/{WORKSPACE_ID}/publish-runs")
    assert response.status_code == 200
    assert response.json() == payload


def test_workspace_run_listing_validates_pagination(monkeypatch):
    """limit/offset 分页参数（doc 6.2 §6.1）由路由 Query 约束。"""
    monkeypatch.setattr(
        workspaces_router, "list_workspace_publish_runs", lambda *a, **k: {}
    )
    with _client() as client:
        too_large = client.get(
            f"/api/workspaces/{WORKSPACE_ID}/publish-runs", params={"limit": 101}
        )
        negative = client.get(
            f"/api/workspaces/{WORKSPACE_ID}/publish-runs", params={"offset": -1}
        )
    assert too_large.status_code == 422
    assert negative.status_code == 422


# ---- 工作区路由的存在性与删除入口缺失（A29） ----


def test_no_delete_routes_for_workspace_or_jobs():
    """本期不提供工作区/作业/版本/运行删除入口（doc 6.2 §1.1）。

    唯一的 DELETE 是数据源登记（受工作区关联检查保护），工作区路由
    不出现任何 DELETE 方法。
    """
    workspace_paths = {
        (method, route.path)
        for route in workspaces_router.router.routes
        for method in route.methods
    }
    assert not any(method == "DELETE" for method, _path in workspace_paths)
    # 采集/发布运行创建与详情路由就位
    for method, path in workspace_paths:
        if method == "POST" and path.endswith("/runs"):
            assert "/publish-jobs/" in path or "/collection-jobs/" in path
    assert (
        "GET",
        "/api/workspaces/{workspace_id}/publish-check",
    ) in workspace_paths
    from backend.api.routers import data_sources as source_router_module

    source_deletes = [
        route.path
        for route in source_router_module.router.routes
        if "DELETE" in route.methods
    ]
    # 唯一的 DELETE 入口是数据源登记（受工作区关联检查保护，A29）
    assert source_deletes == ["/api/data-sources/{source_id}"]


def test_workspace_create_and_list_routes(monkeypatch):
    """工作区创建/列表路由接线（服务语义见 workspaces.service 测试）。"""
    item = {
        "workspace_id": WORKSPACE_ID,
        "source_id": "db_src_1",
        "workspace_name": "生产",
        "workspace_desc": None,
        "created_at": "2026-10-21T15:00:00.000000Z",
        "updated_at": "2026-10-21T15:00:00.000000Z",
    }
    monkeypatch.setattr(
        workspaces_router,
        "list_workspaces",
        lambda *a, **k: {"source_id": "db_src_1", "items": [item]},
    )
    monkeypatch.setattr(workspaces_router, "create_workspace", lambda *a, **k: item)
    with _client() as client:
        listed = client.get(f"/api/data-sources/db_src_1/workspaces")
        created = client.post(
            f"/api/data-sources/db_src_1/workspaces",
            json={"workspace_name": "生产"},
        )
    assert listed.status_code == 200
    assert listed.json() == {"source_id": "db_src_1", "items": [item]}
    assert created.status_code == 201
    assert created.json()["workspace_id"] == WORKSPACE_ID
