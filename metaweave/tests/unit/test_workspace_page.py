"""跨源工作区列表 `GET /api/workspaces`（doc 6.2 §6.1）的路由与服务层测试。

分页信封 items/total/limit/offset、列表行采集/发布作业计数聚合、
Query 参数校验（422）、source_id 短 ID 校验（ShortIdError → 422）、
平台库异常 503。实库排序/过滤/计数由 `test_workspace_page_live.py`
覆盖；本文件不连数据库。
"""

from contextlib import contextmanager
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import SQLAlchemyError

from backend.api.main import create_app
from backend.api.routers import workspaces as workspaces_router
from backend.workspaces import service as workspace_service
from metaweave.utils.source_id import ShortIdError


def _client():
    settings = SimpleNamespace(
        host="platform", port=5432, name="metaweave", user="admin", password="secret"
    )
    return TestClient(
        create_app(settings, cors_origins=[]), raise_server_exceptions=False
    )


def _row(workspace_id: str, source_id: str = "db_src_1", name: str = "工作区"):
    return SimpleNamespace(
        workspace_id=workspace_id,
        source_id=source_id,
        workspace_name=name,
        workspace_desc=None,
        created_at=datetime(2026, 10, 22, 3, 0, 0, tzinfo=timezone.utc),
        updated_at=datetime(2026, 10, 22, 3, 0, 0, tzinfo=timezone.utc),
    )


def _page_dict(rows, total, limit, offset, collection=0, publish=0):
    return {
        "items": [
            {
                "workspace_id": row.workspace_id,
                "source_id": row.source_id,
                "workspace_name": row.workspace_name,
                "workspace_desc": row.workspace_desc,
                "created_at": "2026-10-22T03:00:00.000000Z",
                "updated_at": "2026-10-22T03:00:00.000000Z",
                "collection_job_count": collection,
                "publish_job_count": publish,
            }
            for row in rows
        ],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


# ---- 路由层：信封透传与 Query 校验 ----------------------------------------------


def test_route_returns_envelope_and_passes_defaults(monkeypatch):
    calls = []

    def fake(settings, *, source_id=None, limit=None, offset=None):
        calls.append({"source_id": source_id, "limit": limit, "offset": offset})
        return _page_dict(
            [_row("ws_1")], total=1, limit=20, offset=0, collection=3, publish=5
        )

    monkeypatch.setattr(workspaces_router, "page_workspaces", fake)
    with _client() as client:
        response = client.get("/api/workspaces")
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"items", "total", "limit", "offset"}
    assert body["total"] == 1 and body["limit"] == 20 and body["offset"] == 0
    assert body["items"][0]["workspace_id"] == "ws_1"
    # 列表行聚合计数随信封透传（doc 6.2 §6.1）
    assert body["items"][0]["collection_job_count"] == 3
    assert body["items"][0]["publish_job_count"] == 5
    assert calls == [{"source_id": None, "limit": 20, "offset": 0}]


def test_route_forwards_source_filter_and_paging(monkeypatch):
    calls = []

    def fake(settings, *, source_id=None, limit=None, offset=None):
        calls.append({"source_id": source_id, "limit": limit, "offset": offset})
        return _page_dict([], total=0, limit=5, offset=10)

    monkeypatch.setattr(workspaces_router, "page_workspaces", fake)
    with _client() as client:
        response = client.get("/api/workspaces?source_id=db_src_9&limit=5&offset=10")
    assert response.status_code == 200
    assert calls == [{"source_id": "db_src_9", "limit": 5, "offset": 10}]


@pytest.mark.parametrize(
    "query",
    [
        "limit=0",
        "limit=101",
        "limit=-1",
        "offset=-1",
        "limit=abc",
        "limit=20&limit=abc",
    ],
    ids=[
        "limit-too-small",
        "limit-too-large",
        "limit-negative",
        "offset-negative",
        "limit-not-int",
        "duplicate-limit",
    ],
)
def test_route_rejects_invalid_paging_422(monkeypatch, query):
    """limit 范围 1–100、offset ≥ 0（doc 6.2 §6.1）；非法值 422 不进服务层。"""
    monkeypatch.setattr(
        workspaces_router,
        "page_workspaces",
        lambda *_a, **_kw: pytest.fail("分页校验未通过却进入了服务层"),
    )
    with _client() as client:
        response = client.get(f"/api/workspaces?{query}")
    assert response.status_code == 422


def test_route_rejects_malformed_source_id_422(monkeypatch):
    """source_id 非法短 ID → ShortIdError → 422，不与内部错误混淆。"""
    # 使用真实 service：normalize_source_id 在打开平台会话前抛出
    monkeypatch.setattr(
        workspace_service,
        "page_workspaces_by_filter",
        lambda *_a, **_kw: pytest.fail("短 ID 校验未通过却查询了数据库"),
    )
    with _client() as client:
        response = client.get("/api/workspaces?source_id=ws_bad")
    assert response.status_code == 422
    assert response.json()["code"] == "invalid_request"


