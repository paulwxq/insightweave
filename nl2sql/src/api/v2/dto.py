"""Web API v2 的请求/响应 DTO（doc 2 §4.2/§5.5）。

请求模型 extra="forbid"：多余字段（request_id/user_id/连接字段等）返回 422，
不静默忽略。WebQueryResult 为 DbAgentResult 的结构化安全投影，显式声明
字段，不用任意 dict 悄悄增减。
"""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.modules.db_deepagent.schemas import DbAgentResult, EvidenceItem

StateStr = Literal["active", "invalidated"]
InvalidReasonStr = Literal[
    "knowledge_changed",
    "dependency_unavailable",
    "source_changed",
    "session_binding_missing",
]
TerminalCodeStr = str  # 闭集校验在 codes.py；DTO 层保留 str 以承载拒绝码并集


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------- 请求模型


class SessionCreateRequest(StrictModel):
    source_id: str = Field(min_length=1, max_length=64)

    @field_validator("source_id")
    @classmethod
    def _uuid(cls, value: str) -> str:
        import uuid

        try:
            return str(uuid.UUID(value.strip()))
        except ValueError:
            raise ValueError("source_id 必须是合法 UUID") from None


class QueryRequest(StrictModel):
    question: str
    source_id: str = Field(min_length=1, max_length=64)

    @field_validator("question")
    @classmethod
    def _question(cls, value: str) -> str:
        # 1–1000 个 Unicode 码点；去除首尾空白后不得为空（doc 2 §4.2；
        # 与旧接口的 1000 字符上限不同，按码点计数，前后端一致实现）
        if len(value) < 1 or len(value) > 1000:
            raise ValueError("question 长度须为 1–1000 个 Unicode 码点")
        if not value.strip():
            raise ValueError("question 不能为空白")
        return value

    @field_validator("source_id")
    @classmethod
    def _uuid(cls, value: str) -> str:
        import uuid

        try:
            return str(uuid.UUID(value.strip()))
        except ValueError:
            raise ValueError("source_id 必须是合法 UUID") from None


# ---------------------------------------------------------------- 响应模型


class SessionInfo(StrictModel):
    session_id: str
    source_id: str
    title: str
    created_at: str
    last_active_at: str
    turn_count: int
    state: StateStr
    continuable: bool
    invalid_reason: Optional[InvalidReasonStr]
    invalidated_at: Optional[str]


class TurnEvidenceView(StrictModel):
    """历史轮次可回放的证据投影：只保留画表格和展开 SQL 需要的字段。"""

    sql: Optional[str] = None
    result_ref: Optional[str] = None
    row_count: int = 0
    source_truncated: bool = False


class WebTurn(StrictModel):
    """历史轮次。问答文本始终返回；display 投影供再次打开会话时回放表格和 SQL。

    旧记录没有 display 时 evidence 为空、elapsed_ms 为空，仍只展示问答。
    """

    question: str
    answer: str
    saved_at: Optional[str] = None
    elapsed_ms: Optional[int] = None
    evidence: list[TurnEvidenceView] = Field(default_factory=list)


class PageData(StrictModel):
    """会话/轮次分页 data（doc 2 §4.2）：{items, limit, offset, has_more, next_offset}。"""

    items: list[Any]
    limit: int
    offset: int
    has_more: bool
    next_offset: Optional[int]


class CellV2(StrictModel):
    """结果单元格信封（doc 2 §4.5）：{kind, value}；value 类型由 kind 决定。"""

    model_config = ConfigDict(extra="forbid")

    kind: str
    value: Any = None


class ResultColumn(StrictModel):
    name: str
    pg_type: str


class ResultPageData(StrictModel):
    columns: list[ResultColumn]
    rows: list[list[CellV2]]
    offset: int
    limit: int
    next_offset: Optional[int]
    has_more: bool
    saved_row_count: int
    source_truncated: bool
    max_display_rows: int
    data_from_snapshot: Optional[bool] = None
    snapshot_rows_available: Optional[int] = None


class ReadinessCheckItem(StrictModel):
    name: str
    required: bool
    state: Literal["pass", "warn", "fail"]
    error_code: Optional[str] = None
    message: str = ""
    counts: Optional[dict[str, int]] = None


class ReadinessData(StrictModel):
    source_id: str
    compatible: Optional[bool]
    ready: bool
    checked_at: str
    checks: list[ReadinessCheckItem]


class ErrorData(StrictModel):
    """非 200 标准错误 data：至少 {error_code, request_id, run_id}。"""

    model_config = ConfigDict(extra="allow")

    error_code: str
    request_id: Optional[str] = None
    run_id: Optional[str] = None


# ----------------------------------------------- WebQueryResult（§4.2 字段表）


class WebFinding(StrictModel):
    text: str
    evidence_ids: list[str]


class WebEvidenceOrigin(StrictModel):
    evidence_id: str
    thread_id: str
    request_id: str
    run_id: str
    datasource_id: str
    user_text: Optional[str]


