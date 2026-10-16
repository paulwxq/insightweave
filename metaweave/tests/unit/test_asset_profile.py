"""数据库画像页：只读当前 db 发布，不回退更早的成功画像。"""

from contextlib import contextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import SQLAlchemyError

from backend.api.main import create_app
from backend.assets.catalog import assemble_asset_profile, get_asset_profile
from backend.data_sources.registration_service import (
    PlatformUnavailableError,
    SourceNotFoundError,
)
from backend.persistence.platform_db import PlatformSettings

SOURCE_ID = UUID("b4a87275-7c37-4feb-bf1c-e5a8446c174f")
CURRENT = UUID("63221a9e-6a82-4311-a08c-9f74001da835")
OLDER = UUID("84c017f4-b438-453b-a888-715b7f0cbea2")
VERSION_FULL = "PostgreSQL 18.1 on x86_64-windows, compiled by msvc-19.44.35221, 64-bit"


def _source(**overrides):
    values = {
        "source_id": SOURCE_ID,
        "display_name": "订单库pg",
        "username": "postgres",
        "password_ciphertext": "secret",
        "sslmode": "require",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _publish(**overrides):
    values = {
        "publish_id": CURRENT,
        "source_id": SOURCE_ID,
        "step_name": "db",
        "status": "success",
        "is_current_publish": True,
        "error_summary": None,
        "created_at": datetime(2026, 9, 28, 3, 18, 13, tzinfo=timezone.utc),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _profile(**overrides):
    values = {
        "publish_id": CURRENT,
        "source_id": SOURCE_ID,
        "db_type": "postgresql",
        "host": "172.29.128.1",
        "port": 5432,
        "db_name": "orders",
        "server_version": "18.1",
        "server_version_full": VERSION_FULL,
        "os_platform": "Windows x86_64",
        "collected_at": datetime(2026, 9, 26, 16, 15, 10, tzinfo=timezone.utc),
        "size_bytes": 8525503,
        "size_scope": "whole_database",
        "record_count": None,
        "selected_schemas": ["public"],
        "schemas": [
            {
                "name": "public",
                "ordinary_table_count": 6,
                "partitioned_table_count": 0,
                "partition_child_count": 0,
                "view_count": 0,
                "materialized_view_count": 1,
                "object_count": 7,
            }
        ],
        "ordinary_table_count": 6,
        "partitioned_table_count": 0,
        "partition_child_count": 0,
        "view_count": 0,
        "materialized_view_count": 1,
        "object_count": 7,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_ready_profile_uses_current_publish_and_registration_username():
    result = assemble_asset_profile(
        _source(),
        [
            _publish(),
            _publish(publish_id=OLDER, is_current_publish=False, status="success"),
        ],
        [_profile(), _profile(publish_id=OLDER, db_name="old")],
    )
    assert result["state"] == "ready"
    assert set(result["source"]) == {"source_id", "display_name", "username"}
    assert result["source"]["display_name"] == "订单库pg"
    assert result["source"]["username"] == "postgres"
    profile = result["profile"]
    assert profile["db_name"] == "orders"
    assert profile["host"] == "172.29.128.1"
    assert profile["port"] == 5432
    assert profile["db_type_label"] == "PostgreSQL"
    assert profile["server_version"] == "18.1"
    assert profile["server_version_full"] == VERSION_FULL
    assert profile["os_platform"] == "Windows x86_64"
    assert profile["record_count"] is None
    assert profile["selected_schemas"] == ["public"]
    assert profile["schemas"][0]["object_count"] == 7
    assert profile["totals"]["ordinary_table_count"] == 6
    assert profile["totals"]["materialized_view_count"] == 1
    assert "connection" not in profile
    assert "password" not in profile
    assert "sslmode" not in profile
    assert "password_ciphertext" not in result["source"]


def test_failed_current_does_not_fall_back_to_older_success():
    result = assemble_asset_profile(
        _source(),
        [
            _publish(status="failed", error_summary="画像输入不可用或校验失败"),
            _publish(publish_id=OLDER, is_current_publish=False, status="success"),
        ],
        [_profile(publish_id=OLDER)],
    )
    assert result["state"] == "failed"
    assert result["profile"] is None
    assert result["publish"]["error_summary"] == "画像输入不可用或校验失败"
    assert result["publish"]["publish_id"] == str(CURRENT)


def test_missing_current_is_not_published():
    result = assemble_asset_profile(_source(), [], [_profile()])
    assert result["state"] == "not_published"
    assert result["publish"] is None
    assert result["profile"] is None


def test_success_without_profile_is_inconsistent():
    result = assemble_asset_profile(_source(), [_publish()], [])
    assert result["state"] == "inconsistent"
    assert result["profile"] is None


def test_null_platform_stays_null():
    result = assemble_asset_profile(
        _source(display_name=""),
        [_publish()],
        [_profile(os_platform=None, server_version_full=None)],
    )
    assert result["profile"]["os_platform"] is None
    assert result["profile"]["server_version_full"] is None
    assert result["source"]["display_name"] == "orders"


def test_get_asset_profile_reads_current_row(monkeypatch):
    class Session:
        def get(self, _model, key):
            return _source() if key == SOURCE_ID else None

        def scalars(self, stmt):
            sql = str(stmt)
            if "publish_info" in sql:
                assert "is_current_publish" in sql
                return [
                    _publish(),
                    _publish(publish_id=OLDER, is_current_publish=False),
                ]
            if "db_profile" in sql:
                return [_profile()]
            raise AssertionError(sql)

    @contextmanager
    def fake_session(_settings):
        yield Session()

    monkeypatch.setattr("backend.assets.catalog.platform_session", fake_session)
    result = get_asset_profile(
        PlatformSettings(host="h", port=1, name="n", user="u", password="p"),
        SOURCE_ID,
    )
    assert result["state"] == "ready"
    assert result["profile"]["os_platform"] == "Windows x86_64"


def test_missing_source_is_404(monkeypatch):
    class Session:
        def get(self, _model, _key):
            return None

        def scalars(self, _stmt):
            raise AssertionError("不存在的数据源不应查询发布记录")

    @contextmanager
    def fake_session(_settings):
        yield Session()

    monkeypatch.setattr("backend.assets.catalog.platform_session", fake_session)
    settings = PlatformSettings(host="h", port=1, name="n", user="u", password="p")
    with pytest.raises(SourceNotFoundError):
        get_asset_profile(settings, SOURCE_ID)
    with TestClient(
        create_app(settings, cors_origins=[]), raise_server_exceptions=False
    ) as client:
        response = client.get(f"/api/data-assets/{SOURCE_ID}/profile")
    assert response.status_code == 404
    assert response.json()["code"] == "not_found"


def test_database_error_is_platform_unavailable(monkeypatch):
    class Session:
        def get(self, _model, _key):
            raise SQLAlchemyError("boom")

    @contextmanager
    def fake_session(_settings):
        yield Session()

    monkeypatch.setattr("backend.assets.catalog.platform_session", fake_session)
    settings = PlatformSettings(host="h", port=1, name="n", user="u", password="p")
    with pytest.raises(PlatformUnavailableError):
        get_asset_profile(settings, SOURCE_ID)
    with TestClient(
        create_app(settings, cors_origins=[]), raise_server_exceptions=False
    ) as client:
        response = client.get(f"/api/data-assets/{SOURCE_ID}/profile")
    assert response.status_code == 503
    assert response.json()["code"] == "platform_unavailable"
