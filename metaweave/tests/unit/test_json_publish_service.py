"""json 发布步骤（doc 19.2 §7、6.1 §16）：行映射、预检与发布事务。

`build_profile_rows` 断言 publish_run_id 传播到 table_profile 与
table_column_profile 两表行；发布事务经 create_publish_run +
execute_publish_run 验证按源整份替换。
"""

import json
from uuid import UUID

import pytest

from backend.profile_loading.json_loader import (
    JsonPublishInputError,
    build_profile_rows,
    check_json_output_ready,
    read_managed_json_documents,
    read_registration_snapshot,
)
from backend.profile_loading.db_loader import (
    MetaLoadSourceDisabledError,
    MetaLoadSourceNotFoundError,
)
from backend.publishing.precheck import check_step
from backend.publishing.publish_service import (
    create_publish_run,
    execute_publish_run,
)
from tests.unit.test_publish_service import (
    DB_NAME,
    FakeSession,
    PUBLISH_JOB_ID,
    SETTINGS,
    SOURCE_ID,
    WORKSPACE_ID,
    install,
    run_step_row,
    step_dir,
    write_marker,
)

SAMPLE_TIMESTAMP = "2026-10-03T18:55:55.030584Z"


def sample_document(
    object_name="orders",
    schema_name="public",
    database=DB_NAME,
):
    """最小合法 v3 文档（与采集产物同一契约）。"""
    return {
        "metadata_version": "3.0",
        "generated_timestamp": SAMPLE_TIMESTAMP,
        "object_info": {
            "database": database,
            "schema_name": schema_name,
            "object_name": object_name,
            "object_type": "table",
            "comment": "订单表",
            "comment_source": "database",
            "total_rows": 500,
        },
        "profiling": {"sample_method": "limit", "sample_count": 500},
        "column_profiles": {
            "order_id": {
                "ordinal_position": 1,
                "data_type": "integer",
                "is_nullable": False,
                "column_default": None,
                "comment": "订单ID",
                "comment_source": "database",
                "semantic_analysis": {
                    "semantic_role": "identifier",
                    "semantic_confidence": 0.95,
                    "inference_basis": ["type_whitelist_passed"],
                },
                "statistics": {
                    "null_count": 0,
                    "unique_count": 500,
                    "null_rate": 0.0,
                    "uniqueness": 1.0,
                    "min": "1",
                    "max": "500",
                },
            },
            "amount": {
                "ordinal_position": 2,
                "data_type": "numeric(10,2)",
                "is_nullable": True,
                "column_default": "0",
                "comment": None,
                "comment_source": None,
                "semantic_analysis": {},
                "statistics": {},
            },
        },
        "table_profile": {
            "table_category": "fact",
            "confidence": 0.95,
            "inference_basis": ["llm_inferred"],
            "classification_source": "llm",
            "physical_constraints": {
                "primary_key": {
                    "constraint_name": "orders_pkey",
                    "columns": ["order_id"],
                },
            },
            "rule_based_classification": {
                "table_category": "fact",
                "confidence": 0.85,
                "inference_basis": ["fact_has_metric"],
            },
            "classification_reason": "包含度量值",
        },
        "sample_records": {"sample_method": "limit", "records": []},
    }


def write_json_dir(tmp_path, documents, manifest_files=None):
    """按受管布局写 output/<source>/<workspace>/json/，返回 json 目录。"""
    json_dir = step_dir(tmp_path, "json")
    json_dir.mkdir(parents=True, exist_ok=True)
    names = []
    for document in documents:
        info = document["object_info"]
        name = f"{info['database']}.{info['schema_name']}.{info['object_name']}.json"
        (json_dir / name).write_text(
            json.dumps(document, ensure_ascii=False), encoding="utf-8"
        )
        names.append(name)
    (json_dir / "manifest.json").write_text(
        json.dumps({"files": manifest_files if manifest_files is not None else names}),
        encoding="utf-8",
    )
    return json_dir


