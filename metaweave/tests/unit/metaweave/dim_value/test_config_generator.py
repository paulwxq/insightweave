import json
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
                "object_info": {"database": "dvdrental", "schema_name": "public", "object_name": "dim_company"},
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
                "table_profile": {"table_category": "fact", "schema_name": "public", "table_name": "fact_sales"}
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
