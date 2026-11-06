"""各存储清理器与生产连接适配器（doc 8.1 §6.1–§6.4）。

清理器只依赖注入的 ops 接口，生产适配器按加载器同款配置建立连接；
单测用 fake ops，live 演练按 §8.3 门控。
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from metaweave.core.clean_targets import (
    resolve_source_workspace_dir,
    resolve_workspace_root,
)

from backend.deletion.errors import DeletionStepError


# ---- Milvus ----


def clean_milvus_source(ops: Any, source_id: str, collections: list[str]) -> dict:
    """按 source_id 精确过滤删除并核验剩余为零（§6.2）。

    缺失集合属于无数据；删除后仍有剩余记录视为失败。
    """
    expr = f'source_id == "{source_id}"'
    results: dict = {}
    for name in collections:
        if not ops.has_collection(name):
            results[name] = {"status": "missing_collection"}
            continue
        deleted = int(ops.delete_by_expr(name, expr) or 0)
        remaining = int(ops.count_by_expr(name, expr) or 0)
        if remaining:
            raise DeletionStepError(
                f"Milvus 集合 {name} 清理后仍有 {remaining} 条该源记录"
            )
        results[name] = {"status": "success", "deleted": deleted}
    return results


class MilvusCleanerOps:
    """生产适配器：与加载器同款连接（providers.milvus 子配置）。

    ``_lazy_import_milvus`` 返回七项
    ``(connections, db, FieldSchema, CollectionSchema, Collection,
    DataType, utility)``，各方法按位置取所需对象。
    """

    def __init__(self, milvus_config: dict):
        from metaweave.services.vector_db.milvus_client import (
            MilvusClient,
            _lazy_import_milvus,
        )

        self._client = MilvusClient(milvus_config)
        self._client.connect()
        self._lazy = _lazy_import_milvus

    def has_collection(self, name: str) -> bool:
        *_, utility = self._lazy()
        return bool(utility.has_collection(name, using=self._client.alias))

    def _collection(self, name: str):
        _, _, _, _, Collection, _, _ = self._lazy()
        return Collection(name, using=self._client.alias)

    def delete_by_expr(self, name: str, expr: str) -> int:
        collection = self._collection(name)
        result = collection.delete(expr)
        return int(getattr(result, "delete_count", 0) or 0)

    def count_by_expr(self, name: str, expr: str) -> int:
        collection = self._collection(name)
        rows = collection.query(expr=expr, output_fields=["count(*)"])
        if not rows:
            return 0
        return int(rows[0].get("count(*)", 0) or 0)

    def close(self) -> None:
        self._client.close()


# ---- Neo4j ----

_DETACH_DELETE_NODES = "MATCH (n) WHERE n.source_id = $source_id DETACH DELETE n"
_DELETE_TAGGED_RELS = "MATCH ()-[r]->() WHERE r.source_id = $source_id DELETE r"
# 两条独立 count 查询：带分组键的聚合在空输入下不产生行，串联统计会把
# 干净的空图误判为失败；独立 count 无匹配时仍返回一行 0（§6.3）
_COUNT_NODES = "MATCH (n) WHERE n.source_id = $source_id RETURN count(n) AS count"
_COUNT_RELS = "MATCH ()-[r]->() WHERE r.source_id = $source_id RETURN count(r) AS count"


def clean_neo4j_source(ops: Any, source_id: str, *, lock_probe=None) -> dict:
    """参数化 Cypher 清理该源节点与残留关系，并核验归零（§6.3）。

    计数查询返回空行或缺 ``count`` 视为核验失败，不得默认当零；
    查询异常经 ops.single 抛出，由执行器转为任务 failed。
    ``lock_probe``（doc 10.2 §5.1）：每次独立删除/查询请求前后探测目标
    锁连接；失锁抛 ``LockLostError`` 原样传播，不继续下一次请求。
    """

    def _probe():
        if lock_probe is not None:
            lock_probe()

    _probe()
    ops.run(_DETACH_DELETE_NODES, source_id=source_id)
    _probe()
    ops.run(_DELETE_TAGGED_RELS, source_id=source_id)
    _probe()
    nodes_row = ops.single(_COUNT_NODES, source_id=source_id)
    _probe()
    rels_row = ops.single(_COUNT_RELS, source_id=source_id)
    _probe()
    if (
        not isinstance(nodes_row, dict)
        or not isinstance(rels_row, dict)
        or "count" not in nodes_row
        or "count" not in rels_row
    ):
        raise DeletionStepError("Neo4j 清理核验查询返回不合法结果，按失败处理")
    nodes = int(nodes_row["count"])
    rels = int(rels_row["count"])
    if nodes or rels:
        raise DeletionStepError(f"Neo4j 该源清理后仍有节点 {nodes} 个 / 关系 {rels} 条")
    return {"nodes_remaining": nodes, "rels_remaining": rels}


class Neo4jCleanerOps:
    """生产适配器：normalize 后的 loaders.cql_loader.neo4j 配置段。"""

    def __init__(self, neo4j_section: dict):
        from neo4j import GraphDatabase

        self._driver = GraphDatabase.driver(
            neo4j_section["uri"],
            auth=(neo4j_section["user"], neo4j_section["password"]),
        )
        self._database = neo4j_section.get("database") or "neo4j"

    def run(self, query: str, **params) -> None:
        with self._driver.session(database=self._database) as session:
            session.run(query, **params).consume()

    def single(self, query: str, **params) -> dict | None:
        with self._driver.session(database=self._database) as session:
            record = session.run(query, **params).single()
            return dict(record) if record is not None else None

    def close(self) -> None:
        self._driver.close()


# ---- 文件 ----


def clean_workspace_tree(source_id: str, workspace_id: str) -> dict:
    """删除一个工作区采集树；目录不存在视为成功（§6.4）。"""
    return _remove_tree(resolve_workspace_root(source_id, workspace_id))


def clean_source_tree(source_id: str) -> dict:
    """删除整个数据源采集树；仅供源删除，严禁用于单工作区删除（§6.4）。"""
    return _remove_tree(resolve_source_workspace_dir(source_id))


def _remove_tree(path: Path) -> dict:
    if not path.exists():
        return {"status": "missing", "path": str(path)}
    shutil.rmtree(path)
    return {"status": "removed", "path": str(path)}
