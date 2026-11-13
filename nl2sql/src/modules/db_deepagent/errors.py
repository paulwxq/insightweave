"""db_deepagent 模块异常与工具错误类型常量（docs/v4/5 §16.1）

异常分三层：
- 初始化：CLI 打印原因并以非零码退出
- 工具：一律转换为错误包（§7.1），模型可见
- 运行：持久化异常、模型调用异常由 Runner 转换为结果状态
"""

from __future__ import annotations


def persistence_error_types() -> tuple[type[BaseException], ...]:
    """运行/收尾阶段的持久化后端故障类型（§14.3）。

    doc 6.3 R4-01：来源装配/门控 I/O 的受控异常边界与 P0–P3 共用同一分类，
    避免把 psycopg/PoolTimeout 故障吞成普通业务门控拒绝。延迟导入使本模块
    保持零项目内依赖，可供 runner 与 final_answer 共同引用而不产生循环导入。
    """
    import psycopg
    import psycopg_pool

    return (psycopg.Error, psycopg_pool.PoolTimeout, PlatformSchemaError)


def is_persistence_error(exc: BaseException) -> bool:
    """是否为已识别的持久化后端故障（§14.3；R4-01 供来源装配 I/O 边界复用）。

    确定性协议损坏（ValueError → SourceUnqualified）与未知运行错误均不属于
    此类，分别按受控业务门控拒绝、runtime 规则收敛。
    """
    return isinstance(exc, persistence_error_types())


class DbAgentError(Exception):
    """db_deepagent 模块异常基类"""


# ----------------------------------------------------------------------------
# 初始化层（§16.1）：CLI 打印原因并以非零码退出
# ----------------------------------------------------------------------------


class DbAgentConfigError(DbAgentError):
    """配置加载或校验失败（§4.2）"""


class DatasourceMismatchError(DbAgentError):
    """业务库连接目标与期望不一致（§6.1）"""


class PlatformSchemaError(DbAgentError):
    """平台库 Schema 不在 db_deepagent（如 current_schema() 为 NULL）（§13.1）"""


class EmbeddingConsistencyError(DbAgentError):
    """查询侧与入库侧 Embedding 模型／维度不一致（§6.3）"""


class HarnessProfileError(DbAgentError):
    """HarnessProfile 注册或匹配失败（§10.2）"""


class AssemblyVerificationError(DbAgentError):
    """Agent 装配后验证失败（§10.6）或 SDK 私有摘要状态结构异常（§10.2）"""


# ----------------------------------------------------------------------------
# 执行层（§6.1）：BusinessDbService 抛出，工具层转换为 §7.1 错误包
# ----------------------------------------------------------------------------


class DuplicateColumnNamesError(DbAgentError):
    """业务 SQL 输出列名重复（含未命名表达式的 ?column?），不读取行、不保存结果"""

    def __init__(self, duplicates: list[str]):
        self.duplicates = duplicates
        super().__init__(
            f"输出列名重复: {', '.join(duplicates)}；请为输出列添加唯一别名"
        )


class DbExecutionError(DbAgentError):
    """业务库执行错误（§6.1 错误映射表）；error_type/retryable 按 §7.1 错误包"""

    def __init__(
        self,
        *,
        error_type: str,
        message: str,
        retryable: bool,
        hint: str | None = None,
        sqlstate: str | None = None,
    ):
        self.error_type = error_type
        self.message = message
        self.retryable = retryable
        self.hint = hint
        self.sqlstate = sqlstate
        super().__init__(message)


# ----------------------------------------------------------------------------
# 检索层（§6.2–§6.6）：服务抛出，工具层转换为 §7.1 错误包
# ----------------------------------------------------------------------------


class EmbeddingDimensionError(DbAgentError):
    """Embedding 返回向量长度与 profile dimensions 不一致（§6.2）"""


class RetrievalBackendError(DbAgentError):
    """Milvus 检索失败（§6.4：不吞掉、不降级为空列表，与旧链路"失败降级为空"不同）"""


class GraphBackendError(DbAgentError):
    """Neo4j 图服务失败（§6.6）"""


class InvalidTableIdError(DbAgentError):
    """表标识不是合法的两段／三段键，或三段首段与 table_key_prefix 不符（§6.5）"""


class ResultStoreError(DbAgentError):
    """结果存取错误（§6.8）；error_type 供工具层转换为 §7.1 错误包"""

    def __init__(self, error_type: str, message: str):
        self.error_type = error_type
        self.message = message
        super().__init__(message)


# ----------------------------------------------------------------------------
# 工具错误类型常量（§7.1 错误类型总表；工具内部异常转换为 JSON 错误包时使用）
# ----------------------------------------------------------------------------

