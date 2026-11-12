"""账本记录结构（docs/v4/5 §5.3）

全部为可 JSON 序列化的 TypedDict（不放 datetime／Decimal 对象，时间用 ISO 字符串），
保证 checkpoint 序列化稳定。只有本模块的工具通过 Command(update={...}) 写入对应状态字段。
"""

from __future__ import annotations

from typing import Any, Literal, NotRequired, TypedDict

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)


class ValidationRecord(TypedDict):
    """可信校验记录（§5.4：授权键决定作用域）"""

    key: str  # sha256(thread|request|run|datasource|validation_fp|normalized_sql)
    status: Literal["valid", "revoked"]
    normalized_sql: str  # 授权匹配文本（规范化后的原文，可含结果占位符）
    expanded_sql_sha256: (
        str  # 占位符展开后的文本摘要；无占位符时为 normalized_sql 的摘要
    )
    placeholder_refs: list[str]  # 占位符引用的 result_ref
    purpose: Literal["query", "probe", "pushdown"]
    thread_id: str
    request_id: str
    run_id: str
    datasource_id: str
    validation_fingerprint: str
    layer: str  # all_passed / syntax / security / semantic / output_columns / guard
    guard_evidence: GuardEvidenceSnapshot  # 守卫实际采用的证据完整内容（§9.7）
    warnings: list[str]
    validated_at: str
    tool_call_id: str


class ColumnMeta(TypedDict):
    """结果列元数据（按位置保存，名称唯一性由校验期／执行期检查保证）"""

    name: str
    pg_type: str  # cursor.description 的 type_code 经 pg_type 映射后的类型名


class ExecutionRecord(TypedDict):
    """业务 SQL 执行记录（purpose="query"/"probe"/"pushdown"）"""

    evidence_id: str  # "E-" + sha256(tool_call_id + ":" + purpose) 前 8 位十六进制
    tool_call_id: str
    run_id: str
    request_id: str
    purpose: Literal["query", "probe", "pushdown"]
    datasource_id: str  # 执行时的数据源
    sql_template: str  # 授权匹配文本（规范化原文，可含结果占位符）
    executed_sql: str  # 占位符展开后实际提交数据库的 SQL 全文
    executed_sql_sha256: str
    result_ref: str
    columns: list[ColumnMeta]
    saved_row_count: int
    source_truncated: bool
    elapsed_ms: int
    warnings: list[str]
    reused_from: NotRequired[EvidenceOrigin]  # §7.12.1 沿用登记
    result_generated_at: NotRequired[str]  # 结果生成时间 ISO


class AnalysisRecord(TypedDict):
    """analyze_result 的分析记录（本地计算或下推）"""

    evidence_id: str  # "A-" + sha256(tool_call_id) 前 8 位十六进制
    tool_call_id: str
    run_id: str
    request_id: str
    pushdown_execution_id: (
        str | None
    )  # 下推时对应 ExecutionRecord 的 evidence_id，仅作轨迹
    operation: dict[str, Any]  # 操作名与参数（已校验）
    input_result_refs: list[str]
    output_result_ref: str
    executed_sql: str | None  # 下推时为实际执行的 SQL 全文，本地计算为 None
    saved_row_count: int
    source_truncated: bool  # 输出是否受截断影响（下推后通常为 False）
    warnings: list[str]
    reused_from: NotRequired[EvidenceOrigin]  # §7.12.1 沿用登记
    result_generated_at: NotRequired[str]  # 结果生成时间 ISO


class EvidenceOrigin(TypedDict):
    """证据沿用时的原始来源（§7.12、§13.4）"""

    evidence_id: str  # 原始 time_evidence_id / dim_evidence_id
    thread_id: str
    request_id: str
    run_id: str
    datasource_id: str  # 原始证据的数据源，不被当前数据源覆盖
    user_text: str | None  # 原始证据对应的用户原文（时间证据为该轮问题全文）


class TimeEvidence(TypedDict):
    """时间证据：有限 [start, end)；单边允许恰有一端为 None（§7.5）"""

    time_evidence_id: str
    run_id: str
    source_text: str  # 用户原文中的时间表达
    grain: str  # day / week / month / quarter / year / range
    start: str | None  # 包含的下界；None 表示无下界
    end: str | None  # 不包含的上界；None 表示无上界；两端不可同时为 None（§7.5）
    timezone: str
    datasource_id: (
        str  # 首次取得证据时的数据源；跨数据源沿用时原样保留，仅作追溯（§7.12）
    )
    inherited_from: EvidenceOrigin | None  # 经 reuse_evidence 沿用时的原始来源（§7.12）


