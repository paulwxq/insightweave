"""真实 DashScope embedding 探针（doc 1 §6.5；I1-03 归档补建）。

历史说明：升级实施时（2026-11-09 18:xx）该验证以内联脚本执行、未落盘，
结果记载于 implementation README §D7；本轮应主协调者第 1 轮 I1-03 要求
补建可重放脱敏脚本并复跑一次（本次新增执行，输出见 embedding-fernet-probes.log）。

行为：经生产同源路径（load_db_agent_config → _embedding_profile_and_key →
DbEmbeddingClient）发起一次真实 TextEmbedding 调用；只打印模型名/维度/
非零分量数/耗时，不打印 api_key 或向量内容。
"""

from __future__ import annotations

import sys
import time

sys.path.insert(0, "/opt/projects/insightweave/nl2sql")

PROBE_TEXT = "升级验收 embedding 探针：数据表与字段语义检索抽样句子。"


def main() -> None:
    import importlib.metadata as im

    from src.modules.db_deepagent.config import load_db_agent_config
    from src.modules.db_deepagent.services import _embedding_profile_and_key
    from src.modules.db_deepagent.services.embedding import DbEmbeddingClient

    print(f"dashscope={im.version('dashscope')}")
    cfg = load_db_agent_config()
    profile, _api_key = _embedding_profile_and_key(cfg)
    client = DbEmbeddingClient(
        profile,
        api_key=_api_key,
        request_timeout_s=cfg.embedding.request_timeout_seconds,
    )
    print(f"model={client.model} expected_dimensions={client.dimensions}")
    started = time.monotonic()
    vector = client.embed_query(PROBE_TEXT)
    elapsed_ms = int((time.monotonic() - started) * 1000)
    assert len(vector) == client.dimensions, (
        f"返回维度 {len(vector)} != 配置 {client.dimensions}"
    )
    nonzero = sum(1 for v in vector if v != 0.0)
    assert nonzero > 0, "向量全零，疑似异常响应"
    print(
        f"embed_query OK len={len(vector)} nonzero={nonzero} "
        f"elapsed_ms={elapsed_ms}"
    )
    print("embedding probe PASSED（未打印 api_key 与向量内容）")


if __name__ == "__main__":
    main()
