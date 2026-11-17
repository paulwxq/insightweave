"""受管 JSON 发布加载器（doc 19.2 §7.2/§7.3）：快照读取、校验与行映射。"""

import json
import os
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import UUID

import pytest

from backend.profile_loading import json_loader
from backend.profile_loading.json_loader import (
    JsonPublishInputError,
    build_profile_rows,
    read_managed_json_documents,
    read_registration_snapshot,
)

# 新模型身份：source/workspace 短 ID + 发布运行 UUID（doc 6 §1）
SOURCE_ID = "db_src_1"
JOB_ID = "ws_1"  # 工作区定位槽位
PUBLISH_ID = UUID("63221a9e-6a82-4311-a08c-9f74001da835")
DB_NAME = "ordersdb"
RUN_MARKER = datetime(2026, 10, 3, 10, 0, 0, tzinfo=timezone.utc)


def sample_document(
    object_name="orders",
    schema_name="public",
    database=DB_NAME,
    table_profile="default",
    columns="default",
):
    """构造最小合法 v3 文档；table_profile/columns 传 "default" 以外值可覆盖。"""
    if columns == "default":
        columns = {
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
        }
    if table_profile == "default":
        table_profile = {
            "table_category": "fact",
            "confidence": 0.95,
            "inference_basis": ["llm_inferred"],
            "classification_source": "llm",
            "physical_constraints": {
                "primary_key": {
                    "constraint_name": "orders_pkey",
                    "columns": ["order_id"],
                },
                "foreign_keys": [
                    {
                        "constraint_name": "orders_user_fkey",
                        "source_columns": ["order_id"],
                        "target_schema": "public",
                        "target_table": "users",
                        "target_columns": ["user_id"],
                    }
                ],
                "unique_constraints": [
                    {"constraint_name": "orders_amount_key", "columns": ["amount"]}
                ],
            },
            "indexes": [
                {
                    "index_name": "orders_pkey",
                    "index_type": "btree",
                    "columns": ["order_id"],
                    "is_unique": True,
                    "is_primary": True,
                    "is_constraint_backed": True,
                    "constraint_name": "orders_pkey",
                    "included_columns": [],
                    "key_expressions": ["order_id"],
                    "condition": None,
                    "definition": "CREATE UNIQUE INDEX orders_pkey ON public.orders(order_id)",
                }
            ],
            "unique_column_sets": [
                {
                    "columns": ["order_id"],
                    "confidence_score": 1.0,
                    "uniqueness": 1.0,
                    "null_rate": 0.0,
                }
            ],
            "rule_based_classification": {
                "table_category": "fact",
                "confidence": 0.85,
                "inference_basis": ["fact_has_metric"],
            },
            "classification_reason": "包含度量值",
        }
    return {
        "metadata_version": "3.0",
        "generated_timestamp": "2026-10-03T18:55:55.030584Z",
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
        "column_profiles": columns,
        "table_profile": table_profile,
        "sample_records": {"sample_method": "limit", "records": []},
    }


def write_json_dir(tmp_path, documents, db=DB_NAME, manifest_files=None):
    """按受管布局写 output/<source>/<job>/json/，返回 json 目录。"""
    json_dir = tmp_path / "output" / str(SOURCE_ID) / str(JOB_ID) / "json"
    json_dir.mkdir(parents=True)
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


@pytest.fixture(autouse=True)
def project_root(tmp_path, monkeypatch):
    from metaweave.utils import file_utils

    monkeypatch.setattr(file_utils, "get_project_root", lambda: tmp_path)


def _patch_context(monkeypatch, *, active=True, exists=True):
    source = (
        SimpleNamespace(
            is_active=active,
            updated_at=RUN_MARKER,
            db_name=DB_NAME,
            selected_schemas=["public"],
        )
        if exists
        else None
    )
    monkeypatch.setattr(json_loader, "get_db_source", lambda session, sid: source)
    return source


class TestReadRegistrationSnapshot:
    def test_snapshot_fields(self, monkeypatch):
        _patch_context(monkeypatch)
        snapshot = read_registration_snapshot(object(), SOURCE_ID)
        assert snapshot.updated_at == RUN_MARKER
        assert snapshot.db_name == DB_NAME
        assert snapshot.selected_schemas == ("public",)


