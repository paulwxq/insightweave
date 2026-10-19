"""配置模型、加载、校验、指纹（docs/v4/5 §4.2）

yaml（§4.1）经现有 ConfigLoader 读取（${VAR:default} 展开、根 .env 加载），
转为 Pydantic 模型 DbAgentConfig（extra="forbid"，yaml 出现未知键即失败）。
§4.2 检查表在加载阶段执行，任一失败抛 DbAgentConfigError；错误消息只含字段
名，不含密码。validation_fingerprint 在本模块计算；datasource_id 由
BusinessDbService 启动时从实际连接读出（§6.1），不由配置推断。
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from src.modules.db_deepagent.errors import DbAgentConfigError
from src.services.config_loader import ConfigLoader, get_config

# 合法 Schema 标识符（简单规则即可：本次只接受 db_deepagent 这类普通标识符）
_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

_CONFIG_PATH = Path(__file__).resolve().parent / "config" / "db_deepagent.yaml"


class _StrictModel(BaseModel):
    """extra="forbid" 基类：yaml 未知键在加载阶段即报错"""

    model_config = ConfigDict(extra="forbid")


class ModelConfig(_StrictModel):
    llm_profile: str
    overrides: dict[str, Any] = Field(default_factory=dict)
    expected_harness_key: str | None = None
    request_timeout_seconds: float = Field(gt=0)


class EmbeddingConfig(_StrictModel):
    profile: str
    release_record: str
    request_timeout_seconds: float = Field(gt=0)


class BusinessDbConfig(_StrictModel):
    host: str
    port: int = Field(gt=0)
    database: str
    user: str
    password: str
    # libpq sslmode；登记源注入时取登记值（doc 4 §4.7），yaml 缺省 prefer
    sslmode: str = "prefer"
    # 登记 selected_schemas（doc 4 §4.7）：非空时限制目录访问与表键归一
    # 的 schema 范围，并注入连接 search_path；None（yaml 缺省）不限制
    allowed_schemas: list[str] | None = None
    expected_database: str
    table_key_prefix: str
    timezone: str
    pool_min_size: int = Field(gt=0)
    pool_max_size: int = Field(gt=0)
    explain_timeout_seconds: float = Field(gt=0)
    statement_timeout_seconds: float = Field(gt=0)
    max_saved_result_rows: int = Field(gt=0)


class PlatformDbConfig(_StrictModel):
    # yaml 键为 schema；python 侧用 schema_name 避免遮蔽 BaseModel 的废弃 schema()
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    host: str
    port: int = Field(gt=0)
    database: str
    user: str
    password: str = ""
    expected_database: str
    schema_name: str = Field(alias="schema")
    sslmode: str = "disable"
    connect_timeout_seconds: float = Field(gt=0)
    statement_timeout_ms: int = Field(gt=0)
    pool_min_size: int = Field(gt=0)
    pool_max_size: int = Field(gt=0)


class MaintenanceConfig(_StrictModel):
    statement_timeout_ms: int = Field(gt=0)
    lock_timeout_ms: int = Field(gt=0)


class Neo4jConfig(_StrictModel):
    uri: str
    user: str
    password: str = ""
    database: str
    join_max_hops: int = Field(gt=0)
    join_strategy: Literal["apoc_dijkstra", "shortest_path"]
    allow_shortest_path_fallback: bool
    connection_timeout_seconds: float = Field(gt=0)
    query_timeout_seconds: float = Field(gt=0)


class MilvusSearchParams(_StrictModel):
    metric_type: str
    params: dict[str, Any] = Field(default_factory=dict)


class MilvusCollections(_StrictModel):
    table_schema: str
    dim_value: str
    sql_example: str


class MilvusConfig(_StrictModel):
    host: str
    port: int = Field(gt=0)
    database: str
    user: str = ""
    password: str = ""
    alias: str
    timeout: float = Field(gt=0)
    search_params: MilvusSearchParams
    collections: MilvusCollections


class RetrievalConfig(_StrictModel):
    table_top_k: int = Field(gt=0)
    table_min_score: float = Field(ge=0, le=1)
    column_top_k: int = Field(gt=0)
    column_min_score: float = Field(ge=0, le=1)
    dim_value_top_k: int = Field(gt=0)
    dim_value_min_score: float = Field(ge=0, le=1)
    sql_example_top_k: int = Field(gt=0)
    sql_example_min_score: float = Field(ge=0, le=1)
    max_top_k: int = Field(gt=0)
    table_category_mapping: dict[str, list[str]]


class SyntaxCheckConfig(_StrictModel):
    allow_multiple_statements: bool


class SecurityCheckConfig(_StrictModel):
    allowed_statement_types: list[str]
    forbidden_keywords: list[str]
    allow_comments: bool


class SemanticWarningsConfig(_StrictModel):
    seq_scan_warn: bool
    nested_loop_warn: bool
    estimated_rows_threshold: int


class SemanticCheckConfig(_StrictModel):
    explain_analyze: bool
    warnings: SemanticWarningsConfig


class GuardCheckConfig(_StrictModel):
    enabled: bool
    forbid_realtime_clock_in_filters: bool
    require_time_evidence: bool
    require_dimension_evidence: bool


class ValidationConfig(_StrictModel):
    enable_syntax_check: bool
    enable_security_check: bool
    enable_semantic_check: bool
    syntax: SyntaxCheckConfig
    security: SecurityCheckConfig
    semantic: SemanticCheckConfig
    guard: GuardCheckConfig


class ResultsConfig(_StrictModel):
    preview_rows: int = Field(gt=0)
    read_max_rows: int = Field(gt=0)
    max_tool_output_chars: int = Field(gt=0)
    placeholder_max_values: int = Field(gt=0)
    probe_max_values: int = Field(gt=0)


class BudgetConfig(_StrictModel):
    max_model_calls_per_run: int = Field(gt=0)
    max_tool_calls_per_run: int = Field(gt=0)
    max_execute_calls_per_run: int = Field(gt=0)
    run_timeout_seconds: float = Field(gt=0)
    final_answer_reminders: int = Field(ge=0)


class SummarizationConfig(_StrictModel):
    summary_trigger_tokens: int = Field(gt=0)
    recent_context_tokens: int = Field(gt=0)
    trim_tokens_to_summarize: int | None = Field(default=None, gt=0)


class FilesystemConfig(_StrictModel):
    tool_token_limit_before_evict: int = Field(gt=0)


class ThreadConfig(_StrictModel):
    rotate_after_messages: int = Field(gt=0)
    carry_max_evidence: int = Field(gt=0)
    checkpoint_retention_days: int = Field(gt=0)
    archive_retention_days: int = Field(gt=0)
    result_retention_days: int = Field(gt=0)


class WorkspaceConfig(_StrictModel):
    root: str


class ClockConfig(_StrictModel):
    timezone: str
    fixed_now: str | None = None


class FeaturesConfig(_StrictModel):
    todo_list: bool
    memory: bool


class PersistenceConfig(_StrictModel):
    mode: Literal["postgres", "memory"]


class DbAgentConfig(_StrictModel):
    """db_deepagent 运行配置（§4.1 yaml 的结构化镜像）"""

    model: ModelConfig
    embedding: EmbeddingConfig
    business_db: BusinessDbConfig
    platform_db: PlatformDbConfig
    maintenance: MaintenanceConfig
    neo4j: Neo4jConfig
    milvus: MilvusConfig
    retrieval: RetrievalConfig
    validation: ValidationConfig
    results: ResultsConfig
    budget: BudgetConfig
    summarization: SummarizationConfig
    filesystem: FilesystemConfig
    thread: ThreadConfig
    workspace: WorkspaceConfig
    clock: ClockConfig
    features: FeaturesConfig
    persistence: PersistenceConfig


def _field_paths(exc: ValidationError) -> str:
    """提取 ValidationError 的字段路径（不含输入值，防止密码外泄）。"""
    return "; ".join(".".join(str(p) for p in e["loc"]) for e in exc.errors())


def _check_business_target(cfg: DbAgentConfig) -> None:
    """business_db 三键一致性 + db 前缀键空间（doc 4 §4.7）。

    database/expected_database/table_key_prefix 必须一致（按源注入时同取
    登记库名，不得沿用固定前缀）；键空间按统一判定式校验（连字符、数字
    开头、内部空格合法；点号/控制字符/首尾空白拒绝）。
    """
    b = cfg.business_db
    if not (b.database == b.expected_database == b.table_key_prefix):
        raise DbAgentConfigError(
            "business_db: database/expected_database/table_key_prefix 必须一致"
            "（按选定源登记 db_name 注入），实际 "
            f"database={b.database!r}, expected={b.expected_database!r}, "
            f"table_key_prefix={b.table_key_prefix!r}"
        )
    from src.modules.db_deepagent.keyspace import db_prefix_problem

    problem = db_prefix_problem(b.table_key_prefix)
    if problem:
        raise DbAgentConfigError(f"business_db.table_key_prefix 非法: {problem}")


def apply_source_runtime_config(
    cfg: DbAgentConfig, db_name: str, *, check_embedding_profile: bool = True
) -> DbAgentConfig:
    """按选定源登记 db_name 构造运行配置（doc 4 §4.7）。

    ``business_db.database/expected_database/table_key_prefix`` 三键统一改为
    该源的登记库名（键空间按统一判定式校验，不通过即拒绝），并重跑 §4.2
    检查表。必须在 ``build_services`` 之前调用；与真实连接的一致性由
    BusinessDbService 启动时的 ``current_database()`` 核对兜底。
    """
    from src.modules.db_deepagent.keyspace import db_prefix_problem

    problem = db_prefix_problem(db_name)
    if problem:
        raise DbAgentConfigError(
            f"选定源登记 db_name 不能作为表键前缀: {problem}（该源对象不在本期"
            "NL2SQL 知识接入范围内）"
        )
    updated = cfg.model_copy(deep=True)
    updated.business_db.database = db_name
    updated.business_db.expected_database = db_name
    updated.business_db.table_key_prefix = db_name
    run_config_checks(updated, check_embedding_profile=check_embedding_profile)
    return updated


def _check_platform_target(cfg: DbAgentConfig) -> None:
    if not (
        cfg.platform_db.database == cfg.platform_db.expected_database == "lg_persist"
    ):
        raise DbAgentConfigError(
            "platform_db: database/expected_database 必须一致且为 lg_persist，实际 "
            f"database={cfg.platform_db.database!r}, expected={cfg.platform_db.expected_database!r}"
        )
    schema = cfg.platform_db.schema_name
    if schema == "public" or not _IDENTIFIER_RE.match(schema):
        raise DbAgentConfigError(
            f"platform_db.schema: 非法值（不能为 public，必须是合法标识符）: {schema!r}"
        )


def _check_summarization(cfg: DbAgentConfig) -> None:
    s = cfg.summarization
    if not 0 < s.recent_context_tokens < s.summary_trigger_tokens:
        raise DbAgentConfigError(
            "summarization: 需满足 0 < recent_context_tokens < summary_trigger_tokens，实际 "
            f"recent={s.recent_context_tokens}, trigger={s.summary_trigger_tokens}"
        )
    # trim_tokens_to_summarize: None 或正整数已由字段约束保证；显式传 None 时不回落 SDK 默认 4000（§4.3）。


def _check_maintenance(cfg: DbAgentConfig) -> None:
    m = cfg.maintenance
    # 正整数已由字段约束保证；这里校验锁超时小于语句超时
    if m.lock_timeout_ms >= m.statement_timeout_ms:
        raise DbAgentConfigError(
            "maintenance: lock_timeout_ms 必须小于 statement_timeout_ms，实际 "
            f"lock={m.lock_timeout_ms}, statement={m.statement_timeout_ms}"
        )


def _check_semantic(cfg: DbAgentConfig) -> None:
    if cfg.validation.semantic.explain_analyze:
        raise DbAgentConfigError("validation.semantic.explain_analyze: 必须为 false")


def _check_fixed_now(cfg: DbAgentConfig) -> None:
    if cfg.clock.fixed_now is not None and not re.search(
        r"(Z|[+-]\d{2}:?\d{2})$", cfg.clock.fixed_now
    ):
        raise DbAgentConfigError(
            f"clock.fixed_now: 必须带时区（如 2026-10-13T12:00:00+08:00），实际 {cfg.clock.fixed_now!r}"
        )


def _check_embedding_profile(cfg: DbAgentConfig) -> None:
    """embedding.profile 必须是全局 embedding_profiles 中的具体键，不能是 active 键。"""
    profiles = get_config().get("embedding_profiles", {}) or {}
    name = cfg.embedding.profile
    if name == "active" or name not in profiles:
        raise DbAgentConfigError(
            f"embedding.profile: {name!r} 不在全局 embedding_profiles 中（或为 active 键）；必须显式引用具体 profile"
        )


def run_config_checks(
    cfg: DbAgentConfig, *, check_embedding_profile: bool = True
) -> None:
    """执行 §4.2 检查表（结构约束已由 Pydantic 字段保证，这里做跨字段与外部引用检查）。"""
    _check_business_target(cfg)
    _check_platform_target(cfg)
    _check_summarization(cfg)
    _check_maintenance(cfg)
    _check_semantic(cfg)
    _check_fixed_now(cfg)
    if check_embedding_profile:
        _check_embedding_profile(cfg)


def compute_validation_fingerprint(cfg: DbAgentConfig) -> str:
    """validation_fingerprint = sha256(canonical_json(validation 段) + GUARD_RULES_VERSION)。

    GUARD_RULES_VERSION 延迟导入：sql_guard 位于 services 包，包 __init__ 依赖
    本模块，模块级导入会成环。
    """
    from src.modules.db_deepagent.services.sql_guard import GUARD_RULES_VERSION

    canonical = json.dumps(
        cfg.validation.model_dump(mode="json"),
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256((canonical + GUARD_RULES_VERSION).encode("utf-8")).hexdigest()


def load_base_db_agent_config(config_path: str | Path | None = None) -> DbAgentConfig:
    """读取源无关的基础配置内容（doc 2 §5.3 Web 装配顺序）。

    只做 yaml 解析与结构校验，不执行 §4.2 跨字段检查——business_db 三键
    一致性在按源登记覆盖（apply_source_runtime_config）之后才成立；环境
    DB_NAME 与 YAML 默认 orders 不同时也能先完成解析。Web 公共装配在应用
    登记快照后调用 run_config_checks 完成最终校验。
    """
    path = Path(config_path) if config_path is not None else _CONFIG_PATH
    if not path.exists():
        raise DbAgentConfigError(f"配置文件不存在: {path}")
    raw = ConfigLoader(str(path)).load()
    try:
        return DbAgentConfig.model_validate(raw)
    except ValidationError as exc:
        raise DbAgentConfigError(
            f"配置结构错误（字段路径）: {_field_paths(exc)}"
        ) from exc


def load_db_agent_config(config_path: str | Path | None = None) -> DbAgentConfig:
    """加载 §4.1 yaml → DbAgentConfig，并执行 §4.2 全部检查。

    Args:
        config_path: 配置文件路径；None 时使用模块默认 config/db_deepagent.yaml

    Raises:
        DbAgentConfigError: 文件缺失、结构错误、检查表任一失败（消息不含密码）
    """
    cfg = load_base_db_agent_config(config_path)
    run_config_checks(cfg)
    return cfg
