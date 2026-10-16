"""数据源启用与禁用（doc 11）：启停接口契约、只改状态、以及创建/修改接口拒绝该字段。"""

from contextlib import contextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import SQLAlchemyError

from backend.api.main import create_app
from backend.api.routers import data_sources as source_router
from backend.data_sources import registration_service, repository
from backend.data_sources.registration_service import (
    PlatformUnavailableError,
    SourceNotFoundError,
)
from backend.persistence.models import DbSourceInfo
from backend.persistence.platform_db import PlatformSettings

SOURCE_ID = UUID("b4a87275-7c37-4feb-bf1c-e5a8446c174f")
MISSING_ID = UUID("00000000-0000-4000-8000-000000000000")


def _client():
    settings = PlatformSettings(
        host="platform", port=5432, name="metaweave", user="admin", password="secret"
    )
    return TestClient(
        create_app(settings, cors_origins=[]), raise_server_exceptions=False
    )


def _patch(client, body, source_id=SOURCE_ID):
    return client.patch(f"/api/data-sources/{source_id}/activation", json=body)


# --- 路由层：请求模型与异常映射 -------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        {"is_active": "false"},
        {"is_active": "true"},
        {"is_active": "1"},
        {"is_active": "0"},
        {"is_active": "yes"},
        {"is_active": "no"},
        {"is_active": "on"},
        {"is_active": "off"},
        {"is_active": "TRUE"},
        {"is_active": 0},
        {"is_active": 1},
        {"is_active": None},
        {"is_active": 2},
        {},
        {"is_active": True, "extra": 1},
    ],
)
def test_strict_bool_and_shape_rejected(monkeypatch, body):
    """只接受 JSON 的 true/false；字符串、数字、null、缺字段、多余字段一律 422。"""
    monkeypatch.setattr(
        source_router,
        "set_source_activation",
        lambda *_a, **_kw: pytest.fail("校验未通过却进入了服务层"),
    )
    with _client() as client:
        response = _patch(client, body)
    assert response.status_code == 422
    assert response.json() == {"code": "invalid_request", "message": "请求字段不合法"}


@pytest.mark.parametrize("value", [True, False])
def test_route_passes_target_state_to_service(monkeypatch, value):
    seen = {}

    def fake(settings, source_id, is_active):
        seen["source_id"] = source_id
        seen["is_active"] = is_active
        return {
            "source_id": str(source_id),
            "display_name": "订单库",
            "is_active": is_active,
            **{
                "db_type": "postgresql",
                "host": "h",
                "port": 5432,
                "db_name": "d",
                "username": "u",
                "sslmode": None,
                "selected_schemas": ["public"],
                "description": None,
                "db_version": None,
                "created_at": "2026-09-25T00:00:00Z",
            },
        }

    monkeypatch.setattr(source_router, "set_source_activation", fake)
    with _client() as client:
        response = _patch(client, {"is_active": value})
    assert response.status_code == 200
    assert response.json()["is_active"] is value
    assert seen == {"source_id": SOURCE_ID, "is_active": value}


def test_missing_source_is_404(monkeypatch):
    def missing(_settings, _source_id, _is_active):
        raise SourceNotFoundError("数据源不存在")

    monkeypatch.setattr(source_router, "set_source_activation", missing)
    with _client() as client:
        response = _patch(client, {"is_active": False}, source_id=MISSING_ID)
    assert response.status_code == 404
    assert response.json() == {"code": "not_found", "message": "数据源不存在"}


def test_platform_failure_is_503(monkeypatch):
    def unavailable(_settings, _source_id, _is_active):
        raise PlatformUnavailableError("平台库暂不可用")

    monkeypatch.setattr(source_router, "set_source_activation", unavailable)
    with _client() as client:
        response = _patch(client, {"is_active": False})
    assert response.status_code == 503
    assert response.json() == {
        "code": "platform_unavailable",
        "message": "平台库暂不可用",
    }


@pytest.mark.parametrize(
    "path,payload",
    [
        ("/api/data-sources", {"is_active": False}),
        (f"/api/data-sources/{SOURCE_ID}", {"is_active": False}),
    ],
)
def test_create_and_update_reject_activation_field(monkeypatch, path, payload):
    """创建与修改接口都不接收 is_active：带该字段即 422，且不进入服务层。"""
    monkeypatch.setattr(
        source_router,
        "create_data_source",
        lambda *_a, **_kw: pytest.fail("不应进入创建服务"),
    )
    monkeypatch.setattr(
        source_router,
        "update_registered_source",
        lambda *_a, **_kw: pytest.fail("不应进入修改服务"),
    )
    body = {
        "db_type": "postgresql",
        "host": "h",
        "port": 5432,
        "db_name": "d",
        "username": "u",
        "password": "pw",
        "sslmode": None,
        "display_name": "n",
        "selected_schemas": ["public"],
        "description": None,
        "confirm_duplicate": False,
        **payload,
    }
    with _client() as client:
        response = (
            client.post(path, json=body)
            if path == "/api/data-sources"
            else client.patch(path, json=body)
        )
    assert response.status_code == 422
    assert response.json() == {"code": "invalid_request", "message": "请求字段不合法"}


# --- 仓储层：只改状态列，找不到返回 None ---------------------------------------


