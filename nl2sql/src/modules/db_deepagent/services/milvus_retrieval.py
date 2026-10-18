"""DbMilvusRetrieval：注入 Embedding 客户端的 Milvus 适配器（docs/v4/5 §6.4）

复制自 src/services/vector_adapter/milvus_adapter.py 的六个检索方法，改造点：
- 构造时注入 DbEmbeddingClient 与配置（集合名、搜索参数、连接别名）；
  所有 Collection(...) 调用显式传 using=<别名>，搜索传 timeout=cfg.milvus.timeout
- 连接别名基于配置别名加每实例唯一后缀（doc 4 §3.3/§4.7）：并发请求各持
  独立连接，close 只断开本实例；避免与旧链路的 default 别名互相影响
- 构造时绑定不可变 source_id（doc 4 §3）：六个检索/回查入口全部叠加
  ``source_id ==`` 过滤，精确回查同样按源过滤；缺失或非法来源直接拒绝，
  禁止回退为无源过滤查询
- search_dim_values 结果增加 query_value；dim_table 保持 Milvus 原值（三段表键）
- 新增 vector_field_dim(collection_name, field_name) -> int 供 §6.3 一致性检查
- Milvus 异常向上抛 RetrievalBackendError，不吞掉、不降级为空列表

字段映射、COSINE 分数处理（hit.distance 即相似度，无需转换）、JSON 序列化
表达式均与旧实现保持不变。
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from pymilvus import Collection, connections, db as milvus_db, utility

from src.modules.db_deepagent.config import DbAgentConfig
from src.modules.db_deepagent.errors import RetrievalBackendError
from src.modules.db_deepagent.services.embedding import DbEmbeddingClient
from src.utils.logger import get_module_logger

logger = get_module_logger("nl2sql.db_deepagent.milvus_retrieval")


def _normalized_source_id(source_id: str | None) -> str:
    """校验并规范化 source_id（UUID）；缺失/非法直接拒绝（doc 4 §3.2）。"""
    if not isinstance(source_id, str) or not source_id.strip():
        raise ValueError(
            "Milvus 检索必须绑定会话 source_id（规范化 UUID）；缺失即拒绝查询"
        )
    try:
        import uuid as _uuid

        return str(_uuid.UUID(source_id.strip()))
    except (ValueError, AttributeError):
        raise ValueError("source_id 不是合法 UUID，拒绝执行检索") from None


class DbMilvusRetrieval:
    """Milvus 检索适配器（每实例独立连接别名 + 不可变 source_id 绑定）"""

    def __init__(
        self,
        cfg: DbAgentConfig,
        embedding: DbEmbeddingClient,
        *,
        source_id: str | None = None,
    ):
        self._cfg = cfg
        self._embedding = embedding
        self.source_id = _normalized_source_id(source_id)
        # doc 4 §3.3：每执行实例独立 alias——同源并发请求也不能共用连接，
        # close 只释放本实例
        self._alias = f"{cfg.milvus.alias}_{uuid.uuid4().hex[:8]}"
        self._search_params = {
            "metric_type": cfg.milvus.search_params.metric_type,
            "params": dict(cfg.milvus.search_params.params),
        }
        self._collections: dict[str, Collection | None] = {
            "table_schema": None,
            "dim_value": None,
            "sql_example": None,
        }
        try:
            self._connect()
        except BaseException:
            # 连接已建立但集合缺失等场景下，对象尚未返回给调用方——
            # 构造失败必须自行断开本实例 alias，否则连接残留
            self.close()
            raise

    # ---------------------------------------------------------------- 连接

    def _connect(self) -> None:
        m = self._cfg.milvus
        try:
            connections.connect(
                alias=self._alias,
                host=m.host,
                port=str(m.port),
                user=m.user or None,
                password=m.password or None,
                timeout=m.timeout,
            )
            if m.database:
                existing = milvus_db.list_database(using=self._alias)
                if m.database not in existing:
                    raise RetrievalBackendError(
                        f"Milvus database {m.database!r} 不存在，可用: {existing}（不自动创建）"
                    )
                milvus_db.using_database(m.database, using=self._alias)
            # 连接后核对三个集合存在（缺集合在此失败，不等到首次检索）
            for name in (
                m.collections.table_schema,
                m.collections.dim_value,
                m.collections.sql_example,
            ):
                if not utility.has_collection(name, using=self._alias):
                    raise RetrievalBackendError(f"Milvus 集合不存在: {name}")
        except RetrievalBackendError:
            raise
        except Exception as exc:
            raise RetrievalBackendError(f"Milvus 连接失败: {exc}") from exc
        logger.info(
            "Milvus 已连接: %s:%s/%s alias=%s",
            m.host,
            m.port,
            m.database,
            self._alias,
        )

    def close(self) -> None:
        try:
            connections.disconnect(self._alias)
        except Exception:  # noqa: BLE001（断开失败不影响关闭流程）
            pass

    def _collection(self, kind: str) -> Collection:
        name = getattr(self._cfg.milvus.collections, kind)
        if self._collections[kind] is None:
            self._collections[kind] = Collection(name, using=self._alias)
        return self._collections[kind]

    @property
    def _source_expr(self) -> str:
        """本实例绑定的 source_id 过滤表达式（doc 4 §3.1，六入口统一组合）。"""
        return f'source_id == "{self.source_id}"'

    # ---------------------------------------------------------------- 检索

    def search_tables(
        self, embedding: list[float], top_k: int, similarity_threshold: float
    ) -> list[dict[str, Any]]:
        """检索语义相关的表（本源 + object_type == "table"；按原始分数过滤）。"""
        collection = self._collection("table_schema")
        logger.debug(
            "search_tables: collection=%s limit=%s threshold=%s source_id=%s",
            collection.name,
            top_k,
            similarity_threshold,
            self.source_id,
        )
        results = collection.search(
            data=[embedding],
            anns_field="embedding",
            param=self._search_params,
            limit=top_k,
            expr=f'{self._source_expr} and object_type == "table"',
            output_fields=[
                "object_id",
                "object_type",
                "table_category",
                "time_col_hint",
            ],
            timeout=self._cfg.milvus.timeout,
        )
        matches = []
        for hit in results[0]:
            similarity_raw = float(hit.distance)  # COSINE：distance 即相似度
            if similarity_raw < similarity_threshold:
                continue
            matches.append(
                {
                    "object_id": hit.entity.get("object_id"),  # 三段表键
                    "object_type": "table",
                    "similarity": max(0.0, min(1.0, similarity_raw)),
                    "grain_hint": None,  # Milvus 无此字段，不伪造
                    "time_col_hint": hit.entity.get("time_col_hint"),
                    "table_category": hit.entity.get("table_category", ""),
                }
            )
        return matches

    def search_columns(
        self, embedding: list[float], top_k: int, similarity_threshold: float
    ) -> list[dict[str, Any]]:
        """检索语义相关的列（本源 + object_type == "column"）。"""
        collection = self._collection("table_schema")
        results = collection.search(
            data=[embedding],
            anns_field="embedding",
            param=self._search_params,
            limit=top_k,
            expr=f'{self._source_expr} and object_type == "column"',
            output_fields=["object_id", "parent_id", "object_type"],
            timeout=self._cfg.milvus.timeout,
        )
        matches = []
        for hit in results[0]:
            similarity_raw = float(hit.distance)
            if similarity_raw < similarity_threshold:
                continue
            matches.append(
                {
                    "object_id": hit.entity.get("object_id"),  # 四段列全名
                    "parent_id": hit.entity.get("parent_id"),  # 所属表三段键
                    "object_type": "column",
                    "similarity": max(0.0, min(1.0, similarity_raw)),
                    "grain_hint": None,
                }
            )
        return matches

    def search_dim_values(
        self, query_value: str, top_k: int, min_score: float
    ) -> list[dict[str, Any]]:
        """检索维度值匹配（先向量化 query_value；结果附 query_value 便于追溯）。"""
        collection = self._collection("dim_value")
        query_embedding = self._embedding.embed_query(query_value)
        results = collection.search(
            data=[query_embedding],
            anns_field="col_value_embedding",
            param=self._search_params,
            limit=top_k,
            expr=self._source_expr,
            output_fields=["table_name", "col_name", "col_value"],
            timeout=self._cfg.milvus.timeout,
        )
        matches = []
        for hit in results[0]:
            score_raw = float(hit.distance)
            if score_raw < min_score:
                continue
            matches.append(
                {
                    "query_value": query_value,
                    "dim_table": hit.entity.get(
                        "table_name"
                    ),  # 保持 Milvus 原值（三段表键）
                    "dim_col": hit.entity.get("col_name"),
                    "matched_text": hit.entity.get("col_value"),
                    "score": max(0.0, min(1.0, score_raw)),
                }
            )
        return matches

    def search_similar_sqls(
        self, embedding: list[float], top_k: int, similarity_threshold: float
    ) -> list[dict[str, Any]]:
        """检索历史相似 SQL（本源；question_sql 为 JSON 文本，解析失败时原文入 sql）。"""
        collection = self._collection("sql_example")
        results = collection.search(
            data=[embedding],
            anns_field="embedding",
            param=self._search_params,
            limit=top_k,
            expr=self._source_expr,
            output_fields=["example_id", "question_sql", "domain"],
            timeout=self._cfg.milvus.timeout,
        )
        matches = []
        for hit in results[0]:
            similarity_raw = float(hit.distance)
            if similarity_raw < similarity_threshold:
                continue
            question, sql_text = "", ""
            payload = hit.entity.get("question_sql", "")
            if payload:
                try:
                    parsed = json.loads(payload)
                    question = parsed.get("question", "") or ""
                    sql_text = parsed.get("sql", "") or ""
                except (TypeError, ValueError):
                    sql_text = str(payload)
            matches.append(
                {
                    "question": question,
                    "sql": sql_text,
                    "similarity": max(0.0, min(1.0, similarity_raw)),
                    "example_id": hit.entity.get("example_id", ""),
                    "domain": hit.entity.get("domain", ""),
                }
            )
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "历史 SQL 检索: limit=%s 阈值=%s 保留=%s",
                top_k,
                similarity_threshold,
                len(matches),
            )
        return matches

    def fetch_table_cards(self, table_names: list[str]) -> dict[str, dict[str, Any]]:
        """批量精确查询表卡片（本源；object_desc 映射为 text_raw 保持接口一致）。

        精确回查同样按源过滤——同名 object_id 的字典整理只能发生在已限定
        来源的结果集上（doc 4 §3.1）。
        """
        if not table_names:
            return {}
        collection = self._collection("table_schema")
        expr = (
            f"{self._source_expr} and object_id in {json.dumps(table_names)} "
            'and object_type == "table"'
        )
        results = collection.query(
            expr=expr,
            output_fields=[
                "object_id",
                "object_desc",
                "time_col_hint",
                "table_category",
            ],
            timeout=self._cfg.milvus.timeout,
        )
        cards = {}
        for row in results:
            object_id = row.get("object_id")
            if object_id:
                cards[object_id] = {
                    "object_id": object_id,
                    "text_raw": row.get("object_desc", ""),
                    "grain_hint": None,
                    "time_col_hint": row.get("time_col_hint"),
                    "table_category": row.get("table_category", ""),
                }
        return cards

    def fetch_table_categories(self, table_names: list[str]) -> dict[str, str]:
        """批量查询表类别（本源；只记录非空值）。"""
        if not table_names:
            return {}
        collection = self._collection("table_schema")
        expr = (
            f"{self._source_expr} and object_id in {json.dumps(table_names)} "
            'and object_type == "table"'
        )
        results = collection.query(
            expr=expr,
            output_fields=["object_id", "table_category"],
            timeout=self._cfg.milvus.timeout,
        )
        return {
            row["object_id"]: row.get("table_category", "")
            for row in results
            if row.get("object_id") and row.get("table_category")
        }

    def vector_field_dim(self, collection_name: str, field_name: str) -> int:
        """读取集合向量字段维度（§6.3 检查 4）。必须显式传本模块别名。"""
        try:
            col = Collection(collection_name, using=self._alias)
            for field in col.schema.fields:
                if field.name == field_name:
                    params = getattr(field, "params", {}) or {}
                    dim = params.get("dim")
                    if dim is None:
                        raise RetrievalBackendError(
                            f"字段 {collection_name}.{field_name} 不是向量字段（无 dim 参数）"
                        )
                    return int(dim)
            raise RetrievalBackendError(f"集合 {collection_name} 无字段 {field_name}")
        except RetrievalBackendError:
            raise
        except Exception as exc:
            raise RetrievalBackendError(
                f"读取 {collection_name}.{field_name} 维度失败: {exc}"
            ) from exc
