import json
from unittest.mock import MagicMock

import pytest
from pathlib import Path

from metaweave.core.dim_value.config_generator import DimTableConfigGenerator
from metaweave.core.dim_value.models import DimTablesConfig


def test_generate_dim_tables(tmp_path: Path):
    json_dir = tmp_path / "json"
    json_dir.mkdir()

    # dim 表
    (json_dir / "dvdrental.public.dim_company.json").write_text(
        json.dumps(
            {
                "object_info": {
                    "database": "dvdrental",
                    "schema_name": "public",
                    "object_name": "dim_company",
                },
                "table_profile": {"table_category": "dim"},
            }
        ),
        encoding="utf-8",
    )
    # 通过文件名回退解析 database
    (json_dir / "analytics.public.dim_region.json").write_text(
        json.dumps(
            {
                "table_profile": {
                    "table_category": "dim",
                    "schema_name": "public",
                    "table_name": "dim_region",
                }
            }
        ),
        encoding="utf-8",
    )
    # 非 dim 表
    (json_dir / "fact_sales.json").write_text(
        json.dumps(
            {
                "table_profile": {
                    "table_category": "fact",
                    "schema_name": "public",
                    "table_name": "fact_sales",
                }
            }
        ),
        encoding="utf-8",
    )

    output_path = tmp_path / "dim_tables.yaml"
    generator = DimTableConfigGenerator(json_dir=json_dir, output_path=output_path)

    config = generator.generate()
    assert "dvdrental" in config["databases"]
    assert "analytics" in config["databases"]
    assert "public.dim_company" in config["databases"]["dvdrental"]["tables"]
    assert "public.dim_region" in config["databases"]["analytics"]["tables"]
    assert "public.fact_sales" not in config["databases"]["dvdrental"]["tables"]
    assert output_path.exists()

    parsed = DimTablesConfig.from_yaml(config, database="dvdrental")
    assert "public.dim_company" in parsed.tables
    assert parsed.tables["public.dim_company"].embedding_col is None


def _dim_payload(database: str, table: str) -> str:
    return json.dumps(
        {
            "object_info": {
                "database": database,
                "schema_name": "public",
                "object_name": table,
            },
            "table_profile": {"table_category": "dim"},
        }
    )


def test_generate_with_explicit_json_files_reads_only_the_list(tmp_path: Path):
    """doc 22.1 §5.3（审核修订 1）：传入 json_files 时不再 glob 目录，
    列表外的普通 .json 也不入产物。"""
    json_dir = tmp_path / "json"
    json_dir.mkdir()
    in_list = json_dir / "demo.public.dim_a.json"
    in_list.write_text(_dim_payload("demo", "dim_a"), encoding="utf-8")
    not_in_list = json_dir / "demo.public.dim_b.json"
    not_in_list.write_text(_dim_payload("demo", "dim_b"), encoding="utf-8")

    output_path = tmp_path / "dim_tables.yaml"
    generator = DimTableConfigGenerator(
        json_dir=json_dir, output_path=output_path, json_files=[in_list]
    )

    config = generator.generate()
    tables = config["databases"]["demo"]["tables"]
    assert "public.dim_a" in tables
    assert "public.dim_b" not in tables
    assert output_path.exists()


def test_generate_json_files_none_keeps_directory_scan(tmp_path: Path):
    """doc 22.1 §5.3：json_files=None 时保持现状重扫（CLI 回归语义）。"""
    json_dir = tmp_path / "json"
    json_dir.mkdir()
    (json_dir / "demo.public.dim_a.json").write_text(
        _dim_payload("demo", "dim_a"), encoding="utf-8"
    )
    (json_dir / "demo.public.dim_b.json").write_text(
        _dim_payload("demo", "dim_b"), encoding="utf-8"
    )

    output_path = tmp_path / "dim_tables.yaml"
    generator = DimTableConfigGenerator(json_dir=json_dir, output_path=output_path)

    config = generator.generate()
    tables = config["databases"]["demo"]["tables"]
    assert "public.dim_a" in tables
    assert "public.dim_b" in tables


