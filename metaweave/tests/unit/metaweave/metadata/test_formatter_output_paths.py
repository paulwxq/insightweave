"""测试 OutputFormatter 的输出路径配置功能。"""

from pathlib import Path
import pytest
import yaml
from metaweave.core.metadata.formatter import OutputFormatter
from metaweave.core.metadata.models import TableMetadata, ColumnInfo

@pytest.fixture
def sample_metadata():
    return TableMetadata(
        schema_name="public",
        table_name="test_table",
        columns=[
            ColumnInfo(column_name="id", ordinal_position=1, data_type="integer")
        ]
    )

def test_formatter_uses_default_paths(tmp_path, sample_metadata):
    """测试未指定显式目录时使用默认子目录。"""
    config = {
        "output_dir": str(tmp_path / "output")
    }
    formatter = OutputFormatter(config, database_name="test_db")
    
    # 验证属性
    assert formatter.ddl_dir == tmp_path / "output" / "ddl"
    assert formatter.json_dir == tmp_path / "output" / "json"
    assert formatter.markdown_dir == tmp_path / "output" / "md"
    
    # 验证目录是否创建
    assert formatter.ddl_dir.exists()
    assert formatter.json_dir.exists()
    assert formatter.markdown_dir.exists()
    
    # 验证保存路径
    paths = formatter.format_and_save(sample_metadata)
    assert Path(paths["ddl"]).parent == formatter.ddl_dir
    assert Path(paths["json"]).parent == formatter.json_dir
    assert Path(paths["markdown"]).parent == formatter.markdown_dir

def test_formatter_uses_custom_paths(tmp_path, sample_metadata):
    """测试使用显式的 ddl_directory, json_directory, markdown_directory。"""
    custom_ddl = tmp_path / "custom_ddl"
    custom_json = tmp_path / "custom_json"
    custom_md = tmp_path / "custom_md"
    
    config = {
        "output_dir": str(tmp_path / "output"),
        "ddl_directory": str(custom_ddl),
        "json_directory": str(custom_json),
        "markdown_directory": str(custom_md)
    }
    formatter = OutputFormatter(config, database_name="test_db")
    
    # 验证属性
    assert formatter.ddl_dir == custom_ddl
    assert formatter.json_dir == custom_json
    assert formatter.markdown_dir == custom_md
    
    # 验证目录是否创建
    assert custom_ddl.exists()
    assert custom_json.exists()
    assert custom_md.exists()
    
    # 验证保存路径
    paths = formatter.format_and_save(sample_metadata)
    assert Path(paths["ddl"]).parent == custom_ddl
    assert Path(paths["json"]).parent == custom_json
    assert Path(paths["markdown"]).parent == custom_md

def test_formatter_handles_relative_custom_paths(tmp_path, sample_metadata, monkeypatch):
    """相对路径统一基于项目根目录解析（doc 26 §6.2），与 cwd 无关。"""
    # 项目根指向 tmp_path；chdir 到另一子目录反证 cwd 不再参与解析
    monkeypatch.setattr(
        "metaweave.utils.file_utils.get_project_root", lambda: tmp_path
    )
    other_dir = tmp_path / "subdir"
    other_dir.mkdir()
    monkeypatch.chdir(other_dir)  # 反证 cwd 不再参与解析

    config = {
        "output_dir": "output",
        "ddl_directory": "my_ddls",
        "json_directory": "my_jsons",
        "markdown_directory": "my_mds"
    }
    formatter = OutputFormatter(config, database_name="test_db")

    assert formatter.ddl_dir == tmp_path / "my_ddls"
    assert formatter.json_dir == tmp_path / "my_jsons"
    assert formatter.markdown_dir == tmp_path / "my_mds"
    
    assert (tmp_path / "my_ddls").exists()
    assert (tmp_path / "my_jsons").exists()
    assert (tmp_path / "my_mds").exists()


def test_save_ddl_returns_none_when_write_fails(tmp_path, sample_metadata, monkeypatch):
    """save_text 写入失败只返回 False：_save_ddl 须转成 None，不谎报路径。"""
    config = {"output_dir": str(tmp_path / "output")}
    formatter = OutputFormatter(config, database_name="test_db")
    monkeypatch.setattr(
        "metaweave.core.metadata.formatter.save_text", lambda *_a, **_k: False
    )
    assert formatter._save_ddl(sample_metadata) is None
    # 聚合层不把未落盘的输出计入 output_files（doc 16 §4 以实际输出为准）
    paths = formatter.format_and_save(sample_metadata)
    assert "ddl" not in paths


def test_save_ddl_rejects_filename_with_path_separator(tmp_path):
    """对象名含路径分隔符：写盘前拒绝，不在受管 DDL 目录外产生任何文件。"""
    config = {"output_dir": str(tmp_path / "output")}
    formatter = OutputFormatter(config, database_name="test_db")
    escapee = TableMetadata(
        schema_name="public",
        table_name="../escape",
        columns=[ColumnInfo(column_name="id", ordinal_position=1, data_type="integer")],
    )
    assert formatter._save_ddl(escapee) is None
    # 不越界、也不在目录内留下部分输出（save_text 的 ensure_dir 不被触达）。
    assert not (tmp_path / "escape.sql").exists()
    assert list(formatter.ddl_dir.iterdir()) == []
    paths = formatter.format_and_save(escapee)
    assert "ddl" not in paths


def test_save_markdown_returns_none_when_write_fails(
    tmp_path, sample_metadata, monkeypatch
):
    """save_text 写入失败只返回 False：_save_markdown 须转成 None，不谎报路径。"""
    config = {"output_dir": str(tmp_path / "output")}
    formatter = OutputFormatter(config, database_name="test_db")
    monkeypatch.setattr(
        "metaweave.core.metadata.formatter.save_text", lambda *_a, **_k: False
    )
    assert formatter._save_markdown(sample_metadata) is None
    paths = formatter.format_and_save(sample_metadata, formats_override=["markdown"])
    assert "markdown" not in paths


def test_save_markdown_rejects_filename_with_path_separator(tmp_path):
    """对象名含路径分隔符：Markdown 与 DDL 同一守卫，不越出 md 目录。"""
    config = {"output_dir": str(tmp_path / "output")}
    formatter = OutputFormatter(config, database_name="test_db")
    escapee = TableMetadata(
        schema_name="public",
        table_name="../escape",
        columns=[ColumnInfo(column_name="id", ordinal_position=1, data_type="integer")],
    )
    assert formatter._save_markdown(escapee) is None
    assert not (tmp_path / "escape.md").exists()
    assert list(formatter.markdown_dir.iterdir()) == []
    paths = formatter.format_and_save(escapee, formats_override=["markdown"])
    assert "markdown" not in paths


def test_json_output_path_rejects_filename_with_path_separator(tmp_path):
    """对象名含路径分隔符：json_output_path 抛错，兼容入口转 None 不落盘。"""
    config = {"output_dir": str(tmp_path / "output")}
    formatter = OutputFormatter(config, database_name="test_db")
    escapee = TableMetadata(
        schema_name="public",
        table_name="../escape",
        columns=[ColumnInfo(column_name="id", ordinal_position=1, data_type="integer")],
    )
    with pytest.raises(ValueError, match="路径分隔符"):
        formatter.json_output_path(escapee)
    assert formatter._save_json(escapee) is None
    assert not (tmp_path / "escape.json").exists()
    assert list(formatter.json_dir.iterdir()) == []
