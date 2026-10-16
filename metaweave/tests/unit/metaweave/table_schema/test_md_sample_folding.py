"""doc 20.1 A13：样例值内的换行在 MD 输出边界折叠，字段行始终单行。"""

import pandas as pd

from metaweave.core.metadata.formatter import OutputFormatter
from metaweave.core.table_schema.md_parser import MDParser


def _formatter(tmp_path):
    return OutputFormatter({"output_dir": str(tmp_path)}, database_name="demo")


def test_sample_values_with_newlines_are_folded(tmp_path):
    formatter = _formatter(tmp_path)
    df = pd.DataFrame({"remark": ["第一行\n- 伪字段 (text) - x", "A\r\n## 标题", "ok"]})
    formatter.markdown_sample_value_count = 3

    value = formatter._get_sample_value("remark", df)

    assert "\n" not in value and "\r" not in value
    assert value == "第一行 - 伪字段 (text) - x, A ## 标题, ok"
    # 源数据不被修改
    assert df["remark"].iloc[0] == "第一行\n- 伪字段 (text) - x"


def test_folded_example_does_not_break_field_section():
    line_value = " ".join("a\n- fake (text) - x\n## H".splitlines())
    md = (
        "# db.public.t [table]\n## 字段列表：\n"
        f"- remark (text) - 备注 [示例: {line_value}]\n"
        "- id (integer) - ID [示例: 1]\n"
    )
    assert MDParser.from_string(md).get_column_descriptions() == {
        "remark": "备注",
        "id": "ID",
    }