class WebEvidenceItem(StrictModel):
    """EvidenceItem 的结构化安全投影（doc 2 §4.2）：不输出 preview_truncated。"""

    evidence_id: str
    kind: str
    tool_call_id: str
    source: str
    sql: Optional[str]
    sql_template: Optional[str]
    result_ref: Optional[str]
    input_result_refs: list[str]
    operation: Optional[dict[str, Any]]
    row_count: int
    source_truncated: bool
    warnings: list[str]
    reused_from: Optional[WebEvidenceOrigin]
    result_generated_at: str

    @classmethod
    def project(cls, item: EvidenceItem) -> "WebEvidenceItem":
        reused = (
            WebEvidenceOrigin(**dict(item.reused_from))
            if item.reused_from is not None
            else None
        )
        return cls(
            evidence_id=item.evidence_id,
            kind=item.kind,
            tool_call_id=item.tool_call_id,
            source=item.source,
            sql=item.sql,
            sql_template=item.sql_template,
            result_ref=item.result_ref,
            input_result_refs=list(item.input_result_refs),
            operation=dict(item.operation) if item.operation else None,
            row_count=item.row_count,
            source_truncated=item.source_truncated,
            warnings=list(item.warnings),
            reused_from=reused,
            result_generated_at=item.result_generated_at,
        )


class WebRunFailure(StrictModel):
    phase: str
    kind: str
    message: str
    primary: bool


class WebStats(StrictModel):
    elapsed_ms: int
    model_calls: int
    summary_model_calls: int
    total_model_calls: int
    tool_calls: int
    input_tokens: Optional[int]
    output_tokens: Optional[int]
    token_usage_complete: bool


class WebQueryResult(StrictModel):
    """当前轮查询响应（doc 2 §4.2 字段表，均必返；error_code 为闭集或 null）。

    不内嵌 preview/rows/columns；UI 按 evidence.result_ref 调用结果 API 取页。
    """

    session_id: str
    thread_id: Optional[str]
    request_id: str
    run_id: str
    status: str
    answer: str
    findings: list[WebFinding]
    evidence: list[WebEvidenceItem]
    metric_definitions: list[str]
    limitations: list[str]
    open_questions: list[str]
    errors: list[WebRunFailure]
    warnings: list[str]
    archived: bool
    stats: WebStats
    source_id: str
    history_saved: Optional[bool]
    error_code: Optional[str]

    @classmethod
    def project(
        cls,
        result: DbAgentResult,
        *,
        source_id: str,
        history_saved: Optional[bool],
        error_code: Optional[str],
    ) -> "WebQueryResult":
        return cls(
            session_id=result.session_id,
            thread_id=result.thread_id,
            request_id=result.request_id,
            run_id=result.run_id,
            status=result.status,
            answer=result.answer or "",
            findings=[
                WebFinding(text=f.text, evidence_ids=list(f.evidence_ids))
                for f in result.findings
            ],
            evidence=[WebEvidenceItem.project(e) for e in result.evidence],
            metric_definitions=list(result.metric_definitions),
            limitations=list(result.limitations),
            open_questions=list(result.open_questions),
            errors=[
                WebRunFailure(
                    phase=e.phase, kind=e.kind, message=e.message, primary=e.primary
                )
                for e in result.errors
            ],
            warnings=list(result.warnings),
            archived=result.archived,
            stats=WebStats(**result.stats.model_dump()),
            source_id=source_id,
            history_saved=history_saved,
            error_code=error_code,
        )

    @classmethod
    def unsafe_projection(
        cls,
        result: DbAgentResult,
        *,
        source_id: str,
        history_saved: Optional[bool],
        error_code: str,
        safe_answer: str,
    ) -> "WebQueryResult":
        """一致性未确认终态的强制投影（doc 2 §4.2）：answer 改安全摘要，
        findings/evidence/口径/待澄清固定空数组，warnings 清空；errors 仅
        保留 phase/kind/primary 与按 kind 的固定安全文案，不夹带原答案、
        SQL、结果引用、模型内容或异常原文。"""
        base = cls.project(
            result,
            source_id=source_id,
            history_saved=history_saved,
            error_code=error_code,
        )
        safe_kinds = {
            "knowledge_changed": "执行期间知识版本发生变化",
            "dependency_unavailable": "终态一致性复核不可用",
            "persistence": "持久化异常",
            "session_busy": "会话忙碌，本次未执行",
            "lock_backend_error": "锁后端异常",
            "runtime": "运行期异常",
            "result_encoding_error": "结果编码失败",
        }
        return base.model_copy(
            update={
                "answer": safe_answer,
                "findings": [],
                "evidence": [],
                "metric_definitions": [],
                "limitations": [],
                "open_questions": [],
                "warnings": [],
                "errors": [
                    WebRunFailure(
                        phase=e.phase,
                        kind=e.kind,
                        message=safe_kinds.get(e.kind, "运行终态未确认"),
                        primary=e.primary,
                    )
                    for e in result.errors
                ],
            }
        )