def test_repository_sets_only_flag_and_commits(monkeypatch):
    row = SimpleNamespace(is_active=True, updated_at="old")
    commits = []
    monkeypatch.setattr(repository, "get_db_source", lambda _s, _i: row)
    session = SimpleNamespace(commit=lambda: commits.append(1))

    result = repository.set_db_source_active(session, SOURCE_ID, False)

    assert result is row
    assert row.is_active is False
    assert row.updated_at != "old"  # 写成 func.now()，由会话层执行
    assert commits == [1]


def test_repository_unknown_source_returns_none_without_commit(monkeypatch):
    commits = []
    monkeypatch.setattr(repository, "get_db_source", lambda _s, _i: None)
    session = SimpleNamespace(commit=lambda: commits.append(1))

    assert repository.set_db_source_active(session, MISSING_ID, False) is None
    assert commits == []


# --- 贯穿路由→服务→仓储：幂等与状态稳定性 -------------------------------------


class FakePlatformSession:
    """只模拟启停端点用到的会话行为：按主键取行 + 提交。"""

    def __init__(self, row):
        self.row = row
        self.commits = 0

    def get(self, model, key):
        assert model is DbSourceInfo
        return self.row if key == self.row.source_id else None

    def commit(self):
        self.commits += 1


def test_repeated_activation_is_idempotent(monkeypatch):
    """重复提交同一状态：两次都 200、状态一致、created_at 不变、每次都提交（doc 11 §9 第 3 条）。"""
    row = _row(True)
    created_at = row.created_at
    session = FakePlatformSession(row)

    @contextmanager
    def fake_platform_session(_settings):
        yield session

    monkeypatch.setattr(registration_service, "platform_session", fake_platform_session)
    with _client() as client:
        first = _patch(client, {"is_active": False})
        second = _patch(client, {"is_active": False})

    assert first.status_code == 200 and second.status_code == 200
    assert first.json()["is_active"] is False
    assert second.json()["is_active"] is False
    assert first.json()["created_at"] == second.json()["created_at"]
    assert row.created_at is created_at  # 启停不触碰登记时间
    assert row.updated_at is not None  # 每次切换刷新 updated_at
    assert row.is_active is False
    assert session.commits == 2  # 两次都真的写库，没有被"已是最新值"短路


def test_activation_can_be_restored(monkeypatch):
    """禁用后再启用：状态回到 True，仍走同一个端点。"""
    row = _row(False)
    session = FakePlatformSession(row)

    @contextmanager
    def fake_platform_session(_settings):
        yield session

    monkeypatch.setattr(registration_service, "platform_session", fake_platform_session)
    with _client() as client:
        response = _patch(client, {"is_active": True})

    assert response.status_code == 200
    assert response.json()["is_active"] is True
    assert row.is_active is True


# --- 服务层：短会话、异常映射、返回值带 is_active -------------------------------


def _fake_session(monkeypatch, result=None, error=None):
    @contextmanager
    def fake_platform_session(_settings):
        if error is not None:
            raise error
        yield SimpleNamespace()

    monkeypatch.setattr(registration_service, "platform_session", fake_platform_session)

    def fake_set(_session, _source_id, _is_active):
        return result

    monkeypatch.setattr(registration_service, "set_db_source_active", fake_set)


def _row(is_active):
    return SimpleNamespace(
        source_id=SOURCE_ID,
        display_name="订单库pg",
        db_type="postgresql",
        host="172.29.128.1",
        port=5432,
        db_name="orders",
        username="postgres",
        sslmode="disable",
        selected_schemas=["public"],
        description="包含订单",
        db_version="18.1",
        created_at=datetime(2026, 9, 22, 8, 0, 0, tzinfo=timezone.utc),
        updated_at=None,
        is_active=is_active,
    )


@pytest.mark.parametrize("target", [True, False])
def test_service_returns_projected_item_with_new_state(monkeypatch, target):
    _fake_session(monkeypatch, result=_row(target))
    settings = PlatformSettings(
        host="platform", port=5432, name="metaweave", user="admin", password="secret"
    )
    item = registration_service.set_source_activation(settings, SOURCE_ID, target)
    assert item["is_active"] is target
    assert item["source_id"] == str(SOURCE_ID)
    assert set(item) == {
        "source_id",
        "display_name",
        "db_type",
        "host",
        "port",
        "db_name",
        "username",
        "sslmode",
        "selected_schemas",
        "description",
        "db_version",
        "created_at",
        "is_active",
    }


def test_service_missing_source_raises_not_found(monkeypatch):
    _fake_session(monkeypatch, result=None)
    settings = PlatformSettings(
        host="platform", port=5432, name="metaweave", user="admin", password="secret"
    )
    with pytest.raises(SourceNotFoundError):
        registration_service.set_source_activation(settings, MISSING_ID, False)


def test_service_platform_error_is_unavailable(monkeypatch):
    _fake_session(monkeypatch, error=SQLAlchemyError("boom"))
    settings = PlatformSettings(
        host="platform", port=5432, name="metaweave", user="admin", password="secret"
    )
    with pytest.raises(PlatformUnavailableError):
        registration_service.set_source_activation(settings, SOURCE_ID, False)
