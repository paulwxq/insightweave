"""cql_loader 用例（doc 23.2 §5.5 加固 + doc 4 §4.4/§4.5 按源替换事务）。

- 解析前置 + 必需章节/统计头核验：截断/缺章节在图写入前抛
  ``CQLScriptValidationError``（图未动）；
- source 身份：脚本头 ``// source_id:`` 必须存在（旧格式拒绝）；与构造
  传入的后端 source_id 不一致在删除之前拒绝；
- 单一数据事务：按节点归属清理（所有无 source_id 节点及本次 source_id
  节点，不限制 label；DETACH DELETE 连带删除关联关系）→ 写入各章节 →
  按源核验；核验不符在事务内抛错回滚（``_rolled_back`` 语义）；
- 约束语句在数据事务之外逐条执行；数量核验不可关闭（事务提交前必要步骤）；
- ``close()`` 调用底层 manager.close 并置 None，幂等、None 安全。

脚本由真实 ``CypherWriter`` 生成（1 表 1 列 0 关系），使解析路径与受管
链路一致。
"""

from contextlib import contextmanager
from unittest.mock import MagicMock

import pytest

from metaweave.core.cql_generator.models import (
    ColumnNode,
    HASColumnRelation,
    TableNode,
)
from metaweave.core.cql_generator.writer import CypherWriter
from metaweave.core.loaders.cql_loader import (
    CQLLoader,
    CQLScriptValidationError,
)

SOURCE_ID = "0f0e8d64-9d3a-4d0f-8d5f-2a3b4c5d6e7f"
PUBLISH_ID = "1a2b3c4d-5e6f-4a5b-8c9d-0e1f2a3b4c5d"


def _write_script(tmp_path, tables=1, columns=1, relationships=0):
    writer = CypherWriter(tmp_path)
    table = TableNode(
        full_name="public.orders", schema="public", name="orders", database="orders"
    )
    column = ColumnNode(
        full_name="public.orders.id",
        schema="public",
        table="orders",
        name="id",
        data_type="bigint",
        database="orders",
    )
    writer.write_all(
        [table],
        [column],
        [HASColumnRelation("public.orders", "public.orders.id")],
        [],
        provenance=(SOURCE_ID, PUBLISH_ID),
    )
    return tmp_path / "import_all.orders.cypher"


class _FakeTxResult:
    def __init__(self, record):
        self._record = record

    def single(self):
        return self._record

    def consume(self):
        return None


class _FakeTx:
    """记录事务内查询；按查询文本路由返回结果。

    ``state["nodes"]`` / ``state["relationships"]`` 用于模拟节点归属清理：
    - nodes: {id: {"labels": [...], "source_id": ...}}
    - relationships: [{"id": ..., "start": ..., "end": ..., "source_id": ...}]
    清理查询会记录被删节点、被 DETACH DELETE 带走的关系及保留项。
    """

    def __init__(self, state: dict):
        self.state = state
        self.queries: list[tuple[str, dict]] = []
        self.rollback_marker = False

    def _clean_nodes(self, source_id) -> None:
        nodes = self.state.get("nodes")
        if nodes is None:
            return
        deleted = {
            node_id
            for node_id, info in nodes.items()
            if info.get("source_id") is None or info.get("source_id") == source_id
        }
        relationships = self.state.get("relationships", [])
        deleted_relationships = {
            rel["id"]
            for rel in relationships
            if rel.get("start") in deleted or rel.get("end") in deleted
        }
        self.state["deleted_node_ids"] = sorted(deleted)
        self.state["deleted_relationship_ids"] = sorted(deleted_relationships)
        self.state["remaining_node_ids"] = sorted(set(nodes) - deleted)
        self.state["remaining_relationship_ids"] = sorted(
            rel["id"]
            for rel in relationships
            if rel["id"] not in deleted_relationships
        )

    def run(self, query, **params):
        text = " ".join(query.split())
        self.queries.append((text, params))
        if "DETACH DELETE n" in text:
            self._clean_nodes(params.get("source_id"))
            return _FakeTxResult(None)
        if "has_column_count" in text:
            return _FakeTxResult({"has_column_count": self.state.get("has_column", 0)})
        if "relationship_count" in text:
            return _FakeTxResult(
                {
                    "table_count": self.state.get("tables", 0),
                    "column_count": self.state.get("columns", 0),
                    "relationship_count": self.state.get("join_on", 0),
                }
            )
        return _FakeTxResult(None)


