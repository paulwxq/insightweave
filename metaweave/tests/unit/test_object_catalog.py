"""对象列表与级联候选读取（doc 19.2 §8.1/§8.2）。"""

from contextlib import contextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import UUID

import pytest

from backend.assets import object_catalog
from sqlalchemy.exc import SQLAlchemyError

from backend.assets.object_catalog import (
    get_current_json_publishes,
    list_object_options,
    list_objects,
)
from backend.data_sources.registration_service import (
    PlatformUnavailableError,
    SourceNotFoundError,
)
from backend.persistence.platform_db import PlatformSettings

SOURCE_ID = UUID("b4a87275-7c37-4feb-bf1c-e5a8446c174f")
OTHER_SOURCE_ID = UUID("adb15693-2dda-4e4d-8ca7-ad5164c5e85d")
PUBLISH_ID = UUID("63221a9e-6a82-4311-a08c-9f74001da835")
OTHER_PUBLISH_ID = UUID("84c017f4-b438-452b-a888-715b7f0cbea2")
SETTINGS = PlatformSettings(
    host="platform", port=5432, name="metaweave", user="admin", password="secret"
)


def _publish(**overrides):
    values = {
        "publish_id": PUBLISH_ID,
        "job_id": UUID("550e8400-e29b-41d4-a716-446655440000"),
        "source_id": SOURCE_ID,
        "step_name": "json",
        "status": "success",
        "is_current_publish": True,
        "created_at": datetime(2026, 10, 3, 4, 0, 0, tzinfo=timezone.utc),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _row(**overrides):
    values = {
        "source_id": SOURCE_ID,
        "publish_id": PUBLISH_ID,
        "job_id": UUID("550e8400-e29b-41d4-a716-446655440000"),
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


class FakeCatalogSession:
    def __init__(self, *, source_exists=True):
        self.source = SimpleNamespace(source_id=SOURCE_ID) if source_exists else None
        self.publishes: list = []
        self.table_profiles: list = []
        self.calls: list[str] = []

    def connection(self, execution_options=None):
        self.calls.append("connection")
        return SimpleNamespace(execution_options=execution_options)

    def get(self, model, key):
        self.calls.append("get")
        if model.__name__ == "DbSourceInfo":
            return self.source if key == SOURCE_ID else None
        raise AssertionError(model)

    def scalars(self, statement):
        self.calls.append("scalars")
        entity = statement.column_descriptions[0]["entity"]
        params = statement.compile().params
        if entity.__name__ == "PublishInfo":
            rows = [row for row in self.publishes if row.is_current_publish]
            source_id = params.get("source_id_1")
            step_name = params.get("step_name_1")
            status = params.get("status_1")
            if source_id is not None:
                rows = [row for row in rows if row.source_id == source_id]
            if step_name is not None:
                rows = [row for row in rows if row.step_name == step_name]
            if status is not None:
                rows = [row for row in rows if row.status == status]
            return rows
        if entity.__name__ == "TableProfile":
            source_id = params.get("source_id_1")
            rows = [
                row
                for row in self.table_profiles
                if source_id is None or row.source_id == source_id
            ]
            return sorted(rows, key=lambda r: (r.schema_name, r.object_name))
        raise AssertionError(entity)


def install(monkeypatch, session):
    @contextmanager
    def fake_platform_session(_settings):
        yield session

    monkeypatch.setattr(object_catalog, "platform_session", fake_platform_session)
    return session


def _ready_session(source_exists=True):
    session = FakeCatalogSession(source_exists=source_exists)
    session.publishes.append(_publish())
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
            list_objects(SETTINGS, SOURCE_ID, page=1, page_size=50)

    def test_no_current_returns_not_published(self, monkeypatch):
        install(monkeypatch, FakeCatalogSession())
        result = list_objects(SETTINGS, SOURCE_ID, page=1, page_size=50)
        assert result["state"] == "not_published"
        assert result["json_publish"] is None
        assert result["total"] == 0 and result["items"] == []
        assert result["page"] == 1 and result["page_size"] == 50

    def test_current_without_rows_is_inconsistent(self, monkeypatch):
        session = install(monkeypatch, FakeCatalogSession())
        session.publishes.append(_publish())
        result = list_objects(SETTINGS, SOURCE_ID, page=1, page_size=50)
        assert result["state"] == "inconsistent"
        assert result["json_publish"] is None and result["items"] == []

    def test_multiple_current_is_inconsistent(self, monkeypatch):
        session = install(monkeypatch, FakeCatalogSession())
        session.publishes.extend([_publish(), _publish(publish_id=OTHER_PUBLISH_ID)])
        session.table_profiles.append(_row())
        result = list_objects(SETTINGS, SOURCE_ID, page=1, page_size=50)
        assert result["state"] == "inconsistent"

    def test_rows_version_mismatch_is_inconsistent(self, monkeypatch):
        session = install(monkeypatch, FakeCatalogSession())
        session.publishes.append(_publish())
        session.table_profiles.extend(
            [_row(), _row(object_name="stale", publish_id=OTHER_PUBLISH_ID)]
        )
        result = list_objects(SETTINGS, SOURCE_ID, page=1, page_size=50)
        assert result["state"] == "inconsistent"

    def test_ready_lists_sorted_and_paged(self, monkeypatch):
        install(monkeypatch, _ready_session())
        result = list_objects(SETTINGS, SOURCE_ID, page=1, page_size=2)
        assert result["state"] == "ready"
        assert result["total"] == 3
        # 按 schema_name, object_name 稳定排序：public 先于 sales
        assert [item["object_name"] for item in result["items"]] == [
            "orders",
            "users",
        ]
        page2 = list_objects(SETTINGS, SOURCE_ID, page=2, page_size=2)
        assert [item["object_name"] for item in page2["items"]] == ["invoices"]
        # published_at 为 UTC 六位微秒 ISO 8601 带 Z（doc 19.3 §3）
        assert (
            result["json_publish"]["published_at"] == "2026-10-03T04:00:00.000000Z"
        )

    def test_filters_exact_object_type_and_schema(self, monkeypatch):
        install(monkeypatch, _ready_session())
        result = list_objects(
            SETTINGS,
            SOURCE_ID,
            page=1,
            page_size=50,
            schema_name="sales",
        )
        assert [item["object_name"] for item in result["items"]] == ["invoices"]
        view_session = _ready_session()
        install(monkeypatch, view_session)
        view_session.table_profiles.append(
            _row(object_name="v_orders", object_type="view")
        )
        by_type = list_objects(
            SETTINGS, SOURCE_ID, page=1, page_size=50, object_type="view"
        )
        assert [item["object_name"] for item in by_type["items"]] == ["v_orders"]

    def test_keyword_substring_case_insensitive(self, monkeypatch):
        install(monkeypatch, _ready_session())
        result = list_objects(SETTINGS, SOURCE_ID, page=1, page_size=50, keyword="ORD")
        assert [item["object_name"] for item in result["items"]] == ["orders"]

    def test_keyword_percent_underscore_are_literal(self, monkeypatch):
        install(monkeypatch, _ready_session())
        for wildcard in ("%", "_", "a%"):
            result = list_objects(
                SETTINGS, SOURCE_ID, page=1, page_size=50, keyword=wildcard
            )
            assert result["items"] == []

    def test_primary_key_state_projection(self, monkeypatch):
        session = install(monkeypatch, FakeCatalogSession())
        session.publishes.append(_publish())
        session.table_profiles.extend(
            [
                _row(object_name="with_pk", primary_key_columns=["id"]),
                _row(object_name="no_pk", primary_key_columns=[]),
                _row(object_name="unknown_pk", primary_key_columns=None),
            ]
        )
        result = list_objects(SETTINGS, SOURCE_ID, page=1, page_size=50)
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
        result = list_objects(SETTINGS, SOURCE_ID, page=1, page_size=50)
        item = next(i for i in result["items"] if i["object_name"] == "orders")
        assert item["generated_timestamp"] == "2026-10-03T18:55:55.000000Z"

    def test_connection_configured_before_queries(self, monkeypatch):
        session = install(monkeypatch, _ready_session())
        list_objects(SETTINGS, SOURCE_ID, page=1, page_size=50)
        assert session.calls[0] == "connection"

    def test_sqlalchemy_error_maps_to_platform_unavailable(self, monkeypatch):
        session = install(monkeypatch, _ready_session())

        def boom(statement):
            raise SQLAlchemyError("db down")

        session.scalars = boom
        with pytest.raises(PlatformUnavailableError):
            list_objects(SETTINGS, SOURCE_ID, page=1, page_size=50)


class TestListObjectOptions:
    def test_cascade_and_keyword(self, monkeypatch):
        session = install(monkeypatch, _ready_session())
        session.table_profiles.append(
            _row(schema_name="sales", object_name="v_list", object_type="view")
        )
        result = list_object_options(SETTINGS, SOURCE_ID)
        assert result["state"] == "ready"
        assert result["object_types"] == ["table", "view"]
        assert result["schemas"] == ["public", "sales"]
        # 类型收窄 Schema 与对象
        by_type = list_object_options(SETTINGS, SOURCE_ID, object_type="view")
        assert by_type["schemas"] == ["sales"]
        assert [o["object_name"] for o in by_type["objects"]] == ["v_list"]
        # keyword 只筛对象候选，不改前两级
        narrowed = list_object_options(
            SETTINGS, SOURCE_ID, object_type="table", keyword="order"
        )
        assert narrowed["object_types"] == ["table", "view"]
        assert narrowed["schemas"] == ["public", "sales"]
        assert [o["object_name"] for o in narrowed["objects"]] == ["orders"]

    def test_not_published_empty_arrays(self, monkeypatch):
        install(monkeypatch, FakeCatalogSession())
        result = list_object_options(SETTINGS, SOURCE_ID)
        assert result["state"] == "not_published"
        assert result["object_types"] == [] and result["objects"] == []

    def test_inconsistent_state_empty(self, monkeypatch):
        session = install(monkeypatch, FakeCatalogSession())
        session.publishes.append(_publish())
        result = list_object_options(SETTINGS, SOURCE_ID)
        assert result["state"] == "inconsistent"
        assert result["schemas"] == [] and result["objects"] == []


class TestGetCurrentJsonPublishes:
    def test_filters_current_json_only(self, monkeypatch):
        session = FakeCatalogSession()
        session.publishes.extend(
            [
                _publish(),
                _publish(step_name="ddl"),
                _publish(is_current_publish=False),
            ]
        )
        rows = get_current_json_publishes(session, SOURCE_ID)
        assert len(rows) == 1 and rows[0].step_name == "json"


class TestCurrentPublishAndJobIdChecks:
    """源级检查统一（doc 19.2 §8.2）：current 须 success；行版本含 job_id。"""

    def test_failed_current_row_not_treated_as_current(self, monkeypatch):
        session = install(monkeypatch, FakeCatalogSession())
        session.publishes.append(_publish(status="failed"))
        session.table_profiles.append(_row())
        result = list_objects(SETTINGS, SOURCE_ID, page=1, page_size=50)
        assert result["state"] == "not_published"

    def test_row_job_id_mismatch_is_inconsistent(self, monkeypatch):
        session = install(monkeypatch, FakeCatalogSession())
        session.publishes.append(_publish())
        session.table_profiles.append(
            _row(job_id=UUID("99999999-9999-9999-9999-999999999999"))
        )
        result = list_objects(SETTINGS, SOURCE_ID, page=1, page_size=50)
        assert result["state"] == "inconsistent"
