"""md 步骤的 JSON 输入源用例（doc 17 §3.4/§3.5，验收 1/3/8）。

- 正向：JSON 还原渲染标题/字段/精度说明/约束，且不出现重复修饰符；
- 负向：旧版本、缺 object_info、根节点非对象的 JSON 在枚举阶段使整步
  失败并报告文件名（不静默跳过、不按空值渲染）；
- 多行注释折叠：表/列注释的内部换行在渲染边界折叠为空格。
"""

import json
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from metaweave.cli import metadata_cli
from metaweave.core.metadata.generator import MetadataGenerator
from metaweave.core.metadata.json_loader import JsonLoader, JsonLoaderError
from metaweave.core.table_schema.md_parser import MDParser


def _write_config(tmp_path: Path) -> Path:
    config = {
        "database": {
            "database": "store_db",
            "schemas": ["public"],
            "include_object_types": ["table"],
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
            "markdown_directory": str(tmp_path / "md"),
            "json_directory": str(tmp_path / "json"),
            "formats": ["ddl", "markdown", "json"],
        },
    }
    config_path = tmp_path / "metadata_config.yaml"
    config_path.write_text(
        yaml.safe_dump(config, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return config_path


def _orders_document() -> dict:
    return {
        "metadata_version": "3.0",
        "object_info": {
            "database": "store_db",
            "schema_name": "public",
            "object_name": "orders",
            "object_type": "table",
            "comment": "订单表",
            "comment_source": "ddl",
            "total_rows": 2,
        },
        "column_profiles": {
            "order_id": {
                "ordinal_position": 1,
                "data_type": "integer",
                "is_nullable": False,
                "column_default": None,
                "comment": "订单ID",
                "comment_source": "ddl",
                "semantic_analysis": {
                    "semantic_role": "identifier",
                    "semantic_confidence": 0.9,
                    "inference_basis": [],
                },
            },
            "amount": {
                "ordinal_position": 2,
                "data_type": "numeric(10,2)",
                "is_nullable": True,
                "column_default": None,
                "comment": "订单金额",
                "comment_source": "llm",
                "semantic_analysis": {
                    "semantic_role": "metric",
                    "semantic_confidence": 0.9,
                    "inference_basis": [],
                },
            },
        },
        "table_profile": {
            "table_category": "fact",
            "confidence": 0.9,
            "inference_basis": [],
            "classification_source": "rule",
            "physical_constraints": {
                "primary_key": {
                    "constraint_name": "orders_pkey",
                    "columns": ["order_id"],
                },
                "foreign_keys": [],
                "unique_constraints": [],
            },
            "indexes": [],
        },
        "sample_records": {
            "sample_method": "limit",
            "records": [{"order_id": 1, "amount": 18.0}],
        },
    }


def _write_orders_json(json_dir: Path, document: dict | None = None) -> None:
    json_dir.mkdir(parents=True, exist_ok=True)
    (json_dir / "store_db.public.orders.json").write_text(
        json.dumps(document or _orders_document(), ensure_ascii=False),
        encoding="utf-8",
    )


def test_md_step_renders_from_json(tmp_path) -> None:
    """正向：JSON → md 的标题/字段顺序/精度说明/主键全部复现（验收 1/8）。"""
    config_path = _write_config(tmp_path)
    _write_orders_json(tmp_path / "json")

    result = MetadataGenerator(config_path).generate(step="md", max_workers=1)

    assert result.success is True
    assert result.processed_tables == 1
    markdown = (tmp_path / "md" / "store_db.public.orders.md").read_text(
        encoding="utf-8"
    )
    assert markdown.startswith("# public.orders [table]（订单表）")
    # 列顺序按 ordinal_position 还原：order_id 在 amount 之前
    assert markdown.index("- order_id (integer)") < markdown.index(
        "- amount (numeric(10,2))"
    )
    assert "- amount (numeric(10,2)) - 订单金额 [示例: 18.0]" in markdown
    assert "- order_id (integer) - 订单ID [示例: 1]" in markdown
    # D2：精度说明行复现，且类型不重复追加修饰符
    assert "精确到小数点后2位" in markdown
    assert "numeric(10,2)(10,2)" not in markdown
    assert "- 主键约束 orders_pkey: order_id" in markdown


def test_md_step_fails_at_enumeration_for_invalid_json(tmp_path) -> None:
    """负向：v2 / 缺 object_info / 根节点非对象在枚举阶段使整步失败并报文件名。"""
    config_path = _write_config(tmp_path)
    json_dir = tmp_path / "json"
    json_dir.mkdir(parents=True)

    broken_inputs = {
        "store_db.public.v2.json": json.dumps({"metadata_version": "2.0"}),
        "store_db.public.missing.json": json.dumps(
            {"metadata_version": "3.0", "column_profiles": {}, "table_profile": None}
        ),
        "store_db.public.notobj.json": "[1, 2]",
    }
    for filename, content in broken_inputs.items():
        (json_dir / filename).write_text(content, encoding="utf-8")

    result = MetadataGenerator(config_path).generate(step="md", max_workers=1)

    # fast-fail：第一个坏文件（字典序）即中断整步并点名；不按空值渲染 md
    assert result.success is False
    assert result.processed_tables == 0
    assert any(
        filename in error for error in result.errors for filename in broken_inputs
    ), result.errors
    assert list((tmp_path / "md").glob("*.md")) == []


def test_md_cli_precheck_fails_without_json(tmp_path) -> None:
    """验收 3：JSON 目录缺失时 CLI 前置检查失败，文案指向 --step json，无 DDL 回退。"""
    config_path = _write_config(tmp_path)

    result = CliRunner().invoke(
        metadata_cli.metadata_command,
        ["--config", str(config_path), "--step", "md"],
    )

    assert result.exit_code != 0
    assert "依赖 JSON 产物" in result.output
    assert "--step json" in result.output


def test_md_cli_precheck_rejects_other_database_json(tmp_path) -> None:
    """验收 3：只有其他库的 JSON 不构成当前库输入。"""
    config_path = _write_config(tmp_path)
    json_dir = tmp_path / "json"
    json_dir.mkdir(parents=True)
    document = _orders_document()
    document["object_info"]["database"] = "other_db"
    (json_dir / "other_db.public.orders.json").write_text(
        json.dumps(document, ensure_ascii=False), encoding="utf-8"
    )

    result = CliRunner().invoke(
        metadata_cli.metadata_command,
        ["--config", str(config_path), "--step", "md"],
    )

    assert result.exit_code != 0
    assert "当前数据库 store_db" in result.output


@pytest.mark.parametrize("separator", ["\n", "\r\n", "\r", "\u2028", "\x85"])
def test_md_step_folds_multiline_comments(tmp_path, separator: str) -> None:
    """表/列注释按 MDParser 的行分隔规则折叠，避免后半段被截断。"""
    config_path = _write_config(tmp_path)
    document = _orders_document()
    document["object_info"]["comment"] = f"订单{separator}主表"
    document["column_profiles"]["order_id"]["comment"] = f"第一行{separator}第二行"
    _write_orders_json(tmp_path / "json", document)

    result = MetadataGenerator(config_path).generate(step="md", max_workers=1)

    assert result.success is True
    markdown = (tmp_path / "md" / "store_db.public.orders.md").read_text(
        encoding="utf-8"
    )
    assert markdown.startswith("# public.orders [table]（订单 主表）")
    assert "- order_id (integer) - 第一行 第二行 [示例: 1]" in markdown
    # 折叠后整份 md 不因注释换行拆行：标题行是完整单行
    assert "# public.orders [table]（订单 主表）\n" in markdown
    assert (
        MDParser.from_string(markdown).get_column_descriptions()["order_id"]
        == "第一行 第二行"
    )


def test_json_loader_rejects_identity_mismatch(tmp_path) -> None:
    """身份三方核对：object_info 与文件名不一致时枚举抛错并报文件名。"""
    json_dir = tmp_path / "json"
    json_dir.mkdir(parents=True)
    document = _orders_document()
    document["object_info"]["object_name"] = "other"
    (json_dir / "store_db.public.orders.json").write_text(
        json.dumps(document, ensure_ascii=False), encoding="utf-8"
    )

    with pytest.raises(JsonLoaderError, match="store_db.public.orders.json"):
        JsonLoader(json_dir, database_name="store_db").list_objects("public")


@pytest.mark.parametrize(
    "filename,include_valid",
    [
        ("store_db.public.json", False),
        ("store_db.public.bad.name.json", False),
        ("store_db.public.bad.name.json", True),
    ],
)
def test_md_rejects_misnamed_current_database_json(
    tmp_path, filename: str, include_valid: bool
) -> None:
    """两段/四段文件名均须点名报错，混有合法文件时也不能忽略。"""
    config_path = _write_config(tmp_path)
    json_dir = tmp_path / "json"
    json_dir.mkdir()
    (json_dir / filename).write_text("{}", encoding="utf-8")
    if include_valid:
        _write_orders_json(json_dir)

    result = CliRunner().invoke(
        metadata_cli.metadata_command,
        ["--config", str(config_path), "--step", "md"],
    )

    assert result.exit_code != 0
    assert filename in result.output
    assert "文件名不合规" in result.output
    assert not list((tmp_path / "md").glob("*.md"))

    loader = JsonLoader(json_dir, database_name="store_db")
    with pytest.raises(JsonLoaderError, match=filename):
        loader.list_objects("public")


@pytest.mark.parametrize("invalid_records", ["not-a-list", [{"order_id": 1}, 42]])
def test_md_rejects_invalid_sample_records_per_object(
    tmp_path, invalid_records
) -> None:
    """样例格式损坏应使该对象失败，其他有效对象继续生成。"""
    config_path = _write_config(tmp_path)
    json_dir = tmp_path / "json"
    _write_orders_json(json_dir)
    broken = _orders_document()
    broken["object_info"]["object_name"] = "broken"
    broken["sample_records"]["records"] = invalid_records
    (json_dir / "store_db.public.broken.json").write_text(
        json.dumps(broken, ensure_ascii=False), encoding="utf-8"
    )

    result = MetadataGenerator(config_path).generate(step="md", max_workers=1)

    assert result.success is False
    assert result.processed_tables == 1
    assert result.failed_tables == 1
    assert any(
        "public.broken" in error and "sample_records.records" in error
        for error in result.errors
    )
    assert (tmp_path / "md" / "store_db.public.orders.md").exists()
    assert not (tmp_path / "md" / "store_db.public.broken.md").exists()


@pytest.mark.parametrize(
    "invalid_constraints",
    [
        ["invalid"],
        {"primary_key": ["order_id"]},
        {"foreign_keys": ["invalid"]},
        {"unique_constraints": [1]},
    ],
    ids=["constraints-list", "primary-key-list", "foreign-key-string", "unique-int"],
)
def test_md_wraps_invalid_constraints_per_object(tmp_path, invalid_constraints) -> None:
    """约束形状错误应报告对象映射失败，不中断其余对象。"""
    config_path = _write_config(tmp_path)
    json_dir = tmp_path / "json"
    _write_orders_json(json_dir)
    broken = _orders_document()
    broken["object_info"]["object_name"] = "broken"
    broken["table_profile"]["physical_constraints"] = invalid_constraints
    (json_dir / "store_db.public.broken.json").write_text(
        json.dumps(broken, ensure_ascii=False), encoding="utf-8"
    )

    result = MetadataGenerator(config_path).generate(step="md", max_workers=1)

    assert result.success is False
    assert result.processed_tables == 1
    assert result.failed_tables == 1
    assert any(
        "JSON 映射失败 (public.broken): AttributeError" in error
        for error in result.errors
    ), result.errors
    assert (tmp_path / "md" / "store_db.public.orders.md").exists()
    assert not (tmp_path / "md" / "store_db.public.broken.md").exists()


def test_json_loader_rejects_missing_object_type(tmp_path) -> None:
    """对象类型缺失不能静默当作 table，枚举与直接加载均应明确报错。"""
    json_dir = tmp_path / "json"
    document = _orders_document()
    del document["object_info"]["object_type"]
    _write_orders_json(json_dir, document)
    loader = JsonLoader(json_dir, database_name="store_db")

    with pytest.raises(JsonLoaderError, match="object_info.object_type") as exc_info:
        loader.list_objects("public")
    assert "store_db.public.orders.json" in str(exc_info.value)

    with pytest.raises(JsonLoaderError, match="object_info.object_type") as exc_info:
        loader.load_object("public", "orders")
    assert "public.orders" in str(exc_info.value)


def test_json_loader_uses_enumerated_snapshot(tmp_path) -> None:
    """枚举完成后输入文件变化不影响本次加载，符合 doc 17 的快照口径。"""
    json_dir = tmp_path / "json"
    _write_orders_json(json_dir)
    loader = JsonLoader(json_dir, database_name="store_db")
    assert [obj.object_name for obj in loader.list_objects("public")] == ["orders"]

    (json_dir / "store_db.public.orders.json").unlink()

    metadata = loader.load_object("public", "orders")
    assert metadata.table_name == "orders"
    assert metadata.comment == "订单表"

    with pytest.raises(JsonLoaderError, match="JSON 文件不存在"):
        JsonLoader(json_dir, database_name="store_db").load_object("public", "orders")