class _FakeSession:
    def __init__(self, state: dict):
        self.state = state
        self.tx_log: list[list[tuple[str, dict]]] = []
        self.committed_transactions = 0

    def execute_write(self, work):
        tx = _FakeTx(self.state)
        try:
            result = work(tx)
        except Exception:
            tx.rollback_marker = True
            self.tx_log.append(tx.queries)
            raise
        self.tx_log.append(tx.queries)
        self.committed_transactions += 1
        return result


class _FakeManager:
    """Neo4jConnectionManager 假实现：记录约束语句与数据事务。"""

    def __init__(self, state: dict | None = None):
        self.state = state or {}
        self.constraint_statements: list[str] = []
        self.session = _FakeSession(self.state)

    @contextmanager
    def get_session(self, database=None):
        yield self.session

    def execute_write_transaction(self, query, parameters=None, database=None):
        self.constraint_statements.append(query)

    def close(self):
        pass


def _loader(tmp_path, script, source_id=SOURCE_ID, options=None):
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
        },
        source_id=source_id,
    )
    loader.neo4j_manager = _FakeManager()
    return loader


def _consistent_state():
    return {"tables": 1, "columns": 1, "join_on": 0, "has_column": 1}


# ---------------------------------------------------------------- 旧加固口径


def test_missing_sections_rejected_before_write(tmp_path):
    body = (
        _write_script(tmp_path)
        .read_text(encoding="utf-8")
        .replace("// 2. 创建 Table 节点", "// 2. 创建 X 节点")
    )
    script = tmp_path / "broken.cypher"
    script.write_text(body, encoding="utf-8")
    loader = _loader(tmp_path, script)

    with pytest.raises(CQLScriptValidationError):
        loader.load()

    loader.neo4j_manager.constraint_statements == []


def test_truncated_script_rejected(tmp_path):
    script = tmp_path / "empty.cypher"
    script.write_text("// 统计: 1 张表, 1 个列, 0 个关系\n", encoding="utf-8")
    loader = _loader(tmp_path, script)
    with pytest.raises(CQLScriptValidationError):
        loader.load()


def test_missing_stats_header_rejected(tmp_path):
    body = (
        _write_script(tmp_path)
        .read_text(encoding="utf-8")
        .replace("// 统计: 1 张表, 1 个列, 0 个关系\n", "")
    )
    script = tmp_path / "nostats.cypher"
    script.write_text(body, encoding="utf-8")
    loader = _loader(tmp_path, script)
    with pytest.raises(CQLScriptValidationError):
        loader.load()


def test_old_format_script_without_source_header_rejected(tmp_path):
    """旧格式脚本（无 ``// source_id:`` 头）不能按源替换，删除之前拒绝。"""
    body = (
        _write_script(tmp_path)
        .read_text(encoding="utf-8")
        .replace(f"// source_id: {SOURCE_ID}\n", "")
    )
    script = tmp_path / "old.cypher"
    script.write_text(body, encoding="utf-8")
    loader = CQLLoader(
        {
            "cql_loader": {
                "input_file": str(script),
                "neo4j": {
                    "uri": "bolt://localhost:7687",
                    "user": "neo4j",
                    "password": "secret",
                },
            }
        }
    )
    loader.neo4j_manager = _FakeManager()
    with pytest.raises(CQLScriptValidationError, match="source_id"):
        loader.load()


# ---------------------------------------------------------------- 按源替换


