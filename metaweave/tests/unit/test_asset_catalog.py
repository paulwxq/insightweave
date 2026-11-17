"""数据资产首页目录（doc 6.1 §14 新模型）：db_profile 每源一行即当前内容。

发布来源取画像行的 publish_run_id；published_at 取 PublishRunInfo.created_at；
不再有 current 标记、回退画像或 inconsistent 计数。
"""

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

SOURCE_A = "db_src_1"
SOURCE_B = "db_src_2"
RUN_1 = UUID("84c017f4-b438-453b-a888-715b7f0cbea2")
RUN_2 = UUID("3b1f5dcc-818c-40c3-83c3-1e9b950dafe9")


def _run(publish_run_id=RUN_1, created_at=None, status="success"):
    return SimpleNamespace(
        publish_run_id=publish_run_id,
        created_at=created_at
        or datetime(2026, 9, 27, 23, 48, 35, tzinfo=timezone.utc),
        status=status,
    )


def _profile(**overrides):
    values = {
        "source_id": SOURCE_A,
        "publish_run_id": RUN_1,
        "db_type": "postgresql",
        "host": "172.29.128.1",
        "port": 5432,
        "db_name": "orders",
        "server_version": "18.1",
        "collected_at": datetime(
            2026, 9, 22, 13, 58, 39, 128000, tzinfo=timezone.utc
        ),
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


def test_catalog_items_from_profiles_with_publish_run():
    result = assemble_asset_catalog(
        [_profile()],
        {SOURCE_A: "订单库pg"},
        {RUN_1: _run()},
    )
    assert result["source_count"] == 1
    assert result["object_count"] == 7
    assert result["project_count"] == 0
    # 新模型：无 current 标记，也无 inconsistent 计数
    assert "inconsistent_count" not in result
    item = result["items"][0]
    assert item["publish_run_id"] == str(RUN_1)
    assert item["published_at"] == "2026-09-27T23:48:35.000000Z"
    assert item["source_id"] == SOURCE_A
    assert item["display_name"] == "订单库pg"
    assert item["db_type_label"] == "PostgreSQL"
    assert item["server_version"] == "18.1"
    assert item["connection"] == "172.29.128.1:5432/orders"
    assert item["table_count"] == 7
    assert item["collected_at"] == "2026-09-22T13:58:39.128000Z"


def test_missing_run_row_published_at_is_null():
    result = assemble_asset_catalog([_profile()], {SOURCE_A: "订单库pg"}, {})
    assert result["items"][0]["publish_run_id"] == str(RUN_1)
    assert result["items"][0]["published_at"] is None


def test_missing_source_name_falls_back_to_database_name():
    result = assemble_asset_catalog(
        [_profile(db_type="sqlite", host="", server_version=None)],
        {},
        {RUN_1: _run()},
    )
    item = result["items"][0]
    assert item["display_name"] == "orders"
    assert item["db_type_label"] == "SQLite"
    assert item["server_version"] is None
    # 文件型库连接串只显示库名
    assert item["connection"] == "orders"


def test_items_sort_by_collected_at_desc():
    result = assemble_asset_catalog(
        [
            _profile(),
            _profile(
                source_id=SOURCE_B,
                publish_run_id=RUN_2,
                collected_at=datetime(2026, 9, 23, 1, 0, tzinfo=timezone.utc),
                object_count=2,
                ordinary_table_count=1,
            ),
        ],
        {SOURCE_A: "订单库pg", SOURCE_B: "另一库"},
        {RUN_1: _run(), RUN_2: _run(RUN_2)},
    )
    assert [item["display_name"] for item in result["items"]] == ["另一库", "订单库pg"]
    assert result["object_count"] == 9
    assert result["source_count"] == 2


def test_no_profiles_is_empty_catalog():
    result = assemble_asset_catalog([], {}, {})
    assert result == {
        "source_count": 0,
        "object_count": 0,
        "project_count": 0,
        "items": [],
    }


def test_list_reads_db_profile_rows(monkeypatch):
    seen = []

    class Session:
        def scalars(self, stmt):
            sql = str(stmt)
            seen.append(sql)
            entity = stmt.column_descriptions[0]["entity"].__name__
            if entity == "DbProfile":
                assert "is_current" not in sql  # 新模型不再查 current 标记
                return [_profile()]
            if entity == "DbSourceInfo":
                return [_source()]
            if entity == "PublishRunInfo":
                return [_run()]
            raise AssertionError(sql)

    @contextmanager
    def fake_session(_settings):
        yield Session()

    monkeypatch.setattr("backend.assets.catalog.platform_session", fake_session)
    result = list_asset_catalog(
        PlatformSettings(host="h", port=1, name="n", user="u", password="p")
    )
    assert result["items"][0]["publish_run_id"] == str(RUN_1)
    assert result["source_count"] == 1
    # 只读 db_profile/db_source_info/publish_run_info 三类行
    assert all("publish_info" not in sql for sql in seen)


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
    with TestClient(
        create_app(settings, cors_origins=[]), raise_server_exceptions=False
    ) as client:
        response = client.get("/api/data-assets")
    assert response.status_code == 503
    assert response.json()["code"] == "platform_unavailable"
