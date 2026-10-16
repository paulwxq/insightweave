"""doc 20.1 §1.2 共用受管输入准备函数：A05/A07/M07 的输入层部分。"""

import json
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from metaweave.core.table_schema import LoaderOptions
from metaweave.core.table_schema.managed_input import prepare_managed_table_schema_input
from metaweave.utils.time_utils import utc_isoformat
from tests.unit.test_json_publish_loader import sample_document

MARKER = datetime(2026, 9, 29, 8, 0, 0, 123456, tzinfo=timezone.utc)
NAME = "ordersdb.public.orders"
SOURCE_ID = uuid4()
MD_HEAD = "# ordersdb.public.orders\n## 字段列表\n"


@pytest.fixture
def dirs(tmp_path):
    json_dir, md_dir = tmp_path / "json", tmp_path / "md"
    json_dir.mkdir()
    md_dir.mkdir()
    data = sample_document()
    data["generated_timestamp"] = utc_isoformat(MARKER)
    (json_dir / f"{NAME}.json").write_text(json.dumps(data), encoding="utf-8")
    (json_dir / "manifest.json").write_text(json.dumps({"files": [f"{NAME}.json"]}))
    (md_dir / "manifest.json").write_text(
        json.dumps({"files": [f"{NAME}.md"], "source_json_updated_at": utc_isoformat(MARKER)})
    )
    return json_dir, md_dir


def write_md(md_dir, body, head=MD_HEAD):
    (md_dir / f"{NAME}.md").write_text(head + body, encoding="utf-8")


def prepare(dirs, **opts):
    return prepare_managed_table_schema_input(
        dirs[0], dirs[1], SOURCE_ID, LoaderOptions(**opts)
    )


FULL = "- order_id (integer) - 订单ID\n- amount (numeric(10,2)) - 金额\n"


def test_normal_input_builds_table_and_column_objects(dirs):
    write_md(dirs[1], FULL)
    result = prepare(dirs)
    assert [o.object_id for o in result.groups[0]] == [NAME, f"{NAME}.order_id", f"{NAME}.amount"]
    assert result.groups[0][0].object_desc.startswith("# ordersdb.public.orders")
    assert result.empty_desc_columns == []


@pytest.mark.parametrize("empty", ["", "   ", "无注释"])
def test_empty_descriptions_skipped_when_enabled(dirs, empty):
    write_md(dirs[1], f"- order_id (integer) - {empty}\n- amount (numeric(10,2)) - 金额\n")
    result = prepare(dirs)
    assert result.empty_desc_columns == [f"{NAME}.order_id"]
    assert [o.object_id for o in result.groups[0]] == [NAME, f"{NAME}.amount"]


@pytest.mark.parametrize("include_columns", [True, False])
def test_empty_description_raises_when_skip_disabled(dirs, include_columns):
    write_md(dirs[1], "- order_id (integer) - 无注释\n- amount (numeric(10,2)) - 金额\n")
    with pytest.raises(ValueError, match="order_id"):
        prepare(dirs, skip_empty_desc=False, include_columns=include_columns)


def test_include_columns_false_still_validates_but_returns_table_only(dirs):
    write_md(dirs[1], FULL)
    result = prepare(dirs, include_columns=False)
    assert [o.object_id for o in result.groups[0]] == [NAME]
    write_md(dirs[1], "- order_id (integer) - 订单ID\n")  # 缺 amount
    with pytest.raises(ValueError, match="amount"):
        prepare(dirs, include_columns=False)


def test_all_columns_empty_loads_table_only(dirs):
    write_md(dirs[1], "- order_id (integer) -\n- amount (numeric(10,2)) - 无注释\n")
    result = prepare(dirs)
    assert len(result.groups[0]) == 1
    assert len(result.empty_desc_columns) == 2


@pytest.mark.parametrize(
    "body",
    [
        FULL + "- extra_col (text) -\n",  # 未知且空描述也不能绕过
        FULL + "- extra_col (text) - 多余\n",
        "- order_id (integer) - 订单ID\n",  # 缺失字段
        FULL + "- order_id (integer) - 重复\n",
        FULL + "- bad line without type\n",
    ],
)
def test_unknown_missing_duplicate_and_malformed_rejected(dirs, body):
    write_md(dirs[1], body)
    with pytest.raises(ValueError):
        prepare(dirs)


def test_empty_md_rejected_regardless_of_options(dirs):
    (dirs[1] / f"{NAME}.md").write_text("  \n", encoding="utf-8")
    for opts in ({}, {"skip_empty_desc": False}, {"include_columns": False}):
        with pytest.raises(ValueError, match="MD 内容为空"):
            prepare(dirs, **opts)


def test_description_containing_no_comment_text_is_not_empty(dirs):
    write_md(dirs[1], "- order_id (integer) - 无注释但有说明\n- amount (numeric(10,2)) - 金额\n")
    assert prepare(dirs).empty_desc_columns == []


def test_field_desc_over_limit_reports_object_field_sizes_without_content(dirs):
    # 字段描述 3000 个汉字 = 9000 字节 > 8192（按 UTF-8 字节而非字符数计）
    write_md(dirs[1], "- order_id (integer) - " + "金" * 3000 + "\n- amount (numeric(10,2)) - 金额\n")
    with pytest.raises(ValueError) as info:
        prepare(dirs)
    msg = str(info.value)
    assert f"{NAME}" in msg and "object_desc" in msg
    assert "实际" in msg and "上限 8192" in msg and "金金" not in msg


def test_table_desc_at_limit_passes_and_one_byte_over_fails(dirs):
    head = "# ordersdb.public.orders\n## 字段列表\n"
    base = head + FULL
    pad = 8192 - len(base.encode()) + 1  # 表级 object_desc 为整份 MD（strip 去掉末尾换行）
    # 用字段描述尾部填充，使整份 MD 恰好等于上限
    body = "- order_id (integer) - 订单ID" + "x" * pad + "\n- amount (numeric(10,2)) - 金额\n"
    write_md(dirs[1], body)
    md_text = (dirs[1] / f"{NAME}.md").read_text(encoding="utf-8").strip()
    assert len(md_text.encode()) == 8192
    # 字段描述本身 pad+4 字节也未超限
    assert prepare(dirs)
    write_md(dirs[1], body.replace("x" * pad, "x" * (pad + 1), 1))
    with pytest.raises(ValueError, match="上限 8192"):
        prepare(dirs)