class DimEvidence(TypedDict):
    """维度证据：milvus_dim_hit / db_probe / prior_result 三种来源"""

    dim_evidence_id: str
    run_id: str
    source: Literal["milvus_dim_hit", "db_probe", "prior_result"]
    table: str  # 两段式 schema.table
    column: str
    values: list[str]  # 已确认存在的真实值
    patterns: list[str]  # 已探测的 ILIKE 模式（仅 db_probe 的 contains 模式）
    query_text: str | None  # 触发证据的用户表达
    ref: (
        str | None
    )  # Milvus 命中分数与生效阈值摘要 / 探针 evidence_id / 来源 result_ref
    datasource_id: (
        str  # 取得该证据时的数据源（prior_result 为来源结果的 datasource_id）
    )
    inherited_from: EvidenceOrigin | None


class GuardEvidenceSnapshot(TypedDict):
    """守卫通过时实际采用的证据完整内容（§9.7），随可信校验记录与结果保存"""

    time: list[TimeEvidence]  # 完整记录，不只是 ID
    dim: list[DimEvidence]  # 含占位符生成的 prior_result 证据与下推输入快照中的证据


class CarriedEvidence(TypedDict):
    """线程轮换时携带的已确认证据（§13.4）"""

    kind: Literal["time", "dim"]
    record: TimeEvidence | DimEvidence  # 旧线程中的原始记录
    origin: EvidenceOrigin


class RunStatusRecord(TypedDict):
    """运行状态轨迹（预算判定以进程内 RunBudgetLedger 为准，状态只作轨迹）"""

    run_id: str
    stop_reason: (
        str | None
    )  # model_call_limit / tool_call_limit / execute_limit / run_timeout / tool_set_mismatch
    counters: dict[str, int]  # 写入时 RunBudgetLedger 的计数快照，仅作轨迹（§11.1）
    final_answer_reminders: int  # 已发出的提交提醒次数（§11.3）


class FinalAnswerRecord(TypedDict):
    """被接受的最终答案（按 run_id 键控；§7.11）"""

    run_id: str
    request_id: str
    tool_call_id: str
    answer: dict[str, Any]  # DbAgentFinalAnswer.model_dump()，已通过 §7.11 的检查
    submitted_at: str


class ToolErrorRecord(TypedDict):
    """工具内部异常的错误包记录（写入 db_tool_errors）"""

    tool_call_id: str
    run_id: str
    tool: str
    error_type: str
    message: str
    retryable: bool


# ----------------------------------------------------------------------------
# 返回协议（§14.1／§14.2）：pydantic 模型（模型可见参数与对外交付）
# ----------------------------------------------------------------------------


class FindingItem(BaseModel):
    """最终答案中的一条发现（模型提交，引用 evidence_id）"""

    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=2000)
    evidence_ids: list[str] = Field(min_length=1)


class DbAgentFinalAnswer(BaseModel):
    """数据库 Agent 的最终答案。只引用工具返回过的 evidence_id（§7.11）。"""

    model_config = ConfigDict(extra="forbid")

    status: Literal["completed", "partial", "needs_clarification", "no_data", "failed"]
    answer: str = Field(min_length=1, max_length=8000)
    findings: list[FindingItem] = []
    metric_definitions: list[str] = []  # 采用的口径
    limitations: list[str] = []  # 截断、缺失、口径限制
    open_questions: list[str] = []  # 待澄清或未完成事项

    @field_validator("answer")
    @classmethod
    def _require_nonblank_answer(cls, value: str) -> str:
        """正文必须非空白（doc 6.2 §7.2.1）：纯空白提交在参数层即拒绝。"""
        if not has_nonblank_answer(value):
            raise ValueError("answer 必须包含非空白正文")
        return value  # 不重写正常正文的首尾格式。


# ----------------------------------------------------------------------------
# 共享有效性校验（doc 6.2 §7.2.1：提交参数、Runner 评估与归档资格同一规则）
# ----------------------------------------------------------------------------


def extract_raw_final_answer_payload(answer_record: object) -> Any:
    """原值提取：answer 键存在时按原类型原值返回（含 None/空串/列表）。"""
    if isinstance(answer_record, dict) and "answer" in answer_record:
        return answer_record["answer"]
    return {}  # 仅外层记录/answer 键缺失时使用缺失约定值。


def has_nonblank_answer(value: object) -> bool:
    """正文是否为非空白字符串（strip 后非空）。"""
    return isinstance(value, str) and bool(value.strip())


def normalize_valid_final_answer_payload(payload: object) -> dict[str, Any] | None:
    """结构 + 非空白正文校验通过时返回归一字典，否则 None。

    只做一次 Pydantic 解析（model_validate），不先以布尔函数预检再二次校验。
    """
    if not isinstance(payload, dict) or not has_nonblank_answer(payload.get("answer")):
        return None
    try:
        return DbAgentFinalAnswer.model_validate(payload).model_dump()
    except ValidationError:
        return None