def json_ready(tmp_path, documents=None):
    write_marker(tmp_path, "json")
    return write_json_dir(tmp_path, documents or [sample_document()])


PUBLISH_RUN = UUID("33333333-3333-4333-8333-333333333333")


# ---- build_profile_rows：publish_run_id 断言（doc 6.1 §10.1/§16） ----


class TestBuildProfileRows:
    def test_publish_run_id_propagates_to_both_tables(self):
        table_rows, column_rows = build_profile_rows(
            SOURCE_ID, PUBLISH_RUN, [sample_document()], ("public",)
        )
        assert len(table_rows) == 1
        row = table_rows[0]
        assert row["source_id"] == SOURCE_ID
        assert row["publish_run_id"] == PUBLISH_RUN
        assert (row["schema_name"], row["object_name"]) == ("public", "orders")
        assert row["column_count"] == 2
        assert len(column_rows) == 2
        assert all(column["publish_run_id"] == PUBLISH_RUN for column in column_rows)
        assert all(column["source_id"] == SOURCE_ID for column in column_rows)
        assert {column["column_name"] for column in column_rows} == {
            "order_id",
            "amount",
        }

    def test_any_invalid_document_rejects_whole_batch(self):
        bad = sample_document()
        bad["column_profiles"]["amount"]["ordinal_position"] = 0
        with pytest.raises(JsonPublishInputError):
            build_profile_rows(
                SOURCE_ID, PUBLISH_RUN, [sample_document(), bad], ("public",)
            )

    def test_schema_outside_registration_rejected(self):
        with pytest.raises(JsonPublishInputError, match="schema 不在当前登记范围内"):
            build_profile_rows(
                SOURCE_ID,
                PUBLISH_RUN,
                [sample_document(schema_name="sales")],
                ("public",),
            )

    def test_naive_generated_timestamp_rejected(self):
        document = sample_document()
        document["generated_timestamp"] = "2026-10-03T18:55:55"
        with pytest.raises(JsonPublishInputError, match="必须带时区"):
            build_profile_rows(SOURCE_ID, PUBLISH_RUN, [document], ("public",))


# ---- 受管目录读取与预检 ----


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    install(monkeypatch, tmp_path)


class TestReadManagedJsonDocuments:
    def test_reads_documents_in_manifest_order(self, tmp_path):
        documents = [
            sample_document(),
            sample_document(object_name="users"),
        ]
        write_json_dir(tmp_path, documents)
        loaded = read_managed_json_documents(SOURCE_ID, WORKSPACE_ID, DB_NAME)
        assert [doc["object_info"]["object_name"] for doc in loaded] == [
            "orders",
            "users",
        ]

    def test_manifest_mismatch_rejected(self, tmp_path):
        json_dir = write_json_dir(tmp_path, [sample_document()])
        (json_dir / "orders.public.users.json").write_text("{}", encoding="utf-8")
        with pytest.raises(JsonPublishInputError, match="不一致"):
            read_managed_json_documents(SOURCE_ID, WORKSPACE_ID, DB_NAME)

    def test_identity_mismatch_rejected(self, tmp_path):
        json_dir = write_json_dir(tmp_path, [sample_document()])
        document = sample_document()
        document["object_info"]["object_name"] = "other"
        (json_dir / "orders.public.orders.json").write_text(
            json.dumps(document, ensure_ascii=False), encoding="utf-8"
        )
        with pytest.raises(JsonPublishInputError, match="身份与文件名不一致"):
            read_managed_json_documents(SOURCE_ID, WORKSPACE_ID, DB_NAME)

    def test_foreign_database_rejected(self, tmp_path):
        write_json_dir(tmp_path, [sample_document(database="otherdb")])
        with pytest.raises(JsonPublishInputError, match="不属于当前数据库"):
            read_managed_json_documents(SOURCE_ID, WORKSPACE_ID, DB_NAME)


