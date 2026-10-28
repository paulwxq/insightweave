"""doc 20.1 §1.2 共用受管输入准备函数：A05/A07/M07 的输入层部分。

断代改造适配（doc 6）：source_id 为短 ID；MD 清单的 JSON 来源键为
`source_collection_run_id`，值为 JSON 产物来源采集运行的 UUID
（`md_step_service` 写入、`managed_input.read_managed_pairs` 校验）。
"""

import json

import pytest

from metaweave.core.table_schema import LoaderOptions
from metaweave.core.table_schema.managed_input import prepare_managed_table_schema_input

MARKER_TEXT = "11111111-1111-4111-8111-111111111111"
OTHER_MARKER_TEXT = "22222222-2222-4222-8222-222222222222"
NAME = "ordersdb.public.orders"
SOURCE_ID = "db_src_1"
MD_HEAD = "# ordersdb.public.orders\n## 字段列表\n"


def sample_document():
    """最小合法 v3 文档：order_id/amount 两列，身份与文件名一致。"""
    return {
        "metadata_version": "3.0",
        "object_info": {
            "database": "ordersdb",
            "schema_name": "public",
            "object_name": "orders",
            "object_type": "table",
            "comment": "",
            "comment_source": "",
            "total_rows": 0,
        },
        "column_profiles": {
            "order_id": {"statistics": {}},
            "amount": {"statistics": {}},
        },
        "table_profile": None,
        "sample_records": {"sample_method": "none", "records": []},
    }


@pytest.fixture
def dirs(tmp_path):
    json_dir, md_dir = tmp_path / "json", tmp_path / "md"
    json_dir.mkdir()
    md_dir.mkdir()
    data = sample_document()
    (json_dir / f"{NAME}.json").write_text(json.dumps(data), encoding="utf-8")
    (json_dir / "manifest.json").write_text(json.dumps({"files": [f"{NAME}.json"]}))
    (md_dir / "manifest.json").write_text(
        json.dumps({"files": [f"{NAME}.md"], "source_collection_run_id": MARKER_TEXT})
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
    assert [o.object_id for o in result.groups[0]] == [
        NAME,
        f"{NAME}.order_id",
        f"{NAME}.amount",
    ]
    assert result.groups[0][0].object_desc.startswith("# ordersdb.public.orders")
    assert result.empty_desc_columns == []


def test_source_id_must_be_short_id(dirs):
    """短 ID 校验进入共用准备层：旧 UUID 入口不再合法（doc 6 §1）。"""
    write_md(dirs[1], FULL)
    with pytest.raises(ValueError, match="数据源"):
        prepare_managed_table_schema_input(
            dirs[0],
            dirs[1],
            "b4a87275-7c37-4feb-bf1c-e5a8446c174f",
            LoaderOptions(),
        )


def test_md_manifest_marker_key_and_format(dirs):
    """MD 清单 JSON 来源键为 `source_collection_run_id`，值须为合法 UUID。"""
    write_md(dirs[1], FULL)
    # 键缺失（旧 source_json_updated_at 不再被读取）→ 拒绝
    (dirs[1] / "manifest.json").write_text(
        json.dumps({"files": [f"{NAME}.md"], "source_json_updated_at": MARKER_TEXT})
    )
    with pytest.raises(ValueError):
        prepare(dirs)
    # 非 UUID 文本 → 拒绝
    (dirs[1] / "manifest.json").write_text(
        json.dumps(
            {"files": [f"{NAME}.md"], "source_collection_run_id": "not-a-marker"}
        )
    )
    with pytest.raises(ValueError):
        prepare(dirs)
    # 非字符串 → 拒绝
    (dirs[1] / "manifest.json").write_text(
        json.dumps({"files": [f"{NAME}.md"], "source_collection_run_id": 42})
    )
    with pytest.raises(ValueError):
        prepare(dirs)


def test_expected_marker_mismatch_rejected(dirs):
    write_md(dirs[1], FULL)
    with pytest.raises(ValueError, match="JSON 已重跑"):
        prepare_managed_table_schema_input(
            dirs[0],
            dirs[1],
            SOURCE_ID,
            LoaderOptions(),
            expected_json_marker=OTHER_MARKER_TEXT,
        )
    # 与清单一致时放行
    result = prepare_managed_table_schema_input(
        dirs[0], dirs[1], SOURCE_ID, LoaderOptions(), expected_json_marker=MARKER_TEXT
    )
    assert [o.object_id for o in result.groups[0]]


def test_foreign_database_rejected(dirs):
    write_md(dirs[1], FULL)
    with pytest.raises(ValueError, match="不属于当前登记数据库"):
        prepare_managed_table_schema_input(
            dirs[0], dirs[1], SOURCE_ID, LoaderOptions(), database_name="other_db"
        )
    # 登记库名一致时放行
    assert prepare_managed_table_schema_input(
        dirs[0], dirs[1], SOURCE_ID, LoaderOptions(), database_name="ordersdb"
    ).groups


@pytest.mark.parametrize("empty", ["", "   ", "无注释"])
def test_empty_descriptions_skipped_when_enabled(dirs, empty):
    write_md(
        dirs[1], f"- order_id (integer) - {empty}\n- amount (numeric(10,2)) - 金额\n"
    )
    result = prepare(dirs)
    assert result.empty_desc_columns == [f"{NAME}.order_id"]
    assert [o.object_id for o in result.groups[0]] == [NAME, f"{NAME}.amount"]


@pytest.mark.parametrize("include_columns", [True, False])
def test_empty_description_raises_when_skip_disabled(dirs, include_columns):
    write_md(
        dirs[1], "- order_id (integer) - 无注释\n- amount (numeric(10,2)) - 金额\n"
    )
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
    write_md(
        dirs[1],
        "- order_id (integer) - 无注释但有说明\n- amount (numeric(10,2)) - 金额\n",
    )
    assert prepare(dirs).empty_desc_columns == []


def test_field_desc_over_limit_reports_object_field_sizes_without_content(dirs):
    # 字段描述 3000 个汉字 = 9000 字节 > 8192（按 UTF-8 字节而非字符数计）
    write_md(
        dirs[1],
        "- order_id (integer) - " + "金" * 3000 + "\n- amount (numeric(10,2)) - 金额\n",
    )
    with pytest.raises(ValueError) as info:
        prepare(dirs)
    msg = str(info.value)
    assert f"{NAME}" in msg and "object_desc" in msg
    assert "实际" in msg and "上限 8192" in msg and "金金" not in msg


def test_table_desc_at_limit_passes_and_one_byte_over_fails(dirs):
    head = "# ordersdb.public.orders\n## 字段列表\n"
    base = head + FULL
    pad = (
        8192 - len(base.encode()) + 1
    )  # 表级 object_desc 为整份 MD（strip 去掉末尾换行）
    # 用字段描述尾部填充，使整份 MD 恰好等于上限
    body = (
        "- order_id (integer) - 订单ID"
        + "x" * pad
        + "\n- amount (numeric(10,2)) - 金额\n"
    )
    write_md(dirs[1], body)
    md_text = (dirs[1] / f"{NAME}.md").read_text(encoding="utf-8").strip()
    assert len(md_text.encode()) == 8192
    # 字段描述本身 pad+4 字节也未超限
    assert prepare(dirs)
    write_md(dirs[1], body.replace("x" * pad, "x" * (pad + 1), 1))
    with pytest.raises(ValueError, match="上限 8192"):
        prepare(dirs)
