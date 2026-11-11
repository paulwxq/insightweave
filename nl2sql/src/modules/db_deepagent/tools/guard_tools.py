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
from src.modules.db_deepagent.services.sql_pipeline import LedgerView
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
            LedgerView(runtime.state),
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
# reuse_evidence（§7.12）
# ============================================================================


def make_reuse_evidence_tool(services: DbAgentServices, storage_clock=None):
    """构造 reuse_evidence 工具（§7.12／§7.12.1）。

    storage_clock：存储生命周期时钟（复用保留期判定，禁用 ctx.now／--fixed-now），
    测试可注入固定时刻；默认实时 UTC。
    """
    from datetime import datetime, timedelta, timezone

    from src.modules.db_deepagent.errors import ResultStoreError

    clock = storage_clock or (lambda: datetime.now(timezone.utc))

    def _parse_utc(value: Any) -> datetime | None:
        if not isinstance(value, str):
            return None
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    class ReuseEvidenceArgs(RuntimeToolArgs):
        evidence_ids: list[str] = Field(
            min_length=1,
            max_length=20,
            description="time_evidence_id、dim_evidence_id，或历史执行（E-）／分析（A-）记录 ID",
        )

    @tool(args_schema=ReuseEvidenceArgs)
    def reuse_evidence(
        evidence_ids: list[str],
        runtime: ToolRuntime[DbAgentContext, DbDeepAgentState],
    ):
        """把已确认的时间／维度证据或历史执行／分析结果登记到本运行。

        只能沿用服务端持有的证据：本线程早前运行的账本记录，或线程轮换时
        携带的证据。沿用不重新计算、不产生新数据；prior_result 证据不可沿用；
        引用历史结果须在回答中写明原结果生成时间与数据源。
        """
        ctx = runtime.context
        state = runtime.state
        # 同批输入先按 ID 去重（§7.12.1）
        ids = list(dict.fromkeys(evidence_ids))
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
        by_id: dict[str, tuple[str, Any, EvidenceOrigin | None]] = {}
        for e in old_time:
            by_id[e["time_evidence_id"]] = ("time", e, None)
        for e in old_dim:
            by_id[e["dim_evidence_id"]] = ("dim", e, None)
        for e in old_executions:
            by_id[e["evidence_id"]] = ("execution", e, None)
        for a in old_analyses:
            by_id[a["evidence_id"]] = ("analysis", a, None)
        for carried in ctx.carried_evidence:
            record = carried["record"]
            key = (
                record.get("time_evidence_id")
                or record.get("dim_evidence_id")
                or record.get("evidence_id")
                or ""
            )
            if key:
                by_id[key] = (carried["kind"], record, carried["origin"])

        not_found = [i for i in ids if i not in by_id]
        prior_result_ids = [
            i for i in ids if i in by_id and by_id[i][1].get("source") == "prior_result"
        ]
        if not_found:
            return error_envelope(
                "evidence_not_found",
                "以下证据不存在或不在可沿用范围内: " + ", ".join(not_found),
                retryable=True,
                hint="只可沿用本线程账本或交接清单中的证据",
            )
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

        # ---------------- E/A 沿用检查（§7.12.1；任一失败整批拒绝） ----------------
        retention = timedelta(days=services.config.thread.result_retention_days)
        now = clock()
        verified_meta: dict[str, Any] = {}
        ea_failures: list[str] = []
        ea_code: str | None = None
        for i in ids:
            kind, old, _origin = by_id[i]
            if kind not in ("execution", "analysis"):
                continue
            ref = (
                old.get("result_ref")
                if kind == "execution"
                else old.get("output_result_ref")
            )
            try:
                meta = services.results.verify_integrity(ref, session_id=ctx.session_id)
            except ResultStoreError as exc:
                ea_code = ea_code or exc.error_type
                ea_failures.append(f"{i}: {exc.message}")
                continue
            verified_meta[i] = meta
            created = _parse_utc(meta.created_at)
            if created is not None and (now - created) >= retention:
                ea_code = ea_code or "result_expired"
                ea_failures.append(
                    f"{i}: 原结果生成于 {meta.created_at}，超过复用保留期"
                    f"（{services.config.thread.result_retention_days} 天）"
                )
                continue
            if meta.datasource_id != ctx.datasource_id:
                ea_code = ea_code or "evidence_datasource_mismatch"
                ea_failures.append(
                    f"{i}: 结果数据源 {meta.datasource_id} 与当前数据源"
                    f" {ctx.datasource_id} 不一致"
                )
                continue
            if kind == "execution" and old.get("datasource_id") != meta.datasource_id:
                ea_code = ea_code or "evidence_datasource_mismatch"
                ea_failures.append(f"{i}: 账本副本数据源与结果元数据不一致")
        if ea_failures:
            return error_envelope(
                ea_code or "internal_error",
                "历史结果沿用检查失败: " + "; ".join(ea_failures),
                retryable=True,
                hint="重新查询或分析取得本数据源的新结果",
            )

        new_time: list[TimeEvidence] = []
        new_dim: list[DimEvidence] = []
        new_executions: list[dict[str, Any]] = []
        new_analyses: list[dict[str, Any]] = []
        mapping: list[dict[str, Any]] = []

        def _origin_of(
            evidence_id: str, old: dict[str, Any], origin: EvidenceOrigin | None
        ) -> EvidenceOrigin:
            """原始来源：携带清单已给出；账本记录已是沿用结果时保留链首；
            否则由记录已知字段合成（账本记录不携带 thread_id／request_id）。"""
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
            if kind in ("execution", "analysis"):
                # §7.12.1 登记：事实字段照抄副本，链首 reused_from 不改写；
                # thread_id／request_id／run_id／datasource_id 以 ResultMeta 为准
                meta = verified_meta[evidence_id]
                root = old.get("reused_from") or EvidenceOrigin(
                    evidence_id=old.get("evidence_id", evidence_id),
                    thread_id=meta.thread_id,
                    request_id=meta.request_id,
                    run_id=meta.run_id,
                    datasource_id=meta.datasource_id,
                    user_text=None,
                )
                prefix = "E-" if kind == "execution" else "A-"
                new_id = (
                    prefix
                    + hashlib.sha256(
                        f"{ctx.run_id}:{root['evidence_id']}".encode()
                    ).hexdigest()[:8]
                )
                record = {
                    **old,
                    "evidence_id": new_id,
                    "tool_call_id": runtime.tool_call_id or "",
                    "request_id": ctx.request_id,
                    "run_id": ctx.run_id,
                    "reused_from": dict(root),
                    "result_generated_at": meta.created_at,
                }
                if kind == "execution":
                    record["purpose"] = "query"
                    new_executions.append(record)
                else:
                    new_analyses.append(record)
                mapping.append(
                    {
                        "old_id": evidence_id,
                        "new_id": new_id,
                        "kind": kind,
                        "result_ref": meta.result_ref,
                        "row_count": record.get("saved_row_count", 0),
                        "source_truncated": record.get("source_truncated", False),
                        "result_generated_at": meta.created_at,
                    }
                )
                continue
            origin_final = _origin_of(evidence_id, old, origin)
            if kind == "time":
                new_id = (
                    "T-"
                    + hashlib.sha256(
                        f"{ctx.run_id}:{evidence_id}".encode()
                    ).hexdigest()[:8]
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
                    + hashlib.sha256(
                        f"{ctx.run_id}:{evidence_id}".encode()
                    ).hexdigest()[:8]
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
        if new_executions or new_analyses:
            warnings.append("沿用历史结果须在回答中写明原结果生成时间与数据源")
        content = serialize_tool_output(
            {
                "status": "ok",
                "data": {"mapping": mapping},
                "warnings": warnings,
            }
        )
        logger.info(
            "[request_id=%s run_id=%s] reuse_evidence: time=%d dim=%d exec=%d analysis=%d",
            ctx.request_id,
            ctx.run_id,
            len(new_time),
            len(new_dim),
            len(new_executions),
            len(new_analyses),
        )
        updates: dict[str, Any] = {}
        if new_time:
            updates["db_time_evidence"] = new_time
        if new_dim:
            updates["db_dim_evidence"] = new_dim
        if new_executions:
            updates["db_executions"] = new_executions
        if new_analyses:
            updates["db_analyses"] = new_analyses
        return tool_command(
            content, runtime.tool_call_id or "", updates=updates or None
        )

    _attach_validation_handler(reuse_evidence)
    return reuse_evidence


__all__ = [
    "make_probe_dimension_values_tool",
    "make_resolve_time_range_tool",
    "make_reuse_evidence_tool",
]