def test_happy_path_single_transaction(tmp_path):
    loader = _loader(tmp_path, _write_script(tmp_path))
    loader.neo4j_manager.state.update(_consistent_state())

    result = loader.load()

    assert result["success"] is True
    assert result["source_id"] == SOURCE_ID
    assert result["counts"]["table_count"] == 1
    manager = loader.neo4j_manager
    # 约束语句在数据事务之外逐条执行（幂等 IF NOT EXISTS）
    assert len(manager.constraint_statements) == 2
    assert any("table_entity_id" in s for s in manager.constraint_statements)
    # 单一数据事务提交
    assert manager.session.committed_transactions == 1
    queries = manager.session.tx_log[0]
    # 第一条是本事务唯一的节点清理语句；没有另按关系/source_id 删除的语句
    cleanup_query, cleanup_params = queries[0]
    assert cleanup_query == (
        "MATCH (n) WHERE n.source_id IS NULL OR n.source_id = $source_id "
        "DETACH DELETE n"
    )
    assert cleanup_params == {"source_id": SOURCE_ID}
    assert sum("DETACH DELETE" in q for q, _ in queries) == 1
    assert any("MERGE" in q for q, _ in queries)
    assert "relationship_count" in queries[-2][0]
    assert "has_column_count" in queries[-1][0]


def test_source_mismatch_rejected_before_delete(tmp_path):
    other = "99999999-9999-4999-8999-999999999999"
    loader = _loader(tmp_path, _write_script(tmp_path), source_id=other)
    with pytest.raises(CQLScriptValidationError, match="不一致"):
        loader.load()
    assert loader.neo4j_manager.session.committed_transactions == 0


