"""md 步骤读 JSON 产物的对象类型支持（doc 17 §3.4/§3.5）。

夹具直接写合法的 v3 JSON 文档（不再写 DDL），覆盖 table / view /
materialized_view 三类对象：md 全程零数据库访问、不触碰 DDL 加载器，
对象类型筛选与 exclude/tables 过滤语义保持不变。
"""

import json
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from metaweave.core.metadata.generator import MetadataGenerator


def _write_config(tmp_path: Path, object_types: list[str]) -> Path:
    md_dir = tmp_path / "md"
    config = {
        "database": {
            "database": "store_db",
            "schemas": ["public"],
            "include_object_types": object_types,
            "exclude_tables": [],
        },
        "sampling": {"enabled": False},
        "logical_key_detection": {"enabled": False},
        "ddl_generation": {"comments": {"llm_enabled": False}},
        "json_generation": {
            "comments": {"llm_enabled": False},
            "table_classification": {"llm_enabled": False},
        },
        "output": {
            "output_dir": str(tmp_path / "output"),
            "markdown_directory": str(md_dir),
            "json_directory": str(tmp_path / "json"),
            "formats": ["ddl", "markdown", "json"],
            "ddl_options": {"sample_records": {"enabled": False, "count": 0}},
        },
    }
    config_path = tmp_path / "metadata_config.yaml"
    config_path.write_text(
        yaml.safe_dump(config, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return config_path


def _column(name: str, ordinal: int, *, comment: str = "订单ID") -> dict:
    return {
        "ordinal_position": ordinal,
        "data_type": "integer",
        "is_nullable": False,
        "column_default": None,
        "comment": comment,
        "comment_source": "ddl",
        "semantic_analysis": {
            "semantic_role": "identifier",
            "semantic_confidence": 0.9,
            "inference_basis": [],
        },
    }


def _v3_document(
    object_name: str,
    object_type: str,
    *,
    comment: str,
    primary_key: dict | None = None,
    indexes: list[dict] | None = None,
    records: list[dict] | None = None,
) -> dict:
    """构造一份能通过 MetadataDocument.from_dict 校验的最小 v3 文档。"""
    document = {
        "metadata_version": "3.0",
        "object_info": {
            "database": "store_db",
            "schema_name": "public",
            "object_name": object_name,
            "object_type": object_type,
            "comment": comment,
            "comment_source": "ddl",
            "total_rows": 1,
        },
        "column_profiles": {"order_id": _column("order_id", 1)},
        "table_profile": {
            "table_category": "fact",
            "confidence": 0.9,
            "inference_basis": [],
            "classification_source": "rule",
            "physical_constraints": {
                "primary_key": primary_key,
                "foreign_keys": [],
                "unique_constraints": [],
            },
            "indexes": indexes or [],
        },
        "sample_records": {
            "sample_method": "limit" if records else "none",
            "records": records or [],
        },
    }
    return document


def _write_json(json_dir: Path, document: dict) -> None:
    json_dir.mkdir(parents=True, exist_ok=True)
    object_info = document["object_info"]
    filename = (
        f"{object_info['database']}.{object_info['schema_name']}."
        f"{object_info['object_name']}.json"
    )
    (json_dir / filename).write_text(
        json.dumps(document, ensure_ascii=False), encoding="utf-8"
    )


def _write_table_json(json_dir: Path) -> None:
    _write_json(
        json_dir,
        _v3_document(
            "orders",
            "table",
            comment="订单表",
            primary_key={"constraint_name": "orders_pkey", "columns": ["order_id"]},
            records=[{"order_id": 1}],
        ),
    )


def _write_derived_json(json_dir: Path, object_name: str, object_type: str) -> None:
    _write_json(
        json_dir,
        _v3_document(
            object_name,
            object_type,
            comment="派生订单对象",
            indexes=(
                [
                    {
                        "index_name": f"{object_name}_order_uidx",
                        "index_type": "btree",
                        "columns": ["order_id"],
                        "is_unique": True,
                        "is_primary": False,
                        "condition": None,
                        "is_constraint_backed": False,
                        "constraint_name": None,
                        "definition": None,
                        "included_columns": [],
                        "key_expressions": ["order_id"],
                    }
                ]
                if object_type == "materialized_view"
                else None
            ),
        ),
    )


def _write_all_object_types(json_dir: Path) -> None:
    _write_table_json(json_dir)
    _write_derived_json(json_dir, "v_orders", "view")
    _write_derived_json(json_dir, "mv_orders", "materialized_view")


def test_md_step_generates_table_view_and_materialized_view(tmp_path) -> None:
    config_path = _write_config(
        tmp_path,
        ["table", "view", "materialized_view"],
    )
    _write_all_object_types(tmp_path / "json")
    generator = MetadataGenerator(config_path)

    # md 只读 JSON：DDL 加载器被触碰即失败（doc 17 D1 单一数据源）
    with patch.object(
        generator._get_ddl_loader().__class__,
        "load_table",
        side_effect=AssertionError("md 步骤不得读取 DDL"),
    ):
        result = generator.generate(step="md", max_workers=2)

    assert result.success is True
    assert result.processed_tables == 3
    assert result.failed_tables == 0
    assert result.processed_object_counts == {
        "table": 1,
        "view": 1,
        "materialized_view": 1,
    }
    assert len(result.output_files) == 3
    assert generator.connector is None
    assert generator.llm_service is None

    md_dir = tmp_path / "md"
    assert (
        (md_dir / "store_db.public.orders.md")
        .read_text(encoding="utf-8")
        .startswith("# public.orders [table]（订单表）")
    )
    assert (
        (md_dir / "store_db.public.v_orders.md")
        .read_text(encoding="utf-8")
        .startswith("# public.v_orders [view]（派生订单对象）")
    )
    assert "[示例: null]" in (md_dir / "store_db.public.v_orders.md").read_text(
        encoding="utf-8"
    )
    mv_markdown = (md_dir / "store_db.public.mv_orders.md").read_text(encoding="utf-8")
    assert mv_markdown.startswith(
        "# public.mv_orders [materialized_view]（派生订单对象）"
    )
    assert "- 唯一索引 mv_orders_order_uidx (btree): order_id" in mv_markdown


@pytest.mark.parametrize(
    ("selected_type", "expected_file"),
    [
        ("table", "store_db.public.orders.md"),
        ("view", "store_db.public.v_orders.md"),
        ("materialized_view", "store_db.public.mv_orders.md"),
    ],
)
def test_md_step_honors_include_object_types(
    tmp_path,
    selected_type,
    expected_file,
) -> None:
    config_path = _write_config(tmp_path, [selected_type])
    _write_all_object_types(tmp_path / "json")

    result = MetadataGenerator(config_path).generate(step="md", max_workers=1)

    assert result.processed_tables == 1
    assert result.failed_tables == 0
    assert result.processed_object_counts == {selected_type: 1}
    assert sorted(path.name for path in (tmp_path / "md").glob("*.md")) == [
        expected_file
    ]


def test_md_step_honors_tables_and_exclusions(tmp_path) -> None:
    config_path = _write_config(
        tmp_path,
        ["table", "view", "materialized_view"],
    )
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config["database"]["exclude_tables"] = ["public.v_orders"]
    config_path.write_text(
        yaml.safe_dump(config, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    _write_all_object_types(tmp_path / "json")

    result = MetadataGenerator(config_path).generate(
        step="md",
        tables=["v_orders", "mv_orders"],
        max_workers=1,
    )

    assert result.processed_tables == 1
    assert result.processed_object_counts == {"materialized_view": 1}


def test_md_step_reports_markdown_save_failure_once(tmp_path) -> None:
    config_path = _write_config(tmp_path, ["table"])
    _write_table_json(tmp_path / "json")
    generator = MetadataGenerator(config_path)

    with patch.object(generator.formatter, "format_and_save", return_value={}):
        result = generator.generate(step="md", max_workers=1)

    assert result.success is False
    assert result.processed_tables == 0
    assert result.failed_tables == 1
    assert result.processed_object_counts == {}
    assert len(result.errors) == 1
    assert "Markdown 保存失败" in result.errors[0]
