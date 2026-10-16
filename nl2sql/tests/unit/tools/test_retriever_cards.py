"""retriever 表卡片入参补全测试（§4.4 + §5.4）。

缺口：fetch_table_cards 入参原只含事实表+维度表，桥接表与 JOIN 计划
新增的中间表被排除 —— 与规则 5（只允许使用"表结构"中列出的字段）冲突。

固定输入（不依赖真实召回）：
- 候选表 = [orders.public.orders, orders.public.categories]（products 不在候选中）
- join_plans = 实测 2 跳路径 orders → products → categories
"""

from typing import Any, Dict, List

from src.tools.schema_retrieval.retriever import SchemaRetriever


def _two_hop_join_plans() -> List[Dict[str, Any]]:
    """实测 2 跳路径：orders → products → categories"""
    return [
        {
            "base": "orders.public.orders",
            "targets": ["orders.public.categories"],
            "edges": [
                {
                    "src_table": "orders.public.orders",
                    "dst_table": "orders.public.products",
                    "on": "orders.product_id = products.id",
                },
                {
                    "src_table": "orders.public.products",
                    "dst_table": "orders.public.categories",
                    "on": "products.category_id = categories.id",
                },
            ],
        }
    ]


def _candidate_set(
    fact: List[str], dim: List[str], bridge: List[str]
) -> Dict[str, Any]:
    return {
        "candidate_fact_tables": fact,
        "candidate_dim_tables": dim,
        "candidate_bridge_tables": bridge,
        "table_similarities": {},
    }


class TestCollectCardTableIds:
    """§4.4：候选三表 + JOIN 计划涉及表，全部进入卡片查询入参。"""

    def test_join_intermediate_table_included(self):
        """JOIN 中间表（products）补入卡片入参 —— 即使不在向量候选中"""
        candidate_set = _candidate_set(
            fact=["orders.public.orders"],
            dim=["orders.public.categories"],
            bridge=[],
        )
        ids = SchemaRetriever._collect_card_table_ids(
            candidate_set, _two_hop_join_plans()
        )
        assert "orders.public.products" in ids
        # 候选表保持原序在前，JOIN 表按计划遍历顺序追加
        assert ids[:2] == ["orders.public.orders", "orders.public.categories"]
        assert ids[2] == "orders.public.products"

    def test_bridge_tables_included(self):
        """桥接表补入（原本被排除）"""
        candidate_set = _candidate_set(
            fact=["orders.public.orders"],
            dim=[],
            bridge=["orders.public.order_items"],
        )
        ids = SchemaRetriever._collect_card_table_ids(candidate_set, [])
        assert "orders.public.order_items" in ids

    def test_dedup_preserves_first_occurrence(self):
        """JOIN base 与候选表重复时去重，且候选表位次在前"""
        candidate_set = _candidate_set(
            fact=["orders.public.orders"],
            dim=["orders.public.categories"],
            bridge=[],
        )
        ids = SchemaRetriever._collect_card_table_ids(
            candidate_set, _two_hop_join_plans()
        )
        assert ids.count("orders.public.orders") == 1
        assert ids.count("orders.public.categories") == 1
        assert ids.count("orders.public.products") == 1

    def test_empty_inputs(self):
        """无候选且无 JOIN 计划时返回空列表"""
        candidate_set = _candidate_set(fact=[], dim=[], bridge=[])
        assert SchemaRetriever._collect_card_table_ids(candidate_set, []) == []
