"""Embedding 一致性检查集成测试（docs/v4/5 §6.3）

正向用例需要真实 Milvus（--run-db）；负向用例（发布记录缺失）纯本地。
"""

from __future__ import annotations

import pytest

from src.modules.db_deepagent.config import DbAgentConfig
from src.modules.db_deepagent.errors import EmbeddingConsistencyError

pytestmark = [pytest.mark.unit, pytest.mark.integration]

# 一致性检查不依赖具体来源，用占位短 ID 满足构造绑定（doc 4 §3 + doc 5.1 §1）
_TEST_SOURCE_ID = "db_src_1"


@pytest.fixture()
def _release_info(db_agent_config: DbAgentConfig):
    """构造真实 Embedding/Milvus 客户端所需的 profile 与 key。"""
    from src.modules.db_deepagent.services import _embedding_profile_and_key

    return _embedding_profile_and_key(db_agent_config)


def test_release_record_missing_fails(
    db_agent_config: DbAgentConfig, config_dict, _release_info
):
    """release_record 指向不存在的文件：EmbeddingConsistencyError，字段路径在消息中。"""
    from src.modules.db_deepagent.config import DbAgentConfig as _Cfg
    from src.modules.db_deepagent.services.embedding_consistency import (
        check_embedding_consistency,
    )

    profile, _api_key = _release_info
    config_dict["embedding"]["release_record"] = "/nonexistent/release.yaml"
    cfg = _Cfg.model_validate(config_dict)
    with pytest.raises(EmbeddingConsistencyError, match="发布记录不存在"):
        check_embedding_consistency(cfg, profile, embedding=None, milvus=None)


@pytest.mark.requires_db
def test_consistency_passes_against_live_milvus(
    db_agent_config: DbAgentConfig, _release_info
):
    """真实环境：发布记录 ↔ 全局 profile ↔ Milvus 集合 三方一致。"""
    from src.modules.db_deepagent.services.embedding import DbEmbeddingClient
    from src.modules.db_deepagent.services.embedding_consistency import (
        check_embedding_consistency,
    )
    from src.modules.db_deepagent.services.milvus_retrieval import DbMilvusRetrieval

    profile, api_key = _release_info
    embedding = DbEmbeddingClient(
        profile,
        api_key=api_key,
        request_timeout_s=db_agent_config.embedding.request_timeout_seconds,
    )
    milvus = DbMilvusRetrieval(db_agent_config, embedding, source_id=_TEST_SOURCE_ID)
    try:
        release = check_embedding_consistency(
            db_agent_config, profile, embedding, milvus
        )
    finally:
        milvus.close()
    assert release["release_batch"]  # 发布批次非空
    assert release["model"]
    assert release["dimensions"] > 0
    assert set(release["collections"]) == {
        db_agent_config.milvus.collections.table_schema,
        db_agent_config.milvus.collections.dim_value,
        db_agent_config.milvus.collections.sql_example,
    }