class TestReadManagedJsonDocuments:
    def test_returns_validated_snapshots(self, tmp_path):
        documents = [sample_document(), sample_document(object_name="users")]
        write_json_dir(tmp_path, documents)
        result = read_managed_json_documents(SOURCE_ID, JOB_ID, DB_NAME)
        assert [doc["object_info"]["object_name"] for doc in result] == [
            "orders",
            "users",
        ]

    def test_missing_manifest_rejected(self, tmp_path):
        json_dir = write_json_dir(tmp_path, [sample_document()])
        os.remove(json_dir / "manifest.json")
        with pytest.raises(JsonPublishInputError):
            read_managed_json_documents(SOURCE_ID, JOB_ID, DB_NAME)

    def test_missing_file_rejected(self, tmp_path):
        json_dir = write_json_dir(tmp_path, [sample_document()])
        os.remove(json_dir / f"{DB_NAME}.public.orders.json")
        with pytest.raises(JsonPublishInputError):
            read_managed_json_documents(SOURCE_ID, JOB_ID, DB_NAME)

    def test_extra_file_rejected(self, tmp_path):
        write_json_dir(tmp_path, [sample_document()])
        json_dir = tmp_path / "output" / str(SOURCE_ID) / str(JOB_ID) / "json"
        (json_dir / f"{DB_NAME}.public.extra.json").write_text("{}", encoding="utf-8")
        with pytest.raises(JsonPublishInputError):
            read_managed_json_documents(SOURCE_ID, JOB_ID, DB_NAME)

    def test_symlink_rejected(self, tmp_path):
        json_dir = write_json_dir(tmp_path, [sample_document()])
        target = json_dir / f"{DB_NAME}.public.orders.json"
        os.remove(target)
        os.symlink("/etc/hostname", target)
        with pytest.raises(JsonPublishInputError):
            read_managed_json_documents(SOURCE_ID, JOB_ID, DB_NAME)

    def test_mixed_database_rejected(self, tmp_path):
        write_json_dir(tmp_path, [sample_document(), sample_document(database="other")])
        with pytest.raises(JsonPublishInputError):
            read_managed_json_documents(SOURCE_ID, JOB_ID, DB_NAME)

    def test_identity_mismatch_with_filename_rejected(self, tmp_path):
        # 文件名保持 orders，内容声明 renamed：身份与文件名不一致
        document = sample_document()
        write_json_dir(tmp_path, [document])
        json_dir = tmp_path / "output" / str(SOURCE_ID) / str(JOB_ID) / "json"
        path = json_dir / f"{DB_NAME}.public.orders.json"
        document["object_info"]["object_name"] = "renamed"
        path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
        with pytest.raises(JsonPublishInputError):
            read_managed_json_documents(SOURCE_ID, JOB_ID, DB_NAME)

    def test_non_v3_rejected(self, tmp_path):
        document = sample_document()
        document["metadata_version"] = "2.0"
        write_json_dir(tmp_path, [document])
        with pytest.raises(JsonPublishInputError):
            read_managed_json_documents(SOURCE_ID, JOB_ID, DB_NAME)


