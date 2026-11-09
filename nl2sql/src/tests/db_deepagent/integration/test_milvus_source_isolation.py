"""Milvus 隔离库双源检索验收（doc 4 §3.3）

在**独立 Milvus database**（``insightweave_srcid_test``，不影响正式
``nl2sql`` 库的三集合）中建三张最小 schema 集合，写入两个假源的同名
db/schema/table/维度/SQL 记录，验证：

- 六个检索/回查入口按绑定 source_id 过滤——A 源向量检索与精确回查都
  只返回 A 的知识，B 同理；
- 不存在的 source_id 返回空（不回退为无源过滤）；
- 并发构造两个实例（独立 alias）互不影响、close 只断开自身。

需要真实 Milvus（``--run-db``）；服务不可达时跳过。
"""

from __future__ import annotations

import pytest

from src.modules.db_deepagent.config import DbAgentConfig
from src.modules.db_deepagent.services.milvus_retrieval import DbMilvusRetrieval

pytestmark = [pytest.mark.unit, pytest.mark.integration, pytest.mark.requires_db]

TEST_DATABASE = "insightweave_srcid_test"
# doc 5.1 §1 短 ID 夹具（doc 12.2 M-1：合法来源为 db_src_N；UUID 输入非法）
SOURCE_A = "db_src_1"
SOURCE_B = "db_src_2"
MISSING_SOURCE = "db_src_3"
UUID_SOURCE = "11111111-1111-4111-8111-111111111111"

_VEC_A = [1.0, 0.0]
_VEC_B = [0.0, 1.0]


def _milvus_reachable(cfg: DbAgentConfig) -> bool:
    try:
        from pymilvus import connections

        connections.connect(
            alias="srcid_probe",
            host=cfg.milvus.host,
            port=str(cfg.milvus.port),
            user=cfg.milvus.user or None,
            password=cfg.milvus.password or None,
            timeout=cfg.milvus.timeout,
        )
        connections.disconnect("srcid_probe")
        return True
    except Exception:  # noqa: BLE001
        return False


@pytest.fixture(scope="module")
def isolated_cfg(db_agent_config: DbAgentConfig) -> DbAgentConfig:
    """指向隔离 database 与加前缀集合名的配置副本。"""
    cfg = db_agent_config.model_copy(deep=True)
    cfg.milvus.database = TEST_DATABASE
    prefix = "srcid_test_"
    cfg.milvus.collections.table_schema = prefix + "table_schema"
    cfg.milvus.collections.dim_value = prefix + "dim_value"
    cfg.milvus.collections.sql_example = prefix + "sql_example"
    return cfg


