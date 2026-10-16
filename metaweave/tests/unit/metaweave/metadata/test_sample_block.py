"""JSON 注释块统一编码/解码（doc 19.2 §8.2）：\\u002a 转义与当前格式解码。"""

import json

import pytest

from metaweave.core.metadata.sample_block import (
    build_comment_block,
    decode_current_records,
    find_legacy_sample_records_block,
    find_sampled_records_blocks,
    has_sampled_records_marker_hint,
    iter_sampled_records_marker_hints,
    serialize_comment_payload,
)

# 相邻与嵌套分隔符序列：两趟替换会静默改值的反例（spec §8.2）
TRICKY_VALUES = [
    "a*/b",
    "}*/",
    "/*",
    "/**/",
    "/*/",
    "*/*",
    "/*/*",
    "100%*/*",
    "\\u002a",
    "反斜杠\\值",
    "换行\n值",
    None,
    42,
]


class TestSerializeCommentPayload:
    @pytest.mark.parametrize("value", TRICKY_VALUES)
    def test_no_bare_star_and_roundtrip(self, value):
        payload = {"k": value}
        serialized = serialize_comment_payload(payload)
        assert "*" not in serialized  # 无裸 *，即无 /* 与 */
        assert "/*" not in serialized and "*/" not in serialized
        assert json.loads(serialized) == payload  # 解码还原原值

    def test_star_in_key_also_escaped(self):
        serialized = serialize_comment_payload({"a*b": "c"})
        assert "*" not in serialized
        assert json.loads(serialized) == {"a*b": "c"}

    def test_build_block_wraps_marker(self):
        block = build_comment_block("SAMPLED_RECORDS", {"records": []})
        assert block.startswith("/* SAMPLED_RECORDS\n")
        assert block.endswith("\n*/")
        # 注释体内（去掉首行标记与末行结束符）无裸 *
        body = "\n".join(block.splitlines()[1:-1])
        assert "*" not in body


class TestBlockDetection:
    def test_current_block_found(self):
        content = "CREATE TABLE t (id int);\n" + build_comment_block(
            "SAMPLED_RECORDS", {"records": [{"id": "1*/"}]}
        )
        blocks = find_sampled_records_blocks(content)
        assert len(blocks) == 1
        assert json.loads(blocks[0].group("body")) == {"records": [{"id": "1*/"}]}

    def test_two_blocks_both_found(self):
        block = build_comment_block("SAMPLED_RECORDS", {"records": []})
        assert len(find_sampled_records_blocks(block + "\n" + block)) == 2

    def test_legacy_block_detected_and_not_confused_with_current(self):
        legacy = '/* SAMPLE_RECORDS\n{"table": "public.t", "records": []}\n*/'
        current = build_comment_block("SAMPLED_RECORDS", {"records": []})
        assert find_legacy_sample_records_block(legacy) is not None
        assert find_legacy_sample_records_block(current) is None
        assert find_sampled_records_blocks(legacy) == []
        assert len(find_sampled_records_blocks(current)) == 1

    def test_marker_hint_without_complete_block(self):
        assert has_sampled_records_marker_hint('/* SAMPLED_RECORDS\n{"a": 1')
        assert not has_sampled_records_marker_hint("-- 提到 SAMPLE_RECORDS 的行注释")

    def test_line_comment_text_is_not_marker_hint(self):
        content = "-- SAMPLED_RECORDS 字样\nCREATE TABLE t (id int);"
        assert not has_sampled_records_marker_hint(content)
        assert find_legacy_sample_records_block(content) is None


class TestDecodeCurrentRecords:
    def test_data_and_label_are_plain_fields(self):
        records = [{"data": "x", "label": "y"}, {"data": None, "label": 2}]
        assert decode_current_records({"records": records}) == records

    def test_rejects_non_list_and_non_dict(self):
        with pytest.raises(ValueError):
            decode_current_records({"records": "x"})
        with pytest.raises(ValueError):
            decode_current_records({"records": ["x"]})

    def test_missing_records_rejected(self):
        with pytest.raises(ValueError):
            decode_current_records({})


