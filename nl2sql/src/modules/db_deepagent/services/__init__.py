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
    """服务容器（§6 字段全集；步骤 3+ 的字段在交付前为 None）

    source_id 为本实例绑定的平台数据源（规范化 UUID 字符串，doc 4 §3.2/§4.7）：
    构造后不可变更，Milvus 检索与 Neo4j 图查询均按其过滤/定位。
    """

    config: DbAgentConfig
    business_db: BusinessDbService
    embedding: DbEmbeddingClient
    milvus: DbMilvusRetrieval
    catalog: TableCatalog
    join_graph: JoinGraphService
    embedding_release: dict[str, Any]
    source_id: str
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


def _normalized_source_id(source_id) -> str:
    """校验并规范化服务绑定的 source_id（doc 4 §3.2：缺失/非法拒绝构造）。"""
    import uuid as _uuid

    if isinstance(source_id, _uuid.UUID):
        return str(source_id)
    if not isinstance(source_id, str) or not source_id.strip():
        raise ValueError(
            "build_services 需要 source_id（平台登记 UUID）：来源缺失时拒绝构造，"
            "不得回退为无源过滤的检索"
        )
    try:
        return str(_uuid.UUID(source_id.strip()))
    except ValueError:
        raise ValueError("source_id 不是合法 UUID，拒绝构造服务") from None


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


def build_services(
    cfg: DbAgentConfig, *, source_id=None, verify_embedding: bool = True
) -> DbAgentServices:
    """按依赖顺序构造已交付的服务并执行 Embedding 一致性检查（§6.3）。

    source_id（doc 4 §3.2/§4.7）为必填的服务端绑定：会话启动层读取并校验
    持久化的会话绑定后传入，Milvus 检索与 Neo4j 图查询构造为来源不可变
    服务；按源运行配置（database/expected_database/table_key_prefix）必须
    在本调用之前构造完成（``apply_source_runtime_config``）。

    verify_embedding=False 跳过一致性检查（不再发起 embed_query 探针），
    仅限调用方已在近期以同一配置核验过的链路使用（Web 执行链复用
    readiness 的 300 秒探针缓存，doc 2 §5.5.2）。
    """
    normalized_source = _normalized_source_id(source_id)
    # 构造失败的资源回收（doc 4 §4.7：每轮独立构造的 Web 模式下，部分构造
    # 的连接不得泄漏）：按构造顺序累积，任一步失败按逆序关闭已创建资源
    business_db = BusinessDbService(cfg.business_db)
    milvus: DbMilvusRetrieval | None = None
    join_graph: JoinGraphService | None = None
    try:
        business_db.open()

        profile, api_key = _embedding_profile_and_key(cfg)
        embedding = DbEmbeddingClient(
            profile,
            api_key=api_key,
            request_timeout_s=cfg.embedding.request_timeout_seconds,
        )
        milvus = DbMilvusRetrieval(cfg, embedding, source_id=normalized_source)
        if verify_embedding:
            release = check_embedding_consistency(cfg, profile, embedding, milvus)
        else:
            # 跳过检查：调用方（Web 执行链）已在同一请求内经 readiness
            # 探针核验（300 秒缓存），避免每题重复一次 embed_query 模型请求
            release = {}

        catalog = TableCatalog(cfg, milvus, business_db)
        join_graph = JoinGraphService(
            _neo4j_manager(cfg),
            source_id=normalized_source,
            join_max_hops=cfg.neo4j.join_max_hops,
        )

        from src.modules.db_deepagent.services.result_analysis import (
            ResultAnalysisService,
        )

        validator = DbSqlValidator(
            cfg.validation,
            business_db,
            explain_timeout_s=cfg.business_db.explain_timeout_seconds,
        )
        guard = SqlEvidenceGuard(
            cfg.validation.guard, business_db, DimensionProbeBuilder()
        )
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

        logger.info(
            "DbAgentServices 已构造（步骤 5 全集，source_id=%s）", normalized_source
        )
        return DbAgentServices(
            config=cfg,
            business_db=business_db,
            embedding=embedding,
            milvus=milvus,
            catalog=catalog,
            join_graph=join_graph,
            embedding_release=release,
            source_id=normalized_source,
            validator=validator,
            guard=guard,
            pipeline=pipeline,
            time_resolver=TimeResolver(),
            probe=DimensionProbeBuilder(),
            results=results,
            analysis=ResultAnalysisService(cfg, results),
            budget=budget,
        )
    except BaseException:
        # 逆序释放已构造资源（join_graph → milvus → business_db），释放异常
        # 不覆盖原始错误
        for resource in (join_graph, milvus, business_db):
            if resource is None:
                continue
            try:
                resource.close()
            except Exception:  # noqa: BLE001
                logger.warning("构造失败回收时关闭 %s 出错", type(resource).__name__)
        raise
