"""DbAgentServices 服务容器（docs/v4/5 §6）

按依赖顺序构造，进程内每个 CLI 会话共享一份；close() 释放连接池、Milvus 连接、
Neo4j 驱动。服务不读取模型提供的连接参数。步骤 5 的 analysis 在其交付步骤补入，
字段先以 None 占位。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from src.modules.db_deepagent.config import DbAgentConfig
from src.modules.db_deepagent.services.business_db import BusinessDbService
from src.modules.db_deepagent.services.dimension_probe import DimensionProbeBuilder
from src.modules.db_deepagent.services.embedding import DbEmbeddingClient
from src.modules.db_deepagent.services.embedding_consistency import (
    check_embedding_consistency,
)
from src.modules.db_deepagent.services.join_graph import JoinGraphService
from src.modules.db_deepagent.services.milvus_retrieval import DbMilvusRetrieval
from src.modules.db_deepagent.services.result_store import ResultStore
from src.modules.db_deepagent.services.run_budget import RunBudgetLedger
from src.modules.db_deepagent.services.sql_guard import SqlEvidenceGuard
from src.modules.db_deepagent.services.sql_pipeline import SqlPipeline
from src.modules.db_deepagent.services.sql_validation import DbSqlValidator
from src.modules.db_deepagent.services.table_catalog import TableCatalog
from src.modules.db_deepagent.services.time_resolver import TimeResolver
from src.services.config_loader import get_config
from src.utils.logger import get_module_logger

logger = get_module_logger("nl2sql.db_deepagent.services")


@dataclass
class DbAgentServices:
    """服务容器（§6 字段全集；步骤 3+ 的字段在交付前为 None）"""

    config: DbAgentConfig
    business_db: BusinessDbService
    embedding: DbEmbeddingClient
    milvus: DbMilvusRetrieval
    catalog: TableCatalog
    join_graph: JoinGraphService
    embedding_release: dict[str, Any]
    validator: Optional[DbSqlValidator] = None
    guard: Optional[SqlEvidenceGuard] = None
    pipeline: Optional[SqlPipeline] = None
    time_resolver: Optional[TimeResolver] = None
    probe: Optional[DimensionProbeBuilder] = None
    results: Optional[ResultStore] = None
    analysis: Optional[Any] = None  # ResultAnalysisService（步骤 5 交付）
    budget: Optional[RunBudgetLedger] = None

    def close(self) -> None:
        """按依赖逆序释放已构造的资源。"""
        if self.join_graph is not None:
            self.join_graph.close()
        if self.milvus is not None:
            self.milvus.close()
        if self.business_db is not None:
            self.business_db.close()


def _embedding_profile_and_key(cfg: DbAgentConfig) -> tuple[dict[str, Any], str]:
    """取全局 embedding_profiles[<配置名>] 与其 provider 的 api_key（§6.2）。"""
    profiles = get_config().get("embedding_profiles", {}) or {}
    profile = dict(profiles.get(cfg.embedding.profile) or {})
    if not profile:
        raise ValueError(f"embedding_profiles.{cfg.embedding.profile} 不存在或为空")
    provider_name = profile.get("provider")
    providers = get_config().get("llm_providers", {}) or {}
    api_key = (providers.get(provider_name) or {}).get("api_key")
    if not api_key:
        raise ValueError(
            f"llm_providers.{provider_name} 缺少 api_key，无法构造 Embedding 客户端"
        )
    return profile, api_key


def _neo4j_manager(cfg: DbAgentConfig):
    from src.services.db.neo4j_connection import Neo4jConnectionManager

    return Neo4jConnectionManager(
        {
            "uri": cfg.neo4j.uri,
            "user": cfg.neo4j.user,
            "password": cfg.neo4j.password,
            "database": cfg.neo4j.database,
            "connection_timeout": cfg.neo4j.connection_timeout_seconds,
        }
    )


def build_services(cfg: DbAgentConfig) -> DbAgentServices:
    """按依赖顺序构造已交付的服务并执行 Embedding 一致性检查（§6.3）。"""
    business_db = BusinessDbService(cfg.business_db)
    business_db.open()

    profile, api_key = _embedding_profile_and_key(cfg)
    embedding = DbEmbeddingClient(
        profile,
        api_key=api_key,
        request_timeout_s=cfg.embedding.request_timeout_seconds,
    )
    milvus = DbMilvusRetrieval(cfg, embedding)
    release = check_embedding_consistency(cfg, profile, embedding, milvus)

    catalog = TableCatalog(cfg, milvus, business_db)
    join_graph = JoinGraphService(
        _neo4j_manager(cfg), join_max_hops=cfg.neo4j.join_max_hops
    )

    from src.modules.db_deepagent.services.result_analysis import ResultAnalysisService

    validator = DbSqlValidator(
        cfg.validation,
        business_db,
        explain_timeout_s=cfg.business_db.explain_timeout_seconds,
    )
    guard = SqlEvidenceGuard(cfg.validation.guard, business_db, DimensionProbeBuilder())
    results = ResultStore(Path(cfg.workspace.root))
    budget = RunBudgetLedger()
    pipeline = SqlPipeline(
        config=cfg,
        validator=validator,
        guard=guard,
        db=business_db,
        results=results,
        budget=budget,
    )

    logger.info("DbAgentServices 已构造（步骤 5 全集）")
    return DbAgentServices(
        config=cfg,
        business_db=business_db,
        embedding=embedding,
        milvus=milvus,
        catalog=catalog,
        join_graph=join_graph,
        embedding_release=release,
        validator=validator,
        guard=guard,
        pipeline=pipeline,
        time_resolver=TimeResolver(),
        probe=DimensionProbeBuilder(),
        results=results,
        analysis=ResultAnalysisService(cfg, results),
        budget=budget,
    )