class TestSqlTextMasking:
    """SQL 字符串/行注释里的标记字样不算块注释标记（doc 19.2 §8.2）。"""

    def test_legacy_marker_text_inside_string_literal_ignored(self):
        content = "COMMENT ON TABLE public.orders IS '/* SAMPLE_RECORDS demo */';"
        assert not has_sampled_records_marker_hint(content)
        assert find_legacy_sample_records_block(content) is None
        assert find_sampled_records_blocks(content) == []
        assert iter_sampled_records_marker_hints(content) == []

    def test_current_marker_text_inside_string_literal_ignored(self):
        content = "COMMENT ON TABLE public.orders IS '/* SAMPLED_RECORDS demo */';"
        assert not has_sampled_records_marker_hint(content)
        assert iter_sampled_records_marker_hints(content) == []

    def test_complete_looking_block_inside_string_is_not_a_block(self):
        content = (
            "CREATE TABLE t (id int);\n"
            "COMMENT ON TABLE t IS '/* SAMPLED_RECORDS {\"records\": []} */';\n"
        )
        assert find_sampled_records_blocks(content) == []

    def test_marker_in_line_comment_ignored(self):
        content = "-- /* SAMPLE_RECORDS demo */\nCREATE TABLE t (id int);"
        assert not has_sampled_records_marker_hint(content)

    def test_doubled_quote_keeps_states_correct(self):
        # '' 是字符串内的转义引号；其后的真实标记仍须识别
        real_block = build_comment_block("SAMPLED_RECORDS", {"records": []})
        content = (
            "COMMENT ON TABLE t IS 'it''s /* SAMPLE_RECORDS demo */';\n" + real_block
        )
        assert len(find_sampled_records_blocks(content)) == 1
        assert len(iter_sampled_records_marker_hints(content)) == 1
        assert not has_sampled_records_marker_hint(content.split("\n")[0])

    def test_real_block_payload_untouched_by_masking(self):
        payload = {"records": [{"note": "it's /* 说明 */"}]}
        content = "COMMENT ON TABLE t IS '含 /* SAMPLE_RECORDS 示例 */';\n" + (
            build_comment_block("SAMPLED_RECORDS", payload)
        )
        blocks = find_sampled_records_blocks(content)
        assert len(blocks) == 1
        assert json.loads(blocks[0].group("body")) == payload


class TestDollarQuotedStrings:
    """美元引用字符串（$$...$$ / $tag$...$tag$）内的字样不算标记（doc 19.2 §8.2）。"""

    def test_dollar_quoted_marker_text_ignored(self):
        content = (
            "CREATE MATERIALIZED VIEW public.mv AS "
            "SELECT $$/* SAMPLE_RECORDS demo */$$ AS x;"
        )
        assert not has_sampled_records_marker_hint(content)
        assert find_legacy_sample_records_block(content) is None
        assert find_sampled_records_blocks(content) == []
        assert iter_sampled_records_marker_hints(content) == []

    def test_tagged_dollar_quote_marker_text_ignored(self):
        content = "SELECT $tag$/* SAMPLED_RECORDS demo */$tag$;"
        assert not has_sampled_records_marker_hint(content)
        assert iter_sampled_records_marker_hints(content) == []

    def test_dollar_quote_content_does_not_desync_states(self):
        # 美元引用内的 ' 与 -- 都是内容；其后的真实样例块仍须完整识别
        real_block = build_comment_block("SAMPLED_RECORDS", {"records": [{"x": "1"}]})
        content = "SELECT $$it's -- /* SAMPLE_RECORDS demo */$$ AS x;\n" + real_block
        blocks = find_sampled_records_blocks(content)
        assert len(blocks) == 1
        assert len(iter_sampled_records_marker_hints(content)) == 1
        assert json.loads(blocks[0].group("body")) == {"records": [{"x": "1"}]}

    def test_inner_different_tag_is_content(self):
        content = "SELECT $outer$ $$ /* SAMPLE_RECORDS demo */ $outer$;"
        assert not has_sampled_records_marker_hint(content)

    def test_dollar_not_quote_left_alone(self):
        # $1 位置参数与价格文本不是美元引用，不得影响后续真实块识别
        real_block = build_comment_block("SAMPLED_RECORDS", {"records": []})
        content = "SELECT $1, price $$? no$$\n" + real_block
        assert len(find_sampled_records_blocks(content)) == 1