class TestBuildProfileRowsValidation:
    def _build(self, documents, selected_schemas=("public",)):
        return build_profile_rows(SOURCE_ID, PUBLISH_ID, documents, selected_schemas)

    def test_empty_column_profiles_rejected(self):
        document = sample_document()
        document["column_profiles"] = {}
        with pytest.raises(JsonPublishInputError):
            self._build([document])

    def test_zero_or_duplicate_ordinal_rejected(self):
        document = sample_document()
        document["column_profiles"]["amount"]["ordinal_position"] = 1
        with pytest.raises(JsonPublishInputError):
            self._build([document])
        document["column_profiles"]["amount"]["ordinal_position"] = 0
        with pytest.raises(JsonPublishInputError):
            self._build([document])

    def test_ordinal_gap_allowed(self):
        document = sample_document()
        document["column_profiles"]["amount"]["ordinal_position"] = 3
        table_rows, column_rows = self._build([document])
        assert len(column_rows) == 2

    def test_bool_as_count_rejected(self):
        document = sample_document()
        document["object_info"]["total_rows"] = True
        with pytest.raises(JsonPublishInputError):
            self._build([document])

    def test_nullable_must_be_bool(self):
        document = sample_document()
        document["column_profiles"]["amount"]["is_nullable"] = "yes"
        with pytest.raises(JsonPublishInputError):
            self._build([document])

    def test_confidence_out_of_range_rejected(self):
        document = sample_document()
        document["table_profile"]["confidence"] = 1.5
        with pytest.raises(JsonPublishInputError):
            self._build([document])

    def test_ratio_bool_rejected(self):
        document = sample_document()
        document["column_profiles"]["order_id"]["statistics"]["null_rate"] = True
        with pytest.raises(JsonPublishInputError):
            self._build([document])

    def test_inference_basis_must_be_string_array(self):
        document = sample_document()
        document["table_profile"]["inference_basis"] = "llm"
        with pytest.raises(JsonPublishInputError):
            self._build([document])

    def test_bad_generated_timestamp_rejected(self):
        document = sample_document()
        document["generated_timestamp"] = "not-a-time"
        with pytest.raises(JsonPublishInputError):
            self._build([document])

    def test_unsupported_object_type_rejected(self):
        document = sample_document()
        document["object_info"]["object_type"] = "sequence"
        with pytest.raises(JsonPublishInputError):
            self._build([document])

    def test_schema_outside_selected_rejected(self):
        document = sample_document(schema_name="other")
        with pytest.raises(JsonPublishInputError):
            self._build([document])

    def test_pk_referencing_unknown_column_rejected(self):
        document = sample_document()
        document["table_profile"]["physical_constraints"]["primary_key"] = {
            "constraint_name": "pk",
            "columns": ["no_such"],
        }
        with pytest.raises(JsonPublishInputError):
            self._build([document])

    def test_fk_length_mismatch_rejected(self):
        document = sample_document()
        fk = document["table_profile"]["physical_constraints"]["foreign_keys"][0]
        fk["source_columns"] = []
        with pytest.raises(JsonPublishInputError):
            self._build([document])

    def test_fk_target_columns_length_mismatch_rejected(self):
        document = sample_document()
        fk = document["table_profile"]["physical_constraints"]["foreign_keys"][0]
        fk["target_columns"] = ["user_id", "extra"]
        with pytest.raises(JsonPublishInputError):
            self._build([document])

    def test_index_columns_referencing_unknown_column_rejected(self):
        document = sample_document()
        document["table_profile"]["indexes"][0]["columns"] = ["no_such"]
        with pytest.raises(JsonPublishInputError):
            self._build([document])

    def test_fk_target_object_not_in_batch_allowed(self):
        # 外键目标对象不在本批：允许（doc 19.2 §7.2）
        table_rows, column_rows = self._build([sample_document()])
        assert len(table_rows) == 1


class TestBuildProfileRowsMapping:
    def _build(self, documents, selected_schemas=("public",)):
        return build_profile_rows(SOURCE_ID, PUBLISH_ID, documents, selected_schemas)

    def test_full_mapping(self):
        table_rows, column_rows = self._build([sample_document()])
        assert len(table_rows) == 1 and len(column_rows) == 2
        row = table_rows[0]
        assert row["source_id"] == SOURCE_ID
        assert row["schema_name"] == "public"
        assert row["object_name"] == "orders"
        assert row["db_name"] == DB_NAME
        assert row["object_type"] == "table"
        assert row["publish_run_id"] == PUBLISH_ID
        assert row["metadata_version"] == "3.0"
        assert row["generated_timestamp"] is not None
        assert row["object_comment"] == "订单表"
        assert row["total_rows"] == 500
        assert row["column_count"] == 2
        assert row["sample_method"] == "limit"
        assert row["sample_count"] == 500
        assert row["has_table_profile"] is True
        assert row["table_category"] == "fact"
        assert float(row["category_confidence"]) == 0.95
        assert row["primary_key_name"] == "orders_pkey"
        assert row["primary_key_columns"] == ["order_id"]
        assert row["foreign_key_count"] == 1
        assert row["index_count"] == 1
        assert row["constraint_count"] == 3  # 主键 1 + 外键 1 + 唯一 1
        assert row["foreign_keys"][0]["constraint_name"] == "orders_user_fkey"
        assert row["unique_column_sets"][0]["columns"] == ["order_id"]
        assert row["json_sample_records"] == {"sample_method": "limit", "records": []}
        # 字段行
        order_id = next(c for c in column_rows if c["column_name"] == "order_id")
        assert order_id["publish_run_id"] == PUBLISH_ID
        assert order_id["ordinal_position"] == 1
        assert order_id["data_type"] == "integer"
        assert order_id["is_nullable"] is False
        assert order_id["semantic_role"] == "identifier"
        assert order_id["null_count"] == 0
        assert order_id["sample_min"] == "1"
        amount = next(c for c in column_rows if c["column_name"] == "amount")
        assert amount["column_comment"] is None
        assert amount["null_count"] is None

    def test_null_table_profile_mapping(self):
        document = sample_document(table_profile=None)
        table_rows, column_rows = self._build([document])
        row = table_rows[0]
        assert row["has_table_profile"] is False
        assert row["table_category"] is None
        assert row["primary_key_columns"] is None
        assert row["foreign_keys"] is None
        assert row["foreign_key_count"] is None
        assert row["index_count"] is None
        assert row["constraint_count"] is None
        assert row["unique_column_sets"] is None

    def test_explicit_null_pk_vs_missing_node(self):
        document = sample_document()
        document["table_profile"]["physical_constraints"]["primary_key"] = None
        rows, _ = self._build([document])
        assert rows[0]["primary_key_name"] is None
        assert rows[0]["primary_key_columns"] == []

        document = sample_document()
        del document["table_profile"]["physical_constraints"]["primary_key"]
        rows, _ = self._build([document])
        assert rows[0]["primary_key_columns"] is None

    def test_explicit_empty_arrays_vs_missing_nodes(self):
        document = sample_document()
        constraints = document["table_profile"]["physical_constraints"]
        constraints["foreign_keys"] = []
        constraints["unique_constraints"] = []
        document["table_profile"]["indexes"] = []
        rows, _ = self._build([document])
        assert rows[0]["foreign_keys"] == []
        assert rows[0]["foreign_key_count"] == 0
        assert rows[0]["indexes"] == []
        assert rows[0]["index_count"] == 0
        assert rows[0]["constraint_count"] == 1  # 仅主键计 1

        document = sample_document()
        del document["table_profile"]["physical_constraints"]["foreign_keys"]
        del document["table_profile"]["indexes"]
        rows, _ = self._build([document])
        assert rows[0]["foreign_keys"] is None
        assert rows[0]["foreign_key_count"] is None
        assert rows[0]["indexes"] is None
        assert rows[0]["constraint_count"] is None  # 三部分不齐不计算

    def test_missing_optional_nodes_stay_none(self):
        document = sample_document()
        del document["profiling"]
        del document["generated_timestamp"]
        document["object_info"]["total_rows"] = None
        rows, _ = self._build([document])
        assert rows[0]["sample_method"] is None
        assert rows[0]["sample_count"] is None
        assert rows[0]["generated_timestamp"] is None
        assert rows[0]["total_rows"] is None


