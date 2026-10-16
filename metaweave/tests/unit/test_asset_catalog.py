"""数据资产目录只使用每个数据源 db 步骤的 current 发布画像。"""

from contextlib import contextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import SQLAlchemyError

from backend.api.main import create_app
from backend.assets.catalog import assemble_asset_catalog, list_asset_catalog
from backend.data_sources.registration_service import PlatformUnavailableError
from backend.persistence.platform_db import PlatformSettings

SOURCE_A = UUID("b4a87275-7c37-4feb-bf1c-e5a8446c174f")
SOURCE_B = UUID("11111111-1111-4111-8111-111111111111")
CURRENT = UUID("84c017f4-b438-453b-a888-715b7f0cbea2")
OLDER = UUID("3b1f5dcc-818c-40c3-83c3-1e9b950dafe9")
FAILED = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
OTHER = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")


def _publish(**overrides):
    values = {
        "publish_id": CURRENT,
        "source_id": SOURCE_A,
        "step_name": "db",
        "status": "success",
        "is_current_publish": True,
        "created_at": datetime(2026, 9, 27, 23, 48, 35, tzinfo=timezone.utc),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _profile(**overrides):
    values = {
        "publish_id": CURRENT,
        "source_id": SOURCE_A,
        "db_type": "postgresql",
        "host": "172.29.128.1",
        "port": 5432,
        "db_name": "orders",
        "server_version": "18.1",
        "collected_at": datetime(2026, 9, 22, 13, 58, 39, 128000, tzinfo=timezone.utc),
        "ordinary_table_count": 6,
        "partitioned_table_count": 0,
        "view_count": 0,
        "materialized_view_count": 1,
        "object_count": 7,
        "size_bytes": 8525503,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _source(**overrides):
    values = {"source_id": SOURCE_A, "display_name": "订单库pg"}
    values.update(overrides)
    return SimpleNamespace(**values)


def test_catalog_uses_current_success_profile_and_registered_name():
    result = assemble_asset_catalog(
        [
            _publish(),
            _publish(
                publish_id=OLDER,
                is_current_publish=False,
                created_at=datetime(2026, 9, 28, 8, 0, tzinfo=timezone.utc),
            ),
            _publish(publish_id=OTHER, source_id=SOURCE_B, step_name="cql"),
        ],
        # 画像每源一行（doc 16 §3.1），publish_id 与 current 成功发布一致
        [_profile()],
        [_source()],
    )
    assert result["source_count"] == 1
    assert result["object_count"] == 7
    assert result["project_count"] == 0
    assert result["inconsistent_count"] == 0
    item = result["items"][0]
    assert item["publish_id"] == str(CURRENT)
    assert item["display_name"] == "订单库pg"
    assert item["db_type_label"] == "PostgreSQL"
    assert item["server_version"] == "18.1"
    assert item["connection"] == "172.29.128.1:5432/orders"
    assert item["table_count"] == 7
    assert item["collected_at"] == "2026-09-22T13:58:39.128000Z"


def test_failed_current_does_not_fall_back_to_older_profile():
    result = assemble_asset_catalog(
        [
            _publish(publish_id=FAILED, status="failed"),
            _publish(publish_id=OLDER, is_current_publish=False, status="success"),
        ],
        [_profile(publish_id=OLDER)],
        [_source()],
    )
    assert result["items"] == []
    assert result["source_count"] == 0
    assert result["inconsistent_count"] == 0


def test_missing_profile_is_counted_and_not_replaced():
    result = assemble_asset_catalog(
        [_publish(), _publish(publish_id=OLDER, is_current_publish=False)],
        [_profile(publish_id=OLDER, ordinary_table_count=3, object_count=3)],
        [_source()],
    )
    assert result["items"] == []
    assert result["inconsistent_count"] == 1


def test_missing_source_name_falls_back_to_database_name():
    result = assemble_asset_catalog(
        [_publish()],
        [_profile(db_type="sqlite", host="", server_version=None)],
        [],
    )
    item = result["items"][0]
    assert item["display_name"] == "orders"
    assert item["db_type_label"] == "SQLite"
    assert item["server_version"] is None
    assert item["connection"] == "orders"


def test_items_sort_by_collected_at_desc():
    result = assemble_asset_catalog(
        [
            _publish(),
            _publish(publish_id=OTHER, source_id=SOURCE_B),
        ],
        [
            _profile(),
            _profile(
                publish_id=OTHER,
                source_id=SOURCE_B,
                collected_at=datetime(2026, 9, 23, 1, 0, tzinfo=timezone.utc),
                object_count=2,
                ordinary_table_count=1,
            ),
        ],
        [_source(), _source(source_id=SOURCE_B, display_name="另一库")],
    )
    assert [item["display_name"] for item in result["items"]] == ["另一库", "订单库pg"]
    assert result["object_count"] == 9


def test_list_reads_current_db_publish(monkeypatch):
    seen = []

    class Session:
        def scalars(self, stmt):
            sql = str(stmt)
            seen.append(sql)
            if "publish_info" in sql:
                assert "is_current_publish" in sql
                assert "step_name" in sql
                return [
                    _publish(),
                    _publish(publish_id=OLDER, is_current_publish=False, status="success"),
                    _publish(publish_id=FAILED, source_id=SOURCE_B, status="failed"),
                ]
            if "db_profile" in sql:
                assert "source_id" in sql  # 画像按 source_id 过滤（doc 16 §6）
                return [_profile()]
            if "db_source_info" in sql:
                return [_source()]
            raise AssertionError(sql)

    @contextmanager
    def fake_session(_settings):
        yield Session()

    monkeypatch.setattr("backend.assets.catalog.platform_session", fake_session)
    result = list_asset_catalog(PlatformSettings(host="h", port=1, name="n", user="u", password="p"))
    assert result["items"][0]["publish_id"] == str(CURRENT)
    assert result["source_count"] == 1
    assert any("publish_info" in sql for sql in seen)


def test_database_error_is_platform_unavailable(monkeypatch):
    class Session:
        def scalars(self, _stmt):
            raise SQLAlchemyError("boom")

    @contextmanager
    def fake_session(_settings):
        yield Session()

    monkeypatch.setattr("backend.assets.catalog.platform_session", fake_session)
    settings = PlatformSettings(host="h", port=1, name="n", user="u", password="p")
    with pytest.raises(PlatformUnavailableError):
        list_asset_catalog(settings)
    with TestClient(create_app(settings, cors_origins=[]), raise_server_exceptions=False) as client:
        response = client.get("/api/data-assets")
    assert response.status_code == 503
    assert response.json()["code"] == "platform_unavailable"
