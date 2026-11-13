"""证据工具：resolve_time_range、probe_dimension_values、reuse_evidence
（docs/v4/5 §7.5、§7.6、§7.12）

时间解析、探针与证据沿用的参数核对全部由代码完成（TimeResolver、
DimensionProbeBuilder、SqlPipeline），工具层只做参数转发、账本写入与返回包。
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, Literal, Optional

from langchain.tools import ToolRuntime
from langchain_core.tools import tool
from pydantic import Field

from src.modules.db_deepagent.context import DbAgentContext
from src.modules.db_deepagent.errors import (
    TOOL_ERROR_COLUMN_NOT_FOUND,
    TOOL_ERROR_INVALID_ARGUMENT,
)
from src.modules.db_deepagent.schemas import (
    DimEvidence,
    EvidenceOrigin,
    TimeEvidence,
)
from src.modules.db_deepagent.services import DbAgentServices
from src.modules.db_deepagent.services.dimension_probe import (
    DimensionProbeBuilder,
    ProbeSpec,
    is_probeable_type,
)
from src.modules.db_deepagent.services.sql_pipeline import (
    LedgerView,
    make_ledger_view,
)
from src.modules.db_deepagent.state import DbDeepAgentState
from src.modules.db_deepagent.tools._envelope import (
    error_envelope,
    serialize_tool_output,
    tool_command,
)
from src.modules.db_deepagent.tools.schema_tools import (
    RuntimeToolArgs,
    _attach_validation_handler,
)
from src.utils.logger import get_module_logger

logger = get_module_logger("nl2sql.db_deepagent.tools.guard")

_TWO_PART_TABLE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)$")


# ============================================================================
# resolve_time_range（§7.5）
# ============================================================================


def _sql_hint(ev: TimeEvidence) -> str:
    """由证据生成的时间过滤提示（§7.5 sql_hint）。"""
    col = "<时间列>"
    if ev["start"] and ev["end"]:
        return f"{col} >= '{ev['start']}' AND {col} < '{ev['end']}'"
    if ev["start"]:
        return f"{col} >= '{ev['start']}'"
    if ev["end"]:
        return f"{col} < '{ev['end']}'"
    return ""


def make_resolve_time_range_tool(services: DbAgentServices):
    """构造 resolve_time_range 工具。"""

    class ResolveTimeRangeArgs(RuntimeToolArgs):
        source_text: str = Field(
            min_length=1, max_length=200, description="用户原文中的时间表达，逐字引用"
        )
        kind: Literal["relative", "calendar", "explicit_range", "open_range"]
        relative: Optional[
            Literal[
                "today",
                "yesterday",
                "this_week",
                "last_week",
                "this_month",
                "last_month",
                "this_quarter",
                "last_quarter",
                "this_year",
                "last_year",
                "last_n_days",
            ]
        ] = Field(default=None, description="kind=relative 时必填")
        n: Optional[int] = Field(
            default=None, ge=1, le=366, description="last_n_days 的天数"
        )
        year: Optional[int] = Field(default=None, description="calendar：年")
        quarter: Optional[int] = Field(
            default=None, ge=1, le=4, description="calendar：季度"
        )
        month: Optional[int] = Field(
            default=None, ge=1, le=12, description="calendar：月"
        )
        day: Optional[int] = Field(
            default=None, ge=1, le=31, description="calendar：日"
        )
        start_date: Optional[str] = Field(
            default=None, description="explicit_range 起始日（含），YYYY-MM-DD"
        )
        end_date_inclusive: Optional[str] = Field(
            default=None, description="explicit_range 结束日（含），YYYY-MM-DD"
        )
        boundary_date: Optional[str] = Field(
            default=None, description="open_range 边界日，YYYY-MM-DD"
        )
        relation: Optional[Literal["before", "through", "from", "after"]] = Field(
            default=None, description="open_range 单边方向"
        )

    @tool(args_schema=ResolveTimeRangeArgs)
    def resolve_time_range(
        source_text: str,
        kind: str,
        relative: str | None,
        n: int | None,
        year: int | None,
        quarter: int | None,
        month: int | None,
        day: int | None,
        start_date: str | None,
        end_date_inclusive: str | None,
        boundary_date: str | None,
        relation: str | None,
        runtime: ToolRuntime[DbAgentContext, DbDeepAgentState],
    ):
        """把用户原文中的时间表达解析为半开区间 [start, end) 并登记时间证据。

        SQL 中的时间过滤只能使用本工具返回的非空边界；单边区间恰有一端为 null。
        source_text 必须逐字取自用户问题，参数与原文的一致性由代码核对。
        """
        ctx = runtime.context
        result = services.time_resolver.resolve(
            ctx,
            source_text=source_text,
            kind=kind,
            relative=relative,
            n=n,
            year=year,
            quarter=quarter,
            month=month,
            day=day,
            start_date=start_date,
            end_date_inclusive=end_date_inclusive,
            boundary_date=boundary_date,
            relation=relation,
        )
        if not result.ok:
            content = error_envelope(
                result.error_code or "time_params_mismatch",
                result.message,
                retryable=True,
                hint=result.hint,
            )
            return tool_command(content, runtime.tool_call_id or "")
        ev = result.evidence
        data = {
            "time_evidence_id": ev["time_evidence_id"],
            "grain": ev["grain"],
            "start": ev["start"],
            "end": ev["end"],
            "timezone": ev["timezone"],
            "sql_hint": _sql_hint(ev),
        }
        content = serialize_tool_output({"status": "ok", "data": data, "warnings": []})
        logger.info(
            "[request_id=%s run_id=%s] resolve_time_range: %s -> [%s, %s)",
            ctx.request_id,
            ctx.run_id,
            source_text,
            ev["start"],
            ev["end"],
        )
        return tool_command(
            content,
            runtime.tool_call_id or "",
            updates={"db_time_evidence": [ev]},
        )

    _attach_validation_handler(resolve_time_range)
    return resolve_time_range


# ============================================================================
# probe_dimension_values（§7.6）
# ============================================================================


def make_probe_dimension_values_tool(services: DbAgentServices):
    """构造 probe_dimension_values 工具。"""
    max_values = services.config.results.probe_max_values

    class ProbeDimensionValuesArgs(RuntimeToolArgs):
        table_id: str = Field(description="候选表，两段式 schema.table")
        column: str = Field(description="候选列名")
        mode: Literal["exact", "contains", "distinct"] = Field(
            description="exact=确认值存在；contains=确认包含该文本的值存在；distinct=列出取值"
        )
        value_text: Optional[str] = Field(
            default=None,
            min_length=1,
            max_length=100,
            description="exact/contains 模式必填的待探测文本",
        )
        limit: Optional[int] = Field(
            default=None,
            ge=1,
            le=max_values,
            description="返回值上限（默认取配置 probe_max_values）",
        )

    @tool(args_schema=ProbeDimensionValuesArgs)
    def probe_dimension_values(
        table_id: str,
        column: str,
        mode: str,
        value_text: str | None,
        limit: int | None,
        runtime: ToolRuntime[DbAgentContext, DbDeepAgentState],
    ):
        """在候选列上探测维度值是否真实存在（向量检索未命中时使用）。

        探针本身经校验执行管线执行；exact/contains 命中即登记维度证据，
        distinct 列出取值（截断时只有列出的值有证据）。
        """
        ctx = runtime.context
        m = _TWO_PART_TABLE.match(table_id or "")
        if not m:
            return error_envelope(
                TOOL_ERROR_INVALID_ARGUMENT,
                "table_id 必须是两段式 schema.table",
                retryable=True,
            )
        schema_name, table_name = m.group(1), m.group(2)
        try:
            columns = services.business_db.get_columns(schema_name, table_name)
        except Exception as exc:  # 结构查询失败按列不存在处理
            logger.warning("probe get_columns 失败: %s", exc)
            columns = []
        col_types = {c.column_name: c.data_type for c in columns}
        if column not in col_types:
            return error_envelope(
                TOOL_ERROR_COLUMN_NOT_FOUND,
                f"{table_id} 中不存在列 {column}",
                retryable=True,
                hint="用 inspect_tables 查看列结构",
            )
        if not is_probeable_type(col_types[column]):
            return error_envelope(
                TOOL_ERROR_INVALID_ARGUMENT,
                f"列 {column} 类型为 {col_types[column]}，探针只支持文本或枚举列",
                retryable=True,
            )
        if mode in ("exact", "contains") and not value_text:
            return error_envelope(
                TOOL_ERROR_INVALID_ARGUMENT,
                f"{mode} 模式必须提供 value_text",
                retryable=True,
            )
        n = limit or max_values
        spec = ProbeSpec(
            mode=mode,  # type: ignore[arg-type]
            schema_name=schema_name,
            table=table_name,
            column=column,
            value=value_text,
            limit=n,
        )
        builder: DimensionProbeBuilder = services.probe
        probe_sql = builder.sql_string(spec)
        (val_outcome, exec_outcome), update = services.pipeline.validate_and_execute(
            probe_sql,
            ctx,
            make_ledger_view(services, runtime.state, ctx=ctx),
            purpose="probe",
            probe_spec=spec,
            tool_call_id=runtime.tool_call_id or "",
        )
        if not val_outcome.valid or not exec_outcome.ok:
            codes = val_outcome.error_codes or [
                exec_outcome.error_type or "internal_error"
            ]
            return tool_command(
                error_envelope(
                    exec_outcome.error_type or "validation_failed",
                    exec_outcome.message or val_outcome.errors[0] or "探针执行失败",
                    retryable=True,
                    data={"error_codes": codes},
                ),
                runtime.tool_call_id or "",
                updates=update.as_state_update() or None,
            )
        assert exec_outcome.meta is not None
        meta = exec_outcome.meta
        page = services.results.read_rows(
            meta.result_ref, session_id=ctx.session_id, offset=0, limit=n
        )
        values = [row[0] for row in page.rows]
        # 多读的那一行只用于判定截断，不进入返回与证据（§7.6）
        values_truncated = meta.source_truncated
        exec_record = update.executions[0] if update.executions else None
        evidence_id = exec_record["evidence_id"] if exec_record else ""
        warnings: list[str] = []
        if not values:
            content = serialize_tool_output(
                {
                    "status": "empty",
                    "data": {"values": [], "values_truncated": False},
                    "warnings": [],
                    "error": {
                        "error_type": "empty",
                        "message": "该列无此值，不要放宽条件或替换为相似值",
                        "retryable": True,
                        "hint": "向用户澄清该取值，或检查列是否正确",
                    },
                }
            )
            return tool_command(
                content,
                runtime.tool_call_id or "",
                updates=update.as_state_update() or None,
            )
        dim_evidence = DimEvidence(
            dim_evidence_id="D-"
            + hashlib.sha256(
                "\x1f".join(
                    [ctx.run_id, table_id, column, mode, value_text or ""]
                ).encode()
            ).hexdigest()[:8],
            run_id=ctx.run_id,
            source="db_probe",
            table=table_id,
            column=column,
            values=[str(v) for v in values],
            patterns=(
                [builder.contains_pattern(value_text or "")]
                if mode == "contains"
                else []
            ),
            query_text=value_text if mode != "distinct" else None,
            ref=evidence_id,
            datasource_id=ctx.datasource_id,
            inherited_from=None,
        )
        dim_evidence_id = dim_evidence["dim_evidence_id"]
        if mode == "distinct" and values_truncated:
            warnings.append(f"该列取值多于 limit（{n}），只有列出的值有证据")
        content = serialize_tool_output(
            {
                "status": "ok",
                "data": {
                    "values": [str(v) for v in values],
                    "values_truncated": values_truncated,
                    "evidence_id": evidence_id,
                    "dim_evidence_id": dim_evidence_id,
                    "sql": probe_sql,
                },
                "warnings": warnings,
            }
        )
        logger.info(
            "[request_id=%s run_id=%s] probe_dimension_values: %s.%s mode=%s 命中 %d 值",
            ctx.request_id,
            ctx.run_id,
            table_id,
            column,
            mode,
            len(values),
        )
        return tool_command(
            content,
            runtime.tool_call_id or "",
            updates={
                **(update.as_state_update() or {}),
                "db_dim_evidence": update.dim_evidence + [dim_evidence],
            },
        )

    _attach_validation_handler(probe_dimension_values)
    return probe_dimension_values


# ============================================================================
# reuse_evidence（§7.12；doc 6.3 §6.4 双模式重设计）
# ============================================================================


def make_reuse_evidence_tool(services: DbAgentServices, storage_clock=None):
    """构造 reuse_evidence 工具（§7.12／§7.12.1；doc 6.3 §6.4）。

    两种互斥模式：
    - T/D 模式：``evidence_ids`` 仅接收 time/dim 裸 ID，歧义拒绝；
    - E/A 模式：``source_evidence_refs`` 接收完整来源引用
      ``{thread_id, run_id, evidence_id}``（session 由 context 补齐），并必填
      目标 ``sub_task_id/sub_task_revision``；沿用的成果归属目标任务，
      历史来源保留原 run/task/revision（direct_reuse_from 保直接来源，
      reused_from 保留链首展示）。

    storage_clock：存储生命周期时钟（复用保留期判定，禁用 ctx.now／--fixed-now），
    测试可注入固定时刻；默认实时 UTC。
    """
    from datetime import datetime, timedelta, timezone

    from src.modules.db_deepagent.errors import ResultStoreError
    from src.modules.db_deepagent.persistence.session_guard import (
        SessionGuardRequired,
        validate_lease,
    )
    from src.modules.db_deepagent.services.sql_pipeline import (
        LedgerView,
        TaskAccessError,
    )
    from src.modules.db_deepagent.services.task_ledger import canonical_json

    clock = storage_clock or (lambda: datetime.now(timezone.utc))

    def _parse_utc(value):
        if not isinstance(value, str):
            return None
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    class SourceEvidenceRefItem(RuntimeToolArgs.__bases__[0]):
        """完整来源引用（doc 6.3 §6.4）：session 固定取当前会话"""

        thread_id: str = Field(min_length=1, max_length=128)
        run_id: str = Field(min_length=1, max_length=64)
        evidence_id: str = Field(min_length=1, max_length=32)

    class ReuseEvidenceArgs(RuntimeToolArgs):
        evidence_ids: list[str] = Field(
            default_factory=list,
            max_length=20,
            description="仅 T-/D- 证据 ID（时间/维度沿用模式；与 source_evidence_refs 互斥）",
        )
        source_evidence_refs: list[dict[str, Any]] = Field(
            default_factory=list,
            max_length=20,
            description=(
                "历史执行/分析沿用模式：完整来源引用 "
                "[{thread_id, run_id, evidence_id}]（取自工具返回或交接清单），"
                "与 evidence_ids 互斥"
            ),
        )
        sub_task_id: str | None = Field(
            default=None,
            max_length=8,
            description="E/A 沿用模式必填：目标任务 ID",
        )
        sub_task_revision: int | None = Field(
            default=None, ge=1, description="E/A 沿用模式必填：目标任务版本"
        )

    @tool(args_schema=ReuseEvidenceArgs)
    def reuse_evidence(
        evidence_ids: list[str],
        source_evidence_refs: list[dict[str, Any]],
        sub_task_id: str | None,
        sub_task_revision: int | None,
        runtime: ToolRuntime[DbAgentContext, DbDeepAgentState],
    ):
        """把已确认的时间／维度证据或历史执行／分析结果登记到本运行。

        T/D 沿用：evidence_ids 填时间/维度证据 ID。
        E/A 沿用：source_evidence_refs 填交接清单/工具返回中的完整来源引用
        （thread_id+run_id+evidence_id），并填目标任务 sub_task_id/
        sub_task_revision；沿用不重新计算、不产生新数据；prior_result 证据
        不可沿用；引用历史结果须在回答中写明原结果生成时间与数据源。
        """
        ctx = runtime.context
        state = runtime.state
        td_ids = [i for i in dict.fromkeys(evidence_ids or [])]
        ea_refs = list(source_evidence_refs or [])
        # 模式互斥与参数条件（schema 层同契约，函数层兜底）
        if td_ids and ea_refs:
            return error_envelope(
                TOOL_ERROR_INVALID_ARGUMENT,
                "evidence_ids（T/D 模式）与 source_evidence_refs（E/A 模式）互斥",
                retryable=True,
            )
        if not td_ids and not ea_refs:
            return error_envelope(
                TOOL_ERROR_INVALID_ARGUMENT,
                "必须提供 evidence_ids（T/D）或 source_evidence_refs（E/A）之一",
                retryable=True,
            )
        bare_ea = [i for i in td_ids if i.startswith(("E-", "A-"))]
        if bare_ea:
            return error_envelope(
                TOOL_ERROR_INVALID_ARGUMENT,
                "E-/A- 证据必须经 source_evidence_refs 提供完整来源引用，"
                "不接受裸 ID: " + ", ".join(bare_ea),
                retryable=True,
            )
        if ea_refs and (not sub_task_id or sub_task_revision is None):
            return error_envelope(
                "task_not_registered",
                "E/A 沿用必须提供目标任务 sub_task_id 与 sub_task_revision",
                retryable=True,
            )
        if td_ids and (sub_task_id or sub_task_revision is not None):
            return error_envelope(
                TOOL_ERROR_INVALID_ARGUMENT,
                "T/D 沿用模式不携带业务目标（sub_task_id/sub_task_revision）",
                retryable=True,
            )

        # doc 6.3 §6.5：沿用要求有效运行持锁凭据（运行/清理共用会话互斥）
        try:
            validate_lease(ctx.session_guard_lease, ctx.session_id)
        except SessionGuardRequired as exc:
            return error_envelope(
                "session_guard_required",
                f"缺少有效运行持锁凭据，不能沿用历史成果: {exc}",
                retryable=False,
            )

        ledger = LedgerView(state)
        old_time = [
            e
            for e in (state.get("db_time_evidence") or [])
            if e.get("run_id") != ctx.run_id
        ]
        old_dim = [
            e
            for e in (state.get("db_dim_evidence") or [])
            if e.get("run_id") != ctx.run_id
        ]
        # E- 仅 purpose="query"（探针／下推仅作轨迹，不可作为答案引用）
        old_executions = [
            e
            for e in (state.get("db_executions") or [])
            if e.get("run_id") != ctx.run_id and e.get("purpose") == "query"
        ]
        old_analyses = [
            a for a in (state.get("db_analyses") or []) if a.get("run_id") != ctx.run_id
        ]

        # ---------------- 可见来源候选索引（doc 6.3 §6.4） ----------------
        # T/D 裸 ID 候选：同 ID 多条不同记录 → 歧义拒绝
        td_by_id: dict[str, tuple[str, dict, EvidenceOrigin | None]] = {}
        td_ambiguous: set[str] = set()
        for e in old_time:
            key = e["time_evidence_id"]
            entry = ("time", e, None)
            if key in td_by_id and td_by_id[key][1] is not e:
                td_ambiguous.add(key)
            td_by_id[key] = entry
        for e in old_dim:
            key = e["dim_evidence_id"]
            entry = ("dim", e, None)
            if key in td_by_id and td_by_id[key][1] is not e:
                td_ambiguous.add(key)
            td_by_id.setdefault(key, entry)
        # E/A 完整键候选：(thread_id, run_id, evidence_id) → (kind, record, origin)
        ea_by_ref: dict[str, tuple[str, dict, EvidenceOrigin | None]] = {}
        ea_conflict: set[str] = set()

        def _ea_put(thread_id, record, kind, origin=None):
            key = canonical_json(
                [thread_id, record.get("run_id"), record.get("evidence_id")]
            )
            entry = (kind, record, origin)
            existing = ea_by_ref.get(key)
            if existing is not None and existing[1] is not record:
                # 同完整键不同事实（重复登记不同记录）→ 冲突拒绝
                if canonical_json(existing[1]) != canonical_json(record):
                    ea_conflict.add(key)
                return
            ea_by_ref[key] = entry

        for e in old_executions:
            _ea_put(ctx.thread_id, e, "execution")
        for a in old_analyses:
            _ea_put(ctx.thread_id, a, "analysis")
        for carried in ctx.carried_evidence:
            record = carried.get("record") or {}
            kind = carried.get("kind")
            origin = carried.get("origin")
            if kind in ("execution", "analysis"):
                thread = (origin or {}).get("thread_id") or ctx.thread_id
                _ea_put(thread, record, kind, origin)
            else:
                key = (
                    record.get("time_evidence_id")
                    or record.get("dim_evidence_id")
                    or ""
                )
                if key and key not in td_by_id:
                    td_by_id[key] = (kind, record, origin)

        # ---------------- T/D 模式（保留原工具名称与能力） ----------------
        if td_ids:
            return _reuse_time_dim(
                services,
                ctx,
                runtime,
                td_ids,
                td_by_id,
                td_ambiguous,
                clock,
            )

        # ---------------- E/A 模式（doc 6.3 §6.4） ----------------
        # 目标任务准入（R1-06）：完整准入——含声明依赖全部 completed，
        # 与 SQL/分析路径同一规则
        from src.modules.db_deepagent.services.sql_pipeline import (
            require_task_ready_for_work,
        )

        _task, terr, tmsg, thint = require_task_ready_for_work(
            ledger, ctx, sub_task_id, sub_task_revision
        )
        if terr is not None:
            from src.modules.db_deepagent.tools._envelope import error_envelope as _env

            return _env(terr, tmsg or "目标任务准入失败", True, hint=thint)

        def _ea_error(error_type, message, *, retryable=True, hint=None, owned=True):
            """E/A 沿用可恢复失败：统一错误包 + 错误账本（R1-08 / R2-11）。

            本闭包定义在 require_task_ready_for_work 通过之后，目标身份已
            核验；正常业务错误一律绑定该已核验目标（owned=True 默认），
            返回的 failure_ref 可直接登记 failed/tool_failed。未知目标/
            准入失败在此前用普通 error_envelope 返回，不伪造归属。
            """
            from src.modules.db_deepagent.tools._envelope import error_envelope as _env
            from src.modules.db_deepagent.tools._task_errors import (
                failure_ref_of,
                make_task_error_record,
                source_task_ref_of,
            )

            record = make_task_error_record(
                ctx=ctx,
                tool="reuse_evidence",
                error_type=error_type,
                message=message,
                retryable=retryable,
                tool_call_id=runtime.tool_call_id or "",
                source_task_ref=(
                    source_task_ref_of(ctx, sub_task_id, sub_task_revision)
                    if owned
                    else None
                ),
            )
            updates = {"db_tool_errors": [record]}
            content = _env(
                error_type,
                message,
                retryable,
                hint=hint,
                data={"failure_ref": failure_ref_of(record)},
            )
            return tool_command(content, runtime.tool_call_id or "", updates=updates)

        targets: list[tuple[str, dict, EvidenceOrigin | None]] = []
        not_found: list[str] = []
        for ref in ea_refs:
            key = canonical_json(
                [ref.get("thread_id"), ref.get("run_id"), ref.get("evidence_id")]
            )
            if key in ea_conflict:
                return _ea_error(
                    "evidence_source_conflict",
                    f"来源 {ref.get('evidence_id')} 同完整键存在不同事实记录，拒绝沿用",
                )
            entry = ea_by_ref.get(key)
            if entry is None:
                not_found.append(ref.get("evidence_id") or "?")
                continue
            # 来源 session 固定当前会话；不能指定其他会话
            targets.append(entry)
        if not_found:
            return _ea_error(
                "evidence_not_found",
                "以下来源不在本会话可见清单（本线程历史/轮换携带）中: "
                + ", ".join(not_found),
                hint="来源引用须取自交接清单或历史工具返回，不能凭完整身份任意检索",
            )
        prior_result_hits = [
            t[1].get("evidence_id")
            for t in targets
            if t[1].get("source") == "prior_result"
        ]
        if prior_result_hits:
            return error_envelope(
                TOOL_ERROR_INVALID_ARGUMENT,
                "prior_result 证据不可沿用: " + ", ".join(prior_result_hits),
                retryable=True,
                hint="在 SQL 中用结果占位符 {{result:<ref>:<column>}} 重新展开",
            )

        # 来源任务归属核验（doc 6.3 §6.4：无任务归属的旧结果不能计入新门控成果）
        no_owner = [
            t[1].get("evidence_id")
            for t in targets
            if not t[1].get("sub_task_id") or t[1].get("sub_task_revision") is None
        ]
        if no_owner:
            return _ea_error(
                "stale_task_inputs",
                "以下来源缺少来源任务归属（旧协议/过渡记录），不能登记为新门控成果: "
                + ", ".join(no_owner),
                hint="在目标任务下重新执行或分析取得新成果",
            )
        # R1-02：来源资格核验（最终有效版本/未作废/completed/选定成果/
        # 递归血缘），与 completed 登记及最终门控共享同一规则
        from src.modules.db_deepagent.services.reuse_qualification import (
            SourceUnqualified,
            verify_reuse_source,
        )

        reuse_archive = None
        if getattr(services, "store", None) is not None:
            from src.modules.db_deepagent.persistence.archive import QaArchive

            reuse_archive = QaArchive(services.store)
        for ref_in, (kind, old, _origin) in zip(ea_refs, targets):
            try:
                verify_reuse_source(
                    {
                        "session_id": ctx.session_id,
                        "thread_id": ref_in.get("thread_id", ""),
                        "run_id": ref_in.get("run_id", ""),
                        "evidence_id": ref_in.get("evidence_id", ""),
                    },
                    old,
                    session_id=ctx.session_id,
                    state=state,
                    archive=reuse_archive,
                    carried=tuple(ctx.carried_evidence or ()),
                )
            except SourceUnqualified as exc:
                return _ea_error(
                    "stale_task_inputs",
                    f"来源 {ref_in.get('evidence_id')} 资格不成立：{exc.reason}",
                    hint="来源须为原 run 最终有效 completed 任务的选定成果；"
                    "上游已 revise/void 时须在新任务下重新取得成果",
                )

        retention = timedelta(days=services.config.thread.result_retention_days)
        now = clock()
        verified_meta: dict[str, Any] = {}
        ea_failures: list[str] = []
        ea_code: str | None = None
        for evidence_id_key, (kind, old, _origin) in zip(
            [
                canonical_json(
                    [r.get("thread_id"), r.get("run_id"), r.get("evidence_id")]
                )
                for r in ea_refs
            ],
            targets,
        ):
            ref = (
                old.get("result_ref")
                if kind == "execution"
                else old.get("output_result_ref")
            )
            try:
                meta = services.results.verify_integrity(ref, session_id=ctx.session_id)
            except ResultStoreError as exc:
                ea_code = ea_code or exc.error_type
                ea_failures.append(f"{old.get('evidence_id')}: {exc.message}")
                continue
            verified_meta[evidence_id_key] = meta
            created = _parse_utc(meta.created_at)
            if created is not None and (now - created) >= retention:
                ea_code = ea_code or "result_expired"
                ea_failures.append(
                    f"{old.get('evidence_id')}: 原结果生成于 {meta.created_at}，"
                    f"超过复用保留期（{services.config.thread.result_retention_days} 天）"
                )
                continue
            if meta.datasource_id != ctx.datasource_id:
                ea_code = ea_code or "evidence_datasource_mismatch"
                ea_failures.append(
                    f"{old.get('evidence_id')}: 结果数据源 {meta.datasource_id} "
                    f"与当前数据源 {ctx.datasource_id} 不一致"
                )
                continue
            if kind == "execution" and old.get("datasource_id") != meta.datasource_id:
                ea_code = ea_code or "evidence_datasource_mismatch"
                ea_failures.append(
                    f"{old.get('evidence_id')}: 账本副本数据源与结果元数据不一致"
                )
        if ea_failures:
            return _ea_error(
                ea_code or "internal_error",
                "历史结果沿用检查失败: " + "; ".join(ea_failures),
                hint="重新查询或分析取得本数据源的新结果",
            )

        new_executions: list[dict[str, Any]] = []
        new_analyses: list[dict[str, Any]] = []
        mapping: list[dict[str, Any]] = []
        for ref_in, (kind, old, _origin) in zip(ea_refs, targets):
            source_key = canonical_json(
                [
                    ref_in.get("thread_id"),
                    ref_in.get("run_id"),
                    ref_in.get("evidence_id"),
                ]
            )
            meta = verified_meta[source_key]
            # 链首 reused_from 保留展示语义；直接来源另存（doc 6.3 §6.4）
            root = old.get("reused_from") or EvidenceOrigin(
                evidence_id=old.get("evidence_id", ref_in.get("evidence_id", "")),
                thread_id=ref_in.get("thread_id", ""),
                request_id=meta.request_id,
                run_id=ref_in.get("run_id", old.get("run_id", "")),
                datasource_id=meta.datasource_id,
                user_text=None,
            )
            direct_evidence_ref = {
                "session_id": ctx.session_id,
                "thread_id": ref_in.get("thread_id", ""),
                "run_id": ref_in.get("run_id", ""),
                "evidence_id": ref_in.get("evidence_id", ""),
            }
            direct_source_task_ref = {
                "session_id": ctx.session_id,
                "thread_id": ref_in.get("thread_id", ""),
                "run_id": ref_in.get("run_id", old.get("run_id", "")),
                "sub_task_id": old.get("sub_task_id", ""),
                "revision": old.get("sub_task_revision", 0),
            }
            prefix = "E-" if kind == "execution" else "A-"
            # doc 6.3 §6.4：沿用 ID 生成包含目标任务版本与完整直接来源身份
            new_id = (
                prefix
                + hashlib.sha256(
                    canonical_json(
                        [
                            ctx.run_id,
                            sub_task_id,
                            sub_task_revision,
                            direct_evidence_ref,
                        ]
                    ).encode()
                ).hexdigest()[:8]
            )
            record = {
                **old,
                "evidence_id": new_id,
                "tool_call_id": runtime.tool_call_id or "",
                "request_id": ctx.request_id,
                "run_id": ctx.run_id,
                # R2-10：沿用登记产生的新记录保存自身完整可信 scope；
                # 不沿用结果文件的原生成线程/会话，也不从 ResultMeta 补造。
                "session_id": ctx.session_id,
                "thread_id": ctx.thread_id,
                "reused_from": dict(root),
                "direct_reuse_from": {
                    "evidence_ref": direct_evidence_ref,
                    "source_task_ref": direct_source_task_ref,
                },
                "sub_task_id": sub_task_id,
                "sub_task_revision": sub_task_revision,
                "result_generated_at": meta.created_at,
            }
            if kind == "execution":
                record["purpose"] = "query"
                new_executions.append(record)
            else:
                new_analyses.append(record)
            mapping.append(
                {
                    "old_id": ref_in.get("evidence_id"),
                    "new_id": new_id,
                    "kind": kind,
                    # R1-07：source_evidence_ref=新登记证据的完整引用（后续
                    # 沿用/清单用同一身份）；origin_evidence_ref 保留直接来源
                    "source_evidence_ref": {
                        "session_id": ctx.session_id,
                        "thread_id": ctx.thread_id,
                        "run_id": ctx.run_id,
                        "evidence_id": new_id,
                    },
                    "origin_evidence_ref": direct_evidence_ref,
                    "result_ref": meta.result_ref,
                    "row_count": record.get("saved_row_count", 0),
                    "source_truncated": record.get("source_truncated", False),
                    "result_generated_at": meta.created_at,
                    "sub_task_id": sub_task_id,
                    "sub_task_revision": sub_task_revision,
                }
            )
        warnings = [
            "沿用口径须在回答中写明（原始表达与区间/取值）",
            "沿用历史结果须在回答中写明原结果生成时间与数据源",
        ]
        content = serialize_tool_output(
            {
                "status": "ok",
                "data": {"mapping": mapping},
                "warnings": warnings,
            }
        )
        logger.info(
            "[request_id=%s run_id=%s] reuse_evidence(E/A): exec=%d analysis=%d target=%s@%s",
            ctx.request_id,
            ctx.run_id,
            len(new_executions),
            len(new_analyses),
            sub_task_id,
            sub_task_revision,
        )
        updates: dict[str, Any] = {}
        if new_executions:
            updates["db_executions"] = new_executions
        if new_analyses:
            updates["db_analyses"] = new_analyses
        return tool_command(
            content, runtime.tool_call_id or "", updates=updates or None
        )

    _attach_validation_handler(reuse_evidence)
    return reuse_evidence


def _reuse_time_dim(services, ctx, runtime, ids, by_id, ambiguous, clock):
    """T/D 沿用模式（保留原工具名称与能力；歧义拒绝不按遍历序取值）。"""
    not_found = [i for i in ids if i not in by_id]
    ambiguous_hits = [i for i in ids if i in ambiguous]
    if ambiguous_hits:
        return error_envelope(
            "evidence_source_ambiguous",
            "以下 T/D 证据 ID 在可见候选中不唯一，须改用可唯一寻址的来源: "
            + ", ".join(ambiguous_hits),
            retryable=True,
        )
    if not_found:
        return error_envelope(
            "evidence_not_found",
            "以下证据不存在或不在可沿用范围内: " + ", ".join(not_found),
            retryable=True,
            hint="只可沿用本线程账本或交接清单中的证据",
        )
    prior_result_ids = [i for i in ids if by_id[i][1].get("source") == "prior_result"]
    if prior_result_ids:
        return error_envelope(
            TOOL_ERROR_INVALID_ARGUMENT,
            "prior_result 证据不可沿用: " + ", ".join(prior_result_ids),
            retryable=True,
            hint="在 SQL 中用结果占位符 {{result:<ref>:<column>}} 重新展开",
        )
    dim_mismatch = {
        i: by_id[i][1].get("datasource_id")
        for i in ids
        if by_id[i][0] == "dim"
        and by_id[i][1].get("datasource_id") != ctx.datasource_id
    }
    if dim_mismatch:
        return error_envelope(
            "evidence_datasource_mismatch",
            "维度证据来自其他数据源: "
            + ", ".join(f"{k}({v})" for k, v in dim_mismatch.items()),
            retryable=True,
            hint="用 search_schema / probe_dimension_values 在当前数据源重新取得证据",
        )

    new_time: list[TimeEvidence] = []
    new_dim: list[DimEvidence] = []
    mapping: list[dict[str, Any]] = []

    def _origin_of(evidence_id, old, origin):
        if origin is not None:
            return origin
        existing = old.get("inherited_from")
        if existing:
            return existing
        return EvidenceOrigin(
            evidence_id=evidence_id,
            thread_id=ctx.thread_id,
            request_id="",
            run_id=old.get("run_id", ""),
            datasource_id=old.get("datasource_id", ""),
            user_text=old.get("source_text") or old.get("query_text"),
        )

    for evidence_id in ids:
        kind, old, origin = by_id[evidence_id]
        origin_final = _origin_of(evidence_id, old, origin)
        if kind == "time":
            new_id = (
                "T-"
                + hashlib.sha256(f"{ctx.run_id}:{evidence_id}".encode()).hexdigest()[:8]
            )
            record = TimeEvidence(
                **{
                    **old,
                    "time_evidence_id": new_id,
                    "run_id": ctx.run_id,
                    "inherited_from": origin_final,
                }
            )
            new_time.append(record)
            mapping.append(
                {
                    "old_id": evidence_id,
                    "new_id": new_id,
                    "kind": "time",
                    "interval": [old.get("start"), old.get("end")],
                }
            )
        else:
            new_id = (
                "D-"
                + hashlib.sha256(f"{ctx.run_id}:{evidence_id}".encode()).hexdigest()[:8]
            )
            record = DimEvidence(
                **{
                    **old,
                    "dim_evidence_id": new_id,
                    "run_id": ctx.run_id,
                    "inherited_from": origin_final,
                }
            )
            new_dim.append(record)
            mapping.append(
                {
                    "old_id": evidence_id,
                    "new_id": new_id,
                    "kind": "dim",
                    "values": old.get("values"),
                }
            )
    warnings = ["沿用口径须在回答中写明（原始表达与区间/取值）"]
    content = serialize_tool_output(
        {
            "status": "ok",
            "data": {"mapping": mapping},
            "warnings": warnings,
        }
    )
    logger.info(
        "[request_id=%s run_id=%s] reuse_evidence(T/D): time=%d dim=%d",
        ctx.request_id,
        ctx.run_id,
        len(new_time),
        len(new_dim),
    )
    updates: dict[str, Any] = {}
    if new_time:
        updates["db_time_evidence"] = new_time
    if new_dim:
        updates["db_dim_evidence"] = new_dim
    return tool_command(content, runtime.tool_call_id or "", updates=updates or None)


__all__ = [
    "make_probe_dimension_values_tool",
    "make_resolve_time_range_tool",
    "make_reuse_evidence_tool",
]
