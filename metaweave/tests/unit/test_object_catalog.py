"""对象列表与级联候选读取（doc 19.2 §8、6.1 §16：table_profile 行即当前内容）。

新模型下不再查 publish_info current 标记：无对象行 → not_published；
对象行发布来源（publish_run_id）不一致 → inconsistent；一致时返回
发布运行块（publish_run_id + published_at）。
"""

from contextlib import contextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import UUID

import pytest
from sqlalchemy.exc import SQLAlchemyError

from backend.assets import object_catalog
from tests.unit.test_publish_service import SETTINGS
from backend.data_sources.registration_service import (
    PlatformUnavailableError,
    SourceNotFoundError,
)

SOURCE_ID = "db_src_1"
PUBLISH_RUN = UUID("63221a9e-6a82-4311-a08c-9f74001da835")
OTHER_RUN = UUID("84c017f4-b438-452b-a888-715b7f0cbea2")
PUBLISHED_AT = datetime(2026, 10, 3, 4, 0, 0, tzinfo=timezone.utc)


class FakeCatalogSession:
    def __init__(self, *, source_exists=True):
        self.source = SimpleNamespace(source_id=SOURCE_ID) if source_exists else None
        self.table_profiles: list = []
        self.runs: dict = {}
        self.calls: list = []

    def connection(self, execution_options=None):
        self.calls.append("connection")
        return SimpleNamespace(execution_options=execution_options)

    def get(self, model, key):
        self.calls.append(("get", model.__name__))
        if model.__name__ == "DbSourceInfo":
            return self.source if key == SOURCE_ID else None
        if model.__name__ == "PublishRunInfo":
            return self.runs.get(key)
        raise AssertionError(model)

    def scalars(self, statement):
        self.calls.append("scalars")
        entity = statement.column_descriptions[0]["entity"]
        params = statement.compile().params
        if entity.__name__ == "TableProfile":
            source_id = params.get("source_id_1")
            rows = [
                row
                for row in self.table_profiles
                if source_id is None or row.source_id == source_id
            ]
            return rows  # 排序由 SQL 完成；fake 按插入序返回，测试自行排序断言
        raise AssertionError(entity)


def install(monkeypatch, session):
    @contextmanager
    def fake_platform_session(_settings):
        yield session

    monkeypatch.setattr(object_catalog, "platform_session", fake_platform_session)
    return session


