"""数据库画像页（doc 6.1 §14 新模型）：db_profile 每源一行即当前内容。

无画像行 → not_published；有则 ready，发布来源取画像行 publish_run_id 对应
的 PublishRunInfo（run 行缺失时 publish 为 null）。无 current 标记、无回退
更早画像、无 inconsistent 态。
"""

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

SOURCE_ID = "db_src_1"
RUN = UUID("63221a9e-6a82-4311-a08c-9f74001da835")
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


def _run(**overrides):
    values = {
        "publish_run_id": RUN,
        "status": "success",
        "created_at": datetime(2026, 9, 28, 3, 18, 13, tzinfo=timezone.utc),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _profile(**overrides):
    values = {
        "source_id": SOURCE_ID,
        "publish_run_id": RUN,
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


def test_ready_profile_uses_row_publish_run():
    result = assemble_asset_profile(_source(), _profile(), _run())
    assert result["state"] == "ready"
    assert set(result["source"]) == {"source_id", "display_name", "username"}
    assert result["source"]["display_name"] == "订单库pg"
    assert result["source"]["username"] == "postgres"
    assert result["publish"] == {
        "publish_run_id": str(RUN),
        "status": "success",
        "published_at": "2026-09-28T03:18:13.000000Z",
    }
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


def test_missing_profile_is_not_published():
    result = assemble_asset_profile(_source(), None, None)
    assert result["state"] == "not_published"
    assert result["publish"] is None
    assert result["profile"] is None
    assert result["source"]["display_name"] == "订单库pg"


def test_profile_without_run_row_publish_is_null():
    result = assemble_asset_profile(_source(), _profile(), None)
    assert result["state"] == "ready"
    assert result["publish"] is None
    assert result["profile"]["db_name"] == "orders"


def test_null_platform_stays_null():
    result = assemble_asset_profile(
        _source(display_name=""),
        _profile(os_platform=None, server_version_full=None),
        _run(),
    )
    assert result["profile"]["os_platform"] is None
    assert result["profile"]["server_version_full"] is None
    # 登记名缺失时回退画像库名
    assert result["source"]["display_name"] == "orders"


def test_get_asset_profile_reads_rows_by_key(monkeypatch):
    class Session:
        def __init__(self):
            self.source = _source()
            self.profile = _profile()
            self.run = _run()

        def get(self, model, key):
            if model.__name__ == "DbSourceInfo":
                return self.source if key == SOURCE_ID else None
            if model.__name__ == "DbProfile":
                return self.profile if key == SOURCE_ID else None
            if model.__name__ == "PublishRunInfo":
                return self.run if key == RUN else None
            raise AssertionError(model)

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
    assert result["publish"]["publish_run_id"] == str(RUN)


def test_missing_source_is_404(monkeypatch):
    class Session:
        def get(self, _model, _key):
            return None

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
