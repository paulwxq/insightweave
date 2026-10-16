"""§5.2 JOIN 链路两级断言。

- 契约级：neo4j_client.plan_join_paths（orders → products → categories 2 跳）
  与 check_table_exists 三段键查询
- 链路级：SchemaRetriever.retrieve() 输出的 join_plans[0]["edges"] 非空
  （守住"候选表键段数与图内 Table.id 一致"这一唯一可能静默失败的契约）
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.services.config_loader import load_subgraph_config
from src.services.db.neo4j_client import get_neo4j_client
from src.tools.schema_retrieval.retriever import SchemaRetriever

# ---- 契约级 ----
neo = get_neo4j_client()

assert neo.check_table_exists("orders.public.orders") is True, (
    "check_table_exists('orders.public.orders') 应为 True"
)

plans = neo.plan_join_paths(
    base_tables=["orders.public.orders"],
    target_tables=["orders.public.categories"],
    max_hops=5,
)
assert plans, "plan_join_paths 返回空"
edges = plans[0].get("edges") or []
assert edges, "plan_join_paths 边为空"

# 路径表序列应为 orders → products → categories
seq = [edges[0]["src_table"]] + [e["dst_table"] for e in edges]
expected = [
    "orders.public.orders",
    "orders.public.products",
    "orders.public.categories",
]
assert seq == expected, f"路径序列不符: {seq}"
print("契约级 OK: 路径", " -> ".join(p.split(".", 1)[1] for p in seq))

# ---- 链路级 ----
# 2026-10-12 根因修复：查询侧 embedding 模型已切换为与 RAG 发布侧一致的
# qwen3.7-text-embedding（config.yaml embedding_profiles.active），自相似恢复
# 1.0。此处直接使用生产子图配置（默认阈值 0.45），按真实召回路径验证。
subgraph_cfg = load_subgraph_config("sql_generation")
retriever = SchemaRetriever(config=subgraph_cfg)
result = retriever.retrieve("查询各商品类别的销售额")

join_plans = result.get("join_plans") or []
assert join_plans, "retrieve() 未产出 JOIN 计划"
assert join_plans[0].get("edges"), "链路级 join_plans[0].edges 为空"

# 卡片补全核查（§4.4 顺带）：JOIN 涉及表是否都拿到了卡片
cards = result.get("table_cards") or {}
join_tables = set()
for p in join_plans:
    if p.get("base"):
        join_tables.add(p["base"])
    for e in p.get("edges") or []:
        join_tables.add(e["src_table"])
        join_tables.add(e["dst_table"])
missing = [t for t in sorted(join_tables) if t not in cards]
print("链路级 OK: join edges =", len(join_plans[0]["edges"]))
print("JOIN 涉及表:", sorted(join_tables))
print("其中缺卡片的表:", missing if missing else "（无）")

print("\n=== §5.2 冒烟全部通过 ===")
