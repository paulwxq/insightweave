"""对象详情与 DDL 摘要/正文读取（doc 19.2 §8.1/§8.2）。"""

from contextlib import contextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import UUID

import pytest

from backend.assets import object_detail
from backend.assets.ddl_display import DefinitionInconsistentError
from backend.assets.object_detail import (
    ObjectNotFoundError,
    ProfileInconsistentError,
    get_object_ddl,
    get_object_detail,
)
from backend.data_sources.registration_service import SourceNotFoundError
from backend.persistence.platform_db import PlatformSettings
from metaweave.core.metadata.sample_block import build_comment_block

SOURCE_ID = UUID("b4a87275-7c37-4feb-bf1c-e5a8446c174f")
JSON_PUBLISH_ID = UUID("63221a9e-6a82-4311-a08c-9f74001da835")
DDL_PUBLISH_ID = UUID("84c017f4-b438-452b-a888-715b7f0cbea2")
JOB_ID = UUID("550e8400-e29b-41d4-a716-446655440000")
SETTINGS = PlatformSettings(
    host="platform", port=5432, name="metaweave", user="admin", password="secret"
)


def _json_publish(**overrides):
    values = {
        "publish_id": JSON_PUBLISH_ID,
        "job_id": JOB_ID,
        "source_id": SOURCE_ID,
        "step_name": "json",
        "status": "success",
        "is_current_publish": True,
        "created_at": datetime(2026, 10, 3, 4, 0, 0, tzinfo=timezone.utc),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _ddl_publish(**overrides):
    values = {
        "publish_id": DDL_PUBLISH_ID,
        "job_id": JOB_ID,
        "source_id": SOURCE_ID,
        "step_name": "ddl",
        "status": "success",
        "is_current_publish": True,
        "created_at": datetime(2026, 10, 3, 5, 0, 0, tzinfo=timezone.utc),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _profile_row(**overrides):
    values = {
        "source_id": SOURCE_ID,
        "schema_name": "public",
        "object_name": "orders",
        "db_name": "ordersdb",
        "object_type": "table",
        "publish_id": JSON_PUBLISH_ID,
        "job_id": JOB_ID,
        "metadata_version": "3.0",
        "generated_timestamp": datetime(2026, 10, 3, 18, 55, 55, tzinfo=timezone.utc),
        "loaded_at": datetime(2026, 10, 3, 4, 0, 5, tzinfo=timezone.utc),
        "object_comment": "订单表",
        "object_comment_source": "database",
        "total_rows": 500,
        "column_count": 2,
        "sample_method": "limit",
        "sample_count": 500,
        "has_table_profile": True,
        "table_category": "fact",
        "category_confidence": 0.95,
        "classification_source": "llm",
        "classification_reason": "包含度量值",
        "classification_basis": ["llm_inferred"],
        "rule_table_category": "fact",
        "rule_confidence": 0.85,
        "rule_inference_basis": ["fact_has_metric"],
        "primary_key_name": "orders_pkey",
        "primary_key_columns": ["order_id"],
        "foreign_key_count": 1,
        "index_count": 1,
        "constraint_count": 2,
        "foreign_keys": [
            {
                "constraint_name": "orders_user_fkey",
                "source_columns": ["order_id"],
                "target_schema": "public",
                "target_table": "users",
                "target_columns": ["user_id"],
            }
        ],
        "unique_constraints": [],
        "indexes": [
            {
                "index_name": "orders_pkey",
                "columns": ["order_id"],
                "included_columns": [],
                "is_unique": True,
                "is_primary": True,
                "is_constraint_backed": True,
                "constraint_name": "orders_pkey",
            }
        ],
        "unique_column_sets": [{"columns": ["order_id"], "confidence_score": 1.0}],
        "json_sample_records": {"sample_method": "limit", "records": []},
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _column_row(column_name="order_id", **overrides):
    values = {
        "source_id": SOURCE_ID,
        "schema_name": "public",
        "object_name": "orders",
        "publish_id": JSON_PUBLISH_ID,
        "column_name": column_name,
        "ordinal_position": 1 if column_name == "order_id" else 2,
        "data_type": "integer",
        "is_nullable": False,
        "column_default": None,
        "column_comment": "订单ID",
        "comment_source": "database",
        "semantic_role": "identifier",
        "semantic_confidence": 0.95,
        "semantic_basis": ["type_whitelist_passed"],
        "null_count": 0,
        "null_rate": 0.0,
        "unique_count": 500,
        "uniqueness": 1.0,
        "sample_min": "1",
        "sample_max": "500",
        "value_distribution": {"1": 10},
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _definition_row(
    define_text="CREATE TABLE public.orders (order_id integer);", **overrides
):
    values = {
        "source_id": SOURCE_ID,
        "schema_name": "public",
        "object_name": "orders",
        "define_type": "ddl",
        "db_name": "ordersdb",
        "object_type": "table",
        "define_text": define_text,
        "job_id": JOB_ID,
        "publish_id": DDL_PUBLISH_ID,
        "loaded_at": datetime(2026, 10, 3, 5, 0, 0, tzinfo=timezone.utc),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class FakeDetailSession:
    def __init__(self):
        self.source = SimpleNamespace(source_id=SOURCE_ID)
        self.publishes: list = []
        self.table_profiles: list = []
        self.table_column_profiles: list = []
        self.definitions: list = []

    def connection(self, execution_options=None):
        return SimpleNamespace(execution_options=execution_options)

    def get(self, model, key):
        if model.__name__ == "DbSourceInfo":
            return self.source if key == SOURCE_ID else None
        raise AssertionError(model)

    def scalars(self, statement):
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
            rows = [
                row
                for row in self.table_profiles
                if row.source_id == params.get("source_id_1")
            ]
            if "schema_name_1" in params:
                rows = [
                    row for row in rows if row.schema_name == params["schema_name_1"]
                ]
            if "object_name_1" in params:
                rows = [
                    row for row in rows if row.object_name == params["object_name_1"]
                ]
            return rows
        if entity.__name__ == "TableColumnProfile":
            rows = [
                row
                for row in self.table_column_profiles
                if row.source_id == params.get("source_id_1")
                and row.schema_name == params.get("schema_name_1")
                and row.object_name == params.get("object_name_1")
            ]
            return sorted(rows, key=lambda r: r.ordinal_position)
        if entity.__name__ == "TableDefinition":
            rows = [
                row
                for row in self.definitions
                if row.source_id == params.get("source_id_1")
                and row.schema_name == params.get("schema_name_1")
                and row.object_name == params.get("object_name_1")
                and row.define_type == params.get("define_type_1")
            ]
            return rows
        raise AssertionError(entity)


def install(monkeypatch, session):
    @contextmanager
    def fake_platform_session(_settings):
        yield session

    monkeypatch.setattr(object_detail, "platform_session", fake_platform_session)
    return session


def ready_session(**kwargs):
    session = FakeDetailSession()
    session.publishes.append(_json_publish())
    session.table_profiles.append(_profile_row(**kwargs))
    session.table_column_profiles.extend(
        [_column_row("order_id"), _column_row("amount", ordinal_position=2)]
    )
    return session


class TestGetObjectDetail:
    def test_source_missing_raises(self, monkeypatch):
        session = install(monkeypatch, FakeDetailSession())
        session.source = None
        with pytest.raises(SourceNotFoundError):
            get_object_detail(SETTINGS, SOURCE_ID, "public", "orders")

    def test_no_current_returns_not_published_payload(self, monkeypatch):
        install(monkeypatch, FakeDetailSession())
        result = get_object_detail(SETTINGS, SOURCE_ID, "public", "orders")
        assert result["state"] == "not_published"
        assert result["json_publish"] is None and result["profile"] is None
        assert result["ddl_state"] == "not_published" and result["ddl_publish"] is None
        assert result["columns"] == []

    def test_current_without_any_rows_raises_profile_inconsistent(self, monkeypatch):
        session = install(monkeypatch, FakeDetailSession())
        session.publishes.append(_json_publish())
        with pytest.raises(ProfileInconsistentError):
            get_object_detail(SETTINGS, SOURCE_ID, "public", "orders")

    def test_object_absent_raises_404_domain(self, monkeypatch):
        install(monkeypatch, ready_session())
        with pytest.raises(ObjectNotFoundError):
            get_object_detail(SETTINGS, SOURCE_ID, "public", "missing")

    def test_row_version_mismatch_raises_profile_inconsistent(self, monkeypatch):
        session = install(monkeypatch, FakeDetailSession())
        session.publishes.append(_json_publish())
        session.table_profiles.append(
            _profile_row(publish_id=UUID("11111111-1111-1111-1111-111111111111"))
        )
        with pytest.raises(ProfileInconsistentError):
            get_object_detail(SETTINGS, SOURCE_ID, "public", "orders")

    def test_column_version_mismatch_raises_profile_inconsistent(self, monkeypatch):
        session = install(monkeypatch, FakeDetailSession())
        session.publishes.append(_json_publish())
        session.table_profiles.append(_profile_row())
        session.table_column_profiles.extend(
            [
                _column_row("order_id"),
                _column_row(
                    "amount",
                    ordinal_position=2,
                    publish_id=UUID("11111111-1111-1111-1111-111111111111"),
                ),
            ]
        )
        with pytest.raises(ProfileInconsistentError):
            get_object_detail(SETTINGS, SOURCE_ID, "public", "orders")

    def test_column_count_mismatch_raises_profile_inconsistent(self, monkeypatch):
        session = install(monkeypatch, FakeDetailSession())
        session.publishes.append(_json_publish())
        session.table_profiles.append(_profile_row())
        session.table_column_profiles.append(_column_row("order_id"))
        with pytest.raises(ProfileInconsistentError):
            get_object_detail(SETTINGS, SOURCE_ID, "public", "orders")

    def test_ready_detail_projects_columns_in_order(self, monkeypatch):
        install(monkeypatch, ready_session())
        result = get_object_detail(SETTINGS, SOURCE_ID, "public", "orders")
        assert result["state"] == "ready"
        assert (
            result["json_publish"]["published_at"] == "2026-10-03T04:00:00.000000Z"
        )
        assert [c["column_name"] for c in result["columns"]] == ["order_id", "amount"]
        order_id = result["columns"][0]
        assert order_id["physical_constraints"]["primary_key_state"] == "present"
        assert order_id["physical_constraints"]["primary_key"] == {
            "constraint_name": "orders_pkey",
            "columns": ["order_id"],
        }
        assert (
            order_id["physical_constraints"]["foreign_keys"][0]["constraint_name"]
            == "orders_user_fkey"
        )
        amount = result["columns"][1]
        assert amount["physical_constraints"]["primary_key_state"] == "none"
        assert amount["physical_constraints"]["foreign_keys"] == []
        assert amount["indexes"] == []
        assert amount["unique_column_sets"] == []
        # §8.1.1：字段行返回 §6 的标量（仅排除 value_distribution），含身份四列
        assert order_id["source_id"] == str(SOURCE_ID)
        assert order_id["schema_name"] == "public"
        assert order_id["object_name"] == "orders"
        assert order_id["publish_id"] == str(JSON_PUBLISH_ID)
        # profile 排除 json_sample_records；字段排除 value_distribution
        assert "json_sample_records" not in result["profile"]
        assert all("value_distribution" not in c for c in result["columns"])
        assert result["profile"]["db_name"] == "ordersdb"
        assert order_id["indexes"][0]["index_name"] == "orders_pkey"

    def test_pk_node_missing_keeps_other_projections(self, monkeypatch):
        install(monkeypatch, ready_session(primary_key_columns=None))
        result = get_object_detail(SETTINGS, SOURCE_ID, "public", "orders")
        order_id = result["columns"][0]
        assert order_id["physical_constraints"]["primary_key_state"] == "unknown"
        assert order_id["physical_constraints"]["primary_key"] is None
        assert order_id["physical_constraints"]["foreign_keys"]  # 仍完整投影

    def test_null_nodes_project_none_not_empty(self, monkeypatch):
        session = install(monkeypatch, FakeDetailSession())
        session.publishes.append(_json_publish())
        session.table_profiles.append(
            _profile_row(indexes=None, unique_column_sets=None, foreign_keys=None)
        )
        session.table_column_profiles.append(_column_row("order_id"))
        session.table_profiles[0].column_count = 1
        result = get_object_detail(SETTINGS, SOURCE_ID, "public", "orders")
        column = result["columns"][0]
        assert column["indexes"] is None
        assert column["unique_column_sets"] is None
        assert column["physical_constraints"]["foreign_keys"] is None


class TestDdlSummary:
    def _session_with(self, publishes, definition):
        session = FakeDetailSession()
        session.publishes.append(_json_publish())
        session.table_profiles.append(_profile_row())
        session.table_column_profiles.extend(
            [_column_row("order_id"), _column_row("amount", ordinal_position=2)]
        )
        session.publishes.extend(publishes)
        if definition is not None:
            session.definitions.append(definition)
        return session

    def test_ddl_not_published_when_no_marker_and_no_row(self, monkeypatch):
        install(monkeypatch, self._session_with([], None))
        result = get_object_detail(SETTINGS, SOURCE_ID, "public", "orders")
        assert result["ddl_state"] == "not_published" and result["ddl_publish"] is None

    def test_ddl_not_published_when_marker_but_no_row(self, monkeypatch):
        install(monkeypatch, self._session_with([_ddl_publish()], None))
        result = get_object_detail(SETTINGS, SOURCE_ID, "public", "orders")
        assert result["ddl_state"] == "not_published"

    def test_ddl_inconsistent_orphan_row(self, monkeypatch):
        install(monkeypatch, self._session_with([], _definition_row()))
        result = get_object_detail(SETTINGS, SOURCE_ID, "public", "orders")
        assert result["ddl_state"] == "inconsistent" and result["ddl_publish"] is None

    def test_ddl_inconsistent_version_mismatch(self, monkeypatch):
        install(
            monkeypatch,
            self._session_with(
                [_ddl_publish()],
                _definition_row(
                    publish_id=UUID("11111111-1111-1111-1111-111111111111")
                ),
            ),
        )
        result = get_object_detail(SETTINGS, SOURCE_ID, "public", "orders")
        assert result["ddl_state"] == "inconsistent"

    def test_ddl_incompatible_identity(self, monkeypatch):
        install(
            monkeypatch,
            self._session_with([_ddl_publish()], _definition_row(db_name="otherdb")),
        )
        result = get_object_detail(SETTINGS, SOURCE_ID, "public", "orders")
        assert result["ddl_state"] == "incompatible"

    def test_ddl_ready_returns_publish_block(self, monkeypatch):
        install(monkeypatch, self._session_with([_ddl_publish()], _definition_row()))
        result = get_object_detail(SETTINGS, SOURCE_ID, "public", "orders")
        assert result["ddl_state"] == "ready"
        assert result["ddl_publish"]["publish_id"] == str(DDL_PUBLISH_ID)
        assert result["ddl_publish"]["loaded_at"] == "2026-10-03T05:00:00.000000Z"


class TestGetObjectDdl:
    def _ready(self, define_text):
        session = ready_session()
        session.publishes.append(_ddl_publish())
        session.definitions.append(_definition_row(define_text=define_text))
        return session

    def test_json_current_missing_raises_404(self, monkeypatch):
        install(monkeypatch, FakeDetailSession())
        with pytest.raises(ObjectNotFoundError):
            get_object_ddl(SETTINGS, SOURCE_ID, "public", "orders")

    def test_ddl_not_published_returns_empty_payload(self, monkeypatch):
        install(monkeypatch, ready_session())
        result = get_object_ddl(SETTINGS, SOURCE_ID, "public", "orders")
        assert result["state"] == "not_published"
        assert result["definition_sql"] is None and result["sample_state"] is None
        assert result["sample_columns"] == [] and result["sample_records"] == []
        assert result["json_publish_id"] == str(JSON_PUBLISH_ID)

    def test_ready_splits_sql_and_sample(self, monkeypatch):
        payload = {
            "object_type": "table",
            "object_name": "public.orders",
            "records": [{"order_id": "1"}, {"order_id": "2"}],
        }
        ddl = "CREATE TABLE public.orders (order_id integer);\n" + build_comment_block(
            "SAMPLED_RECORDS", payload
        )
        install(monkeypatch, self._ready(ddl))
        result = get_object_ddl(SETTINGS, SOURCE_ID, "public", "orders")
        assert result["state"] == "ready"
        assert result["definition_sql"] == (
            "CREATE TABLE public.orders (order_id integer);"
        )
        assert result["sample_state"] == "ready"
        assert result["sample_columns"] == ["order_id"]
        assert result["sample_records"] == [{"order_id": "1"}, {"order_id": "2"}]
        assert result["ddl_publish"]["publish_id"] == str(DDL_PUBLISH_ID)

    def test_ddl_inconsistent_raises_409_domain(self, monkeypatch):
        session = ready_session()
        session.definitions.append(_definition_row())  # 无 ddl current 标记
        install(monkeypatch, session)
        with pytest.raises(DefinitionInconsistentError):
            get_object_ddl(SETTINGS, SOURCE_ID, "public", "orders")

    def test_object_absent_raises_404(self, monkeypatch):
        install(monkeypatch, self._ready("CREATE TABLE public.orders (a int);"))
        with pytest.raises(ObjectNotFoundError):
            get_object_ddl(SETTINGS, SOURCE_ID, "public", "missing")


class TestSourceLevelConsistencyUnification:
    """详情与列表的源级检查统一（doc 19.2 §8.2）。"""

    def test_other_object_version_mismatch_raises(self, monkeypatch):
        """另一对象错版：详情也必须 409，不得 ready（与列表口径一致）。"""
        session = FakeDetailSession()
        session.publishes.append(_json_publish())
        session.table_profiles.append(_profile_row())
        session.table_column_profiles.extend(
            [_column_row("order_id"), _column_row("amount", ordinal_position=2)]
        )
        session.table_profiles.append(
            _profile_row(
                object_name="stale",
                publish_id=UUID("11111111-1111-1111-1111-111111111111"),
            )
        )
        install(monkeypatch, session)
        with pytest.raises(ProfileInconsistentError):
            get_object_detail(SETTINGS, SOURCE_ID, "public", "orders")

    def test_target_row_job_id_mismatch_raises(self, monkeypatch):
        session = FakeDetailSession()
        session.publishes.append(_json_publish())
        session.table_profiles.append(
            _profile_row(job_id=UUID("99999999-9999-9999-9999-999999999999"))
        )
        session.table_column_profiles.extend(
            [_column_row("order_id"), _column_row("amount", ordinal_position=2)]
        )
        install(monkeypatch, session)
        with pytest.raises(ProfileInconsistentError):
            get_object_detail(SETTINGS, SOURCE_ID, "public", "orders")

    def test_failed_json_current_row_is_not_published_for_detail(self, monkeypatch):
        session = FakeDetailSession()
        session.publishes.append(_json_publish(status="failed"))
        install(monkeypatch, session)
        result = get_object_detail(SETTINGS, SOURCE_ID, "public", "orders")
        assert result["state"] == "not_published"

    def test_failed_ddl_current_marker_treated_as_missing(self, monkeypatch):
        """ddl current 标记为 failed：视同无标记（孤儿行 → inconsistent）。"""
        session = FakeDetailSession()
        session.publishes.append(_json_publish())
        session.table_profiles.append(_profile_row())
        session.table_column_profiles.extend(
            [_column_row("order_id"), _column_row("amount", ordinal_position=2)]
        )
        session.publishes.append(_ddl_publish(status="failed"))
        session.definitions.append(_definition_row())
        install(monkeypatch, session)
        result = get_object_detail(SETTINGS, SOURCE_ID, "public", "orders")
        assert result["ddl_state"] == "inconsistent"