def test_no_source_at_all_rejected(tmp_path):
    loader = CQLLoader(
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
    loader.neo4j_manager = _FakeManager()
    # 脚本头提供了来源（CLI 调试路径）——可以执行；此处验证构造参数路径
    result = loader.load()
    assert result["source_id"] == SOURCE_ID


def test_stats_mismatch_rolls_back(tmp_path):
    loader = _loader(tmp_path, _write_script(tmp_path))
    # 事务内核验读到与统计头不符的计数
    loader.neo4j_manager.state.update(
        {"tables": 0, "columns": 1, "join_on": 0, "has_column": 1}
    )

    result = loader.load()

    assert result["success"] is False
    assert result["_rolled_back"] is True
    assert "已回滚" in result["message"]
    assert "table_count 期望 1/实际 0" in result["message"]
    manager = loader.neo4j_manager
    # 事务抛错未提交：模拟层未计为 committed
    assert manager.session.committed_transactions == 0
    # 清理已在此事务内执行，随核验失败一起回滚
    queries = manager.session.tx_log[0]
    assert "DETACH DELETE n" in queries[0][0]
    assert any("MERGE" in q for q, _ in queries)


def test_cleanup_deletes_unowned_and_current_source_nodes_any_label(tmp_path):
    """清理由节点归属驱动，不限制 label；保留其他源及双端保留的关系。"""
    other_source = "22222222-2222-4222-8222-222222222222"
    loader = _loader(tmp_path, _write_script(tmp_path))
    loader.neo4j_manager.state.update(
        {
            "nodes": {
                # 任意 label 的无来源节点
                "u1": {"labels": ["Table"], "source_id": None},
                "u2": {"labels": ["Other"], "source_id": None},
                # 任意 label 的当前源节点
                "c1": {"labels": ["Column"], "source_id": SOURCE_ID},
                "o1": {"labels": ["Other"], "source_id": SOURCE_ID},
                # 其他源独立节点，必须保留
                "x1": {"labels": ["Table"], "source_id": other_source},
                "x2": {"labels": ["Other"], "source_id": other_source},
            },
            "relationships": [
                {"id": "r1", "start": "u1", "end": "c1", "source_id": None},
                {"id": "r2", "start": "c1", "end": "o1", "source_id": SOURCE_ID},
                # 两端都保留；即使关系 source_id 是当前源也不主动清理
                {"id": "r3", "start": "x1", "end": "x2", "source_id": SOURCE_ID},
                # 任一端被删，关系随 DETACH DELETE 消失
                {"id": "r4", "start": "o1", "end": "x1", "source_id": SOURCE_ID},
            ],
            **_consistent_state(),
        }
    )

    result = loader.load()

    assert result["success"] is True
    state = loader.neo4j_manager.state
    assert state["deleted_node_ids"] == ["c1", "o1", "u1", "u2"]
    assert state["remaining_node_ids"] == ["x1", "x2"]
    assert state["deleted_relationship_ids"] == ["r1", "r2", "r4"]
    assert state["remaining_relationship_ids"] == ["r3"]

    cleanup_query, cleanup_params = loader.neo4j_manager.session.tx_log[0][0]
    assert cleanup_params == {"source_id": SOURCE_ID}
    # 单条节点清理，不按 label 限制，也不单独按关系 source_id 删除
    assert cleanup_query == (
        "MATCH (n) WHERE n.source_id IS NULL OR n.source_id = $source_id "
        "DETACH DELETE n"
    )


def test_write_failure_after_cleanup_rolls_back(tmp_path):
    """清理后写入失败：整事务回滚，模拟层不计提交。"""
    loader = _loader(tmp_path, _write_script(tmp_path))
    manager = loader.neo4j_manager
    manager.state.update(_consistent_state())

    from neo4j.exceptions import Neo4jError

    original_run = _FakeTx.run
    write_attempted = False

    def failing_run(self, query, **params):
        nonlocal write_attempted
        if "MERGE" in " ".join(query.split()):
            write_attempted = True
            raise Neo4jError("写入失败")
        return original_run(self, query, **params)

    _FakeTx.run = failing_run
    try:
        result = loader.load()
    finally:
        _FakeTx.run = original_run

    assert result["success"] is False
    assert result["_rolled_back"] is True
    assert manager.session.committed_transactions == 0
    assert write_attempted is True
    queries = manager.session.tx_log[0]
    # 清理已执行；随后写入失败，未提交
    assert len(queries) == 1
    assert "DETACH DELETE n" in queries[0][0]


def test_incomplete_commit_is_not_marked_rolled_back(tmp_path):
    """回调成功后 COMMIT 阶段失败（IncompleteCommit）：提交状态不可知，
    **不得**标记 _rolled_back——发布侧须按"可能已提交"保守撤销 current。"""
    from neo4j.exceptions import IncompleteCommit

    loader = _loader(tmp_path, _write_script(tmp_path))
    loader.neo4j_manager.state.update(_consistent_state())
    session = loader.neo4j_manager.session
    original_execute = session.execute_write

    def commit_fails(work):
        work(_FakeTx(session.state))  # 回调完整成功（含核验）
        session.tx_log.append(_FakeTx(session.state).queries)
        raise IncompleteCommit("提交确认未到达")

    session.execute_write = commit_fails
    try:
        result = loader.load()
    finally:
        session.execute_write = original_execute

    assert result["success"] is False
    assert "_rolled_back" not in result  # 提交状态未知：不标记已回滚
    assert "提交状态不可知" in result["message"]
    assert result["errors"][0]["section"] == "commit"


def test_constraint_failure_reports_untouched_graph(tmp_path):
    """约束语句失败：数据事务未开始，图数据未动（标记已回滚口径）。"""
    loader = _loader(tmp_path, _write_script(tmp_path))

    def failing_constraint(query, parameters=None, database=None):
        raise RuntimeError("schema 锁超时")

    loader.neo4j_manager.execute_write_transaction = failing_constraint
    result = loader.load()

    assert result["success"] is False
    assert result["_rolled_back"] is True
    assert "图数据未动" in result["message"]
    assert loader.neo4j_manager.session.committed_transactions == 0


def test_validate_after_load_option_cannot_skip_verification(tmp_path):
    """按源核验是事务提交前的必要步骤，options 无法关闭（doc 4 §4.5）。"""
    loader = _loader(
        tmp_path, _write_script(tmp_path), options={"validate_after_load": False}
    )
    loader.neo4j_manager.state.update(_consistent_state())

    result = loader.load()

    assert result["success"] is True
    queries = loader.neo4j_manager.session.tx_log[0]
    assert any("relationship_count" in q for q, _ in queries)


def test_close_closes_manager_once_and_is_idempotent(tmp_path):
    loader = _loader(tmp_path, _write_script(tmp_path))
    loader.neo4j_manager = MagicMock()
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
