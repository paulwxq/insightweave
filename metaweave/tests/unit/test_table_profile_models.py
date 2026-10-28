"""table_profile / table_column_profile ORM 映射与 doc 6.1 §16/§17 对照。

断代改造后：`source_id` 为 text 短 ID；内容来源由 `publish_run_id`
（关联 publish_run_info）表达，旧 `publish_id`/`job_id` 列已删除。
"""

from backend.persistence.models import TableColumnProfile, TableProfile


def _columns(model):
    return {column.name for column in model.__table__.columns}


def _foreign_key_targets(table):
    """每个外键约束 → 其全部目标表名组成的元组（复合键为一对多列）。"""
    return {
        tuple(sorted({fk.column.table.name for fk in constraint.elements}))
        for constraint in table.foreign_key_constraints
    }


def test_table_profile_columns_match_ddl():
    cols = _columns(TableProfile)
    expected = {
        "source_id",
        "schema_name",
        "object_name",
        "db_name",
        "object_type",
        "publish_run_id",
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
    # 断代改造移除的旧身份列不再出现（doc 6.2 §1.2：删除旧 job_id，publish_id 改 publish_run_id）
    assert "job_id" not in cols
    assert "publish_id" not in cols
    pk = [column.name for column in TableProfile.__table__.primary_key.columns]
    assert pk == ["source_id", "schema_name", "object_name"]
    # source_id 为 text 短 ID（db_src_1 等，doc 6.1 §13）
    assert TableProfile.__table__.columns["source_id"].type.python_type is str
    # 内容行实际写入来源指向发布运行表（doc 6.1 §16）
    assert _foreign_key_targets(TableProfile.__table__) == {("publish_run_info",)}


def test_table_column_profile_columns_constraints_and_index():
    cols = _columns(TableColumnProfile)
    expected = {
        "source_id",
        "schema_name",
        "object_name",
        "publish_run_id",
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
    assert "job_id" not in cols and "publish_id" not in cols
    indexes = {index.name: index for index in TableColumnProfile.__table__.indexes}
    assert "table_column_profile_object_order_idx" in indexes
    assert indexes["table_column_profile_object_order_idx"].unique is True
    fks = list(TableColumnProfile.__table__.foreign_key_constraints)
    assert _foreign_key_targets(TableColumnProfile.__table__) == {
        ("table_profile",),
        ("publish_run_info",),
    }
    assert len(fks) == 2
    ondelete = {constraint.name: constraint.ondelete for constraint in fks}
    assert ondelete["table_column_profile_object_fk"] == "CASCADE"
    pk = [column.name for column in TableColumnProfile.__table__.primary_key.columns]
    assert pk == ["source_id", "schema_name", "object_name", "column_name"]