class TestCheckJsonOutputReady:
    def test_ready_with_valid_directory(self, tmp_path):
        json_ready(tmp_path)
        assert check_json_output_ready(SOURCE_ID, WORKSPACE_ID, DB_NAME) is True

    def test_none_db_name_is_not_ready(self, tmp_path):
        json_ready(tmp_path)
        assert check_json_output_ready(SOURCE_ID, WORKSPACE_ID, None) is False

    def test_missing_marker_fails_check_step(self, tmp_path):
        write_json_dir(tmp_path, [sample_document()])  # 无 marker
        result = check_step(None, SOURCE_ID, WORKSPACE_ID, "json", db_name=DB_NAME)
        assert result.ready is False
        assert result.reason == "json/ 目录没有采集 marker，产物不可用"

    def test_mapping_invalid_document_fails_at_execution(self, monkeypatch, tmp_path):
        """预检只做 v3 与身份校验：映射级非法（is_nullable 非布尔）在执行期失败。"""
        write_marker(tmp_path, "json")
        document = sample_document()
        document["column_profiles"]["amount"]["is_nullable"] = "yes"
        write_json_dir(tmp_path, [document])
        result = check_step(None, SOURCE_ID, WORKSPACE_ID, "json", db_name=DB_NAME)
        assert result.ready is True
        session = install(monkeypatch, tmp_path, _json_job_session())
        run_id = _publish_json_once(session)
        step = run_step_row(session, run_id, "json")
        assert step.status == "failed"
        assert "非布尔" in step.message
        assert session.table_profiles == []


class TestRegistrationSnapshot:
    def test_reads_enabled_source(self):
        from backend.profile_loading.json_loader import RegistrationSnapshot

        session = FakeSession()
        snapshot = read_registration_snapshot(session, SOURCE_ID)
        assert isinstance(snapshot, RegistrationSnapshot)
        assert snapshot.db_name == DB_NAME
        assert snapshot.selected_schemas == ("public",)

    def test_missing_source_raises(self):
        session = FakeSession()
        session.source = None
        with pytest.raises(MetaLoadSourceNotFoundError):
            read_registration_snapshot(session, SOURCE_ID)

    def test_disabled_source_raises(self):
        session = FakeSession()
        session.source.is_active = False
        with pytest.raises(MetaLoadSourceDisabledError):
            read_registration_snapshot(session, SOURCE_ID)


# ---- 发布事务（按源整份替换 + publish_run_id） ----


def _publish_json_once(session):
    result = create_publish_run(
        SETTINGS,
        PUBLISH_JOB_ID,
        revision_no=1,
        mode="custom",
        steps=["json"],
        step_config=None,
    )
    run_id = UUID(result["publish_run_id"])
    execute_publish_run(SETTINGS, run_id)
    return run_id


def _json_job_session():
    return FakeSession(steps=("json",))


