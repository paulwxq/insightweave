"""rel 产物重渲染链用例（doc 23.2 §5.2，D4）。

- ``relation_from_json_entry``：json 条目 → Relation 反序列化（FK / manual /
  三族推断的反向映射）；
- ``render_markdown_report``：模块级纯渲染（不写盘），``_write_markdown``
  保留为「render + 写默认路径」薄封装；
- A14：未编辑产物「json 序列化 → 反序列化 → 重渲染」与算法直出逐字节一致
  （仅生成时间行除外）；
- 统计摘要 manual 口径：manual 条目 > 0 时加「- 手工: N」行，无 manual 时
  输出不含该行（采集路径无 manual，输出不变）。
"""

import re

from metaweave.core.relationships.models import (
    FOREIGN_KEY_COMPOSITE_SCORE,
    Relation,
    relation_from_json_entry,
)
from metaweave.core.relationships.writer import (
    RelationshipWriter,
    render_markdown_report,
)

DATABASE = "orders"
GENERATED_BY = "rel"


def _writer(tmp_path):
    return RelationshipWriter(
        {
            "output": {"rel_directory": str(tmp_path / "rel")},
            "database": {"database": DATABASE},
        }
    )


def _fk_relation():
    return Relation(
        relationship_id="rel_aaaa00000001",
        source_schema="public",
        source_table="orders",
        source_columns=["user_id"],
        target_schema="public",
        target_table="users",
        target_columns=["id"],
        relationship_type="foreign_key",
        cardinality="N:1",
        constraint_name="orders_user_id_fkey",
    )


def _rule_relation():
    return Relation(
        relationship_id="rel_bbbb00000002",
        source_schema="public",
        source_table="payments",
        source_columns=["ref_no"],
        target_schema="public",
        target_table="invoices",
        target_columns=["number"],
        relationship_type="inferred",
        cardinality="N:1",
        composite_score=0.85,
        score_details={
            "inclusion_rate": 0.99,
            "name_similarity": 0.2,
            "comment_similarity": 0.1,
            "type_compatibility": 1.0,
            "jaccard_index": 0.9,
        },
        inference_method="rule_physical_key",
        candidate_origin="rule",
    )


def _llm_composite_relation():
    return Relation(
        relationship_id="rel_cccc00000003",
        source_schema="public",
        source_table="order_lines",
        source_columns=["store_id", "date_day"],
        target_schema="public",
        target_table="daily_inventory",
        target_columns=["store_id", "date_day"],
        relationship_type="inferred",
        cardinality="N:1",
        composite_score=0.92,
        score_details={
            "inclusion_rate": 0.98,
            "name_similarity": 0.9,
            "comment_similarity": 0.7,
            "type_compatibility": 1.0,
            "jaccard_index": 0.8,
        },
        inference_method="llm_inferred",
        candidate_origin="llm",
    )


def _manual_relation():
    return Relation(
        relationship_id="rel_manual_0123456789",
        source_schema="public",
        source_table="a",
        source_columns=["x", "y"],
        target_schema="public",
        target_table="b",
        target_columns=["p", "q"],
        relationship_type="manual",
        cardinality="M:N",
        composite_score=1.0,
    )


def _strip_timestamp(text: str) -> str:
    return re.sub(r"^生成时间: .*$", "生成时间: <ts>", text, flags=re.MULTILINE)


def test_a14_rebuilt_render_matches_algorithm_output(tmp_path):
    """A14：反序列化+重渲染与算法直出除生成时间行外逐字节一致。"""
    writer = _writer(tmp_path)
    relations = [_fk_relation(), _rule_relation(), _llm_composite_relation()]

    path_a_file = writer._write_markdown(relations, generated_by=GENERATED_BY)
    path_a = path_a_file.read_text(encoding="utf-8")

    entries = [writer._convert_to_v32_format(rel) for rel in relations]
    rebuilt = [relation_from_json_entry(entry) for entry in entries]
    path_b = render_markdown_report(
        rebuilt, generated_by=GENERATED_BY, database_name=DATABASE
    )

    assert _strip_timestamp(path_b) == _strip_timestamp(path_a)