def test_last_readable_count_tracks_parseable_files(tmp_path: Path):
    """doc 22.1 §5.3（审核修订 7）：last_readable_count 统计本轮成功解析为
    JSON 对象的画像文件数；无法解析的文件不计入。"""
    json_dir = tmp_path / "json"
    json_dir.mkdir()
    good = json_dir / "demo.public.dim_a.json"
    good.write_text(_dim_payload("demo", "dim_a"), encoding="utf-8")
    corrupt = json_dir / "demo.public.dim_b.json"
    corrupt.write_text("{ not json", encoding="utf-8")
    not_object = json_dir / "demo.public.dim_c.json"
    not_object.write_text('["a", "b"]', encoding="utf-8")

    output_path = tmp_path / "dim_tables.yaml"
    generator = DimTableConfigGenerator(json_dir=json_dir, output_path=output_path)
    generator.generate()
    assert generator.last_readable_count == 1

    # 显式列表路径同样计数
    output_path2 = tmp_path / "dim_tables2.yaml"
    generator2 = DimTableConfigGenerator(
        json_dir=json_dir, output_path=output_path2, json_files=[corrupt, not_object]
    )
    generator2.generate()
    assert generator2.last_readable_count == 0


# ---- doc 7.1：维度值字段推荐映射与逐表注释 ----


def _dim_payload_with_rec(
    database: str,
    table: str,
    columns="missing",
    confidence="missing",
    column_profiles=None,
) -> str:
    table_profile: dict = {"table_category": "dim"}
    if columns != "missing":
        table_profile["dimension_value_columns"] = columns
    if confidence != "missing":
        table_profile["dimension_value_confidence"] = confidence
    return json.dumps(
        {
            "object_info": {
                "database": database,
                "schema_name": "public",
                "object_name": table,
            },
            "column_profiles": column_profiles
            if column_profiles is not None
            else {"product_id": {}, "product_name": {}},
            "table_profile": table_profile,
        }
    )


def _write_profile(json_dir, filename: str, payload: str) -> None:
    (json_dir / filename).write_text(payload, encoding="utf-8")


def test_recommendation_maps_to_embedding_col_with_comment(tmp_path: Path):
    json_dir = tmp_path / "json"
    json_dir.mkdir()
    _write_profile(
        json_dir,
        "demo.public.dim_product.json",
        _dim_payload_with_rec("demo", "dim_product", ["product_name"], 0.92),
    )

    output_path = tmp_path / "dim_tables.yaml"
    generator = DimTableConfigGenerator(json_dir=json_dir, output_path=output_path)
    config = generator.generate()

    tables = config["databases"]["demo"]["tables"]
    assert tables["public.dim_product"]["embedding_col"] == "product_name"
    text = output_path.read_text(encoding="utf-8")
    lines = text.splitlines()
    key_idx = lines.index("      public.dim_product:")
    assert lines[key_idx - 1] == "      # 模型原始推荐: [product_name]；推荐置信度: 0.92"
    assert lines[key_idx + 1] == "        embedding_col: product_name"


def test_no_recommendation_keeps_null_with_comment(tmp_path: Path):
    json_dir = tmp_path / "json"
    json_dir.mkdir()
    _write_profile(
        json_dir,
        "demo.public.dim_a.json",
        _dim_payload_with_rec("demo", "dim_a", [], None),
    )

    output_path = tmp_path / "dim_tables.yaml"
    generator = DimTableConfigGenerator(json_dir=json_dir, output_path=output_path)
    config = generator.generate()

    tables = config["databases"]["demo"]["tables"]
    assert tables["public.dim_a"]["embedding_col"] is None
    lines = output_path.read_text(encoding="utf-8").splitlines()
    key_idx = lines.index("      public.dim_a:")
    assert lines[key_idx - 1] == "      # 模型原始推荐: 无；推荐置信度: 不适用"
    assert lines[key_idx + 1] == "        embedding_col: null"


def test_missing_recommendation_attributes_treated_as_none(tmp_path: Path):
    json_dir = tmp_path / "json"
    json_dir.mkdir()
    _write_profile(
        json_dir,
        "demo.public.dim_old.json",
        _dim_payload_with_rec("demo", "dim_old"),
    )

    output_path = tmp_path / "dim_tables.yaml"
    generator = DimTableConfigGenerator(json_dir=json_dir, output_path=output_path)
    config = generator.generate()

    assert config["databases"]["demo"]["tables"]["public.dim_old"][
        "embedding_col"
    ] is None
    assert "模型原始推荐: 无" in output_path.read_text(encoding="utf-8")


