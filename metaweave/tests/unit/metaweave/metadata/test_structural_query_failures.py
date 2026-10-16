"""结构查询失败传播：空集合只表示查询成功且无对象（doc 16 §4）。

受管 DDL 的对象枚举与结构提取查询失败时必须上抛，由上层按失败计数；
否则多 schema 采集时现存 schema 的产物会掩盖缺失整个 schema 的不完整
结果，发布则整批替换当前表定义。异常日志只记对象身份与错误类别，
不记异常原文。
"""

import pytest

from metaweave.core.metadata.connector import DatabaseConnector
from metaweave.core.metadata.extractor import MetadataExtractor
from metaweave.core.metadata.generator import MetadataGenerator

SECRET = "SQL_SECRET must stay out of logs"


def _failing_connector(monkeypatch) -> DatabaseConnector:
    """绕过建池的 __init__，execute_query 一律抛带敏感文本的异常。"""
    connector = DatabaseConnector.__new__(DatabaseConnector)

    def failing_query(*_args, **_kwargs):
        raise RuntimeError(f"查询失败: {SECRET}")

    monkeypatch.setattr(connector, "execute_query", failing_query)
    return connector


# ---------- 对象枚举：失败上抛，空 schema 仍是正常空集 ----------


def test_get_database_objects_failure_raises(monkeypatch, caplog):
    """schema 枚举查询失败必须上抛：返回空列表会被当作"schema 无对象"。"""
    connector = _failing_connector(monkeypatch)
    with pytest.raises(RuntimeError):
        connector.get_database_objects("public", ["table"])
    assert "获取数据库对象失败" in caplog.text
    assert "public" in caplog.text
    assert SECRET not in caplog.text


def test_get_database_objects_empty_schema_is_not_error(monkeypatch):
    """查询成功且无对象：空列表是正常语义，不得误判为失败。"""
    connector = DatabaseConnector.__new__(DatabaseConnector)
    monkeypatch.setattr(connector, "execute_query", lambda *_a, **_k: [])
    assert connector.get_database_objects("public", ["table"]) == []


def test_check_database_object_exists_failure_raises(monkeypatch, caplog):
    """存在性查询失败不能误报成"对象不存在"（会被当作并发删除跳过）。"""
    connector = _failing_connector(monkeypatch)
    with pytest.raises(RuntimeError):
        connector.check_database_object_exists("public", "users", "table")
    assert "检查数据库对象失败" in caplog.text
    assert SECRET not in caplog.text


def test_object_enumeration_failure_propagates_to_generator(monkeypatch):
    """generator 不吞枚举异常：上抛到 generate() 外层置 success=False，
    受管核对按"未全部成功"拒绝（doc 16 §4）。"""
    generator = MetadataGenerator.__new__(MetadataGenerator)
    generator.config = {"database": {}}
    generator.active_step = "ddl"
    generator.connector = _failing_connector(monkeypatch)
    with pytest.raises(RuntimeError):
        generator._get_tables_to_process(["public"], None)


# ---------- 结构提取：失败上抛，由 extract_all 统一转为对象失败 ----------


@pytest.mark.parametrize(
    ("method", "args"),
    [
        ("extract_table_info", ("public", "users", "table")),
        ("extract_columns", ("public", "users", "table")),
        ("extract_primary_keys", ("public", "users")),
        ("extract_foreign_keys", ("public", "users")),
        ("extract_unique_constraints", ("public", "users")),
        ("extract_indexes", ("public", "users")),
        ("extract_view_definition", ("public", "v_orders")),
    ],
)
def test_structural_extraction_failure_raises(monkeypatch, caplog, method, args):
    """结构查询失败上抛：空列/空约束会生成不完整 DDL 却仍计入成功对象。"""
    extractor = MetadataExtractor(_failing_connector(monkeypatch))
    with pytest.raises(RuntimeError):
        getattr(extractor, method)(*args)
    assert SECRET not in caplog.text


def test_extract_all_converts_query_failure_to_none(monkeypatch, caplog):
    """对象级统一出口：结构查询异常转为 None，上层按提取失败计数。

    生成器对 None 抛 ValueError 并 failed_tables+1，受管采集因此判
    failed，不完整目录不可发布。
    """
    extractor = MetadataExtractor(_failing_connector(monkeypatch))
    assert extractor.extract_all("public", "users", "table") is None
    assert "提取表元数据失败" in caplog.text
    assert SECRET not in caplog.text