def _row(**overrides):
    values = {
        "source_id": SOURCE_ID,
        "publish_run_id": PUBLISH_RUN,
        "schema_name": "public",
        "object_name": "orders",
        "object_type": "table",
        "object_comment": "订单表",
        "table_category": "fact",
        "column_count": 6,
        "total_rows": 500,
        "primary_key_columns": ["order_id"],
        "generated_timestamp": datetime(2026, 10, 3, 18, 55, 55, tzinfo=timezone.utc),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _ready_session(source_exists=True):
    session = FakeCatalogSession(source_exists=source_exists)
    session.runs[PUBLISH_RUN] = SimpleNamespace(
        publish_run_id=PUBLISH_RUN, created_at=PUBLISHED_AT
    )
    session.table_profiles.extend(
        [
            _row(),
            _row(object_name="users", total_rows=None),
            _row(schema_name="sales", object_name="invoices"),
        ]
    )
    return session


class TestListObjects:
    def test_source_missing_raises_404_domain(self, monkeypatch):

        install(monkeypatch, FakeCatalogSession(source_exists=False))
        with pytest.raises(SourceNotFoundError):
            object_catalog.list_objects(SETTINGS, SOURCE_ID, page=1, page_size=50)

    def test_no_rows_is_not_published(self, monkeypatch):

        install(monkeypatch, FakeCatalogSession())
        result = object_catalog.list_objects(SETTINGS, SOURCE_ID, page=1, page_size=50)
        assert result["state"] == "not_published"
        assert result["json_publish"] is None
        assert result["total"] == 0 and result["items"] == []
        assert result["page"] == 1 and result["page_size"] == 50

    def test_mixed_publish_runs_is_inconsistent(self, monkeypatch):

        session = install(monkeypatch, FakeCatalogSession())
        session.table_profiles.extend(
            [_row(), _row(object_name="stale", publish_run_id=OTHER_RUN)]
        )
        result = object_catalog.list_objects(SETTINGS, SOURCE_ID, page=1, page_size=50)
        assert result["state"] == "inconsistent"
        assert result["json_publish"] is None and result["items"] == []

    def test_ready_lists_sorted_and_paged_with_publish_block(self, monkeypatch):

        install(monkeypatch, _ready_session())
        result = object_catalog.list_objects(SETTINGS, SOURCE_ID, page=1, page_size=2)
        assert result["state"] == "ready"
        assert result["total"] == 3
        # 按 schema_name, object_name 稳定排序：public 先于 sales
        assert [item["object_name"] for item in result["items"]] == [
            "orders",
            "users",
        ]
        page2 = object_catalog.list_objects(SETTINGS, SOURCE_ID, page=2, page_size=2)
        assert [item["object_name"] for item in page2["items"]] == ["invoices"]
        # 发布来源块：publish_run_id + UTC 六位微秒 ISO（doc 19.3 §3）
        assert result["json_publish"] == {
            "publish_run_id": str(PUBLISH_RUN),
            "published_at": "2026-10-03T04:00:00.000000Z",
        }

    def test_filters_exact_object_type_and_schema(self, monkeypatch):

        session = install(monkeypatch, _ready_session())
        session.table_profiles.append(_row(object_name="v_orders", object_type="view"))
        by_schema = object_catalog.list_objects(
            SETTINGS, SOURCE_ID, page=1, page_size=50, schema_name="sales"
        )
        assert [item["object_name"] for item in by_schema["items"]] == ["invoices"]
        by_type = object_catalog.list_objects(
            SETTINGS, SOURCE_ID, page=1, page_size=50, object_type="view"
        )
        assert [item["object_name"] for item in by_type["items"]] == ["v_orders"]

    def test_keyword_substring_case_insensitive(self, monkeypatch):

        install(monkeypatch, _ready_session())
        result = object_catalog.list_objects(
            SETTINGS, SOURCE_ID, page=1, page_size=50, keyword="ORD"
        )
        assert [item["object_name"] for item in result["items"]] == ["orders"]

    def test_keyword_percent_underscore_are_literal(self, monkeypatch):

        install(monkeypatch, _ready_session())
        for wildcard in ("%", "_", "a%"):
            result = object_catalog.list_objects(
                SETTINGS, SOURCE_ID, page=1, page_size=50, keyword=wildcard
            )
            assert result["items"] == []

    def test_primary_key_state_projection(self, monkeypatch):

        session = install(monkeypatch, FakeCatalogSession())
        session.runs[PUBLISH_RUN] = SimpleNamespace(
            publish_run_id=PUBLISH_RUN, created_at=PUBLISHED_AT
        )
        session.table_profiles.extend(
            [
                _row(object_name="with_pk", primary_key_columns=["id"]),
                _row(object_name="no_pk", primary_key_columns=[]),
                _row(object_name="unknown_pk", primary_key_columns=None),
            ]
        )
        result = object_catalog.list_objects(SETTINGS, SOURCE_ID, page=1, page_size=50)
        states = {
            item["object_name"]: item["primary_key_state"] for item in result["items"]
        }
        assert states == {
            "with_pk": "present",
            "no_pk": "none",
            "unknown_pk": "unknown",
        }

    def test_generated_timestamp_returns_utc_microseconds(self, monkeypatch):

        install(monkeypatch, _ready_session())
        result = object_catalog.list_objects(SETTINGS, SOURCE_ID, page=1, page_size=50)
        item = next(i for i in result["items"] if i["object_name"] == "orders")
        assert item["generated_timestamp"] == "2026-10-03T18:55:55.000000Z"

    def test_connection_configured_before_queries(self, monkeypatch):

        session = install(monkeypatch, _ready_session())
        object_catalog.list_objects(SETTINGS, SOURCE_ID, page=1, page_size=50)
        assert session.calls[0] == "connection"

    def test_sqlalchemy_error_maps_to_platform_unavailable(self, monkeypatch):

        session = install(monkeypatch, _ready_session())

        def boom(statement):
            raise SQLAlchemyError("db down")

        session.scalars = boom
        with pytest.raises(PlatformUnavailableError):
            object_catalog.list_objects(SETTINGS, SOURCE_ID, page=1, page_size=50)


class TestListObjectOptions:
    def test_cascade_and_keyword(self, monkeypatch):

        session = install(monkeypatch, _ready_session())
        session.table_profiles.append(
            _row(schema_name="sales", object_name="v_list", object_type="view")
        )
        result = object_catalog.list_object_options(SETTINGS, SOURCE_ID)
        assert result["state"] == "ready"
        assert result["object_types"] == ["table", "view"]
        assert result["schemas"] == ["public", "sales"]
        by_type = object_catalog.list_object_options(
            SETTINGS, SOURCE_ID, object_type="view"
        )
        assert by_type["schemas"] == ["sales"]
        assert [o["object_name"] for o in by_type["objects"]] == ["v_list"]
        narrowed = object_catalog.list_object_options(
            SETTINGS, SOURCE_ID, object_type="table", keyword="order"
        )
        assert narrowed["object_types"] == ["table", "view"]
        assert narrowed["schemas"] == ["public", "sales"]
        assert [o["object_name"] for o in narrowed["objects"]] == ["orders"]

    def test_not_published_empty_arrays(self, monkeypatch):

        install(monkeypatch, FakeCatalogSession())
        result = object_catalog.list_object_options(SETTINGS, SOURCE_ID)
        assert result["state"] == "not_published"
        assert result["object_types"] == [] and result["objects"] == []

    def test_inconsistent_state_empty(self, monkeypatch):

        session = install(monkeypatch, FakeCatalogSession())
        session.table_profiles.extend(
            [_row(), _row(object_name="stale", publish_run_id=OTHER_RUN)]
        )
        result = object_catalog.list_object_options(SETTINGS, SOURCE_ID)
        assert result["state"] == "inconsistent"
        assert result["schemas"] == [] and result["objects"] == []
