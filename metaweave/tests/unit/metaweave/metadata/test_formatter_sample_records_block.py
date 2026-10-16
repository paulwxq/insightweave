import json
import re
from threading import Lock
from unittest.mock import MagicMock

import pandas as pd
import pytest

from metaweave.core.metadata.formatter import OutputFormatter
from metaweave.core.metadata.generator import MetadataGenerator
from metaweave.core.metadata.models import ColumnInfo, TableMetadata


@pytest.mark.parametrize("invalid_count", [-1, True, "5", 1.5])
def test_sample_record_count_must_be_a_non_negative_integer(
    tmp_path,
    invalid_count,
):
    with pytest.raises(ValueError, match="sample_records.count 必须是非负整数"):
        OutputFormatter(
            {
                "output_dir": tmp_path,
                "ddl_options": {
                    "sample_records": {"count": invalid_count},
                },
            },
            database_name="store_db",
        )


def test_generate_ddl_uses_compact_sampled_records_block(tmp_path):
    formatter = OutputFormatter(
        {
            "output_dir": tmp_path,
            "formats": ["ddl"],
            "ddl_options": {
                "sample_records": {
                    "enabled": True,
                    "count": 1,
                }
            },
        },
        database_name="store_db",
    )

    metadata = TableMetadata(
        schema_name="public",
        table_name="department",
        columns=[
            ColumnInfo(
                column_name="dept_id",
                ordinal_position=1,
                data_type="integer",
                is_nullable=False,
            ),
            ColumnInfo(
                column_name="dept_name",
                ordinal_position=2,
                data_type="character varying",
                is_nullable=False,
            ),
        ],
    )
    sample_df = pd.DataFrame([{"dept_id": 1, "dept_name": "HR"}])

    ddl = formatter.generate_ddl(metadata, sample_data=sample_df)
    match = re.search(
        r"/\*\s*SAMPLED_RECORDS\s*(?P<body>\{.*?\})\s*\*/",
        ddl,
        re.DOTALL,
    )
    assert match, "SAMPLED_RECORDS block should exist"

    payload = json.loads(match.group("body"))
    assert payload == {
        "object_type": "table",
        "object_name": "public.department",
        "records": [{"dept_id": "1", "dept_name": "HR"}],
    }
    assert "/* SAMPLE_RECORDS" not in ddl


def test_configured_sample_count_flows_from_ddl_to_json(tmp_path):
    formatter = OutputFormatter(
        {
            "output_dir": tmp_path,
            "ddl_options": {
                "sample_records": {
                    "enabled": True,
                    "count": 10,
                }
            },
        },
        database_name="store_db",
    )
    metadata = TableMetadata(
        schema_name="public",
        table_name="events",
        columns=[ColumnInfo("event_id", 1, "integer")],
        row_count=10,
    )
    sample_df = pd.DataFrame(
        [{"event_id": value} for value in range(10)],
        dtype=object,
    )

    formatter._save_ddl(metadata, sample_df)
    extracted = formatter._extract_sample_records_from_ddl(metadata)

    assert extracted is not None
    assert extracted["sample_method"] == "limit"
    assert extracted["sample_size"] == 10
    assert len(extracted["records"]) == 10

    json_path = formatter._save_json(metadata)
    json_data = json.loads(json_path.read_text(encoding="utf-8"))
    assert json_data["sample_records"] == {
        "sample_method": "limit",
        "records": [{"event_id": value} for value in range(10)],
    }


