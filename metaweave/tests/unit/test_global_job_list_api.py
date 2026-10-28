"""全局作业列表 API（设计稿 35/37）：GET /api/collection-jobs、/api/publish-jobs。

路由层用 fake 服务验证参数透传与响应信封；SQL 装配（join/排序/过滤）
由 test_global_job_list_live.py 在隔离演练库上冒烟。
"""

import pytest
from fastapi.testclient import TestClient

from backend.api.main import create_app
from backend.api.routers import workspaces as workspaces_router
from backend.persistence.platform_db import PlatformSettings


def _client():
    settings = PlatformSettings(
        host="platform", port=5432, name="metaweave", user="admin", password="secret"
    )
    return TestClient(
        create_app(settings, cors_origins=[]), raise_server_exceptions=False
    )


def _item(job_id: str, **overrides) -> dict:
    row = {
        "job_id": job_id,
        "workspace_id": "ws_68",
        "job_name": f"作业 {job_id}",
        "job_desc": None,
        "created_at": "2026-10-27T00:00:00Z",
        "updated_at": "2026-10-27T00:00:00Z",
        "job_type": "collection",
        "latest_revision_no": 2,
        "revision": None,
        "default_steps": ["db", "ddl"],
        "run_count": 3,
        "workspace_name": "风控特征分析",
        "source_id": "db_src_16",
        "source_name": "Olist 生产库(只读)",
        "last_run_at": "2026-10-26T07:09:45Z",
    }
    row.update(overrides)
    return row


def test_collection_global_defaults_and_envelope(monkeypatch):
    calls = []

    def fake(settings, kind, *, workspace_id=None, source_id=None, q=None, limit=10, offset=0):
        calls.append({"kind": kind, "workspace_id": workspace_id, "source_id": source_id,
                      "q": q, "limit": limit, "offset": offset})
        return {
            "job_type": "collection",
            "workspace_id": None,
            "source_id": None,
            "q": None,
            "total": 1,
            "limit": limit,
            "offset": offset,
            "items": [_item("coll_job_9")],
        }

    monkeypatch.setattr(workspaces_router, "list_jobs_global_page", fake)
    with _client() as client:
        response = client.get("/api/collection-jobs")
    assert response.status_code == 200
    body = response.json()
    assert body["job_type"] == "collection"
    assert body["total"] == 1 and body["limit"] == 10 and body["offset"] == 0
    row = body["items"][0]
    # 全局行在工作区作业之上附源/工作区名与最近运行时间（设计稿 35 列）
    assert row["source_name"] == "Olist 生产库(只读)"
    assert row["workspace_name"] == "风控特征分析"
    assert row["last_run_at"] == "2026-10-26T07:09:45Z"
    assert row["run_count"] == 3
    assert calls == [{
        "kind": "collection", "workspace_id": None, "source_id": None,
        "q": None, "limit": 10, "offset": 0,
    }]


def test_publish_global_forwards_filters_and_paging(monkeypatch):
    calls = []

    def fake(settings, kind, *, workspace_id=None, source_id=None, q=None, limit=10, offset=0):
        calls.append({"kind": kind, "workspace_id": workspace_id, "source_id": source_id,
                      "q": q, "limit": limit, "offset": offset})
        return {
            "job_type": "publish", "workspace_id": workspace_id, "source_id": source_id,
            "q": q, "total": 0, "limit": limit, "offset": offset, "items": [],
        }

    monkeypatch.setattr(workspaces_router, "list_jobs_global_page", fake)
    with _client() as client:
        response = client.get(
            "/api/publish-jobs",
            params={"workspace_id": "ws_68", "source_id": "db_src_16",
                    "q": "订单", "limit": 5, "offset": 10},
        )
    assert response.status_code == 200
    assert response.json()["items"] == []
    assert calls == [{
        "kind": "publish", "workspace_id": "ws_68", "source_id": "db_src_16",
        "q": "订单", "limit": 5, "offset": 10,
    }]


@pytest.mark.parametrize("params", [
    {"limit": 0}, {"limit": 101}, {"offset": -1},
])
def test_global_rejects_out_of_range_paging(monkeypatch, params):
    monkeypatch.setattr(
        workspaces_router, "list_jobs_global_page",
        lambda *a, **k: pytest.fail("参数非法时不应到达服务层"),
    )
    with _client() as client:
        response = client.get("/api/collection-jobs", params=params)
    assert response.status_code == 422


def test_global_rejects_malformed_ids(monkeypatch):
    from backend.jobs.job_service import JobValidationError

    def invalid_workspace(settings, kind, *, workspace_id=None, **_):
        raise JobValidationError("工作区 ID 不是合法短 ID")

    monkeypatch.setattr(workspaces_router, "list_jobs_global_page", invalid_workspace)
    with _client() as client:
        response = client.get("/api/collection-jobs", params={"workspace_id": "not-a-ws"})
    assert response.status_code == 400