@pytest.fixture(scope="module")
def seeded_collections(isolated_cfg: DbAgentConfig):
    """在隔离 database 中建集合并写入 A/B 双源同名记录；用例结束后清理。"""
    if not _milvus_reachable(isolated_cfg):
        pytest.skip("Milvus 不可达，跳过双源隔离验收")
    from pymilvus import (
        Collection,
        CollectionSchema,
        DataType,
        FieldSchema,
        connections,
        db as milvus_db,
        utility,
    )

    alias = "srcid_setup"
    connections.connect(
        alias=alias,
        host=isolated_cfg.milvus.host,
        port=str(isolated_cfg.milvus.port),
        user=isolated_cfg.milvus.user or None,
        password=isolated_cfg.milvus.password or None,
        timeout=isolated_cfg.milvus.timeout,
    )
    existing = milvus_db.list_database(using=alias)
    if TEST_DATABASE not in existing:
        milvus_db.create_database(TEST_DATABASE, using=alias)
    milvus_db.using_database(TEST_DATABASE, using=alias)

    names = (
        isolated_cfg.milvus.collections.table_schema,
        isolated_cfg.milvus.collections.dim_value,
        isolated_cfg.milvus.collections.sql_example,
    )
    for name in names:
        if utility.has_collection(name, using=alias):
            utility.drop_collection(name, using=alias)

    dim = 2

    def varchar(name: str, max_len: int = 512, is_primary: bool = False):
        return FieldSchema(
            name=name,
            dtype=DataType.VARCHAR,
            max_length=max_len,
            is_primary=is_primary,
        )

    table_schema_col = Collection(
        isolated_cfg.milvus.collections.table_schema,
        schema=CollectionSchema(
            [
                varchar("entity_id", 128, is_primary=True),
                varchar("source_id", 36),
                varchar("object_id", 512),
                varchar("object_type", 16),
                varchar("object_desc", 2048),
                varchar("table_category", 64),
                varchar("time_col_hint", 512),
                varchar("parent_id", 512),
                FieldSchema(name="embedding", dtype=DataType.FLOAT_VECTOR, dim=dim),
            ]
        ),
        using=alias,
    )
    dim_value_col = Collection(
        isolated_cfg.milvus.collections.dim_value,
        schema=CollectionSchema(
            [
                varchar("col_id", 128, is_primary=True),
                varchar("source_id", 36),
                varchar("table_name", 512),
                varchar("col_name", 128),
                varchar("col_value", 256),
                FieldSchema(
                    name="col_value_embedding", dtype=DataType.FLOAT_VECTOR, dim=dim
                ),
            ]
        ),
        using=alias,
    )
    sql_example_col = Collection(
        isolated_cfg.milvus.collections.sql_example,
        schema=CollectionSchema(
            [
                varchar("example_id", 128, is_primary=True),
                varchar("source_id", 36),
                varchar("question_sql", 4096),
                varchar("domain", 128),
                FieldSchema(name="embedding", dtype=DataType.FLOAT_VECTOR, dim=dim),
            ]
        ),
        using=alias,
    )

    import json as _json

    # 双源同名 db/schema/table：A 与 B 各自拥有 orders.public.orders
    table_schema_col.insert(
        [
            {
                "entity_id": f"{SOURCE_A}:orders.public.orders",
                "source_id": SOURCE_A,
                "object_id": "orders.public.orders",
                "object_type": "table",
                "object_desc": "A 源订单表",
                "table_category": "订单",
                "time_col_hint": "",
                "parent_id": "",
                "embedding": _VEC_A,
            },
            {
                "entity_id": f"{SOURCE_B}:orders.public.orders",
                "source_id": SOURCE_B,
                "object_id": "orders.public.orders",
                "object_type": "table",
                "object_desc": "B 源订单表",
                "table_category": "订单",
                "time_col_hint": "",
                "parent_id": "",
                "embedding": _VEC_B,
            },
            # 仅 B 存在的表（A 会话的存在性/检索都不应看到）
            {
                "entity_id": f"{SOURCE_B}:orders.public.only_b",
                "source_id": SOURCE_B,
                "object_id": "orders.public.only_b",
                "object_type": "table",
                "object_desc": "仅 B 源表",
                "table_category": "",
                "time_col_hint": "",
                "parent_id": "",
                "embedding": _VEC_B,
            },
            {
                "entity_id": f"{SOURCE_A}:orders.public.orders.order_id",
                "source_id": SOURCE_A,
                "object_id": "orders.public.orders.order_id",
                "object_type": "column",
                "object_desc": "A 源订单 ID 列",
                "parent_id": "orders.public.orders",
                "table_category": "",
                "time_col_hint": "",
                "embedding": _VEC_A,
            },
            {
                "entity_id": f"{SOURCE_B}:orders.public.orders.order_id",
                "source_id": SOURCE_B,
                "object_id": "orders.public.orders.order_id",
                "object_type": "column",
                "object_desc": "B 源订单 ID 列",
                "parent_id": "orders.public.orders",
                "table_category": "",
                "time_col_hint": "",
                "embedding": _VEC_B,
            },
        ]
    )
    dim_value_col.insert(
        [
            {
                "col_id": f"{SOURCE_A}:orders.public.dim_status:status:paid",
                "source_id": SOURCE_A,
                "table_name": "orders.public.dim_status",
                "col_name": "status",
                "col_value": "paid-A",
                "col_value_embedding": _VEC_A,
            },
            {
                "col_id": f"{SOURCE_B}:orders.public.dim_status:status:paid",
                "source_id": SOURCE_B,
                "table_name": "orders.public.dim_status",
                "col_name": "status",
                "col_value": "paid-B",
                "col_value_embedding": _VEC_B,
            },
        ]
    )
    sql_example_col.insert(
        [
            {
                "example_id": f"{SOURCE_A}:ex1",
                "source_id": SOURCE_A,
                "question_sql": _json.dumps(
                    {"question": "A 源问题", "sql": "SELECT 1"}, ensure_ascii=False
                ),
                "domain": "sales",
                "embedding": _VEC_A,
            },
            {
                "example_id": f"{SOURCE_B}:ex1",
                "source_id": SOURCE_B,
                "question_sql": _json.dumps(
                    {"question": "B 源问题", "sql": "SELECT 2"}, ensure_ascii=False
                ),
                "domain": "sales",
                "embedding": _VEC_B,
            },
        ]
    )
    for col in (table_schema_col, dim_value_col, sql_example_col):
        col.create_index(
            field_name=(
                "col_value_embedding" if col.name.endswith("dim_value") else "embedding"
            ),
            index_params={"index_type": "FLAT", "metric_type": "COSINE"},
        )
        col.load()
    try:
        yield isolated_cfg
    finally:
        for name in names:
            if utility.has_collection(name, using=alias):
                utility.drop_collection(name, using=alias)
        connections.disconnect(alias)


class _NoEmbedding:
    """检索路径不触发向量化（search_dim_values 除外）；构造注入占位。"""

    def embed_query(self, text: str) -> list[float]:
        return _VEC_A  # 维度值检索用 A 向量查询


