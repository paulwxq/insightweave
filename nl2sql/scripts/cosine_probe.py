"""列注释相似度探针：用与系统相同的 embedding 配置复现列级召回分数。

背景：
    排查 "public.test1 为何出现在 SQL 生成提示词" 时确认：列级向量检索
    （table_schema_embeddings 中 object_type='column' 的记录）比对的文本
    是列记录的 object_desc 字段，即裸列注释（如 "测试值"）；Milvus 的
    COSINE 度量等价于裸余弦相似度。本脚本在不连接 Milvus 的前提下，
    用同一个 embedding 模型现场计算"查询文本 vs 列注释文本"的余弦
    相似度，用于：
    1. 复现/验证检索日志中的相似度分数（embedding 具确定性，可逐位对齐
       日志中 search_columns / column dimension 回填的 hit.distance）；
    2. 评估 similarity_threshold 取值是否合理（观察相关/无关文本的分数
       带宽，实测泛化短注释如 "测试值" 对任意业务问句可达 0.55+）；
    3. embedding 模型切换后观察分数分布变化。

用法：
    # 默认：复现 test1 案例（"客户"/完整问题 vs 三个列注释）
    uv run python scripts/cosine_probe.py

    # 自定义：第一个参数为查询文本，其后为若干对照文本（列注释）
    uv run python scripts/cosine_probe.py "客户" "测试值" "用户名称"

依赖：
    仅 embedding 客户端（config.yaml 的 embedding.active，当前
    qwen3.7-text-embedding，1024 维），无向量库依赖。
"""

import argparse
import math
import sys
from pathlib import Path

# 允许从任意 cwd 运行：把项目根加入 sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.services.embedding.embedding_client import get_embedding_client

# 默认复现用例：2026-10-12 q_be442260（"请共有多少个客户"）检索日志中的
# 三个列注释，期望值 0.606 / 0.598 / 0.659（"客户" 侧）与 0.550 附近（问题侧）
DEFAULT_QUERY_TEXTS = ["客户", "请共有多少个客户"]
DEFAULT_TARGET_TEXTS = ["测试值", "测试ID", "用户名称"]


def cosine(a: list[float], b: list[float]) -> float:
    """计算两个向量的余弦相似度（与 Milvus COSINE 度量同口径）。"""
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    return dot / (norm_a * norm_b)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="计算查询文本与各列注释文本的余弦相似度（同系统 embedding 配置）",
    )
    parser.add_argument(
        "texts",
        nargs="*",
        help="可选：查询文本 + 若干对照文本；缺省时使用内置 test1 复现用例",
    )
    args = parser.parse_args()

    if args.texts:
        if len(args.texts) < 2:
            parser.error("至少需要两个参数：查询文本 与 至少一个对照文本")
        query_texts, target_texts = [args.texts[0]], args.texts[1:]
    else:
        query_texts, target_texts = DEFAULT_QUERY_TEXTS, DEFAULT_TARGET_TEXTS

    emb = get_embedding_client()
    vectors = {t: emb.embed_query(t) for t in query_texts + target_texts}

    print(f"向量维度: {len(next(iter(vectors.values())))}\n")
    for query in query_texts:
        print(f"查询文本 {query!r} 与各对照文本的余弦相似度:")
        for target in target_texts:
            score = cosine(vectors[query], vectors[target])
            print(f"  cos({query!r}, {target!r}) = {score:.4f}")
        print()


if __name__ == "__main__":
    main()
