"""DDL 展示切分与样例解析（doc 19.2 §3.2/§3.6/§8.2）。"""

import pytest

from backend.assets.ddl_display import (
    DefinitionInvalidError,
    extract_sample,
    split_definition_sql,
)
from metaweave.core.metadata.sample_block import build_comment_block

SQL_PREFIX = (
    "-- ====================================\n"
    "-- Database: ordersdb\n"
    "CREATE TABLE public.orders (order_id integer);\n"
    "COMMENT ON TABLE public.orders IS '订单表';\n"
    "CREATE INDEX idx_orders ON public.orders(order_id);\n"
)


def _payload(records, object_name="public.orders", object_type="table"):
    return {
        "object_type": object_type,
        "object_name": object_name,
        "records": records,
    }


class TestSplitDefinitionSql:
    def test_plain_ddl_without_block_returns_sql_and_none(self):
        sql, payload = split_definition_sql(SQL_PREFIX)
        assert sql == SQL_PREFIX.rstrip("\n")
        assert payload is None

    def test_block_stripped_and_sql_preserved(self):
        ddl = SQL_PREFIX + build_comment_block("SAMPLED_RECORDS", _payload([]))
        sql, payload = split_definition_sql(ddl)
        assert sql == SQL_PREFIX.rstrip("\n")
        assert "SAMPLED_RECORDS" not in sql
        assert payload is not None and payload["object_name"] == "public.orders"

    def test_tricky_values_roundtrip(self):
        from tests.unit.metaweave.metadata.test_sample_block import TRICKY_VALUES

        records = [{"val": value} for value in TRICKY_VALUES]
        ddl = SQL_PREFIX + build_comment_block("SAMPLED_RECORDS", _payload(records))
        sql, payload = split_definition_sql(ddl)
        assert payload["records"] == records

    def test_legacy_marker_rejected(self):
        ddl = SQL_PREFIX + '/* SAMPLE_RECORDS\n{"table": "public.orders"}\n*/'
        with pytest.raises(DefinitionInvalidError):
            split_definition_sql(ddl)

    def test_legacy_marker_hint_rejected(self):
        ddl = SQL_PREFIX + '/* SAMPLE_RECORDS\n{"broken": '
        with pytest.raises(DefinitionInvalidError):
            split_definition_sql(ddl)

    def test_broken_boundary_rejected(self):
        ddl = SQL_PREFIX + '/* SAMPLED_RECORDS\n{"records": [{"a": 1}'
        with pytest.raises(DefinitionInvalidError):
            split_definition_sql(ddl)

    def test_multiple_blocks_rejected(self):
        block = build_comment_block("SAMPLED_RECORDS", _payload([]))
        with pytest.raises(DefinitionInvalidError):
            split_definition_sql(SQL_PREFIX + block + "\n" + block)

    def test_content_after_block_rejected(self):
        block = build_comment_block("SAMPLED_RECORDS", _payload([]))
        with pytest.raises(DefinitionInvalidError):
            split_definition_sql(SQL_PREFIX + block + "\nCREATE INDEX late ON t(c);")

    def test_line_comment_marker_word_not_rejected(self):
        sql, payload = split_definition_sql("-- SAMPLE_RECORDS 已废弃\n" + SQL_PREFIX)
        assert payload is None and "已废弃" in sql


class TestExtractSample:
    def test_ready_returns_columns_and_first_five(self):
        records = [{"id": str(n), "name": f"n{n}"} for n in range(7)]
        state, columns, sample = extract_sample(
            _payload(records),
            schema_name="public",
            object_name="orders",
            object_type="table",
        )
        assert state == "ready"
        assert columns == ["id", "name"]
        assert len(sample) == 5
        assert sample[0] == {"id": "0", "name": "n0"}

    def test_no_payload_is_no_sample_block(self):
        state, columns, sample = extract_sample(
            None, schema_name="public", object_name="orders", object_type="table"
        )
        assert state == "no_sample_block"
        assert columns == [] and sample == []

    def test_empty_records_is_empty_state(self):
        state, _, _ = extract_sample(
            _payload([]),
            schema_name="public",
            object_name="orders",
            object_type="table",
        )
        assert state == "empty"

    def test_identity_must_be_qualified_name(self):
        with pytest.raises(DefinitionInvalidError):
            extract_sample(
                _payload([], object_name="orders"),
                schema_name="public",
                object_name="orders",
                object_type="table",
            )

    def test_object_type_must_match(self):
        with pytest.raises(DefinitionInvalidError):
            extract_sample(
                _payload([], object_type="view"),
                schema_name="public",
                object_name="orders",
                object_type="table",
            )

    def test_keyset_mismatch_rejected(self):
        records = [{"a": 1}, {"a": 2, "b": 3}]
        with pytest.raises(DefinitionInvalidError):
            extract_sample(
                _payload(records),
                schema_name="public",
                object_name="orders",
                object_type="table",
            )

    def test_data_and_label_are_plain_fields(self):
        records = [{"data": {"inner": 1}, "label": "x"}]
        state, columns, sample = extract_sample(
            _payload(records),
            schema_name="public",
            object_name="orders",
            object_type="table",
        )
        assert state == "ready"
        assert columns == ["data", "label"]
        assert sample == [{"data": {"inner": 1}, "label": "x"}]