def _retrieval(cfg: DbAgentConfig, source_id: str) -> DbMilvusRetrieval:
    return DbMilvusRetrieval(cfg, _NoEmbedding(), source_id=source_id)


class TestDoubleSourceIsolation:
    def test_table_search_returns_only_bound_source(self, seeded_collections):
        cfg = seeded_collections
        a = _retrieval(cfg, SOURCE_A)
        b = _retrieval(cfg, SOURCE_B)
        try:
            hits_a = a.search_tables(_VEC_A, top_k=10, similarity_threshold=0.0)
            ids_a = {h["object_id"] for h in hits_a}
            assert "orders.public.orders" in ids_a
            assert "orders.public.only_b" not in ids_a  # 仅 B 的表不可见

            hits_b = b.search_tables(_VEC_B, top_k=10, similarity_threshold=0.0)
            ids_b = {h["object_id"] for h in hits_b}
            assert "orders.public.only_b" in ids_b
        finally:
            a.close()
            b.close()

    def test_column_search_scoped(self, seeded_collections):
        cfg = seeded_collections
        a = _retrieval(cfg, SOURCE_A)
        try:
            hits = a.search_columns(_VEC_A, top_k=10, similarity_threshold=0.0)
            for hit in hits:
                assert hit["object_id"].startswith("orders.public.orders.order_id")
            # A 与 B 的同名列 object_id 相同——按源过滤后 A 只能拿到 1 条
            assert len([h for h in hits if h["object_type"] == "column"]) == 1
        finally:
            a.close()

    def test_dim_values_scoped(self, seeded_collections):
        cfg = seeded_collections
        a = _retrieval(cfg, SOURCE_A)
        b = _retrieval(cfg, SOURCE_B)
        try:
            hits_a = a.search_dim_values("paid", top_k=10, min_score=0.0)
            assert {h["matched_text"] for h in hits_a} == {"paid-A"}
            hits_b = b.search_dim_values("paid", top_k=10, min_score=0.0)
            assert {h["matched_text"] for h in hits_b} == {"paid-B"}
        finally:
            a.close()
            b.close()

    def test_similar_sqls_scoped(self, seeded_collections):
        cfg = seeded_collections
        a = _retrieval(cfg, SOURCE_A)
        try:
            hits = a.search_similar_sqls(_VEC_A, top_k=10, similarity_threshold=0.0)
            assert [h["sql"] for h in hits] == ["SELECT 1"]
        finally:
            a.close()

    def test_fetch_table_cards_exact_lookup_scoped(self, seeded_collections):
        """精确回查同名 object_id 只返回本源卡片（doc 4 §3.1）。"""
        cfg = seeded_collections
        a = _retrieval(cfg, SOURCE_A)
        b = _retrieval(cfg, SOURCE_B)
        try:
            cards_a = a.fetch_table_cards(["orders.public.orders"])
            assert cards_a["orders.public.orders"]["text_raw"] == "A 源订单表"
            cards_b = b.fetch_table_cards(["orders.public.orders"])
            assert cards_b["orders.public.orders"]["text_raw"] == "B 源订单表"
            cats_a = a.fetch_table_categories(["orders.public.orders"])
            assert cats_a == {"orders.public.orders": "订单"}
        finally:
            a.close()
            b.close()

    def test_missing_source_returns_empty_not_other_source(self, seeded_collections):
        """不存在的源返回空/未就绪，不回退为无源过滤、不返回他源数据。"""
        cfg = seeded_collections
        missing = _retrieval(cfg, MISSING_SOURCE)
        try:
            assert (
                missing.search_tables(_VEC_A, top_k=10, similarity_threshold=0.0) == []
            )
            assert missing.fetch_table_cards(["orders.public.orders"]) == {}
            assert (
                missing.search_similar_sqls(_VEC_A, top_k=10, similarity_threshold=0.0)
                == []
            )
        finally:
            missing.close()

    def test_concurrent_instances_independent(self, seeded_collections):
        """同源并发实例独立 alias；close 一个不影响另一个（doc 4 §3.3）。"""
        cfg = seeded_collections
        first = _retrieval(cfg, SOURCE_A)
        second = _retrieval(cfg, SOURCE_A)
        assert first._alias != second._alias
        first.close()
        # 第二个实例仍可检索
        hits = second.search_tables(_VEC_A, top_k=5, similarity_threshold=0.0)
        assert hits
        second.close()

    def test_uuid_source_id_rejected_at_construction(self, seeded_collections):
        """doc 12.2 M-1：UUID 属非法来源——构造即拒绝，不归一为短 ID。"""
        cfg = seeded_collections
        with pytest.raises(ValueError, match="短 ID"):
            _retrieval(cfg, UUID_SOURCE)
