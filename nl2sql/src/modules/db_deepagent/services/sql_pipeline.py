"""SQL 校验与执行管线（docs/v4/5 §8）

核心函数只接收显式传入的只读视图（LedgerView），不修改 runtime.state；外层
把账本更新收集成 LedgerUpdate，由工具一次性放进 Command（K20）。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Mapping, Optional, Sequence

from psycopg import sql as psql
from sqlglot import exp, parse_one
from sqlglot.errors import ParseError

from src.modules.db_deepagent.config import DbAgentConfig
from src.modules.db_deepagent.context import DbAgentContext
from src.modules.db_deepagent.errors import DuplicateColumnNamesError, DbExecutionError
from src.modules.db_deepagent.schemas import (
    ColumnMeta,
    DimEvidence,
    ExecutionRecord,
    GuardEvidenceSnapshot,
    TimeEvidence,
    ValidationRecord,
)
from src.modules.db_deepagent.services.business_db import BusinessDbService
from src.modules.db_deepagent.services.result_store import ResultMeta, ResultStore
from src.modules.db_deepagent.services.run_budget import RunBudgetLedger
from src.modules.db_deepagent.services.sql_guard import SqlEvidenceGuard
from src.modules.db_deepagent.services.sql_validation import (
    DbSqlValidator,
    ValidationOutcome,
)
from src.utils.logger import get_module_logger

logger = get_module_logger("nl2sql.db_deepagent.sql_pipeline")

# §8.1 规范化：只去首尾 ASCII 空白
_ASCII_WS = " \t\r\n"

# §8.3 结果占位符
_PLACEHOLDER_RE = re.compile(
    r"\{\{result:(res_[0-9a-f]{32}):([A-Za-z_][A-Za-z0-9_]*)\}\}"
)
_SENTINEL_NAME_RE = re.compile(r"^__dbda_ph_(\d+)__$")


def normalize_sql(sql: str) -> str:
    """§8.1：strip 首尾 ASCII 空白；保留内部全部字符。"""
    return sql.strip(_ASCII_WS)


def auth_key(ctx: DbAgentContext, normalized_sql: str) -> str:
    """§5.4：授权记录键 = sha256(US 拼接的作用域字段 + 规范化文本)。"""
    payload = "\x1f".join(
        [
            ctx.thread_id,
            ctx.request_id,
            ctx.run_id,
            ctx.datasource_id,
            ctx.validation_fingerprint,
            normalized_sql,
        ]
    )
    return hashlib.sha256(payload.encode()).hexdigest()


class PipelineError(Exception):
    """管线级失败（占位符、预算、授权等）；error_type 对应 §7.1 错误包"""

    def __init__(self, error_type: str, message: str, hint: str | None = None):
        self.error_type = error_type
        self.message = message
        self.hint = hint
        super().__init__(message)


# ---------------------------------------------------------------- 账本视图


class LedgerView:
    """状态账本的只读包装（§8.2：核心函数不读写图状态）"""

    def __init__(self, state: Mapping[str, Any]):
        self._state = state

    @property
    def validations(self) -> dict[str, ValidationRecord]:
        return dict(self._state.get("db_validations") or {})

    def evidence_of_run(
        self, run_id: str
    ) -> tuple[list[TimeEvidence], list[DimEvidence]]:
        time_ev = [
            e
            for e in (self._state.get("db_time_evidence") or [])
            if e.get("run_id") == run_id
        ]
        dim_ev = [
            e
            for e in (self._state.get("db_dim_evidence") or [])
            if e.get("run_id") == run_id
        ]
        return time_ev, dim_ev


@dataclass
class LedgerUpdate:
    """一次管线调用产生的账本更新；工具合并进同一个 Command"""

    validations: dict[str, ValidationRecord] = field(default_factory=dict)
    executions: list[ExecutionRecord] = field(default_factory=list)
    time_evidence: list[TimeEvidence] = field(default_factory=list)
    dim_evidence: list[DimEvidence] = field(default_factory=list)

    def merge(self, other: "LedgerUpdate") -> "LedgerUpdate":
        self.validations.update(other.validations)
        self.executions.extend(other.executions)
        self.time_evidence.extend(other.time_evidence)
        self.dim_evidence.extend(other.dim_evidence)
        return self

    def as_state_update(self) -> dict[str, Any]:
        """转换为 Command(update=...) 的负载（空段不写，避免无效键）。"""
        update: dict[str, Any] = {}
        if self.validations:
            update["db_validations"] = self.validations
        if self.executions:
            update["db_executions"] = self.executions
        if self.time_evidence:
            update["db_time_evidence"] = self.time_evidence
        if self.dim_evidence:
            update["db_dim_evidence"] = self.dim_evidence
        return update


@dataclass
class ExecOutcome:
    """execute 的结果（§8.3）"""

    ok: bool
    error_type: Optional[str] = None
    message: Optional[str] = None
    hint: Optional[str] = None
    meta: Optional[ResultMeta] = None
    preview_rows: list[list[Any]] = field(default_factory=list)
    elapsed_ms: int = 0
    warnings: list[str] = field(default_factory=list)
    stop_reason: Optional[str] = None


# ---------------------------------------------------------------- 占位符


def _render_literal(value: Any) -> str:
    """值 → SQL 字面量（psycopg Literal；日期加类型转换，§8.3）。"""
    if isinstance(value, datetime):
        return psql.Literal(value.isoformat(sep=" ")).as_string(None) + "::timestamp"
    if isinstance(value, date):
        return psql.Literal(value.isoformat()).as_string(None) + "::date"
    return psql.Literal(value).as_string(None)


def expand_placeholders(
    text: str,
    ctx: DbAgentContext,
    *,
    guard: SqlEvidenceGuard,
    results: ResultStore,
    placeholder_max_values: int,
) -> tuple[str, list[str], list[DimEvidence]]:
    """§8.3 结果占位符展开：返回 (expanded, refs, ph_evidence)。

    失败抛 PipelineError("placeholder_error"/子码)。
    """
    if "__dbda_ph_" in text:
        # 保留前缀检查先于 {{ 短路：任何手写哨兵都拒绝（§8.3）
        raise PipelineError(
            "placeholder_malformed",
            "SQL 文本包含保留前缀 __dbda_ph_，不允许手工书写哨兵",
        )
    if "{{" not in text:
        return text, [], []
    matches = list(_PLACEHOLDER_RE.finditer(text))
    remainder = _PLACEHOLDER_RE.sub("", text)
    if "{{" in remainder:
        raise PipelineError(
            "placeholder_malformed",
            "文本中出现 {{ 但不能完整匹配 {{result:<ref>:<column>}}",
            hint="占位符语法：{{result:res_<32位十六进制>:<列名>}}",
        )
    # 哨兵替换后做位置检查（§8.3 第 2 步）
    sentinel_sql = text
    for i, m in enumerate(matches):
        sentinel_sql = sentinel_sql.replace(m.group(0), f"(__dbda_ph_{i}__)", 1)
    try:
        owners = guard.in_left_owners(sentinel_sql, len(matches))
    except ParseError as exc:
        raise PipelineError(
            "placeholder_malformed", f"占位符 SQL 解析失败: {exc}"
        ) from exc
    except ValueError as exc:
        raise PipelineError("placeholder_position_invalid", str(exc)) from exc
    ph_evidence: list[DimEvidence] = []
    refs: list[str] = []
    expanded = sentinel_sql
    from src.modules.db_deepagent.errors import ResultStoreError

    for i, m in enumerate(matches):
        res_ref, column = m.group(1), m.group(2)
        owner = owners[i]
        if owner is None:
            raise PipelineError(
                "placeholder_owner_unresolved",
                f"占位符 {m.group(0)} 的 IN 左侧列归属无法确认",
                hint="给 IN 左侧列加上表别名",
            )
        try:
            meta = results.load_meta(res_ref, session_id=ctx.session_id)
            values = results.column_values(
                res_ref, session_id=ctx.session_id, column=column
            )
        except ResultStoreError as exc:
            raise PipelineError("placeholder_error", exc.message) from exc
        if meta.datasource_id != ctx.datasource_id:
            raise PipelineError(
                "placeholder_datasource_mismatch",
                "前序结果来自另一数据源，不能用于当前查询的占位符",
                hint="在当前数据源上重新查询前序结果",
            )
        if meta.source_truncated:
            raise PipelineError(
                "placeholder_source_truncated",
                "前序结果被截断，不能用不完整集合限定范围",
                hint="改用子查询或单条 SQL 完成筛选",
            )
        uniq: list[Any] = []
        seen: set[tuple[str, str]] = set()
        for v in values:
            key = (type(v).__name__, str(v))
            if key not in seen:
                seen.add(key)
                uniq.append(v)
        if not uniq:
            raise PipelineError("placeholder_empty", "前序结果该列为空，没有可展开的值")
        if len(uniq) > placeholder_max_values:
            raise PipelineError(
                "placeholder_too_many_values",
                f"前序结果该列有 {len(uniq)} 个值，超过占位符上限 {placeholder_max_values}",
                hint="改用子查询或单条 SQL",
            )
        rendered = "(" + ", ".join(_render_literal(v) for v in uniq) + ")"
        expanded = expanded.replace(f"(__dbda_ph_{i}__)", rendered, 1)
        refs.append(res_ref)
        ph_evidence.append(
            DimEvidence(
                dim_evidence_id="D-ph-"
                + hashlib.sha256(
                    f"{res_ref}:{owner.table_key}:{owner.column}".encode()
                ).hexdigest()[:8],
                run_id=ctx.run_id,
                source="prior_result",
                table=owner.table_key,
                column=owner.column,
                values=[str(v) for v in uniq],
                patterns=[],
                query_text=None,
                ref=res_ref,
                datasource_id=meta.datasource_id,
                inherited_from=None,
            )
        )
    return expanded, refs, ph_evidence


# ---------------------------------------------------------------- 输出列检查


def _check_output_columns(expanded: str) -> Optional[ValidationOutcome]:
    """§8.2 第 3 步：最外层 SELECT 投影名重复；含 * 留给执行期。"""
    try:
        tree = parse_one(expanded, read="postgres")
    except ParseError:
        return None  # 语法层已由三层校验把关；此处解析失败不额外拒绝
    select = tree if isinstance(tree, exp.Select) else None
    if select is None:
        return None
    names: list[str] = []
    for proj in select.selects:
        if isinstance(
            proj.unalias() if isinstance(proj, exp.Alias) else proj, exp.Star
        ):
            return None  # 无法静态确定
        name = proj.alias_or_name
        if name:
            names.append(name)
    duplicates = {n for n in names if names.count(n) > 1}
    if duplicates:
        return ValidationOutcome(
            valid=False,
            layer="output_columns",
            errors=[f"输出列名重复: {sorted(duplicates)}"],
            error_codes=["duplicate_column_names"],
        )
    return None


# ---------------------------------------------------------------- 管线


class SqlPipeline:
    """validate / execute / validate_and_execute（§8.2–§8.4）"""

    def __init__(
        self,
        *,
        config: DbAgentConfig,
        validator: DbSqlValidator,
        guard: SqlEvidenceGuard,
        db: BusinessDbService,
        results: ResultStore,
        budget: RunBudgetLedger,
    ):
        self._cfg = config
        self._validator = validator
        self._guard = guard
        self._db = db
        self._results = results
        self._budget = budget

    # ------------------------------------------------------------ 校验

    def validate(
        self,
        sql: str,
        ctx: DbAgentContext,
        ledger: LedgerView,
        purpose: str = "query",
        *,
        probe_spec: Any = None,
        source_metas: Sequence[Any] = (),
        tool_call_id: str = "",
    ) -> tuple[ValidationOutcome, Optional[ValidationRecord], LedgerUpdate]:
        update = LedgerUpdate()
        text = normalize_sql(sql)
        if not text:
            return (
                ValidationOutcome(
                    False, "pipeline", ["SQL 为空"], error_codes=["invalid_argument"]
                ),
                None,
                update,
            )
        try:
            expanded, refs, ph_evidence = expand_placeholders(
                text,
                ctx,
                guard=self._guard,
                results=self._results,
                placeholder_max_values=self._cfg.results.placeholder_max_values,
            )
        except PipelineError as exc:
            message = exc.message + (f"（{exc.hint}）" if exc.hint else "")
            return (
                ValidationOutcome(
                    False, "placeholder", [message], error_codes=[exc.error_type]
                ),
                None,
                update,
            )

        outcome = self._validator.validate(expanded)
        if outcome.valid:
            dup = _check_output_columns(expanded)
            if dup is not None:
                outcome = dup
        used_ev: GuardEvidenceSnapshot = {"time": [], "dim": []}
        if outcome.valid and self._cfg.validation.guard.enabled:
            snapshot = self._pushdown_snapshots(ctx, purpose, source_metas)
            if isinstance(snapshot, ValidationOutcome):  # 下推前置检查失败
                outcome = snapshot
            else:
                run_time, run_dim = ledger.evidence_of_run(ctx.run_id)
                g = self._guard.check(
                    ctx,
                    expanded,
                    time_evidence=[*run_time, *snapshot[0]],
                    dim_evidence=[*run_dim, *ph_evidence, *snapshot[1]],
                    purpose=purpose,
                    probe_spec=probe_spec,
                )
                if not g.ok:
                    outcome = ValidationOutcome(
                        False, "guard", g.errors, error_codes=g.error_codes
                    )
                else:
                    used_ev = g.used_evidence or {"time": [], "dim": []}
        key = auth_key(ctx, text)
        if not outcome.valid:
            existing = ledger.validations.get(key)
            if existing is not None and existing.get("status") == "valid":
                update.validations[key] = {**existing, "status": "revoked"}
            return outcome, None, update
        record = ValidationRecord(
            key=key,
            status="valid",
            normalized_sql=text,
            expanded_sql_sha256=hashlib.sha256(expanded.encode()).hexdigest(),
            placeholder_refs=refs,
            purpose=purpose,
            thread_id=ctx.thread_id,
            request_id=ctx.request_id,
            run_id=ctx.run_id,
            datasource_id=ctx.datasource_id,
            validation_fingerprint=ctx.validation_fingerprint,
            layer="all_passed",
            guard_evidence=used_ev,
            warnings=outcome.warnings,
            validated_at=datetime.now().astimezone().isoformat(timespec="seconds"),
            tool_call_id=tool_call_id,
        )
        update.validations[key] = record
        return outcome, record, update

    def _pushdown_snapshots(
        self, ctx: DbAgentContext, purpose: str, source_metas: Sequence[Any]
    ) -> tuple[list[TimeEvidence], list[DimEvidence]] | ValidationOutcome:
        """§9.6 下推证据：缺快照 / 维度证据数据源不一致时直接失败。"""
        snap_time: list[TimeEvidence] = []
        snap_dim: list[DimEvidence] = []
        if purpose != "pushdown":
            return snap_time, snap_dim
        for meta in source_metas:
            ge = getattr(meta, "guard_evidence", None)
            if ge is None and isinstance(meta, Mapping):
                ge = meta.get("guard_evidence")
            if not ge:
                return ValidationOutcome(
                    False,
                    "guard",
                    ["输入结果缺少守卫证据快照，无法下推"],
                    error_codes=["pushdown_evidence_missing"],
                )
            for d in ge.get("dim") or []:
                if d.get("datasource_id") != ctx.datasource_id:
                    return ValidationOutcome(
                        False,
                        "guard",
                        ["下推输入快照中的维度证据来自另一数据源"],
                        error_codes=["evidence_datasource_mismatch"],
                    )
            snap_time.extend(ge.get("time") or [])
            snap_dim.extend(ge.get("dim") or [])
        return snap_time, snap_dim

    # ------------------------------------------------------------ 执行

    def execute(
        self,
        sql: str,
        ctx: DbAgentContext,
        ledger: LedgerView,
        purpose: str = "query",
        *,
        trusted_record: Optional[ValidationRecord] = None,
        tool_call_id: str = "",
        probe_limit: Optional[int] = None,
        row_cap: Optional[int] = None,
    ) -> tuple[ExecOutcome, LedgerUpdate]:
        update = LedgerUpdate()
        text = normalize_sql(sql)
        key = auth_key(ctx, text)
        record: Optional[ValidationRecord] = trusted_record or ledger.validations.get(
            key
        )
        if (
            record is None
            or record.get("status") != "valid"
            or record.get("key") != key
        ):
            return (
                ExecOutcome(
                    False,
                    "not_validated",
                    "SQL 未通过校验（或授权已失效），先调用 validate_sql",
                    hint="校验与执行需分步串行调用",
                ),
                update,
            )
        try:
            expanded, _, _ = expand_placeholders(
                text,
                ctx,
                guard=self._guard,
                results=self._results,
                placeholder_max_values=self._cfg.results.placeholder_max_values,
            )
        except PipelineError as exc:
            return (ExecOutcome(False, exc.error_type, exc.message, exc.hint), update)
        if hashlib.sha256(expanded.encode()).hexdigest() != record.get(
            "expanded_sql_sha256"
        ):
            return (
                ExecOutcome(
                    False,
                    "placeholder_error",
                    "占位符展开结果与校验时不一致（源结果被替换或损坏）",
                ),
                update,
            )
        reservation = self._budget.reserve(ctx.run_id, "db_execute")
        if not reservation.ok:
            return (
                ExecOutcome(
                    False,
                    "budget_exhausted",
                    "执行预算已耗尽",
                    stop_reason=reservation.stop_reason,
                ),
                update,
            )
        remaining = self._budget.remaining_seconds(ctx.run_id)
        timeout = min(self._cfg.business_db.statement_timeout_seconds, remaining)
        # 探针按 §7.6 用 limit=n 读取（读 n+1 行判定截断）；下推显式 row_cap
        # （distinct_values 读 max_values+1 判截断后存 max_values 行）；普通查
        # 询用保存上限
        effective_cap = (
            row_cap
            if row_cap is not None
            else (
                probe_limit
                if probe_limit is not None
                else self._cfg.business_db.max_saved_result_rows
            )
        )
        try:
            r = self._db.run_readonly(
                expanded, timeout_s=timeout, max_rows=effective_cap
            )
        except DuplicateColumnNamesError as exc:
            return (ExecOutcome(False, "duplicate_column_names", str(exc)), update)
        except DbExecutionError as exc:
            return (
                ExecOutcome(False, exc.error_type, exc.message, exc.hint),
                update,
            )
        columns: list[ColumnMeta] = [
            ColumnMeta(name=n, pg_type=t) for n, t in zip(r.columns, r.column_types)
        ]
        meta = self._results.save(
            ctx=ctx,
            purpose=purpose,
            sql_template=text,
            executed_sql=expanded,
            columns=columns,
            rows=r.rows,
            source_truncated=r.source_truncated,
            derived_from=[],
            guard_evidence=record.get("guard_evidence") or {"time": [], "dim": []},
        )
        evidence_id = (
            "E-" + hashlib.sha256(f"{tool_call_id}:{purpose}".encode()).hexdigest()[:8]
        )
        record_out = ExecutionRecord(
            evidence_id=evidence_id,
            tool_call_id=tool_call_id,
            run_id=ctx.run_id,
            request_id=ctx.request_id,
            purpose=purpose,
            datasource_id=ctx.datasource_id,
            sql_template=text,
            executed_sql=expanded,
            executed_sql_sha256=meta.executed_sql_sha256,
            result_ref=meta.result_ref,
            columns=columns,
            saved_row_count=meta.saved_row_count,
            source_truncated=r.source_truncated,
            elapsed_ms=r.elapsed_ms,
            warnings=[],
        )
        update.executions.append(record_out)
        preview_n = self._cfg.results.preview_rows
        return (
            ExecOutcome(
                True,
                meta=meta,
                preview_rows=[list(row) for row in r.rows[:preview_n]],
                elapsed_ms=r.elapsed_ms,
            ),
            update,
        )

    # ------------------------------------------------------------ 统一入口

    def validate_and_execute(
        self,
        sql: str,
        ctx: DbAgentContext,
        ledger: LedgerView,
        *,
        purpose: str,
        probe_spec: Any = None,
        source_metas: Sequence[Any] = (),
        tool_call_id: str = "",
        row_cap: Optional[int] = None,
    ) -> tuple[tuple[ValidationOutcome, ExecOutcome], LedgerUpdate]:
        """§8.4：服务生成 SQL（探针／下推）的统一入口。"""
        outcome, record, update = self.validate(
            sql,
            ctx,
            ledger,
            purpose,
            probe_spec=probe_spec,
            source_metas=source_metas,
            tool_call_id=tool_call_id,
        )
        if record is None:
            return (
                outcome,
                ExecOutcome(False, "validation_failed", "校验失败"),
            ), update
        exec_outcome, exec_update = self.execute(
            sql,
            ctx,
            ledger,
            purpose,
            trusted_record=record,
            tool_call_id=tool_call_id,
            probe_limit=(
                getattr(probe_spec, "limit", None) if probe_spec is not None else None
            ),
            row_cap=row_cap,
        )
        return (outcome, exec_outcome), update.merge(exec_update)