TOOL_ERROR_INVALID_ARGUMENT = "invalid_argument"
TOOL_ERROR_INVALID_TABLE_ID = "invalid_table_id"
TOOL_ERROR_TABLE_NOT_FOUND = "table_not_found"
TOOL_ERROR_COLUMN_NOT_FOUND = "column_not_found"
TOOL_ERROR_RETRIEVAL_BACKEND = "retrieval_backend_error"
TOOL_ERROR_GRAPH_BACKEND = "graph_backend_error"
TOOL_ERROR_AMBIGUOUS_TIME = "ambiguous_time_reference"
TOOL_ERROR_TIME_TEXT_NOT_IN_QUESTION = "time_text_not_in_question"
TOOL_ERROR_EVIDENCE_NOT_FOUND = "evidence_not_found"
TOOL_ERROR_EVIDENCE_DATASOURCE_MISMATCH = "evidence_datasource_mismatch"
TOOL_ERROR_RESULT_DATASOURCE_MISMATCH = "result_datasource_mismatch"
TOOL_ERROR_TIME_PARAMS_MISMATCH = "time_params_mismatch"
TOOL_ERROR_VALIDATION_FAILED = "validation_failed"
TOOL_ERROR_NOT_VALIDATED = "not_validated"
TOOL_ERROR_PLACEHOLDER = "placeholder_error"
TOOL_ERROR_DB_SEMANTIC = "db_semantic_error"
TOOL_ERROR_DB_DATA = "db_data_error"
TOOL_ERROR_TIMEOUT = "timeout"
TOOL_ERROR_DUPLICATE_COLUMN_NAMES = "duplicate_column_names"
TOOL_ERROR_READONLY_VIOLATION = "readonly_violation"
TOOL_ERROR_DATASOURCE_UNAVAILABLE = "datasource_unavailable"
TOOL_ERROR_INVALID_RESULT_REF = "invalid_result_ref"
TOOL_ERROR_RESULT_NOT_FOUND = "result_not_found"
TOOL_ERROR_RESULT_ACCESS_DENIED = "result_access_denied"
TOOL_ERROR_RESULT_INTEGRITY = "result_integrity_error"
TOOL_ERROR_INCOMPLETE_SOURCE = "incomplete_source"
TOOL_ERROR_BUDGET_EXHAUSTED = "budget_exhausted"
TOOL_ERROR_FINAL_ANSWER_NOT_ALONE = "final_answer_not_alone"
TOOL_ERROR_INVALID_EVIDENCE_REFERENCE = "invalid_evidence_reference"
TOOL_ERROR_FINAL_ANSWER_ALREADY_SUBMITTED = "final_answer_already_submitted"

# 守卫规则码（§9.4／§9.5／§9.6／§9.8；守卫实现于步骤 3，常量先落位便于引用）
GUARD_UNPARSEABLE = "guard_unparseable"
GUARD_TOO_COMPLEX = "guard_too_complex"
GUARD_REALTIME_CLOCK_IN_FILTER = "realtime_clock_in_filter"
GUARD_UNSUPPORTED_TIME_LITERAL = "unsupported_time_literal"
GUARD_TIME_OPERATOR_NOT_HALF_OPEN = "time_operator_not_half_open"
GUARD_TIME_LOWER_BOUND_MISMATCH = "time_lower_bound_mismatch"
GUARD_TIME_UPPER_BOUND_MISMATCH = "time_upper_bound_mismatch"
GUARD_EMPTY_TIME_RANGE = "empty_time_range"
GUARD_TIME_RANGE_NOT_EVIDENCED = "time_range_not_evidenced"
GUARD_TIME_PREDICATE_UNDER_NOT = "time_predicate_under_not"
GUARD_UNSUPPORTED_DIMENSION_VALUE = "unsupported_dimension_value"
GUARD_UNSUPPORTED_PATTERN_FILTER = "unsupported_pattern_filter"
GUARD_COLUMN_OWNER_UNRESOLVED = "column_owner_unresolved"
GUARD_UNSUPPORTED_PREDICATE_FORM = "unsupported_predicate_form"
GUARD_CONSTANT_SOURCE_UNSUPPORTED = "constant_source_unsupported"
GUARD_PROBE_SHAPE_MISMATCH = "probe_shape_mismatch"
GUARD_PUSHDOWN_EVIDENCE_MISSING = "pushdown_evidence_missing"
GUARD_EVIDENCE_DATASOURCE_MISMATCH = "evidence_datasource_mismatch"
GUARD_PLACEHOLDER_POSITION_INVALID = "placeholder_position_invalid"
GUARD_PLACEHOLDER_MALFORMED = "placeholder_malformed"
