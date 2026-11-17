"""doc 8.1 §6.2/§6.3/§6.4/§7 目标身份与各存储清理器（fake 驱动单测）。"""

import pytest

from backend.deletion.cleaners import (
    clean_milvus_source,
    clean_neo4j_source,
    clean_workspace_tree,
)
from backend.deletion.errors import DeletionStepError
from backend.deletion.targets import resolve_deletion_targets

SOURCE_ID = "db_src_68"


def _main_config():
    return {
        "vector_database": {
            "active": "milvus",
            "providers": {
                "milvus": {
                    "host": "Milvus-Host ",
                    "port": 19530,
                    "database": "rehearsal",
                }
            },
        },
        "loaders": {
            "table_schema_loader": {"collection_name": "table_schema_embeddings"},
            "dim_loader": {"collection_name": "dim_value_embeddings"},
            "sql_loader": {"collection_name": "sql_example_embeddings"},
            "cql_loader": {
                "neo4j": {
                    "uri": "bolt://Graph-Host:7687",
                    "user": "neo4j",
                    "password": "secret",
                    "database": "graph",
                }
            },
        },
    }


def test_resolve_targets_identity_and_fingerprint():
    targets = resolve_deletion_targets(_main_config())
    milvus = targets["milvus"]
    assert milvus["host"] == "milvus-host"  # 规范化：小写去空白
    assert milvus["port"] == "19530"
    assert milvus["database"] == "rehearsal"
    assert milvus["collections"] == [
        "table_schema_embeddings",
        "dim_value_embeddings",
        "sql_example_embeddings",
    ]
    neo4j = targets["neo4j"]
    assert neo4j["uri"] == "bolt://graph-host:7687"
    assert neo4j["database"] == "graph"
    assert targets["fingerprint"]
    # 同配置 → 同指纹；配置变化 → 指纹变化
    assert (
        resolve_deletion_targets(_main_config())["fingerprint"]
        == targets["fingerprint"]
    )
    changed = _main_config()
    changed["vector_database"]["providers"]["milvus"]["database"] = "other"
    assert resolve_deletion_targets(changed)["fingerprint"] != targets["fingerprint"]


def test_resolve_targets_rejects_missing_collection_name():
    config = _main_config()
    del config["loaders"]["dim_loader"]["collection_name"]
    with pytest.raises(ValueError):
        resolve_deletion_targets(config)


class FakeMilvusOps:
    def __init__(self, collections, remaining=0):
        self._collections = collections
        self._remaining = remaining
        self.delete_calls = []

    def has_collection(self, name):
        return name in self._collections

    def delete_by_expr(self, name, expr):
        self.delete_calls.append((name, expr))
        return 5

    def count_by_expr(self, name, expr):
        return self._remaining


def test_milvus_cleaner_uses_source_filter_and_skips_missing_collection():
    ops = FakeMilvusOps(collections={"table_schema_embeddings"})
    results = clean_milvus_source(
        ops,
        SOURCE_ID,
        ["table_schema_embeddings", "dim_value_embeddings"],
    )
    assert ops.delete_calls == [
        ("table_schema_embeddings", f'source_id == "{SOURCE_ID}"')
    ]
    assert results["table_schema_embeddings"] == {"status": "success", "deleted": 5}
    assert results["dim_value_embeddings"]["status"] == "missing_collection"


def test_milvus_cleaner_fails_when_records_remain():
    ops = FakeMilvusOps(collections={"table_schema_embeddings"}, remaining=2)
    with pytest.raises(DeletionStepError):
        clean_milvus_source(ops, SOURCE_ID, ["table_schema_embeddings"])


class FakeNeo4jOps:
    """按查询文本分别回放两条计数查询的结果（真实驱动语义：空图也返回 count=0）。"""

    def __init__(self, nodes=0, rels=0, invalid=False):
        self.nodes = nodes
        self.rels = rels
        self.invalid = invalid
        self.queries = []

    def run(self, query, **params):
        self.queries.append((query, params))

    def single(self, query, **params):
        self.queries.append((query, params))
        if self.invalid:
            return None  # 返回空/不合法结果 → 核验失败，不得当零
        if "count(n)" in query:
            return {"count": self.nodes}
        if "count(r)" in query:
            return {"count": self.rels}
        raise AssertionError(query)


def test_neo4j_cleaner_param_queries_and_zero_verification():
    ops = FakeNeo4jOps(nodes=0, rels=0)
    result = clean_neo4j_source(ops, SOURCE_ID)
    detach = ops.queries[0]
    assert detach[0] == "MATCH (n) WHERE n.source_id = $source_id DETACH DELETE n"
    assert detach[1] == {"source_id": SOURCE_ID}
    rel_cleanup = ops.queries[1]
    assert rel_cleanup[0] == (
        "MATCH ()-[r]->() WHERE r.source_id = $source_id DELETE r"
    )
    # 两条独立计数查询：空图也各自返回一行 count=0
    assert ops.queries[2][0] == (
        "MATCH (n) WHERE n.source_id = $source_id RETURN count(n) AS count"
    )
    assert ops.queries[3][0] == (
        "MATCH ()-[r]->() WHERE r.source_id = $source_id RETURN count(r) AS count"
    )
    assert result == {"nodes_remaining": 0, "rels_remaining": 0}


def test_neo4j_cleaner_succeeds_when_source_never_had_graph_data():
    """该源原本没有图数据：DETACH DELETE/关系删除为空操作，计数为 0 → 成功。"""
    ops = FakeNeo4jOps(nodes=0, rels=0)
    assert clean_neo4j_source(ops, SOURCE_ID) == {
        "nodes_remaining": 0,
        "rels_remaining": 0,
    }


def test_neo4j_cleaner_fails_when_nodes_remain():
    ops = FakeNeo4jOps(nodes=1, rels=0)
    with pytest.raises(DeletionStepError):
        clean_neo4j_source(ops, SOURCE_ID)


def test_neo4j_cleaner_fails_when_rels_remain():
    ops = FakeNeo4jOps(nodes=0, rels=2)
    with pytest.raises(DeletionStepError):
        clean_neo4j_source(ops, SOURCE_ID)


def test_neo4j_cleaner_fails_on_invalid_count_result():
    """计数查询返回空/缺 count：核验失败，不得默认当零（含查询异常路径）。"""
    ops = FakeNeo4jOps(invalid=True)
    with pytest.raises(DeletionStepError):
        clean_neo4j_source(ops, SOURCE_ID)


def test_workspace_tree_missing_directory_is_success(tmp_path, monkeypatch):
    from backend.deletion import cleaners

    monkeypatch.setattr(
        cleaners,
        "resolve_workspace_root",
        lambda source_id, workspace_id: tmp_path / "absent",
    )
    result = clean_workspace_tree(SOURCE_ID, "ws_1")
    assert result["status"] == "missing"


def test_workspace_tree_removes_directory(tmp_path, monkeypatch):
    from backend.deletion import cleaners

    target = tmp_path / "ws_1"
    target.mkdir()
    (target / "ddl").mkdir()
    (target / "ddl" / "a.sql").write_text("x", encoding="utf-8")
    monkeypatch.setattr(
        cleaners,
        "resolve_workspace_root",
        lambda source_id, workspace_id: target,
    )
    result = clean_workspace_tree(SOURCE_ID, "ws_1")
    assert result["status"] == "removed"
    assert not target.exists()
