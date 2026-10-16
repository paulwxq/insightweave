"""retrieve() 卡片接线测试（§4.4 + §5.4，终审 Important-1 补守卫）。

固定输入（§5.4 场景，不依赖真实召回）：候选表 = orders + categories
（products 不在向量候选中），JOIN 计划 = 2 跳 orders → products → categories。
断言 retrieve() 真实链路把候选三表 + JOIN 涉及表送进 fetch_table_cards，
且 JOIN 中间表拿到卡片 —— 守住 §4.4 的两行接线不被无声回退
（仅测 _collect_card_table_ids 纯函数时，回退接线不会导致任何测试变红）。
"""

from typing import List
from unittest.mock import Mock

from src.tools.schema_retrieval.retriever import SchemaRetriever

ORDERS = "orders.public.orders"
PRODUCTS = "orders.public.products"
CATEGORIES = "orders.public.categories"


def _two_hop_edges() -> List[dict]:
    return [
        {
            "src_table": ORDERS,
            "dst_table": PRODUCTS,
            "on": "orders.product_id = products.id",
        },
        {
            "src_table": PRODUCTS,
            "dst_table": CATEGORIES,
            "on": "products.category_id = categories.id",
        },
    ]


class _VectorStub:
    """vector_client 边界桩：记录 fetch_table_cards 调用参数。"""

    def __init__(self):
        self.cards_calls: List[List[str]] = []

    def search_tables(self, **kwargs):
        return [
            {
                "object_id": ORDERS,
                "object_type": "table",
                "similarity": 0.9,
                "grain_hint": None,
                "time_col_hint": None,
                "table_category": "fact",
            },
            {
                "object_id": CATEGORIES,
                "object_type": "table",
                "similarity": 0.8,
                "grain_hint": None,
                "time_col_hint": None,
                "table_category": "dim",
            },
        ]

    def search_columns(self, **kwargs):
        return []

    def search_dim_values(self, **kwargs):
        return []

    def search_similar_sqls(self, **kwargs):
        return []

    def fetch_table_categories(self, table_names):
        return {}

    def fetch_table_cards(self, table_names):
        self.cards_calls.append(list(table_names))
        return {
            tid: {
                "object_id": tid,
                "text_raw": f"{tid} 字段说明",
                "grain_hint": None,
                "time_col_hint": None,
                "table_category": "",
            }
            for tid in table_names
        }


class _Neo4jStub:
    """neo4j_client 边界桩：对 base=orders 返回固定 2 跳路径。"""

    def __init__(self, edges):
        self.edges = edges

    def plan_join_paths(self, base_tables, target_tables, max_hops=5, strategy=None):
        assert base_tables == [ORDERS], f"意外 base: {base_tables}"
        return [{"base": ORDERS, "targets": list(target_tables), "edges": self.edges}]


def _retriever(neo4j, vector) -> SchemaRetriever:
    """跳过 __init__（不连真实服务），在客户端边界注入桩。"""
    r = object.__new__(SchemaRetriever)
    r.config = {"schema_retrieval": {}}
    r.pg_client = Mock()
    r.neo4j_client = neo4j
    r.embedding_client = Mock()
    r.embedding_client.embed_query.return_value = [0.1] * 1024
    r.vector_client = vector
    r.table_schema_top_k = 10
    r.table_schema_similarity_threshold = 0.45
    r.column_schema_top_k = 10
    r.column_schema_similarity_threshold = 0.45
    r.column_backfill_top_k = 10
    r.column_backfill_similarity_threshold = 0.65
    r.dim_value_top_k = 3
    r.dim_value_similarity_threshold = 0.75
    r.join_max_hops = 5
    r._category_mapping_lower = {
        "fact": {"fact"},
        "dimension": {"dim"},
        "bridge": set(),
    }
    return r


class TestRetrieveCardWiring:
    def test_join_intermediate_table_gets_card_via_retrieve(self):
        """§5.4 断言①②：retrieve() 把 JOIN 中间表送进 fetch_table_cards 并拿到卡片"""
        vector = _VectorStub()
        retriever = _retriever(_Neo4jStub(_two_hop_edges()), vector)

        result = retriever.retrieve("查询各商品类别的销售额")

        # 断言①：fetch_table_cards 调用参数包含 JOIN 中间表与候选表
        assert vector.cards_calls, "fetch_table_cards 未被调用"
        call = vector.cards_calls[0]
        assert PRODUCTS in call, f"JOIN 中间表未进入卡片查询: {call}"
        assert ORDERS in call and CATEGORIES in call

        # 断言②前提：返回的 table_cards 含中间表卡片（提示词表结构段将渲染其字段说明）
        assert PRODUCTS in result["table_cards"]
        assert result["table_cards"][PRODUCTS]["text_raw"]

        # 链路完整性：JOIN 计划边非空（2 跳路径完整保留）
        assert result["join_plans"], "未产出 JOIN 计划"
        assert len(result["join_plans"][0]["edges"]) == 2
