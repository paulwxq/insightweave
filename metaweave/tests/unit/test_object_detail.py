"""对象详情与 DDL 摘要/正文读取（doc 19.2 §8.1/§8.2、6.1 §15–§17 新模型）。

新模型断言：table_profile/table_column_profile 行即当前内容；对象行与字段行
的 publish_run_id 一致性由本模块复核（_load_rows_checked / 字段核对）；
json_publish 与 ddl_publish 均以 publish_run_id 标识，published_at 取
PublishRunInfo.created_at；_ddl_summary 只有 not_published/incompatible/ready
三态，无 current 标记与 inconsistent 分支。
"""

from contextlib import contextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import UUID

import pytest

from backend.assets import object_detail
from backend.assets.object_detail import (
    ObjectNotFoundError,
    ProfileInconsistentError,
    get_object_ddl,
    get_object_detail,
)
from backend.data_sources.registration_service import SourceNotFoundError
from backend.persistence.platform_db import PlatformSettings
from metaweave.core.metadata.sample_block import build_comment_block

SOURCE_ID = "db_src_1"
JSON_RUN = UUID("63221a9e-6a82-4311-a08c-9f74001da835")
DDL_RUN = UUID("84c017f4-b438-452b-a888-715b7f0cbea2")
OTHER_RUN = UUID("11111111-1111-4111-8111-111111111111")
PUBLISHED_AT = datetime(2026, 10, 3, 4, 0, 0, tzinfo=timezone.utc)
SETTINGS = PlatformSettings(
    host="platform", port=5432, name="metaweave", user="admin", password="secret"
)


def _run_row(publish_run_id=JSON_RUN, created_at=PUBLISHED_AT):
    return SimpleNamespace(publish_run_id=publish_run_id, created_at=created_at)