class TestManifestRulesShared:
    """json 发布入口与采集侧入口共用同一套清单规则（doc 19.2 §7.2）。

    两处各自实现（Task 4 Ruling），漂移由本组测试钉住：常量一致 +
    同一组畸形目录在两个入口都拒绝。
    """

    def test_manifest_name_constant_shared(self):
        from backend.data_sources import json_step_service

        assert json_loader.JSON_MANIFEST_NAME == json_step_service.JSON_MANIFEST_NAME

    @pytest.mark.parametrize(
        "corrupt",
        [
            "missing_file",
            "extra_file",
            "missing_manifest",
            "duplicate_entries",
            "bad_stem",
        ],
    )
    def test_malformed_dirs_rejected_by_both_entrypoints(
        self, tmp_path, monkeypatch, corrupt
    ):
        from backend.data_sources.json_step_service import (
            ManagedJsonInputError,
            read_managed_json_input,
        )
        from metaweave.utils import file_utils

        monkeypatch.setattr(file_utils, "get_project_root", lambda: tmp_path)
        document = sample_document()
        documents = [document]
        if corrupt == "missing_file":
            documents = []
        write_json_dir(tmp_path, documents)
        json_dir = tmp_path / "output" / str(SOURCE_ID) / str(JOB_ID) / "json"
        if corrupt == "missing_file":
            # 清单声明 orders 但文件已删：直接改清单还原声明
            (json_dir / "manifest.json").write_text(
                json.dumps({"files": [f"{DB_NAME}.public.orders.json"]}),
                encoding="utf-8",
            )
        elif corrupt == "extra_file":
            (json_dir / f"{DB_NAME}.public.extra.json").write_text(
                "{}", encoding="utf-8"
            )
        elif corrupt == "missing_manifest":
            (json_dir / "manifest.json").unlink()
        elif corrupt == "duplicate_entries":
            (json_dir / "manifest.json").write_text(
                json.dumps({"files": [f"{DB_NAME}.public.orders.json"] * 2}),
                encoding="utf-8",
            )
        elif corrupt == "bad_stem":
            target = json_dir / f"{DB_NAME}.public.orders.json"
            data = target.read_text(encoding="utf-8")
            target.unlink()
            (json_dir / "orders.json").write_text(data, encoding="utf-8")
            (json_dir / "manifest.json").write_text(
                json.dumps({"files": ["orders.json"]}), encoding="utf-8"
            )
        with pytest.raises(JsonPublishInputError):
            read_managed_json_documents(SOURCE_ID, JOB_ID, DB_NAME)
        with pytest.raises(ManagedJsonInputError):
            read_managed_json_input(SOURCE_ID, JOB_ID, DB_NAME)


