"""Embedding 一致性检查（docs/v4/5 §6.3）

初始化时执行，任一失败抛 EmbeddingConsistencyError（CLI 显示具体集合与字段，
不进入 Agent 创建）。入库模型只认发布记录 embedding_release.yaml，不由维度反推；
不自动重建集合，也不回退到其他 profile。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from src.modules.db_deepagent.config import DbAgentConfig
from src.modules.db_deepagent.errors import EmbeddingConsistencyError
from src.modules.db_deepagent.services.embedding import DbEmbeddingClient
from src.modules.db_deepagent.services.milvus_retrieval import DbMilvusRetrieval
from src.utils.logger import get_module_logger

logger = get_module_logger("nl2sql.db_deepagent.embedding_consistency")

# 发布记录中的三个集合键 → 模块配置 milvus.collections 的字段名
_COLLECTION_KEYS = {
    "table_schema_embeddings": "table_schema",
    "dim_value_embeddings": "dim_value",
    "sql_example_embeddings": "sql_example",
}

_REQUIRED_COLLECTION_FIELDS = ("model", "dimensions", "vector_field")


def _load_release_record(path: Path) -> dict[str, Any]:
    """检查 1：读取发布记录；文件缺失或关键字段为空视为"入库模型未知"。"""
    if not path.exists():
        raise EmbeddingConsistencyError(f"Embedding 发布记录不存在: {path}")
    record = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not record.get("release_batch"):
        raise EmbeddingConsistencyError(
            "发布记录缺少 release_batch（入库模型未知，不能反推）"
        )
    if not record.get("milvus_database"):
        raise EmbeddingConsistencyError("发布记录缺少 milvus_database")
    collections = record.get("collections") or {}
    for key in _COLLECTION_KEYS:
        entry = collections.get(key) or {}
        missing = [f for f in _REQUIRED_COLLECTION_FIELDS if not entry.get(f)]
        if missing:
            raise EmbeddingConsistencyError(
                f"发布记录集合 {key} 缺少字段 {missing}（入库模型未知，不能反推）"
            )
    return record


def check_embedding_consistency(
    cfg: DbAgentConfig,
    profile: dict[str, Any],
    embedding: DbEmbeddingClient,
    milvus: DbMilvusRetrieval,
) -> dict[str, Any]:
    """执行 §6.3 全部五项检查；通过时返回发布信息（release_batch 等）供日志与 doctor。

    Args:
        cfg: 模块配置
        profile: 全局 embedding_profiles[<配置名>] 的原始字典
        embedding: 查询侧客户端
        milvus: 已连接的 Milvus 适配器（别名 db_deepagent）
    """
    record = _load_release_record(Path(cfg.embedding.release_record))

    # 检查 2：目标一致（Milvus 库与三个集合名）
    if record["milvus_database"] != cfg.milvus.database:
        raise EmbeddingConsistencyError(
            f"发布记录 milvus_database={record['milvus_database']!r} 与配置 "
            f"milvus.database={cfg.milvus.database!r} 不一致"
        )
    configured = {
        "table_schema_embeddings": cfg.milvus.collections.table_schema,
        "dim_value_embeddings": cfg.milvus.collections.dim_value,
        "sql_example_embeddings": cfg.milvus.collections.sql_example,
    }
    if set(record["collections"]) != set(configured):
        raise EmbeddingConsistencyError(
            f"发布记录集合名集合 {sorted(record['collections'])} 与配置集合 "
            f"{sorted(configured.values())} 不一致"
        )

    # 检查 3：模型与维度一致（入库模型只认发布记录）
    query_model = profile.get("model")
    query_dims = profile.get("dimensions")
    if not query_model or not query_dims:
        raise EmbeddingConsistencyError(
            f"查询侧 embedding profile 缺少 model/dimensions: model={query_model!r}, "
            f"dimensions={query_dims!r}"
        )
    query_dims = int(query_dims)
    for key, entry in record["collections"].items():
        if entry["model"] != query_model:
            raise EmbeddingConsistencyError(
                f"集合 {key} 入库模型 {entry['model']!r} 与查询侧 profile 模型 "
                f"{query_model!r} 不一致（维度一致只是必要条件，不能反推）"
            )
        if int(entry["dimensions"]) != int(query_dims):
            raise EmbeddingConsistencyError(
                f"集合 {key} 入库维度 {entry['dimensions']} 与查询侧 profile 维度 "
                f"{query_dims} 不一致"
            )

    # 检查 4：集合向量字段实际维度（经本模块别名读取）
    for key, entry in record["collections"].items():
        actual = milvus.vector_field_dim(configured[key], entry["vector_field"])
        if actual != int(query_dims):
            raise EmbeddingConsistencyError(
                f"集合 {configured[key]} 字段 {entry['vector_field']} 实际维度 {actual} "
                f"与 profile 维度 {query_dims} 不一致"
            )

    # 检查 5：用 profile 生成一次查询向量，长度必须等于 dimensions
    probe = embedding.embed_query("维度一致性探针文本")
    if len(probe) != int(query_dims):
        raise EmbeddingConsistencyError(
            f"查询向量长度 {len(probe)} 与 profile 维度 {query_dims} 不一致"
        )

    info = {
        "release_batch": record["release_batch"],
        "milvus_database": record["milvus_database"],
        "collections": sorted(configured.values()),
        "model": query_model,
        "dimensions": query_dims,
    }
    logger.info(
        "Embedding 一致性检查通过: release_batch=%s milvus_database=%s collections=%s",
        info["release_batch"],
        info["milvus_database"],
        info["collections"],
    )
    return info