class TestDoubleQuotedIdentifiers:
    """双引号标识符与美元引用的词法位置（doc 19.2 §8.2）。"""

    def test_dollar_tag_in_quoted_identifier_is_not_quote_start(self):
        real_block = build_comment_block("SAMPLED_RECORDS", {"records": []})
        content = 'CREATE INDEX "idx$tag$" ON public.orders(order_id);\n' + real_block
        assert len(find_sampled_records_blocks(content)) == 1
        assert len(iter_sampled_records_marker_hints(content)) == 1

    def test_dollar_tag_in_unquoted_identifier_is_not_quote_start(self):
        real_block = build_comment_block("SAMPLED_RECORDS", {"records": []})
        content = "CREATE INDEX idx$tag$ ON public.orders(order_id);\n" + real_block
        assert len(find_sampled_records_blocks(content)) == 1
        assert len(iter_sampled_records_marker_hints(content)) == 1

    def test_marker_text_inside_quoted_identifier_ignored(self):
        content = 'CREATE INDEX "weird /* SAMPLE_RECORDS demo */" ON t(c);'
        assert not has_sampled_records_marker_hint(content)
        assert find_sampled_records_blocks(content) == []

    def test_doubled_quote_in_identifier_keeps_states_correct(self):
        real_block = build_comment_block("SAMPLED_RECORDS", {"records": []})
        content = 'CREATE INDEX "idx""$tag$" ON t(c);\n' + real_block
        assert len(find_sampled_records_blocks(content)) == 1

    def test_real_dollar_quote_still_masked(self):
        # 词法位置合法（前为空白/运算符）的美元引用照常掩码
        real_block = build_comment_block("SAMPLED_RECORDS", {"records": []})
        content = "SELECT ($$/* SAMPLE_RECORDS demo */$$)\n" + real_block
        assert len(find_sampled_records_blocks(content)) == 1
        assert len(iter_sampled_records_marker_hints(content)) == 1


class TestEscapeStrings:
    """E'...' 字符串按反斜杠转义规则解析（doc 19.2 §8.2）。"""

    def test_escape_string_with_escaped_quote_keeps_block_detected(self):
        real_block = build_comment_block("SAMPLED_RECORDS", {"records": []})
        content = "SELECT E'abc\\'/* SAMPLE_RECORDS demo */' AS x;\n" + real_block
        assert len(find_sampled_records_blocks(content)) == 1
        assert len(iter_sampled_records_marker_hints(content)) == 1

    def test_escape_string_marker_text_ignored(self):
        content = "SELECT E'/* SAMPLE_RECORDS demo */' AS x;"
        assert not has_sampled_records_marker_hint(content)

    def test_lowercase_e_prefix(self):
        content = "SELECT e'it\\'s /* SAMPLE_RECORDS demo */' AS x;"
        assert not has_sampled_records_marker_hint(content)

    def test_identifier_prefixed_quote_is_plain_string(self):
        # xE'...'：E 前是标识符延续字符，不是独立前缀，按普通字符串处理
        content = "SELECT xE'/* SAMPLE_RECORDS demo */' AS x;"
        assert not has_sampled_records_marker_hint(content)

    def test_plain_string_backslash_is_literal(self):
        # 非 E 字符串中反斜杠是普通字符（standard_conforming_strings）：
        # 'C:\' 的 ' 即闭引号，其后真实块不受影响
        real_block = build_comment_block("SAMPLED_RECORDS", {"records": []})
        content = "SELECT 'C:\\' AS x;\n" + real_block
        assert len(find_sampled_records_blocks(content)) == 1