def normalize_final_answer_record(
    answer_record: object,
    *,
    run_id: str,
    request_id: str,
) -> dict[str, Any] | None:
    """运行归属严格校验（doc 6.2 §7.2.1）：外层 run_id/request_id 必须与
    Runner 本轮参数精确相等（比较基准来自 Runner，不从记录推导）；缺失或
    任一不一致视为无效提交，不根据字典键自动补齐或改写。
    """
    if not isinstance(answer_record, dict):
        return None
    if answer_record.get("run_id") != run_id:
        return None
    if answer_record.get("request_id") != request_id:
        return None
    return normalize_valid_final_answer_payload(
        extract_raw_final_answer_payload(answer_record)
    )


class EvidenceItem(BaseModel):
    """交付结果中的证据条目；事实字段全部来自服务端记录（§14.2）"""

    model_config = ConfigDict(extra="forbid")

    evidence_id: str
    kind: Literal["sql_query", "result_analysis"]
    tool_call_id: str
    source: str  # datasource_id，不含凭据
    sql: str | None  # 实际执行的 SQL 全文（executed_sql）
    sql_template: str | None = None  # 含结果占位符时的授权原文；无占位符时为 None
    result_ref: str | None
    input_result_refs: list[str] = []
    operation: dict[str, Any] | None = None
    row_count: int
    preview_truncated: bool
    source_truncated: bool
    warnings: list[str] = []
    reused_from: EvidenceOrigin | None = (
        None  # 受控沿用登记的链首原始来源；本运行执行为 None（§7.12.1）
    )
    result_generated_at: str = (
        ""  # 结果生成时间 ISO；所有记录都填，沿用记录为原结果生成时间
    )

    @model_validator(mode="after")
    def _check_kind(self) -> "EvidenceItem":
        if self.kind == "sql_query":
            if (
                self.sql is None
                or self.result_ref is None
                or self.operation is not None
            ):
                raise ValueError(
                    "sql_query 需要 sql 与 result_ref，operation 必须为 None"
                )
            if self.input_result_refs:
                raise ValueError("sql_query 的 input_result_refs 必须为空")
        else:
            if (
                self.operation is None
                or not self.input_result_refs
                or self.result_ref is None
            ):
                raise ValueError(
                    "result_analysis 需要 operation、input_result_refs 与 result_ref"
                )
        return self


class RunStats(BaseModel):
    """运行统计（§14.2）；未知用量不填零"""

    model_config = ConfigDict(extra="forbid")

    elapsed_ms: int
    model_calls: int  # 主 Agent 模型步骤预占数，来自预算快照
    summary_model_calls: int  # 摘要模型调用尝试数，来自回调
    total_model_calls: int  # 主模型 + 摘要的可观察调用尝试数，非 HTTP 请求数
    tool_calls: int  # budget_snapshot.counters["tool_call"]
    input_tokens: int | None  # 回调聚合；任一调用用量未知时为 None
    output_tokens: int | None
    token_usage_complete: bool


class RunFailureRecord(BaseModel):
    """一次运行失败的轨迹条目（phase/kind/异常摘要；§14.3）"""

    model_config = ConfigDict(extra="forbid")

    phase: Literal["P0", "P1", "P1.close_run", "P1.observer_finalize", "P2", "P3", "P4"]
    kind: Literal[
        "persistence",
        "runtime",
        "tool_set_mismatch",
        "budget_exhausted",
        "no_final_answer",
        # Web 前置拒绝的结构化类型（doc 2 §4.2）：分别映射同名 error_code；
        # Runner 的 status 仍为 failed（session_busy 等是“请求未执行”特例）
        "session_busy",
        "lock_backend_error",
        "knowledge_changed",
        "dependency_unavailable",
        "result_encoding_error",
    ]
    message: str
    primary: bool = False


class DbAgentResult(BaseModel):
    """Runner 对外交付的运行结果（§14.2）"""

    model_config = ConfigDict(extra="forbid")

    session_id: str
    thread_id: str | None  # P0 未能确定线程时为 None（§14.3）
    request_id: str
    run_id: str
    status: Literal[
        "completed",
        "partial",
        "needs_clarification",
        "no_data",
        "failed",
        "budget_exhausted",
        "persistence_error",
    ]
    answer: str
    findings: list[FindingItem]
    evidence: list[EvidenceItem]
    metric_definitions: list[str]
    limitations: list[str]
    open_questions: list[str]
    errors: list[RunFailureRecord]
    warnings: list[str]
    archived: bool  # P3 归档是否成功（§14.3）
    stats: RunStats