def test_relation_from_json_entry_round_trips_discovery_families(tmp_path):
    """FK / 三族推断经序列化→反序列化还原：类型、评分、明细、推断方法。"""
    writer = _writer(tmp_path)
    relations = [_fk_relation(), _rule_relation(), _llm_composite_relation()]

    for original in relations:
        entry = writer._convert_to_v32_format(original)
        rebuilt = relation_from_json_entry(entry)

        assert rebuilt.relationship_id == original.relationship_id
        assert rebuilt.source_schema == original.source_schema
        assert rebuilt.source_columns == original.source_columns
        assert rebuilt.target_columns == original.target_columns
        assert rebuilt.relationship_type == original.relationship_type
        assert rebuilt.cardinality == original.cardinality
        if original.relationship_type == "foreign_key":
            # FK 评分在产物语义上恒为 1.0（序列化即补写，反序列化取常量）
            assert rebuilt.composite_score == FOREIGN_KEY_COMPOSITE_SCORE
        else:
            assert rebuilt.composite_score == original.composite_score
        assert rebuilt.score_details == original.score_details
        assert rebuilt.inference_method == original.inference_method
        assert rebuilt.constraint_name == original.constraint_name


def test_relation_from_json_entry_maps_manual_entry():
    """D7 手工条目：relationship_type=manual、评分取条目值、无推断方法。"""
    entry = {
        "relationship_id": "rel_manual_0123456789",
        "type": "composite",
        "from_table": {"schema": "public", "table": "a"},
        "from_columns": ["x", "y"],
        "to_table": {"schema": "public", "table": "b"},
        "to_columns": ["p", "q"],
        "discovery_method": "manual",
        "target_source_type": "manual",
        "source_constraint": None,
        "composite_score": 1.0,
        "confidence_level": "high",
        "cardinality": "M:N",
    }

    rebuilt = relation_from_json_entry(entry)

    assert rebuilt.relationship_type == "manual"
    assert rebuilt.is_composite
    assert rebuilt.composite_score == 1.0
    assert rebuilt.score_details is None
    assert rebuilt.inference_method is None
    assert rebuilt.cardinality == "M:N"


def test_render_markdown_report_counts_manual_bucket():
    """manual 条目 > 0 → 摘要含「- 手工: N」；无 manual → 不出现该行。"""
    with_manual = render_markdown_report(
        [_fk_relation(), _manual_relation()],
        generated_by=GENERATED_BY,
        database_name=DATABASE,
    )
    assert "- 手工: 1" in with_manual

    without_manual = render_markdown_report(
        [_fk_relation(), _rule_relation()],
        generated_by=GENERATED_BY,
        database_name=DATABASE,
    )
    assert "手工" not in without_manual


def test_render_markdown_report_is_pure_text(tmp_path):
    """纯渲染不写盘：目录在调用前后保持为空（写盘只属于薄封装）。"""
    writer = _writer(tmp_path)
    render_markdown_report(
        [_rule_relation()], generated_by=GENERATED_BY, database_name=DATABASE
    )
    assert list(writer.rel_dir.iterdir()) == []

    wrapper_file = writer._write_markdown([_rule_relation()], generated_by=GENERATED_BY)
    assert wrapper_file == (writer.rel_dir / f"{DATABASE}.relationships_global.md")
    assert wrapper_file.exists()


def test_render_markdown_report_thresholds_are_explicit():
    """阈值显式入参：0.85 在缺省 (0.80/0.90) 下为中、提高下限后为低。"""
    default = render_markdown_report(
        [_rule_relation()], generated_by=GENERATED_BY, database_name=DATABASE
    )
    assert "**置信度**: 0.850 (中)" in default

    raised = render_markdown_report(
        [_rule_relation()],
        generated_by=GENERATED_BY,
        database_name=DATABASE,
        high_confidence_threshold=0.99,
        medium_confidence_threshold=0.90,
    )
    assert "**置信度**: 0.850 (低)" in raised
