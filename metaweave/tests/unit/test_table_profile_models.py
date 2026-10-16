"""table_profile / table_column_profile ORM 映射与 doc 19.2 §6.2 对照。"""

from backend.persistence.models import TableColumnProfile, TableProfile


def _columns(model):
    return {column.name for column in model.__table__.columns}


def test_table_profile_columns_match_ddl():
    cols = _columns(TableProfile)
    expected = {
        "source_id",
        "schema_name",
        "object_name",
        "db_name",
        "object_type",
        "publish_id",
        "job_id",
        "metadata_version",
        "generated_timestamp",
        "loaded_at",
        "object_comment",
        "object_comment_source",
        "total_rows",
        "column_count",
        "sample_method",
        "sample_count",
        "has_table_profile",
        "table_category",
        "category_confidence",
        "classification_source",
        "classification_reason",
        "classification_basis",
        "rule_table_category",
        "rule_confidence",
        "rule_inference_basis",
        "primary_key_name",
        "primary_key_columns",
        "foreign_key_count",
        "index_count",
        "constraint_count",
        "foreign_keys",
        "unique_constraints",
        "indexes",
        "unique_column_sets",
        "json_sample_records",
    }
    assert cols == expected
    pk = [column.name for column in TableProfile.__table__.primary_key.columns]
    assert pk == ["source_id", "schema_name", "object_name"]


def test_table_column_profile_columns_constraints_and_index():
    cols = _columns(TableColumnProfile)
    expected = {
        "source_id",
        "schema_name",
        "object_name",
        "publish_id",
        "column_name",
        "ordinal_position",
        "data_type",
        "is_nullable",
        "column_default",
        "column_comment",
        "comment_source",
        "semantic_role",
        "semantic_confidence",
        "semantic_basis",
        "null_count",
        "null_rate",
        "unique_count",
        "uniqueness",
        "sample_min",
        "sample_max",
        "value_distribution",
    }
    assert cols == expected
    indexes = {index.name: index for index in TableColumnProfile.__table__.indexes}
    assert "table_column_profile_object_order_idx" in indexes
    assert indexes["table_column_profile_object_order_idx"].unique is True
    fks = list(TableColumnProfile.__table__.foreign_key_constraints)
    assert len(fks) == 1
    assert fks[0].ondelete == "CASCADE"
    pk = [column.name for column in TableColumnProfile.__table__.primary_key.columns]
    assert pk == ["source_id", "schema_name", "object_name", "column_name"]