class TestOrphanMarkerFragment:
    """有标记起始但不成块的片段：起点数多于完整块 → 拒绝（doc 19.2 §8.2）。"""

    def test_orphan_current_marker_before_valid_block_rejected(self):
        ddl = (
            SQL_PREFIX
            + "/* SAMPLED_RECORDS hidden=secret */\n"
            + build_comment_block("SAMPLED_RECORDS", _payload([]))
        )
        with pytest.raises(DefinitionInvalidError):
            split_definition_sql(ddl)

    def test_orphan_marker_without_any_block_rejected(self):
        ddl = SQL_PREFIX + "/* SAMPLED_RECORDS hidden */"
        with pytest.raises(DefinitionInvalidError):
            split_definition_sql(ddl)

    def test_single_valid_block_still_passes(self):
        ddl = SQL_PREFIX + build_comment_block("SAMPLED_RECORDS", _payload([]))
        sql, payload = split_definition_sql(ddl)
        assert payload is not None and "SAMPLED_RECORDS" not in sql


class TestCommentStringWithMarkerText:
    """注释字符串里的标记字样不算样例块：SQL 保留原文、块正常剥离。"""

    def test_comment_text_preserved_and_block_stripped(self):
        comment_line = (
            "COMMENT ON TABLE public.orders IS '/* SAMPLE_RECORDS demo */';\n"
        )
        block = build_comment_block("SAMPLED_RECORDS", _payload([{"id": "1"}]))
        sql, payload = split_definition_sql(SQL_PREFIX + comment_line + block)
        assert payload is not None
        assert "/* SAMPLE_RECORDS demo */" in sql  # 注释原文保留在 SQL 中
        assert "SAMPLED_RECORDS\n{" not in sql  # 真样例块已剥离

    def test_complete_looking_block_in_string_not_stripped(self):
        comment_line = (
            "COMMENT ON TABLE public.orders IS "
            "'/* SAMPLED_RECORDS {\"records\": []} */';\n"
        )
        block = build_comment_block("SAMPLED_RECORDS", _payload([]))
        sql, payload = split_definition_sql(SQL_PREFIX + comment_line + block)
        assert payload is not None
        assert "'/* SAMPLED_RECORDS {\"records\": []} */'" in sql


class TestSampleValuesNeverInSql:
    """切块后 rstrip 只清块前格式空行；样例值不得出现在 SQL（doc 19.2 §3.2）。"""

    def test_sample_values_and_marker_never_leak_into_sql(self):
        block = build_comment_block(
            "SAMPLED_RECORDS",
            _payload([{"user_name": "用户001", "note": "LEAK-PIN-7391"}]),
        )
        # 生成器形态：块前有空行
        ddl = SQL_PREFIX + "\n" + block
        sql, payload = split_definition_sql(ddl)
        assert payload is not None
        assert "LEAK-PIN-7391" not in sql
        assert "用户001" not in sql
        assert "SAMPLED_RECORDS" not in sql
        # 切点在块起点之前：rstrip 仅去掉块前格式换行，内容边界不受影响
        assert sql == SQL_PREFIX.rstrip("\n")


class TestDollarQuotedMarkerText:
    """美元引用内的标记字样不算样例块：SQL 保留原文、真块正常剥离。"""

    def test_dollar_quoted_text_preserved_and_block_stripped(self):
        definition = "SELECT $$/* SAMPLE_RECORDS demo */$$ AS note;\n"
        block = build_comment_block("SAMPLED_RECORDS", _payload([{"id": "1"}]))
        sql, payload = split_definition_sql(SQL_PREFIX + definition + block)
        assert payload is not None
        assert "$$/* SAMPLE_RECORDS demo */$$" in sql
        assert "SAMPLED_RECORDS\n{" not in sql


class TestQuotedIdentifierWithDollarTag:
    """合法索引名含 $tag$ 时，SQL Tab 绝不出现样例块（doc 19.2 §3.2 回归）。"""

    def test_sql_tab_never_contains_sample_block(self):
        block = build_comment_block("SAMPLED_RECORDS", _payload([{"id": "1"}]))
        ddl = 'CREATE INDEX "idx$tag$" ON public.orders(id);\n' + block
        sql, payload = split_definition_sql(ddl)
        assert payload is not None
        assert '"idx$tag$"' in sql
        assert "SAMPLED_RECORDS" not in sql


class TestEscapeStringViewDefinition:
    """E'...' 视图定义 + 正常样例块：不得误判边界异常（doc 19.2 §8.2）。"""

    def test_escape_string_with_escaped_quote_not_rejected(self):
        definition = "SELECT E'abc\\'/* SAMPLE_RECORDS demo */' AS x;\n"
        block = build_comment_block("SAMPLED_RECORDS", _payload([{"id": "1"}]))
        sql, payload = split_definition_sql(SQL_PREFIX + definition + block)
        assert payload is not None
        assert "SAMPLED_RECORDS" not in sql
        assert "E'abc" in sql