class TestNodeTypeChecks:
    """非 dict 的节点必须受控拒绝，不得被 or {} 吞成缺失（doc 19.2 §7.2）。"""

    def _build(self, documents, selected_schemas=("public",)):
        return build_profile_rows(SOURCE_ID, PUBLISH_ID, documents, selected_schemas)

    def test_non_dict_physical_constraints_rejected(self):
        document = sample_document()
        document["table_profile"]["physical_constraints"] = []
        with pytest.raises(JsonPublishInputError):
            self._build([document])

    def test_non_dict_semantic_analysis_rejected(self):
        document = sample_document()
        document["column_profiles"]["amount"]["semantic_analysis"] = []
        with pytest.raises(JsonPublishInputError):
            self._build([document])

    def test_non_dict_statistics_rejected(self):
        document = sample_document()
        document["column_profiles"]["amount"]["statistics"] = []
        with pytest.raises(JsonPublishInputError):
            self._build([document])

    def test_non_dict_profiling_rejected(self):
        document = sample_document()
        document["profiling"] = []
        with pytest.raises(JsonPublishInputError):
            self._build([document])

    def test_non_dict_table_profile_rejected(self):
        document = sample_document()
        document["table_profile"] = []
        with pytest.raises(JsonPublishInputError):
            self._build([document])

    def test_explicit_null_nodes_still_allowed(self):
        """显式 null 与缺失仍是合法输入，映射为未知（与畸形类型区分）。"""
        document = sample_document()
        document["profiling"] = None
        document["column_profiles"]["amount"]["semantic_analysis"] = None
        document["column_profiles"]["amount"]["statistics"] = None
        document["table_profile"]["physical_constraints"] = None
        rows, columns = self._build([document])
        assert rows[0]["sample_count"] is None
        assert rows[0]["primary_key_columns"] is None
        amount = next(c for c in columns if c["column_name"] == "amount")
        assert amount["semantic_role"] is None
        assert amount["null_count"] is None


class TestAwareTimestampConstraint:
    """generated_timestamp 必须带时区并归一为 UTC（doc 19.3 §2.2.4）。

    生成器固定输出 Z；加载器接受合法的其他时区偏移（同一瞬间换算），
    无时区输入拒绝——不再按本地无时区解析。
    """

    def _build(self, documents, selected_schemas=("public",)):
        return build_profile_rows(SOURCE_ID, PUBLISH_ID, documents, selected_schemas)

    def test_naive_timestamp_rejected(self):
        document = sample_document()
        document["generated_timestamp"] = "2026-10-03T18:55:55.030584"
        with pytest.raises(JsonPublishInputError):
            self._build([document])

    @pytest.mark.parametrize(
        "value",
        [
            "2026-10-03T18:55:55.030584Z",
            "2026-10-04T02:55:55.030584+08:00",
            "2026-10-03T13:55:55.030584-05:00",
        ],
    )
    def test_aware_timestamp_normalized_to_same_instant(self, value):
        document = sample_document()
        document["generated_timestamp"] = value
        rows, _ = self._build([document])
        assert rows[0]["generated_timestamp"] == datetime(
            2026, 10, 3, 18, 55, 55, 30584, tzinfo=timezone.utc
        )

    def test_missing_timestamp_maps_to_none(self):
        document = sample_document()
        del document["generated_timestamp"]
        rows, _ = self._build([document])
        assert rows[0]["generated_timestamp"] is None


# ---- doc 7.1 §8.13：画像新增推荐属性的发布兼容回归 ----


class TestDimensionValueRecommendationPublishCompat:
    def test_validate_accepts_valid_recommendation(self):
        doc = sample_document()
        doc["table_profile"]["dimension_value_columns"] = ["amount"]
        doc["table_profile"]["dimension_value_confidence"] = 0.8
        json_loader._validate_document(doc, ["public"])  # 不抛即通过

    @pytest.mark.parametrize(
        "extra",
        [
            {"dimension_value_columns": [], "dimension_value_confidence": None},
            {},
        ],
        ids=["empty", "missing"],
    )
    def test_validate_accepts_empty_and_missing_variants(self, extra):
        doc = sample_document()
        doc["table_profile"].update(extra)
        json_loader._validate_document(doc, ["public"])

    def test_table_row_drops_recommendation_fields(self):
        doc = sample_document()
        doc["table_profile"]["dimension_value_columns"] = ["amount"]
        doc["table_profile"]["dimension_value_confidence"] = 0.8

        row = json_loader._table_row(
            doc, source_id=SOURCE_ID, publish_run_id=PUBLISH_ID
        )

        assert "dimension_value_columns" not in row
        assert "dimension_value_confidence" not in row
        assert row["table_category"] == "fact"
