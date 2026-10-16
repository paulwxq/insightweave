"""DbEmbeddingClient：显式 profile 与 api_key 的 Embedding 客户端（docs/v4/5 §6.2）

复制自 src/services/embedding/embedding_client.py，改造点：
- 构造签名 DbEmbeddingClient(profile, api_key, request_timeout_s)：profile 来自全局
  embedding_profiles[<配置名>]，api_key 来自 llm_providers[profile.provider].api_key
- 调用 TextEmbedding.call(..., api_key=api_key, request_timeout=request_timeout_s)（C12），
  不写 dashscope.api_key 全局变量
- 去掉 print 改用日志；返回向量长度与 dimensions 不一致抛 EmbeddingDimensionError
- 重试逻辑沿用原实现（指数退避，max_retries 来自 profile）
"""

from __future__ import annotations

import time
from typing import Any

from dashscope import TextEmbedding

from src.modules.db_deepagent.errors import EmbeddingDimensionError
from src.utils.logger import get_module_logger

logger = get_module_logger("nl2sql.db_deepagent.embedding")


class DbEmbeddingClient:
    """查询侧 Embedding 客户端（服务端显式配置，不跟随 active）"""

    def __init__(self, profile: dict[str, Any], api_key: str, request_timeout_s: int):
        self.model = profile.get("model", "text-embedding-v3")
        self.dimensions = profile.get("dimensions", 1024)
        self.max_retries = profile.get("max_retries", 3)
        self.batch_size = profile.get("batch_size", 20)
        self.api_key = api_key
        self.request_timeout_s = request_timeout_s
        logger.info(
            "Embedding 客户端已初始化: model=%s dimensions=%s",
            self.model,
            self.dimensions,
        )

    def embed_query(self, text: str) -> list[float]:
        """单文本向量化；长度与 dimensions 不一致抛 EmbeddingDimensionError。"""
        if not text or not text.strip():
            raise ValueError("文本不能为空")
        return self.embed_documents([text])[0]

    def embed_documents(
        self, texts: list[str], batch_size: int | None = None
    ) -> list[list[float]]:
        """批量向量化（分批 + 重试，沿用原实现）。"""
        if not texts:
            return []
        size = batch_size or self.batch_size
        all_embeddings: list[list[float]] = []
        for i in range(0, len(texts), size):
            all_embeddings.extend(self._embed_batch(texts[i : i + size]))
        return all_embeddings

    def _embed_batch(self, texts: list[str]) -> list[list[float]]:
        """一批文本向量化（带重试与指数退避）。"""
        last_exception: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                response = TextEmbedding.call(
                    model=self.model,
                    input=texts,
                    dimension=self.dimensions,
                    api_key=self.api_key,
                    request_timeout=self.request_timeout_s,
                )
                if response.status_code != 200:
                    raise RuntimeError(
                        f"Embedding API 返回错误: status_code={response.status_code}, "
                        f"message={response.message}"
                    )
                embeddings = [
                    item["embedding"] for item in response.output["embeddings"]
                ]
                for vec in embeddings:
                    if len(vec) != self.dimensions:
                        raise EmbeddingDimensionError(
                            f"返回向量长度 {len(vec)} 与 profile dimensions {self.dimensions} 不一致"
                        )
                return embeddings
            except EmbeddingDimensionError:
                raise  # 维度不一致不是瞬时故障，不重试
            except Exception as exc:  # noqa: BLE001（沿用原实现的宽捕获 + 重试）
                last_exception = exc
                if attempt < self.max_retries - 1:
                    delay = 2**attempt
                    logger.warning(
                        "Embedding 失败（尝试 %d/%d），%ds 后重试: %s",
                        attempt + 1,
                        self.max_retries,
                        delay,
                        exc,
                    )
                    time.sleep(delay)
                else:
                    logger.error(
                        "Embedding 失败（已重试 %d 次）: %s", self.max_retries, exc
                    )
        raise last_exception  # type: ignore[misc]
