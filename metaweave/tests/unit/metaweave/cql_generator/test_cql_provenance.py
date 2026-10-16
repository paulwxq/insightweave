"""cql writer provenance 注入用例（doc 23.2 §5.4，A13/D3）。

- 传 ``provenance=(source_id, publish_id)``：import_all 的 Table 节点 SET 含
  ``n.source_id/n.publish_id``，tables_json 数据行带两字段；Column/
  HAS_COLUMN/JOIN_ON 章节不含（出现次数钉死为仅 Table 章节）；
- 缺省 ``None``：全部不含（CLI ``--step cql`` 行为不变，D9）；
- ``CQLGenerator`` 同名参数透传给 writer（stub 断言）。
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

from metaweave.core.cql_generator.generator import CQLGenerator
from metaweave.core.cql_generator.models import TableNode
from metaweave.core.cql_generator.reader import RelationshipFilterStats
from metaweave.core.cql_generator.writer import CypherWriter

SOURCE_ID = "0f0e8d64-9d3a-4d0f-8d5f-2a3b4c5d6e7f"
PUBLISH_ID = "1a2b3c4d-5e6f-4a5b-8c9d-0e1f2a3b4c5d"


@pytest.fixture
def temp_config_file(tmp_path):
    config = {
        "database": {
            "host": "localhost",
            "port": 5432,
            "database": "orders",
            "user": "test_user",
            "password": "test_pass",
            "schemas": ["public"],
        },
        "output": {
            "json_directory": str(tmp_path / "json"),
            "rel_directory": str(tmp_path / "rel"),
            "cql_directory": str(tmp_path / "cql"),
        },
    }
    config_path = tmp_path / "test_config.yaml"
    with open(config_path, "w", encoding="utf-8") as f:
        yaml.dump(config, f)
    return config_path


def _table():
    return TableNode(
        full_name="public.users",
        schema="public",
        name="users",
        database="orders",
    )


def test_write_all_with_provenance_injects_table_attrs(tmp_path):
    """A13：provenance 注入 Table 节点 SET 与数据行，且仅 Table 章节。"""
    writer = CypherWriter(tmp_path / "cql")
    files = writer.write_all([_table()], [], [], [], provenance=(SOURCE_ID, PUBLISH_ID))
    assert [Path(f).name for f in files] == ["import_all.orders.cypher"]
    content = Path(files[0]).read_text(encoding="utf-8")

    assert "n.source_id = t.source_id" in content
    assert "n.publish_id = t.publish_id" in content
    assert f'"source_id": "{SOURCE_ID}"' in content
    assert f'"publish_id": "{PUBLISH_ID}"' in content
    # 仅 Table 章节：数据行 1 次 + SET 行 2 次，再多即泄入其它章节
    assert content.count("source_id") == 3
    assert content.count("publish_id") == 3


def test_write_all_without_provenance_omits_attrs(tmp_path):
    """D9：缺省 None 时属性全部省略，CLI 生成行为不变。"""
    writer = CypherWriter(tmp_path / "cql")
    files = writer.write_all([_table()], [], [], [])
    content = Path(files[0]).read_text(encoding="utf-8")

    assert "source_id" not in content
    assert "publish_id" not in content


@patch("metaweave.core.cql_generator.generator.JSONReader")
@patch("metaweave.core.cql_generator.generator.CypherWriter")
def test_generator_passes_provenance_to_writer(
    mock_writer_class, mock_reader_class, temp_config_file
):
    """generator 同名参数透传：write_all 收到 provenance 元组。"""
    mock_reader = MagicMock()
    mock_reader.read_all.return_value = ([], [], [], [], RelationshipFilterStats())
    mock_reader_class.return_value = mock_reader

    mock_writer = MagicMock()
    mock_writer.write_all.return_value = ["/tmp/import_all.cypher"]
    mock_writer.write_metadata.return_value = Path("/tmp/import_all.md")
    mock_writer_class.return_value = mock_writer

    generator = CQLGenerator(temp_config_file, provenance=(SOURCE_ID, PUBLISH_ID))
    result = generator.generate()

    assert result.success is True
    _, kwargs = mock_writer.write_all.call_args
    assert kwargs.get("provenance") == (SOURCE_ID, PUBLISH_ID)


@patch("metaweave.core.cql_generator.generator.JSONReader")
@patch("metaweave.core.cql_generator.generator.CypherWriter")
def test_generator_default_provenance_is_none(
    mock_writer_class, mock_reader_class, temp_config_file
):
    """缺省构造：write_all 收到 provenance=None。"""
    mock_reader = MagicMock()
    mock_reader.read_all.return_value = ([], [], [], [], RelationshipFilterStats())
    mock_reader_class.return_value = mock_reader

    mock_writer = MagicMock()
    mock_writer.write_all.return_value = ["/tmp/import_all.cypher"]
    mock_writer.write_metadata.return_value = Path("/tmp/import_all.md")
    mock_writer_class.return_value = mock_writer

    CQLGenerator(temp_config_file).generate()

    _, kwargs = mock_writer.write_all.call_args
    assert kwargs.get("provenance") is None
