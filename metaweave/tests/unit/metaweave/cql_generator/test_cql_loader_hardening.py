"""cql_loader 加固用例（doc 23.2 §5.5：A23/A24/A26/A28）。

- 解析前置 + 必需章节/统计头核验（A23）：截断/缺章节/缺统计头在 clean 前
  抛 ``CQLScriptValidationError``，图未动（clean 未执行）；
- 数量核验置败（A24）：图内计数与统计头不符 → ``success=False`` +
  ``stats_mismatch`` + message；三项一致（含零关系）→ 照常成功；
  ``_get_graph_stats`` 查询用 ``OPTIONAL MATCH``（零 JOIN_ON 不再全零兜底）；
- ``validate_after_load=False`` 跳过核验（A28 的 loader 侧门控语义——发布
  侧强制 True 在批次 3 验收）；
- ``close()``（A26）：调用底层 manager.close 并置 None，幂等、None 安全。
"""

from unittest.mock import MagicMock

import pytest

from metaweave.core.loaders.cql_loader import CQLLoader, CQLScriptValidationError

SCRIPT = """// import_all.orders.cypher
// Neo4j 元数据导入脚本（global 模式，包含所有表和关系）
// 生成时间: 2026-10-10T00:00:00Z
// 统计: {tables} 张表, {columns} 个列, {relationships} 个关系

// =====================================================================
// 1. 创建唯一约束
// =====================================================================

	CREATE CONSTRAINT table_id IF NOT EXISTS FOR (t:Table) REQUIRE t.id IS UNIQUE;

// =====================================================================
// 2. 创建 Table 节点
// =====================================================================

	UNWIND $tables AS t
	MERGE (n:Table {{full_name: t.full_name}})
	SET n.id = t.id;

// =====================================================================
// 3. 创建 Column 节点
// =====================================================================

	UNWIND $columns AS c
	MERGE (n:Column {{full_name: c.full_name}})
	SET n.id = c.id;

// =====================================================================
// 4. 创建 HAS_COLUMN 关系
// =====================================================================

	UNWIND $has_column AS r
	MATCH (t:Table {{full_name: r.src_full_name}})
	SET t.hc = 1;

// =====================================================================
// 5. 创建 JOIN_ON 关系
// =====================================================================

	UNWIND $join_on AS r
	MATCH (t:Table {{full_name: r.src_full_name}})
	SET t.jo = 1;
"""


def _write_script(tmp_path, tables=1, columns=1, relationships=0, body=None):
    script = tmp_path / "import_all.orders.cypher"
    if body is None:
        body = SCRIPT.format(
            tables=tables, columns=columns, relationships=relationships
        )
    script.write_text(body, encoding="utf-8")
    return script


def _loader(tmp_path, script, options=None):
    loader = CQLLoader(
        {
            "cql_loader": {
                "input_file": str(script),
                "neo4j": {
                    "uri": "bolt://localhost:7687",
                    "user": "neo4j",
                    "password": "secret",
                    "database": "neo4j",
                },
                "options": options or {},
            }
        }
    )
    loader.neo4j_manager = MagicMock()
    return loader


def test_missing_sections_rejected_before_clean(tmp_path):
    """A23：缺 Table 章节的脚本在 clean 前抛 CQLScriptValidationError。"""
    body = SCRIPT.format(tables=1, columns=1, relationships=0).replace(
        "// 2. 创建 Table 节点", "// 2. 创建 X 节点"
    )
    loader = _loader(tmp_path, _write_script(tmp_path, body=body))

    with pytest.raises(CQLScriptValidationError):
        loader.load(clean=True)

    loader.neo4j_manager.execute_write_transaction.assert_not_called()


def test_truncated_script_rejected_before_clean(tmp_path):
    """A23：零章节（截断）脚本同样在 clean 前拒绝。"""
    loader = _loader(
        tmp_path, _write_script(tmp_path, body="// 统计: 1 张表, 1 个列, 0 个关系\n")
    )

    with pytest.raises(CQLScriptValidationError):
        loader.load(clean=True)

    loader.neo4j_manager.execute_write_transaction.assert_not_called()


def test_missing_stats_header_rejected(tmp_path):
    """A23：统计头缺失（fail closed）在 clean 前拒绝。"""
    body = SCRIPT.format(tables=1, columns=1, relationships=0).replace(
        "// 统计: 1 张表, 1 个列, 0 个关系\n", ""
    )
    loader = _loader(tmp_path, _write_script(tmp_path, body=body))

    with pytest.raises(CQLScriptValidationError):
        loader.load(clean=True)