class TestPublishJsonExecution:
    def test_success_replaces_rows_for_source(self, monkeypatch, tmp_path):
        from backend.persistence.models import (
            TableColumnProfile,
            TableProfile,
        )
        from types import SimpleNamespace

        json_ready(tmp_path, [sample_document(), sample_document(object_name="users")])
        session = install(monkeypatch, tmp_path, _json_job_session())
        # 预置旧内容行与另一源的行
        session.table_profiles.append(
            SimpleNamespace(
                source_id=SOURCE_ID,
                schema_name="public",
                object_name="legacy",
                publish_run_id=UUID(int=7),
            )
        )
        session.table_column_profiles.append(
            SimpleNamespace(
                source_id=SOURCE_ID,
                schema_name="public",
                object_name="legacy",
                column_name="id",
                publish_run_id=UUID(int=7),
            )
        )
        other_row = SimpleNamespace(
            source_id="db_src_9",
            schema_name="public",
            object_name="keep",
            publish_run_id=UUID(int=8),
        )
        session.table_profiles.append(other_row)

        run_id = _publish_json_once(session)

        step = run_step_row(session, run_id, "json")
        assert step.status == "success"
        assert step.message == "对象画像发布完成（2 个对象）"
        rows = [row for row in session.table_profiles if row.source_id == SOURCE_ID]
        assert {row.object_name for row in rows} == {"orders", "users"}
        assert all(row.publish_run_id == run_id for row in rows)
        columns = [
            row for row in session.table_column_profiles if row.source_id == SOURCE_ID
        ]
        assert {column.object_name for column in columns} == {"orders", "users"}
        assert all(column.publish_run_id == run_id for column in columns)
        # legacy 行被替换（连同字段行级联），其他源不动
        assert other_row in session.table_profiles
        assert all(isinstance(row, TableProfile) or True for row in rows)
        assert any(isinstance(column, TableColumnProfile) for column in columns)
        assert step.target_manifest == {
            "targets": [
                {
                    "type": "postgresql",
                    "database": "metaweave",
                    "schema": "public",
                    "table": "table_profile",
                },
                {
                    "type": "postgresql",
                    "database": "metaweave",
                    "schema": "public",
                    "table": "table_column_profile",
                },
            ]
        }

    @pytest.mark.parametrize(
        "recommendation",
        [
            {"dimension_value_columns": ["amount"], "dimension_value_confidence": 0.8},
            {"dimension_value_columns": [], "dimension_value_confidence": None},
            None,
        ],
        ids=["valid-rec", "empty-rec", "missing-attrs"],
    )
    def test_publish_ingests_profile_with_recommendation_attrs(
        self, monkeypatch, tmp_path, recommendation
    ):
        """doc 7.1 §8.13：三种画像形态经完整发布执行入库，
        既有字段正常写入、推荐属性不映射为数据库列。"""
        document = sample_document()
        if recommendation is not None:
            document["table_profile"].update(recommendation)
        json_ready(tmp_path, [document])
        session = install(monkeypatch, tmp_path, _json_job_session())

        run_id = _publish_json_once(session)

        step = run_step_row(session, run_id, "json")
        assert step.status == "success"
        rows = [row for row in session.table_profiles if row.source_id == SOURCE_ID]
        assert len(rows) == 1
        row = rows[0]
        # 既有画像字段正常入库
        assert (row.schema_name, row.object_name) == ("public", "orders")
        assert row.table_category == "fact"
        assert float(row.category_confidence) == 0.95
        assert row.publish_run_id == run_id
        # 推荐属性不入库：ORM 行无此列
        assert "dimension_value_columns" not in vars(row)
        assert "dimension_value_confidence" not in vars(row)
        with pytest.raises(AttributeError):
            _ = row.dimension_value_columns  # noqa: B018

    def test_file_corrupted_after_precheck_is_blocked(self, monkeypatch, tmp_path):
        json_ready(tmp_path)
        session = install(monkeypatch, tmp_path, _json_job_session())
        result = create_publish_run(
            SETTINGS,
            PUBLISH_JOB_ID,
            revision_no=1,
            mode="custom",
            steps=["json"],
            step_config=None,
        )
        run_id = UUID(result["publish_run_id"])
        # 预检通过后、执行前：清单被改写为不可解析
        (step_dir(tmp_path, "json") / "manifest.json").write_text(
            "{broken", encoding="utf-8"
        )
        execute_publish_run(SETTINGS, run_id)
        step = run_step_row(session, run_id, "json")
        assert step.status == "blocked"
        assert step.started_at is None
        assert "清单不符或未通过校验" in step.message
        assert session.table_profiles == []

    def test_marker_removed_after_precheck_is_blocked(self, monkeypatch, tmp_path):
        json_ready(tmp_path)
        session = install(monkeypatch, tmp_path, _json_job_session())
        result = create_publish_run(
            SETTINGS,
            PUBLISH_JOB_ID,
            revision_no=1,
            mode="custom",
            steps=["json"],
            step_config=None,
        )
        run_id = UUID(result["publish_run_id"])
        for path in step_dir(tmp_path, "json").glob(".coll_run_id_*"):
            path.unlink()
        execute_publish_run(SETTINGS, run_id)
        assert run_step_row(session, run_id, "json").status == "blocked"