def test_recommendation_case_preserved(tmp_path: Path):
    json_dir = tmp_path / "json"
    json_dir.mkdir()
    _write_profile(
        json_dir,
        "demo.public.dim_c.json",
        _dim_payload_with_rec(
            "demo",
            "dim_c",
            ["ProductName"],
            0.5,
            column_profiles={"product_id": {}, "ProductName": {}},
        ),
    )

    output_path = tmp_path / "dim_tables.yaml"
    generator = DimTableConfigGenerator(json_dir=json_dir, output_path=output_path)
    config = generator.generate()

    assert (
        config["databases"]["demo"]["tables"]["public.dim_c"]["embedding_col"]
        == "ProductName"
    )


@pytest.mark.parametrize(
    "columns,confidence",
    [
        (["product_id", "product_name"], 0.9),
        (["not_a_column"], 0.9),
        (["product_name"], "missing"),
        (["product_name"], "high"),
        (["product_name"], 10**400),
        ("product_name", 0.9),
    ],
    ids=[
        "multi",
        "unknown-col",
        "missing-confidence",
        "bad-confidence",
        "huge-confidence",
        "not-list",
    ],
)
def test_invalid_recommendation_rejected_and_logged(
    tmp_path: Path, caplog, columns, confidence
):
    import logging

    json_dir = tmp_path / "json"
    json_dir.mkdir()
    payload = _dim_payload_with_rec("demo", "dim_x", columns, confidence)
    _write_profile(json_dir, "demo.public.dim_x.json", payload)

    output_path = tmp_path / "dim_tables.yaml"
    generator = DimTableConfigGenerator(json_dir=json_dir, output_path=output_path)
    with caplog.at_level(logging.WARNING):
        config = generator.generate()

    assert config["databases"]["demo"]["tables"]["public.dim_x"][
        "embedding_col"
    ] is None
    assert "维度值字段推荐" in caplog.text
    # 不回写输入画像（§5.2）
    assert (json_dir / "demo.public.dim_x.json").read_text(encoding="utf-8") == payload


def test_generated_yaml_roundtrip_ignoring_comments(tmp_path: Path):
    import yaml as yaml_lib

    json_dir = tmp_path / "json"
    json_dir.mkdir()
    _write_profile(
        json_dir,
        "demo.public.dim_product.json",
        _dim_payload_with_rec("demo", "dim_product", ["product_name"], 0.92),
    )
    _write_profile(
        json_dir,
        "demo.public.dim_none.json",
        _dim_payload_with_rec("demo", "dim_none", [], None),
    )

    output_path = tmp_path / "dim_tables.yaml"
    DimTableConfigGenerator(json_dir=json_dir, output_path=output_path).generate()

    parsed = yaml_lib.safe_load(output_path.read_text(encoding="utf-8"))
    tables = parsed["databases"]["demo"]["tables"]
    assert tables["public.dim_product"]["embedding_col"] == "product_name"
    assert tables["public.dim_none"]["embedding_col"] is None
    cfg = DimTablesConfig.from_yaml(parsed, database="demo")
    assert cfg.tables["public.dim_product"].embedding_col == "product_name"


def test_empty_databases_renders_flow_empty(tmp_path: Path):
    json_dir = tmp_path / "json"
    json_dir.mkdir()
    _write_profile(
        json_dir,
        "demo.public.fact_a.json",
        json.dumps(
            {
                "object_info": {
                    "database": "demo",
                    "schema_name": "public",
                    "object_name": "fact_a",
                },
                "table_profile": {"table_category": "fact"},
            }
        ),
    )

    output_path = tmp_path / "dim_tables.yaml"
    DimTableConfigGenerator(json_dir=json_dir, output_path=output_path).generate()

    text = output_path.read_text(encoding="utf-8")
    assert "databases: {}" in text


def test_header_copy_updated(tmp_path: Path):
    json_dir = tmp_path / "json"
    json_dir.mkdir()
    _write_profile(
        json_dir,
        "demo.public.dim_a.json",
        _dim_payload_with_rec("demo", "dim_a", ["product_name"], 0.9),
    )

    output_path = tmp_path / "dim_tables.yaml"
    DimTableConfigGenerator(json_dir=json_dir, output_path=output_path).generate()

    text = output_path.read_text(encoding="utf-8")
    assert "需人工填写" not in text
    assert "多列" not in text
    assert "逗号" not in text
    assert "自动填入单个维度值字段" in text
    assert "生成时" in text  # 文件头说明注释仅描述生成时模型推荐
    assert "仅支持一个字段" in text


