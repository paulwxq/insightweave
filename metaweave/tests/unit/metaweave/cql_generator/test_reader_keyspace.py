"""reader 键空间整源校验测试（doc 4 §4.2）

画像 JSON 含不支持键空间的对象时，read_all 在返回前拒绝整批——
不静默跳过个别对象、不改写名称。
"""

import json

import pytest

from metaweave.core.cql_generator.reader import JSONReader


def _profile(db="orders", schema="public", table="orders", columns=("id", "amount")):
    return {
        "object_info": {
            "database": db,
            "schema_name": schema,
            "object_name": table,
            "object_type": "table",
            "comment": "",
        },
        "table_profile": {
            "physical_constraints": {},
            "indexes": [],
            "unique_column_sets": [],
            "table_category": None,
        },
        "column_profiles": {
            col: {"data_type": "bigint", "comment": "", "statistics": {}}
            for col in columns
        },
    }


def _workspace(tmp_path, profiles, rels=None):
    json_dir = tmp_path / "json"
    rel_dir = tmp_path / "rel"
    json_dir.mkdir(parents=True)
    rel_dir.mkdir(parents=True)
    for name, data in profiles.items():
        (json_dir / name).write_text(
            json.dumps(data, ensure_ascii=False), encoding="utf-8"
        )
    (rel_dir / "orders.relationships_global.json").write_text(
        json.dumps({"relationships": rels or [], "statistics": {}}),
        encoding="utf-8",
    )
    return json_dir, rel_dir


def test_valid_profiles_pass(tmp_path):
    json_dir, rel_dir = _workspace(tmp_path, {"orders.json": _profile()})
    reader = JSONReader(json_dir, rel_dir)
    tables, columns, _, _, _ = reader.read_all()
    assert len(tables) == 1
    assert len(columns) == 2


def test_hyphenated_table_name_rejects_whole_batch(tmp_path):
    json_dir, rel_dir = _workspace(
        tmp_path,
        {
            "orders.json": _profile(),
            "bad.json": _profile(table="my-orders"),
        },
    )
    reader = JSONReader(json_dir, rel_dir)
    with pytest.raises(ValueError, match="整体拒绝"):
        reader.read_all()


def test_dotted_db_name_rejects(tmp_path):
    json_dir, rel_dir = _workspace(tmp_path, {"orders.json": _profile(db="my.orders")})
    reader = JSONReader(json_dir, rel_dir)
    with pytest.raises(ValueError, match="库名前缀"):
        reader.read_all()


def test_leading_digit_column_rejects_with_name(tmp_path):
    json_dir, rel_dir = _workspace(
        tmp_path, {"orders.json": _profile(columns=("1bad",))}
    )
    reader = JSONReader(json_dir, rel_dir)
    with pytest.raises(ValueError, match="1bad"):
        reader.read_all()


def test_missing_database_rejects(tmp_path):
    """无 database 的画像无法构造三段键——明确拒绝。"""
    json_dir, rel_dir = _workspace(tmp_path, {"orders.json": _profile(db=None)})
    reader = JSONReader(json_dir, rel_dir)
    with pytest.raises(ValueError, match="库名前缀"):
        reader.read_all()
