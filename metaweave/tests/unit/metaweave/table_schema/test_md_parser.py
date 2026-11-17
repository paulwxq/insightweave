import pytest

from metaweave.core.table_schema.md_parser import MDParser


MD_SAMPLE = """# public.dim_company（公司维表）
## 字段列表：
- company_id (integer(32)) - 公司ID（主键） [示例: 1, 2]
- company_name (character varying(200)) - 公司名称，唯一 [示例: 京东便利, 喜士多]
## 字段补充说明：
- company_id 为主键
"""


def test_extract_table_name():
    parser = MDParser.from_string(MD_SAMPLE)
    assert parser.extract_table_name() == "public.dim_company"


def test_extract_table_name_with_database_object_type():
    parser = MDParser.from_string(
        "# public.mv_sales [materialized_view]（销售汇总）\n"
        "## 字段列表：\n"
        "- category_id (integer) - 分类ID [示例: 1]\n"
    )

    assert parser.extract_table_name() == "public.mv_sales"
    assert parser.get_column_descriptions() == {"category_id": "分类ID"}


def test_get_column_descriptions_standard():
    parser = MDParser.from_string(MD_SAMPLE)
    cols = parser.get_column_descriptions()
    assert cols["company_id"] == "公司ID（主键）"
    assert cols["company_name"] == "公司名称，唯一"
    # 确认遇到下一个章节后停止解析
    assert len(cols) == 2


def test_get_table_description_returns_full_content():
    parser = MDParser.from_string(MD_SAMPLE)
    desc = parser.get_table_description()
    assert "# public.dim_company" in desc
    assert "公司名称，唯一" in desc


def test_extract_table_name_invalid_title_raises():
    bad_md = "- no title\n- row"
    parser = MDParser.from_string(bad_md)
    with pytest.raises(ValueError):
        parser.extract_table_name()


def _parse(*lines, header="## 字段列表："):
    return MDParser.from_string(
        "\n".join(["# db.public.t", header, *lines])
    ).get_column_descriptions()


def test_nested_parentheses_types_and_example_markers():
    cols = _parse(
        "- amount (numeric(10,2)) - 金额 [示例: 12.50]",
        "- name (character varying(200)) - 名称",
        "- ts (timestamp(3) with time zone) - 时间 [示例: 2026-01-01]",
    )
    assert cols == {"amount": "金额", "name": "名称", "ts": "时间"}


def test_empty_descriptions_are_kept_not_dropped():
    cols = _parse(
        "- remark (text) -",
        "- note (text) - [示例: x]",
        "- blank (text) -   ",
        "- none (text) - 无注释 [示例: x]",
        "- order_id (integer) - 订单ID",
    )
    assert cols == {
        "remark": "",
        "note": "",
        "blank": "",
        "none": "无注释",
        "order_id": "订单ID",
    }


def test_is_empty_description_only_exact_match():
    from metaweave.core.table_schema.md_parser import is_empty_description

    assert is_empty_description("") and is_empty_description("  ")
    assert is_empty_description("无注释") and is_empty_description(" 无注释 ")
    assert not is_empty_description("无注释但有说明")
    assert not is_empty_description("订单")


def test_names_with_spaces_and_dollar_signs():
    cols = _parse(
        "- order id (integer) - 订单",
        "- price$ (numeric(10,2)) - 价格 [示例: 1]",
    )
    assert cols == {"order id": "订单", "price$": "价格"}


def test_example_containing_closing_bracket_and_rightmost_marker():
    cols = _parse(
        "- a (text) - 描述 [示例: x], y]",
        "- b (text) - 说明 [示例: 假 [示例: 内层] [示例: z]",
    )
    assert cols["a"] == "描述"
    assert cols["b"] == "说明 [示例: 假 [示例: 内层]"


def test_blank_lines_and_prose_ignored_and_section_ends():
    md = (
        "# db.public.t\n前言\n## 字段列表\n\n说明文字\n- a (integer) - A\n"
        "## 字段补充说明：\n- 不是字段\n"
    )
    assert MDParser.from_string(md).get_column_descriptions() == {"a": "A"}


@pytest.mark.parametrize(
    "line",
    [
        "-a (integer) - x",  # “-”后缺空格
        "- (integer) - x",  # 缺字段名
        "- a - x",  # 缺类型
        "- a () - x",  # 类型为空
        "- a (integer - x",  # 括号未闭合
        "- a (integer)",  # 缺描述分隔符
        "- a (integer)- x",  # 分隔符前无空白
        "- a (integer) -说明",  # 分隔符后无空白
        "- a (text) - [示例: x",  # 示例缺行末 ]
        "-",
    ],
)
def test_malformed_field_lines_raise_without_echoing_line(line):
    with pytest.raises(ValueError) as info:
        _parse(line)
    assert "示例: x" not in str(info.value)


def test_duplicate_fields_raise():
    with pytest.raises(ValueError, match="重复"):
        _parse("- a (integer) - 一", "- a (integer) - 二")