def test_recommended_counts_exposed(tmp_path: Path):
    json_dir = tmp_path / "json"
    json_dir.mkdir()
    _write_profile(
        json_dir,
        "demo.public.dim_rec.json",
        _dim_payload_with_rec("demo", "dim_rec", ["product_name"], 0.9),
    )
    _write_profile(
        json_dir,
        "demo.public.dim_none.json",
        _dim_payload_with_rec("demo", "dim_none", [], None),
    )

    output_path = tmp_path / "dim_tables.yaml"
    generator = DimTableConfigGenerator(json_dir=json_dir, output_path=output_path)
    generator.generate()

    assert generator.last_recommended_count == 1
    assert generator.last_unrecommended_count == 1


def test_full_chain_from_llm_response_to_yaml(tmp_path: Path):
    """doc 7.1 §6/§8.1：增强器合并结果可直接被生成器消费成配置。"""
    from metaweave.core.metadata.json_llm_enhancer import JsonLlmEnhancer

    config = {
        "llm": {
            "active": "qwen",
            "providers": {
                "qwen": {"model": "qwen-plus", "api_key": "k", "api_base": "u"}
            },
        },
        "json_generation": {
            "comments": {"llm_enabled": False},
            "table_classification": {"llm_enabled": True},
        },
    }
    enhancer = JsonLlmEnhancer(config)
    service = MagicMock()
    service.model = "test-model"
    service.call_llm.return_value = json.dumps(
        {
            "table_category": "dim",
            "confidence": 0.95,
            "reason": "以产品为实体",
            "dimension_value_columns": ["product_name"],
            "dimension_value_confidence": 0.92,
        }
    )
    enhancer.llm_service = service

    json_dir = tmp_path / "json"
    json_dir.mkdir()
    profile_path = json_dir / "demo.public.dim_product.json"
    profile_path.write_text(
        json.dumps(
            {
                "metadata_version": "3.0",
                "object_info": {
                    "schema_name": "public",
                    "object_name": "dim_product",
                    "comment": "",
                    "database": "demo",
                },
                "column_profiles": {
                    "product_id": {"ordinal_position": 1},
                    "product_name": {"ordinal_position": 2},
                },
                "table_profile": {
                    "table_category": "unknown",
                    "confidence": 0.5,
                    "inference_basis": ["fallback"],
                    "classification_source": "rule",
                    "indexes": [],
                },
                "sample_records": {"sample_method": "none", "records": []},
            }
        ),
        encoding="utf-8",
    )
    enhanced = enhancer.enhance_document(json.loads(profile_path.read_text("utf-8")))
    assert enhanced.success is True
    # 模拟 JSON 步骤落盘增强后的画像
    profile_path.write_text(
        json.dumps(enhanced.document, ensure_ascii=False), encoding="utf-8"
    )

    output_path = tmp_path / "dim_tables.yaml"
    generator = DimTableConfigGenerator(
        json_dir=json_dir, output_path=output_path, json_files=[profile_path]
    )
    config_out = generator.generate()

    assert (
        config_out["databases"]["demo"]["tables"]["public.dim_product"][
            "embedding_col"
        ]
        == "product_name"
    )
    text = output_path.read_text(encoding="utf-8")
    assert "模型原始推荐: [product_name]；推荐置信度: 0.92" in text


def test_broken_column_profiles_keeps_table_with_null(tmp_path: Path, caplog):
    """doc 7.1 §4.4/§5.2：画像 column_profiles 结构异常只清空该表推荐，不阻断生成。"""
    import logging

    json_dir = tmp_path / "json"
    json_dir.mkdir()
    (json_dir / "demo.public.dim_x.json").write_text(
        json.dumps(
            {
                "object_info": {
                    "database": "demo",
                    "schema_name": "public",
                    "object_name": "dim_x",
                },
                "column_profiles": ["broken"],
                "table_profile": {
                    "table_category": "dim",
                    "dimension_value_columns": ["name"],
                    "dimension_value_confidence": 0.9,
                },
            }
        ),
        encoding="utf-8",
    )

    output_path = tmp_path / "dim_tables.yaml"
    generator = DimTableConfigGenerator(json_dir=json_dir, output_path=output_path)
    with caplog.at_level(logging.WARNING):
        config = generator.generate()

    tables = config["databases"]["demo"]["tables"]
    assert tables["public.dim_x"]["embedding_col"] is None
    assert "维度值字段推荐" in caplog.text
    assert generator.last_unrecommended_count == 1
