"""删除/清理目标的实际配置身份与指纹（doc 8.1 §6.2/§6.3/§7）。

复用加载器实际使用的配置键定位三类 collection 与 Neo4j 连接；
database 维度必须显式携带，不能省略后退到客户端默认库。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from metaweave.core.clean_targets import normalize_neo4j_uri

# 与三类向量发布加载器相同的配置键（publish_service._milvus_collection）
MILVUS_LOADER_KEYS = ("table_schema_loader", "dim_loader", "sql_loader")


def _require_mapping(container: Any, key: str, path: str) -> dict:
    node = container.get(key)
    if node is None:
        return {}
    if not isinstance(node, dict):
        raise ValueError(f"{path} 必须是对象（mapping）")
    return node


def resolve_deletion_targets(main_config: dict) -> dict:
    """解析 Milvus / Neo4j 清理目标身份与稳定指纹。

    Raises:
        ValueError: 配置缺失或非法（向量库不是 Milvus、collection 名为空等）。
    """
    vector_db = _require_mapping(main_config, "vector_database", "vector_database")
    if vector_db.get("active") != "milvus":
        raise ValueError(
            f"vector_database.active 必须为 'milvus'（实际为 {vector_db.get('active')!r}）"
        )
    providers = _require_mapping(vector_db, "providers", "vector_database.providers")
    milvus = _require_mapping(providers, "milvus", "vector_database.providers.milvus")
    if not milvus:
        raise ValueError("缺少 Milvus 配置: vector_database.providers.milvus")

    host = str(milvus.get("host", "localhost")).strip().lower()
    port_value = milvus.get("port", 19530)
    if isinstance(port_value, bool):
        raise ValueError(f"Milvus port 非法（布尔值不受理）: {port_value!r}")
    port = str(int(port_value))
    database = str(milvus.get("database", "default")).strip() or "default"

    loaders = _require_mapping(main_config, "loaders", "loaders")
    collections = []
    for key in MILVUS_LOADER_KEYS:
        node = _require_mapping(loaders, key, f"loaders.{key}")
        name = str(node.get("collection_name", "")).strip()
        if not name:
            raise ValueError(f"loaders.{key}.collection_name 为空")
        collections.append(name)

    cql_loader = _require_mapping(loaders, "cql_loader", "loaders.cql_loader")
    neo4j_raw = _require_mapping(cql_loader, "neo4j", "loaders.cql_loader.neo4j")
    if not neo4j_raw or not str(neo4j_raw.get("uri", "")).strip():
        raise ValueError("loaders.cql_loader.neo4j.uri 缺失或为空")
    uri = normalize_neo4j_uri(neo4j_raw["uri"])
    database_name = str(neo4j_raw.get("database", "neo4j")).strip() or "neo4j"

    identity = {
        "milvus": {
            "host": host,
            "port": port,
            "database": database,
            "collections": collections,
        },
        "neo4j": {"uri": uri, "database": database_name},
    }
    canonical = json.dumps(identity, sort_keys=True, ensure_ascii=False)
    fingerprint = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
    return {**identity, "fingerprint": fingerprint}
