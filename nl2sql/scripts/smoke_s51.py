"""§5.1 集成冒烟：直连 Milvus，依次调用 6 个方法并断言键名契约。

断言要点（docs/upgrade/1_Milvus结构变更与访问代码适配改造方案.md §5.1）：
- 不抛 65535（字段不存在）
- search_tables：含 object_id、不含 table_name、grain_hint is None、表键三段
- search_columns：含 object_id 与 parent_id、不含 table_name/table_category、grain_hint is None
- fetch_table_cards(['orders.public.categories']) 命中并返回 text_raw
- search_dim_values：非空，dim_table 恰好三段且各段非空，与表级记录 object_id 一致
- search_similar_sqls 正常返回
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.services.embedding.embedding_client import get_embedding_client
from src.services.vector_adapter import create_vector_search_adapter

adapter = create_vector_search_adapter()
emb_client = get_embedding_client()
q_emb = emb_client.embed_query("查询各商品类别的销售额")

# ---- 全量表级记录 object_id 集合（用于 dim_table 一致性校验）----
table_coll = adapter._get_table_schema_collection()
table_rows = table_coll.query(
    expr='object_type == "table"', output_fields=["object_id"], limit=100
)
all_table_object_ids = {r["object_id"] for r in table_rows}
print(f"表级记录 {len(all_table_object_ids)} 条: {sorted(all_table_object_ids)}")

# 2026-10-12 根因修复：查询侧 embedding 模型已切换为与 RAG 发布侧一致的
# qwen3.7-text-embedding（config.yaml embedding_profiles.active），自相似恢复
# 1.0。以下按生产默认阈值验证真实召回与键名契约。
SMOKE_THRESHOLD = 0.45

# ---- 1. search_tables ----
tables = adapter.search_tables(embedding=q_emb, top_k=10, similarity_threshold=SMOKE_THRESHOLD)
assert tables, "search_tables 空结果"
_max_sim = max(t["similarity"] for t in tables)
if _max_sim < 0.2:
    print(f"⚠️ 警告: 最高相似度 {_max_sim:.4f} << 0.2，向量空间疑似不匹配（数据侧待重灌）")
for t in tables:
    assert t.get("object_id"), f"缺 object_id: {t}"
    assert "table_name" not in t, f"不应含 table_name: {t}"
    assert t.get("grain_hint") is None, f"grain_hint 应为 None: {t}"
    assert t["object_id"].count(".") == 2, f"表键应三段: {t['object_id']}"
print("search_tables OK:", [t["object_id"] for t in tables])

# ---- 2. search_columns ----
cols = adapter.search_columns(embedding=q_emb, top_k=10, similarity_threshold=SMOKE_THRESHOLD)
assert cols, "search_columns 空结果"
for c in cols:
    assert c.get("object_id") and c.get("parent_id"), f"缺两键: {c}"
    assert "table_name" not in c and "table_category" not in c, f"不应含旧键: {c}"
    assert c.get("grain_hint") is None, f"grain_hint 应为 None: {c}"
    assert c["parent_id"].count(".") == 2, f"parent_id 应三段: {c['parent_id']}"
    assert c["parent_id"] in all_table_object_ids, (
        f"parent_id 不在表级记录集合: {c['parent_id']}"
    )
print("search_columns OK:", [(c["object_id"], c["parent_id"]) for c in cols][:5], "...")

# ---- 3. fetch_table_cards ----
cards = adapter.fetch_table_cards(["orders.public.categories"])
assert "orders.public.categories" in cards, f"卡片未命中: {list(cards)}"
card = cards["orders.public.categories"]
assert card.get("text_raw"), f"text_raw 为空: {card}"
print("fetch_table_cards OK: text_raw 头部 =", card["text_raw"][:60].replace("\n", " "))

# ---- 4. search_dim_values（用已发布维度值作输入，保证非空）----
dim_coll = adapter._get_dim_value_collection()
dim_rows = dim_coll.query(
    expr='col_id != ""', output_fields=["col_value"], limit=5
)
assert dim_rows, "dim_value_embeddings 无数据"
sample_value = dim_rows[0]["col_value"]
print(f"取样维度值: {sample_value!r}")
dims = adapter.search_dim_values(query_value=sample_value, top_k=5)
assert dims, "search_dim_values 空结果（校验无效）"
for d in dims:
    dt = d.get("dim_table") or ""
    parts = dt.split(".")
    assert len(parts) == 3 and all(parts), f"dim_table 应恰好三段非空: {dt!r}"
    assert dt in all_table_object_ids, f"dim_table 不在表级记录集合: {dt}"
print("search_dim_values OK:", [(d["dim_table"], d["matched_text"]) for d in dims])

# ---- 5. search_similar_sqls ----
sqls = adapter.search_similar_sqls(embedding=q_emb, top_k=3, similarity_threshold=SMOKE_THRESHOLD)
print("search_similar_sqls OK:", len(sqls), "条（无 65535、正常返回即通过）")

print("\n=== §5.1 冒烟全部通过 ===")