def _profile_row(**overrides):
    values = {
        "source_id": SOURCE_ID,
        "schema_name": "public",
        "object_name": "orders",
        "db_name": "ordersdb",
        "object_type": "table",
        "publish_run_id": JSON_RUN,
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
        "publish_run_id": JSON_RUN,
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
        "publish_run_id": DDL_RUN,
        "loaded_at": datetime(2026, 10, 3, 5, 0, 0, tzinfo=timezone.utc),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class FakeDetailSession:
    def __init__(self, *, source_exists=True):
        self.source = SimpleNamespace(source_id=SOURCE_ID) if source_exists else None
        self.runs: dict = {}
        self.table_profiles: list = []
        self.table_column_profiles: list = []
        self.definitions: list = []

    def connection(self, execution_options=None):
        return SimpleNamespace(execution_options=execution_options)

    def get(self, model, key):
        if model.__name__ == "DbSourceInfo":
            return self.source if key == SOURCE_ID else None
        if model.__name__ == "PublishRunInfo":
            return self.runs.get(key)
        raise AssertionError(model)

    def scalars(self, statement):
        entity = statement.column_descriptions[0]["entity"]
        params = statement.compile().params
        if entity.__name__ == "TableProfile":
            return [
                row
                for row in self.table_profiles
                if row.source_id == params.get("source_id_1")
            ]
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
            return [
                row
                for row in self.definitions
                if row.source_id == params.get("source_id_1")
                and row.schema_name == params.get("schema_name_1")
                and row.object_name == params.get("object_name_1")
                and row.define_type == params.get("define_type_1")
            ]
        raise AssertionError(entity)


def install(monkeypatch, session):
    @contextmanager
    def fake_platform_session(_settings):
        yield session

    monkeypatch.setattr(object_detail, "platform_session", fake_platform_session)
    return session


def ready_session(**kwargs):
    session = FakeDetailSession()
    session.runs[JSON_RUN] = _run_row()
    session.table_profiles.append(_profile_row(**kwargs))
    session.table_column_profiles.extend(
        [_column_row("order_id"), _column_row("amount", ordinal_position=2)]
    )
    return session


class TestGetObjectDetail:
    def test_source_missing_raises(self, monkeypatch):
        session = install(monkeypatch, FakeDetailSession(source_exists=False))
        with pytest.raises(SourceNotFoundError):
            get_object_detail(SETTINGS, SOURCE_ID, "public", "orders")

    def test_no_rows_returns_not_published_payload(self, monkeypatch):
        install(monkeypatch, FakeDetailSession())
        result = get_object_detail(SETTINGS, SOURCE_ID, "public", "orders")
        assert result["state"] == "not_published"
        assert result["json_publish"] is None and result["profile"] is None
        assert result["ddl_state"] == "not_published" and result["ddl_publish"] is None
        assert result["columns"] == []

    def test_object_absent_raises_404_domain(self, monkeypatch):
        install(monkeypatch, ready_session())
        with pytest.raises(ObjectNotFoundError):
            get_object_detail(SETTINGS, SOURCE_ID, "public", "missing")

    def test_other_object_row_run_mismatch_raises_profile_inconsistent(
        self, monkeypatch
    ):
        """源内任一对象行 publish_run_id 不一致 → 409（_load_rows_checked）。"""
        session = FakeDetailSession()
        session.table_profiles.append(_profile_row())
        session.table_profiles.append(
            _profile_row(object_name="stale", publish_run_id=OTHER_RUN)
        )
        session.table_column_profiles.extend(
            [_column_row("order_id"), _column_row("amount", ordinal_position=2)]
        )
        install(monkeypatch, session)
        with pytest.raises(ProfileInconsistentError, match="对象行发布来源不一致"):
            get_object_detail(SETTINGS, SOURCE_ID, "public", "orders")

    def test_column_run_mismatch_raises_profile_inconsistent(self, monkeypatch):
        session = FakeDetailSession()
        session.table_profiles.append(_profile_row())
        session.table_column_profiles.extend(
            [
                _column_row("order_id"),
                _column_row("amount", ordinal_position=2, publish_run_id=OTHER_RUN),
            ]
        )
        install(monkeypatch, session)
        with pytest.raises(ProfileInconsistentError, match="字段行版本或数量不符"):
            get_object_detail(SETTINGS, SOURCE_ID, "public", "orders")

    def test_column_count_mismatch_raises_profile_inconsistent(self, monkeypatch):
        session = FakeDetailSession()
        session.table_profiles.append(_profile_row())
        session.table_column_profiles.append(_column_row("order_id"))
        install(monkeypatch, session)
        with pytest.raises(ProfileInconsistentError, match="字段行版本或数量不符"):
            get_object_detail(SETTINGS, SOURCE_ID, "public", "orders")

    def test_ready_detail_projects_columns_in_order(self, monkeypatch):
        install(monkeypatch, ready_session())
        result = get_object_detail(SETTINGS, SOURCE_ID, "public", "orders")
        assert result["state"] == "ready"
        # 发布来源取行上 publish_run_id；published_at 取 PublishRunInfo.created_at
        assert result["json_publish"] == {
            "publish_run_id": str(JSON_RUN),
            "published_at": "2026-10-03T04:00:00.000000Z",
        }
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
        assert order_id["source_id"] == SOURCE_ID
        assert order_id["schema_name"] == "public"
        assert order_id["object_name"] == "orders"
        assert order_id["publish_run_id"] == str(JSON_RUN)
        # profile 排除 json_sample_records；字段排除 value_distribution
        assert "json_sample_records" not in result["profile"]
        assert all("value_distribution" not in c for c in result["columns"])
        assert result["profile"]["db_name"] == "ordersdb"
        assert result["profile"]["publish_run_id"] == str(JSON_RUN)
        assert order_id["indexes"][0]["index_name"] == "orders_pkey"

    def test_missing_run_row_published_at_is_null(self, monkeypatch):
        """画像行指向的发布运行行缺失：仍 ready，published_at 为 null。"""
        session = FakeDetailSession()
        session.table_profiles.append(_profile_row())
        session.table_column_profiles.extend(
            [_column_row("order_id"), _column_row("amount", ordinal_position=2)]
        )
        install(monkeypatch, session)  # runs 为空
        result = get_object_detail(SETTINGS, SOURCE_ID, "public", "orders")
        assert result["state"] == "ready"
        assert result["json_publish"]["publish_run_id"] == str(JSON_RUN)
        assert result["json_publish"]["published_at"] is None

    def test_pk_node_missing_keeps_other_projections(self, monkeypatch):
        install(monkeypatch, ready_session(primary_key_columns=None))
        result = get_object_detail(SETTINGS, SOURCE_ID, "public", "orders")
        order_id = result["columns"][0]
        assert order_id["physical_constraints"]["primary_key_state"] == "unknown"
        assert order_id["physical_constraints"]["primary_key"] is None
        assert order_id["physical_constraints"]["foreign_keys"]  # 仍完整投影

    def test_null_nodes_project_none_not_empty(self, monkeypatch):
        session = FakeDetailSession()
        session.table_profiles.append(
            _profile_row(indexes=None, unique_column_sets=None, foreign_keys=None)
        )
        session.table_column_profiles.append(_column_row("order_id"))
        session.table_profiles[0].column_count = 1
        install(monkeypatch, session)
        result = get_object_detail(SETTINGS, SOURCE_ID, "public", "orders")
        column = result["columns"][0]
        assert column["indexes"] is None
        assert column["unique_column_sets"] is None
        assert column["physical_constraints"]["foreign_keys"] is None


class TestDdlSummary:
    """新模型：DDL 行即当前定义，无 current 标记，无 inconsistent 态。"""

    def _session_with(self, definition):
        session = FakeDetailSession()
        session.runs[JSON_RUN] = _run_row()
        session.table_profiles.append(_profile_row())
        session.table_column_profiles.extend(
            [_column_row("order_id"), _column_row("amount", ordinal_position=2)]
        )
        if definition is not None:
            session.definitions.append(definition)
        return session

    def test_ddl_not_published_when_no_row(self, monkeypatch):
        install(monkeypatch, self._session_with(None))
        result = get_object_detail(SETTINGS, SOURCE_ID, "public", "orders")
        assert result["ddl_state"] == "not_published" and result["ddl_publish"] is None

    def test_ddl_row_with_other_run_id_is_still_ready(self, monkeypatch):
        """DDL 行发布来源独立于 JSON 行（doc 6.1 §15）：不比对两者 run id。"""
        install(
            monkeypatch,
            self._session_with(_definition_row(publish_run_id=OTHER_RUN)),
        )
        result = get_object_detail(SETTINGS, SOURCE_ID, "public", "orders")
        assert result["ddl_state"] == "ready"
        assert result["ddl_publish"]["publish_run_id"] == str(OTHER_RUN)

    def test_ddl_incompatible_identity(self, monkeypatch):
        install(
            monkeypatch,
            self._session_with(_definition_row(db_name="otherdb")),
        )
        result = get_object_detail(SETTINGS, SOURCE_ID, "public", "orders")
        assert result["ddl_state"] == "incompatible"
        assert result["ddl_publish"] is None

    def test_ddl_ready_returns_publish_block(self, monkeypatch):
        session = self._session_with(_definition_row())
        session.runs[DDL_RUN] = _run_row(DDL_RUN)
        install(monkeypatch, session)
        result = get_object_detail(SETTINGS, SOURCE_ID, "public", "orders")
        assert result["ddl_state"] == "ready"
        assert result["ddl_publish"]["publish_run_id"] == str(DDL_RUN)
        assert result["ddl_publish"]["loaded_at"] == "2026-10-03T05:00:00.000000Z"


class TestGetObjectDdl:
    def _ready(self, define_text):
        session = ready_session()
        session.definitions.append(_definition_row(define_text=define_text))
        return session

    def test_json_rows_missing_raises_404(self, monkeypatch):
        install(monkeypatch, FakeDetailSession())
        with pytest.raises(ObjectNotFoundError):
            get_object_ddl(SETTINGS, SOURCE_ID, "public", "orders")

    def test_ddl_not_published_returns_empty_payload(self, monkeypatch):
        install(monkeypatch, ready_session())
        result = get_object_ddl(SETTINGS, SOURCE_ID, "public", "orders")
        assert result["state"] == "not_published"
        assert result["definition_sql"] is None and result["sample_state"] is None
        assert result["sample_columns"] == [] and result["sample_records"] == []
        assert result["json_publish_run_id"] == str(JSON_RUN)

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
        assert result["ddl_publish"]["publish_run_id"] == str(DDL_RUN)
        assert result["json_publish_run_id"] == str(JSON_RUN)

    def test_object_absent_raises_404(self, monkeypatch):
        install(monkeypatch, self._ready("CREATE TABLE public.orders (a int);"))
        with pytest.raises(ObjectNotFoundError):
            get_object_ddl(SETTINGS, SOURCE_ID, "public", "missing")

    def test_object_rows_inconsistent_raises_409_domain(self, monkeypatch):
        """对象行 run 不一致：DDL 正文读取同样先过 _load_rows_checked。"""
        session = self._ready("CREATE TABLE public.orders (a int);")
        session.table_profiles.append(
            _profile_row(object_name="stale", publish_run_id=OTHER_RUN)
        )
        install(monkeypatch, session)
        with pytest.raises(ProfileInconsistentError):
            get_object_ddl(SETTINGS, SOURCE_ID, "public", "orders")