def test_route_platform_failure_is_503(monkeypatch):
    def unavailable(*_a, **_kw):
        raise workspace_service.PlatformUnavailableError("平台库暂不可用")

    monkeypatch.setattr(workspaces_router, "page_workspaces", unavailable)
    with _client() as client:
        response = client.get("/api/workspaces")
    assert response.status_code == 503
    assert response.json() == {
        "code": "platform_unavailable",
        "message": "平台库暂不可用",
    }


# ---- 服务层：信封构造、默认值与异常映射 -----------------------------------------


def _fake_session_get(model, key, **_kwargs):
    from backend.persistence.models import DbSourceInfo

    if model is DbSourceInfo:
        # 工作区所属源存在且有效（本假件不建模删除）
        return SimpleNamespace(
            source_id=key, deleted_at=None, deletion_state="active"
        )
    return None


@contextmanager
def _fake_platform_session():
    yield SimpleNamespace(
        # doc 8.1 §7.3：删除任务查询（本假件不建模任务表 → 无未解决任务）
        scalars=lambda _statement: iter([]),
        get=_fake_session_get,
    )


def _install_repo(monkeypatch, rows, total, collection_counts=None, publish_counts=None):
    captured = {}

    def fake_page(session, *, source_id=None, limit=None, offset=None):
        captured["source_id"] = source_id
        captured["limit"] = limit
        captured["offset"] = offset
        return rows, total

    def fake_counts(session, workspace_ids):
        captured["count_ids"] = list(workspace_ids)
        return collection_counts or {}, publish_counts or {}

    monkeypatch.setattr(workspace_service, "page_workspaces_by_filter", fake_page)
    monkeypatch.setattr(workspace_service, "count_workspace_jobs", fake_counts)
    monkeypatch.setattr(
        workspace_service, "platform_session", lambda _s: _fake_platform_session()
    )
    return captured


def test_service_builds_envelope_with_projection(monkeypatch):
    rows = [_row("ws_2", "db_src_1", "采集"), _row("ws_1", "db_src_2", "分析")]
    captured = _install_repo(monkeypatch, rows, total=2)

    settings = SimpleNamespace(database_url="fake")
    result = workspace_service.page_workspaces(
        settings, source_id=None, limit=20, offset=0
    )

    assert captured == {
        "source_id": None,
        "limit": 20,
        "offset": 0,
        "count_ids": ["ws_2", "ws_1"],
    }
    assert set(result) == {"items", "total", "limit", "offset"}
    assert result["total"] == 2
    assert [item["workspace_id"] for item in result["items"]] == ["ws_2", "ws_1"]
    assert result["items"][0] == {
        "workspace_id": "ws_2",
        "source_id": "db_src_1",
        "workspace_name": "采集",
        "workspace_desc": None,
        "created_at": "2026-10-22T03:00:00.000000Z",
        "updated_at": "2026-10-22T03:00:00.000000Z",
        "collection_job_count": 0,
        "publish_job_count": 0,
        "deletion_task": None,
        "effective_state": "active",
        "blocked_by": None,
        "blocked_reason": None,
    }


def test_service_merges_job_counts_with_zero_default(monkeypatch):
    """计数映射缺键的工作区按 0 展示；有键的按聚合值合并进行。"""
    rows = [_row("ws_2", "db_src_1", "采集"), _row("ws_1", "db_src_2", "分析")]
    _install_repo(
        monkeypatch,
        rows,
        total=2,
        collection_counts={"ws_1": 2},
        publish_counts={"ws_1": 1, "ws_2": 4},
    )

    settings = SimpleNamespace(database_url="fake")
    result = workspace_service.page_workspaces(settings, limit=20, offset=0)

    counts = {
        item["workspace_id"]: (
            item["collection_job_count"],
            item["publish_job_count"],
        )
        for item in result["items"]
    }
    assert counts == {"ws_2": (0, 4), "ws_1": (2, 1)}


def test_service_normalizes_source_filter(monkeypatch):
    captured = _install_repo(monkeypatch, [], total=0)
    settings = SimpleNamespace(database_url="fake")

    workspace_service.page_workspaces(settings, source_id="db_src_3")

    assert captured["source_id"] == "db_src_3"


def test_service_malformed_source_raises_short_id(monkeypatch):
    _install_repo(monkeypatch, [], total=0)
    settings = SimpleNamespace(database_url="fake")
    with pytest.raises(ShortIdError):
        workspace_service.page_workspaces(settings, source_id="src_1")


def test_service_paging_defaults_and_range(monkeypatch):
    captured = _install_repo(monkeypatch, [], total=0)
    settings = SimpleNamespace(database_url="fake")

    workspace_service.page_workspaces(settings)

    assert captured == {"source_id": None, "limit": 20, "offset": 0, "count_ids": []}
    with pytest.raises(ValueError):
        workspace_service.page_workspaces(settings, limit=101)
    with pytest.raises(ValueError):
        workspace_service.page_workspaces(settings, offset=-1)


def test_service_sqlalchemy_error_is_unavailable(monkeypatch):
    @contextmanager
    def failing_session():
        raise SQLAlchemyError("boom")
        yield  # pragma: no cover

    monkeypatch.setattr(
        workspace_service, "platform_session", lambda _s: failing_session()
    )
    settings = SimpleNamespace(database_url="fake")
    with pytest.raises(workspace_service.PlatformUnavailableError):
        workspace_service.page_workspaces(settings)