def test_formatter_rejects_legacy_sample_records_block(tmp_path):
    """旧 SAMPLE_RECORDS 标记按输入错误上抛，不再按旧包装解读（doc 19.2 §8.2）。"""
    formatter = OutputFormatter(
        {
            "output_dir": tmp_path,
            "ddl_directory": str(tmp_path),
            "ddl_options": {"sample_records": {"count": 5}},
        },
        database_name="store_db",
    )
    metadata = TableMetadata(
        schema_name="public",
        table_name="legacy_events",
        columns=[ColumnInfo("event_id", 1, "integer")],
        row_count=20,
    )
    ddl_path = tmp_path / "store_db.public.legacy_events.sql"
    ddl_path.write_text(
        """/* SAMPLE_RECORDS
{
  "version": 1,
  "table": "public.legacy_events",
  "sample_method": "limit",
  "records": [
    {"label": "Record 1", "data": {"event_id": "7"}}
  ]
}
*/""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="旧格式"):
        formatter._extract_sample_records_from_ddl(metadata)


def test_md_step_uses_typed_json_sample_records() -> None:
    """md 从 JSON 还原样例：数值已类型化，DataFrame 原样交给渲染。"""
    metadata = TableMetadata(
        schema_name="public",
        table_name="events",
        columns=[ColumnInfo("event_id", 1, "integer")],
        sample_records=[{"event_id": 1}, {"event_id": 2}],
    )
    loader = MagicMock()
    loader.load_object.return_value = metadata

    generator = MetadataGenerator.__new__(MetadataGenerator)
    generator.database_name = "store_db"
    generator._get_json_loader = MagicMock(return_value=loader)
    generator._result_lock = Lock()
    generator.formatter = MagicMock()
    generator.formatter.format_and_save.return_value = {
        "markdown": "/tmp/store_db.public.events.md"
    }

    generator._process_table_from_json_for_md(
        "public",
        "events",
        "table",
        MagicMock(),
    )

    call = generator.formatter.format_and_save.call_args
    assert call.kwargs["formats_override"] == ["markdown"]
    assert call.args[1].to_dict(orient="records") == [
        {"event_id": 1},
        {"event_id": 2},
    ]


def test_generate_ddl_escapes_tricky_sample_values(tmp_path):
    """样例值含 /*、*/、*/* 等序列时块注释无裸 *，解码还原原值（doc 19.2 §8.2）。"""
    from tests.unit.metaweave.metadata.test_sample_block import TRICKY_VALUES
    from metaweave.core.metadata.sample_block import find_sampled_records_blocks

    formatter = OutputFormatter(
        {
            "output_dir": tmp_path,
            "ddl_options": {"sample_records": {"count": len(TRICKY_VALUES)}},
        },
        database_name="store_db",
    )
    metadata = TableMetadata(
        schema_name="public",
        table_name="tricky",
        columns=[
            ColumnInfo("val", 1, "text"),
        ],
    )
    sample_df = pd.DataFrame([{"val": value} for value in TRICKY_VALUES])

    ddl = formatter.generate_ddl(metadata, sample_data=sample_df)
    blocks = find_sampled_records_blocks(ddl)
    assert len(blocks) == 1
    payload = json.loads(blocks[0].group("body"))
    expected = [
        {"val": None if value is None else str(value)} for value in TRICKY_VALUES
    ]
    assert payload["records"] == expected
    # 块注释体（除首尾标记行）不含裸 *，即不可能出现 /* 或 */
    block_text = ddl[blocks[0].start() : blocks[0].end()]
    body_lines = block_text.splitlines()[1:-1]
    assert all("*" not in line for line in body_lines)


def test_view_object_metadata_block_escapes_tricky_comments(tmp_path):
    """View/MV 的 OBJECT_METADATA 块同样转义；解析还原注释原文（doc 19.2 §8.2）。"""
    from metaweave.core.metadata.sample_block import find_sampled_records_blocks

    formatter = OutputFormatter(
        {"output_dir": tmp_path, "ddl_options": {"sample_records": {"count": 0}}},
        database_name="store_db",
    )
    metadata = TableMetadata(
        schema_name="public",
        table_name="v_tricky",
        object_type="view",
        view_definition="SELECT 1 AS id",
        comment="含 */ 与 /* 与 100%*/* 的对象注释",
        columns=[
            ColumnInfo(
                "id",
                1,
                "integer",
                comment="列注释 }*/ 结尾",
            ),
        ],
    )

    ddl = formatter.generate_ddl(metadata)
    assert "/* OBJECT_METADATA" in ddl
    assert find_sampled_records_blocks(ddl) == []  # count=0 无样例块
    object_block = re.search(
        r"/\*\s*OBJECT_METADATA\s*(?P<body>\{.*?\})\s*\*/", ddl, re.DOTALL
    )
    assert object_block, "OBJECT_METADATA 块应存在"
    body_lines = ddl[object_block.start() : object_block.end()].splitlines()[1:-1]
    assert all("*" not in line for line in body_lines)
    payload = json.loads(object_block.group("body"))
    assert payload["object_comment"] == "含 */ 与 /* 与 100%*/* 的对象注释"
    assert payload["columns"][0]["column_comment"] == "列注释 }*/ 结尾"