def test_stats_mismatch_fails_load(tmp_path):
    """A24：图内计数与统计头不符 → success=False + stats_mismatch + message。"""
    loader = _loader(tmp_path, _write_script(tmp_path))
    loader.neo4j_manager.execute_query.side_effect = [
        [{"table_count": 0, "column_count": 1, "relationship_count": 0}],
        [{"has_column_count": 1}],
    ]

    result = loader.load(clean=True)

    assert result["success"] is False
    mismatches = {m["item"]: m for m in result["stats_mismatch"]}
    assert mismatches["table_count"] == {
        "item": "table_count",
        "expected": 1,
        "actual": 0,
    }
    assert "图内数量与脚本统计不符" in result["message"]
    assert "table_count 期望 1/实际 0" in result["message"]


def test_zero_relationship_consistent_succeeds(tmp_path):
    """A24/D10：有表有列零关系且四项一致（含 HAS_COLUMN=列数）→ 照常成功；
    主查询用 OPTIONAL MATCH（零 JOIN_ON 不再全零兜底）。"""
    loader = _loader(tmp_path, _write_script(tmp_path))
    loader.neo4j_manager.execute_query.side_effect = [
        [{"table_count": 1, "column_count": 1, "relationship_count": 0}],
        [{"has_column_count": 1}],
    ]

    result = loader.load(clean=True)

    assert result["success"] is True
    assert not result.get("stats_mismatch")
    main_query = loader.neo4j_manager.execute_query.call_args_list[0][0][0]
    assert "OPTIONAL MATCH" in main_query
    assert "MATCH ()-[r:JOIN_ON]->()" not in main_query.replace("OPTIONAL MATCH", "")


def test_validate_after_load_false_skips_validation(tmp_path):
    """A28（loader 侧门控）：options 显式 false 时跳过数量核验。"""
    loader = _loader(
        tmp_path,
        _write_script(tmp_path),
        options={"validate_after_load": False},
    )
    loader.neo4j_manager.execute_query.return_value = [
        {"table_count": 0, "column_count": 0, "relationship_count": 0}
    ]

    result = loader.load(clean=True)

    assert result["success"] is True
    loader.neo4j_manager.execute_query.assert_not_called()


def test_close_closes_manager_once_and_is_idempotent(tmp_path):
    """A26：close 调 manager.close 并置 None；重复 close / 未建连均安全。"""
    loader = _loader(tmp_path, _write_script(tmp_path))
    manager = loader.neo4j_manager

    loader.close()
    manager.close.assert_called_once()
    assert loader.neo4j_manager is None

    loader.close()  # 幂等：manager 已置 None，不重复调用
    manager.close.assert_called_once()

    fresh = CQLLoader(
        {
            "cql_loader": {
                "input_file": str(_write_script(tmp_path)),
                "neo4j": {
                    "uri": "bolt://localhost:7687",
                    "user": "neo4j",
                    "password": "secret",
                },
            }
        }
    )
    fresh.close()  # 未 validate 过：manager 为 None，安全无操作


def test_truncated_after_column_rejected_before_clean(tmp_path):
    """A23 强化：Column 章节后截断（缺 HAS_COLUMN/JOIN_ON）→ clean 前拒绝。

    该截断位下表/列/零 JOIN_ON 计数本可全匹配——若无四章节核验会「成功」
    加载出一个没有任何表-列边的图。"""
    body = SCRIPT.format(tables=1, columns=1, relationships=0)
    marker = "// " + "=" * 69 + "\n// 4."
    cut = body[: body.index(marker)]
    loader = _loader(tmp_path, _write_script(tmp_path, body=cut))

    with pytest.raises(CQLScriptValidationError, match="HAS_COLUMN"):
        loader.load(clean=True)

    loader.neo4j_manager.execute_write_transaction.assert_not_called()


def test_missing_join_on_section_rejected(tmp_path):
    """A23/D10：仅缺 JOIN_ON 章节也拒绝（零关系时该章节为 UNWIND [] 恒存在）。"""
    body = SCRIPT.format(tables=1, columns=1, relationships=0)
    start = body.index("// 5. 创建 JOIN_ON 关系")
    head = body[
        : body.rindex(
            "// =====================================================================",
            0,
            start,
        )
    ]
    loader = _loader(tmp_path, _write_script(tmp_path, body=head.rstrip() + "\n"))

    with pytest.raises(CQLScriptValidationError, match="JOIN_ON"):
        loader.load(clean=True)


def test_has_column_zero_fails_post_load_validation(tmp_path):
    """A24 强化：表/列数量正确但 HAS_COLUMN 边数为零 → 置败（期望=列数）。"""
    loader = _loader(tmp_path, _write_script(tmp_path))
    loader.neo4j_manager.execute_query.side_effect = [
        [{"table_count": 1, "column_count": 1, "relationship_count": 0}],
        [{"has_column_count": 0}],
    ]

    result = loader.load(clean=True)

    assert result["success"] is False
    mismatches = {m["item"]: m for m in result["stats_mismatch"]}
    assert mismatches["has_column_count"] == {
        "item": "has_column_count",
        "expected": 1,
        "actual": 0,
    }
    assert "has_column_count 期望 1/实际 0" in result["message"]
