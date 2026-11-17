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
from src.modules.db_deepagent.state import task_ledger_key
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


def auth_key(
    ctx: DbAgentContext,
    normalized_sql: str,
    *,
    purpose: str | None = None,
    sub_task_id: str | None = None,
    sub_task_revision: int | None = None,
) -> str:
    """授权记录键 = sha256(US 拼接的作用域字段 + 规范化文本)。

    doc 6.3 §6.1：键升级为既有作用域加 purpose + sub_task_id + revision
    （无歧义规范编码）：A 校验的 SQL 不能用于 B 执行，旧 revision 授权
    不能用于新版；同 SQL 的不同任务/版本/purpose 各自独立授权。probe
    使用独立 None 作用域（空段）。
    """
    payload = "\x1f".join(
        [
            ctx.thread_id,
            ctx.request_id,
            ctx.run_id,
            ctx.datasource_id,
            ctx.validation_fingerprint,
            purpose or "",
            sub_task_id or "",
            "" if sub_task_revision is None else str(int(sub_task_revision)),
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


class TaskAccessError(Exception):
    """任务账本准入/读取失败（doc 6.3 §4.3/§6.1；error_type 对应 §7.3 错误码）"""

    def __init__(
        self,
        error_type: str,
        message: str,
        hint: str | None = None,
        task_refs: list[str] | None = None,
    ):
        self.error_type = error_type
        self.message = message
        self.hint = hint
        self.task_refs = task_refs or []
        super().__init__(message)


def resolve_placeholder_lineage(
    ledger: "LedgerView",
    ctx: DbAgentContext,
    ph_refs: list[str],
    sub_task_id: str | None,
) -> tuple[list[dict[str, Any]] | None, str | None, str | None]:
    """解析占位符输入的结构化血缘（R1-03：validate/execute 共享）。

    返回 (lineage, error_type, message)：lineage 非 None 表示通过。
    - 每个 ref 唯一归属当前运行的任务版本（歧义/缺失拒绝）；
    - 同任务来源必须为当前 revision（旧 revision 不得用于新版）；
    - 跨任务来源必须为已声明依赖的固定版本；
    - R3-02：构造完整 lineage 后，在 EXPLAIN/授权/实际执行之前调用共享
      消费资格规则，核验跨任务选定成果与全部祖先（含沿用记录的完整来源
      图）；失败返回受控错误，不产生任何校验/执行副作用。
    """
    if not ph_refs:
        return [], None, None
    task = ledger.current_tasks(ctx.run_id).get(sub_task_id) if sub_task_id else None
    declared = {
        (d.get("sub_task_id"), d.get("revision"))
        for d in ((task or {}).get("dependencies") or [])
    }
    lineage: list[dict[str, Any]] = []
    for res_ref in ph_refs:
        candidates = ledger.records_by_result_ref(res_ref)
        owned = [
            r
            for r in candidates
            if r.get("sub_task_id") is not None and r.get("run_id") == ctx.run_id
        ]
        distinct = {(r.get("sub_task_id"), r.get("sub_task_revision")) for r in owned}
        if not owned or len(distinct) != 1:
            return (
                None,
                "stale_task_inputs",
                f"占位符来源 {res_ref} 无法唯一归属当前运行的任务版本"
                "（缺失、歧义或非本轮成果）；不能猜测归属",
            )
        source = owned[0]
        source_task_id = source.get("sub_task_id")
        source_rev = source.get("sub_task_revision")
        if source_task_id == sub_task_id:
            if source_rev != (task or {}).get("revision"):
                return (
                    None,
                    "stale_task_inputs",
                    f"占位符来源属于本任务旧 revision={source_rev}（当前 "
                    f"{(task or {}).get('revision')}），不能把上次版本的结果"
                    "用于新版任务",
                )
        elif (source_task_id, source_rev) not in declared:
            return (
                None,
                "stale_task_inputs",
                f"占位符来源属于任务 {source_task_id}@{source_rev}，"
                "未在本任务声明依赖中（固定版本）",
            )
        lineage.append(
            {
                "result_ref": res_ref,
                "evidence_ref": {
                    "session_id": ctx.session_id,
                    "thread_id": ctx.thread_id,
                    "run_id": source.get("run_id", ""),
                    "evidence_id": source.get("evidence_id", ""),
                },
                "source_task_ref": {
                    "session_id": ctx.session_id,
                    "thread_id": ctx.thread_id,
                    "run_id": source.get("run_id", ""),
                    "sub_task_id": source_task_id or "",
                    "revision": source_rev or 0,
                },
            }
        )
    # R3-02：共享消费资格前置核验（完整身份解析 + 全部祖先递归）。
    # 未选中的跨任务成果或失效祖先在此拒绝，EXPLAIN/DB 执行/授权/结果
    # 都不会产生；同任务探索输入与已选上游成果继续合法。
    if task is not None:
        from src.modules.db_deepagent.services.reuse_qualification import (
            SourceUnqualified,
            evidence_edges,
            verify_consumed_edges,
        )

        try:
            edges = evidence_edges(
                {"input_result_refs": lineage}, session_id=ctx.session_id
            )
        except SourceUnqualified as exc:
            return None, "stale_task_inputs", f"占位符输入血缘畸形：{exc.reason}"
        ok, reason = verify_consumed_edges(
            edges,
            session_id=ctx.session_id,
            state=ledger.state,
            archive=ledger.archive,
            carried=ledger.carried,
            consumer_task=task,
            consumer_run=ctx.run_id,
            consumer_thread=ctx.thread_id,
            consumer_task_id=sub_task_id or "",
            consumer_task_rev=(task.get("revision") if task else None),
        )
        if not ok:
            return (
                None,
                "stale_task_inputs",
                f"占位符输入资格不成立：{reason}",
            )
    return lineage, None, None


def make_ledger_view(
    services: Any, state: Mapping[str, Any], *, ctx: Any = None
) -> "LedgerView":
    """装配带只读归档/轮换携带的 LedgerView（R3-01/02 工具侧共享）。

    store 未注入时（只读/单测环境）退化为仅线程账本，不改变既有行为。
    """
    archive = None
    if getattr(services, "store", None) is not None:
        from src.modules.db_deepagent.persistence.archive import QaArchive

        archive = QaArchive(services.store)
    carried = tuple(getattr(ctx, "carried_evidence", ()) or ()) if ctx else ()
    return LedgerView(state, archive=archive, carried=carried)


def require_session_lease(ctx: DbAgentContext) -> tuple[str | None, str | None]:
    """服务侧会话守卫校验（R1-06）：占位符/校验/执行服务要求有效运行
    持锁凭据；直接内部调用同样必须满足。返回 (error_type, message)。
    """
    from src.modules.db_deepagent.persistence.session_guard import (
        SessionGuardRequired,
        validate_lease,
    )

    try:
        validate_lease(
            getattr(ctx, "session_guard_lease", None),
            getattr(ctx, "session_id", ""),
        )
    except SessionGuardRequired as exc:
        return "session_guard_required", f"缺少有效运行持锁凭据: {exc}"
    return None, None


def require_task_ready_for_work(
    ledger: "LedgerView",
    ctx: DbAgentContext,
    sub_task_id: str | None,
    sub_task_revision: int | None,
    *,
    require_dependencies_completed: bool = True,
) -> tuple[dict[str, Any] | None, str | None, str | None, str | None]:
    """副作用前的任务准入（doc 6.3 §6.1）。

    返回 (record, error_type, message, hint)：record 非空表示通过。
    - query/pushdown 必须提供目标任务；probe 传 None（独立作用域）。
    - 目标必须当前、未作废、pending/in_progress（task_not_writable 等）。
    - 声明依赖必须全部为当前、未作废且 completed（§5.2：校验/执行/分析
      前置；首版无非 completed 依赖的执行例外）。
    """
    if not sub_task_id or sub_task_revision is None:
        return (
            None,
            "task_not_registered",
            "本工具需要目标任务版本（sub_task_id + sub_task_revision）；请先 record_sub_tasks 登记",
            "探针类操作不经本入口",
        )
    try:
        record = ledger.require_writable_task(
            ctx.run_id, sub_task_id, sub_task_revision
        )
    except TaskAccessError as exc:
        return None, exc.error_type, exc.message, exc.hint
    if require_dependencies_completed:
        tasks = ledger.current_tasks(ctx.run_id)
        for dep in record.get("dependencies") or []:
            target = tasks.get(dep.get("sub_task_id"))
            if (
                target is None
                or target.get("revision") != dep.get("revision")
                or target.get("status") == "voided"
            ):
                return (
                    None,
                    "invalid_task_dependency",
                    f"任务 {sub_task_id} 的依赖 {dep.get('sub_task_id')}"
                    f"@{dep.get('revision')} 已失效（非当前版本或已作废）",
                    "先 revise 重绑依赖或作废该任务",
                )
            if target.get("status") != "completed":
                return (
                    None,
                    "stale_task_inputs",
                    f"任务 {sub_task_id} 的依赖 {dep.get('sub_task_id')}"
                    f"@{dep.get('revision')} 尚未 completed，"
                    "下游校验/执行/分析必须等待依赖完成",
                    None,
                )
    return record, None, None, None


# ---------------------------------------------------------------- 账本视图


class LedgerView:
    """状态账本的只读包装（§8.2：核心函数不读写图状态；doc 6.3 §4.3 扩展）"""

    def __init__(
        self,
        state: Mapping[str, Any],
        *,
        archive: Any = None,
        carried: tuple[Any, ...] = (),
    ):
        self._state = state
        # R3-01/02：工具装配时注入只读归档与轮换携带，供共享证据图核验；
        # 缺省为仅有线程账本（兼容只读/现有调用）。
        self.archive = archive
        self.carried = carried

    @property
    def state(self) -> Mapping[str, Any]:
        """只读原始状态（共享证据图解析用，不写账本）。"""
        return self._state

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

    # ------------------------------------------------------------ 任务视图（doc 6.3 §4.3）

    def current_tasks(
        self, run_id: str, *, include_voided: bool = True
    ) -> dict[str, dict[str, Any]]:
        """先取最大 revision 再判断状态（§4.2；不做 void 预过滤）。"""
        from src.modules.db_deepagent.services.task_ledger import (
            LedgerConsistencyError as _LCE,
        )
        from src.modules.db_deepagent.services.task_ledger import current_task_view

        try:
            return current_task_view(
                self._state.get("db_sub_tasks") or {},
                run_id,
                include_voided=include_voided,
            )
        except _LCE as exc:
            raise TaskAccessError("task_ledger_corrupt", str(exc)) from exc

    def task_plan_version(self, run_id: str) -> int:
        record = (self._state.get("db_task_plans") or {}).get(run_id) or {}
        version = record.get("plan_version", 0)
        return version if type(version) is int else 0

    def require_current_task(
        self, run_id: str, sub_task_id: str, revision: int | None
    ) -> dict[str, Any]:
        """拒绝未知、旧版或已作废目标（§4.3）。"""
        tasks = self.current_tasks(run_id)
        record = tasks.get(sub_task_id)
        if record is None:
            raise TaskAccessError(
                "task_not_registered",
                f"任务 {sub_task_id} 不在本运行账本中",
                task_refs=[sub_task_id],
            )
        if revision is None or record.get("revision") != revision:
            raise TaskAccessError(
                "task_revision_mismatch",
                f"任务 {sub_task_id} 当前版本为 revision={record.get('revision')}，"
                f"请求 revision={revision}",
                task_refs=[sub_task_id],
            )
        if record.get("status") == "voided":
            raise TaskAccessError(
                "task_not_registered",
                f"任务 {sub_task_id}@{revision} 已作废，不能产生新成果；"
                "恢复需求请新增任务 ID",
                task_refs=[sub_task_id],
            )
        return record

    def require_writable_task(
        self, run_id: str, sub_task_id: str, revision: int | None
    ) -> dict[str, Any]:
        """身份校验后仅允许 pending/in_progress 产生新授权/执行/分析/沿用（§4.3）。"""
        record = self.require_current_task(run_id, sub_task_id, revision)
        if record.get("status") not in ("pending", "in_progress"):
            raise TaskAccessError(
                "task_not_writable",
                f"任务 {sub_task_id}@{revision} 已处于终态（{record.get('status')}），"
                "不能继续查库；completed 补证须先 revise，作废请 void",
                task_refs=[sub_task_id],
            )
        return record

    def executions_of_run(self, run_id: str) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in (self._state.get("db_executions") or [])
            if r.get("run_id") == run_id
        ]

    def analyses_of_run(self, run_id: str) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in (self._state.get("db_analyses") or [])
            if r.get("run_id") == run_id
        ]

    def evidence_by_id(self, run_id: str) -> dict[str, dict[str, Any]]:
        """本轮可引用证据索引（query E- 与分析 A-，含沿用登记）。"""
        out: dict[str, dict[str, Any]] = {}
        for record in self._state.get("db_executions") or []:
            if record.get("run_id") == run_id and record.get("purpose") == "query":
                out[record["evidence_id"]] = dict(record)
        for record in self._state.get("db_analyses") or []:
            if record.get("run_id") == run_id:
                out[record["evidence_id"]] = dict(record)
        return out

    def records_by_result_ref(self, result_ref: str) -> list[dict[str, Any]]:
        """按 result_ref 查找执行/分析记录（§6.3：不能随便取第一条）。"""
        out: list[dict[str, Any]] = []
        for record in self._state.get("db_executions") or []:
            if record.get("result_ref") == result_ref:
                out.append(dict(record))
        for record in self._state.get("db_analyses") or []:
            if record.get("output_result_ref") == result_ref:
                out.append(dict(record))
        return out

    def error_by_ref(self, error_ref: Mapping[str, Any]) -> dict[str, Any]:
        """完整错误引用读取（§4.3）：未知报未找到；重复/缺身份报一致性错误。"""
        from src.modules.db_deepagent.services.task_ledger import canonical_json

        def _identity(record: Mapping[str, Any]) -> tuple | None:
            parts = (
                record.get("session_id"),
                record.get("thread_id"),
                record.get("run_id"),
                record.get("error_id"),
            )
            if not all(isinstance(part, str) and part for part in parts):
                return None
            return parts

        wanted = (
            error_ref.get("session_id"),
            error_ref.get("thread_id"),
            error_ref.get("run_id"),
            error_ref.get("error_id"),
        )
        found: dict[str, Any] | None = None
        duplicates = 0
        for record in self._state.get("db_tool_errors") or []:
            identity = _identity(record) if isinstance(record, Mapping) else None
            if identity is None:
                raise TaskAccessError("task_ledger_corrupt", "错误记录缺失完整身份")
            if identity == wanted:
                duplicates += 1
                found = dict(record)
        if duplicates > 1:
            raise TaskAccessError(
                "task_ledger_corrupt",
                f"error_id={error_ref.get('error_id')!r} 同完整引用重复",
            )
        if found is None:
            raise TaskAccessError(
                "invalid_task_failure_ref",
                f"error_id={error_ref.get('error_id')!r} 不在服务端错误账本中",
            )
        return found

    def task_snapshot(self, full_task_ref: Mapping[str, Any]) -> dict[str, Any]:
        """按完整身份读取任务记录（当前线程状态内；历史归档走 QaArchive）。"""
        if full_task_ref.get("run_id") and full_task_ref.get("sub_task_id"):
            revision = full_task_ref.get("revision")
            if type(revision) is not int:
                raise TaskAccessError(
                    "task_ledger_corrupt", "task_snapshot 缺少合法 revision"
                )
            key = task_ledger_key(
                full_task_ref["run_id"], full_task_ref["sub_task_id"], revision
            )
            record = (self._state.get("db_sub_tasks") or {}).get(key)
            if record is None:
                raise TaskAccessError(
                    "invalid_task_failure_ref",
                    "任务快照不存在（可能已被清理或归属其他线程）",
                )
            return dict(record)
        raise TaskAccessError("task_ledger_corrupt", "task_snapshot 身份不完整")


@dataclass
class LedgerUpdate:
    """一次管线调用产生的账本更新；工具合并进同一个 Command"""

    validations: dict[str, ValidationRecord] = field(default_factory=dict)
    executions: list[ExecutionRecord] = field(default_factory=list)
    time_evidence: list[TimeEvidence] = field(default_factory=list)
    dim_evidence: list[DimEvidence] = field(default_factory=list)
    tool_errors: list[dict[str, Any]] = field(default_factory=list)  # doc 6.3 §5.1

    def merge(self, other: "LedgerUpdate") -> "LedgerUpdate":
        self.validations.update(other.validations)
        self.executions.extend(other.executions)
        self.time_evidence.extend(other.time_evidence)
        self.dim_evidence.extend(other.dim_evidence)
        self.tool_errors.extend(other.tool_errors)
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
        if self.tool_errors:
            update["db_tool_errors"] = self.tool_errors
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
        sub_task_id: str | None = None,
        sub_task_revision: int | None = None,
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
        # R1-06：服务侧会话守卫校验（占位符读取也在保护窗口内）
        lease_err, lease_msg = require_session_lease(ctx)
        if lease_err is not None:
            return (
                ValidationOutcome(False, "task", [lease_msg], error_codes=[lease_err]),
                None,
                update,
            )
        # doc 6.3 §6.1：副作用前任务准入（query/pushdown 必填目标；probe 独立 None）
        task_record: dict[str, Any] | None = None
        if purpose in ("query", "pushdown"):
            task_record, terr, tmsg, thint = require_task_ready_for_work(
                ledger, ctx, sub_task_id, sub_task_revision
            )
            if terr is not None:
                message = (tmsg or "") + (f"（{thint}）" if thint else "")
                return (
                    ValidationOutcome(False, "task", [message], error_codes=[terr]),
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

        # R1-03：占位符输入资格在 EXPLAIN 等校验副作用前核验（同任务
        # 当前 revision / 声明依赖固定版本 / 唯一归属）；失败不产生授权。
        _lineage, lineage_err, lineage_msg = resolve_placeholder_lineage(
            ledger, ctx, refs, sub_task_id
        )
        if lineage_err is not None:
            return (
                ValidationOutcome(
                    False,
                    "task",
                    [lineage_msg or "占位符输入血缘不可核验"],
                    error_codes=[lineage_err],
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
        key = auth_key(
            ctx,
            text,
            purpose=purpose,
            sub_task_id=sub_task_id,
            sub_task_revision=sub_task_revision,
        )
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
            sub_task_id=(
                sub_task_id
                if purpose in ("query", "pushdown") and sub_task_id
                else None
            ),
            sub_task_revision=(
                sub_task_revision
                if purpose in ("query", "pushdown") and sub_task_revision is not None
                else None
            ),
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
        sub_task_id: str | None = None,
        sub_task_revision: int | None = None,
    ) -> tuple[ExecOutcome, LedgerUpdate]:
        update = LedgerUpdate()
        text = normalize_sql(sql)
        # R1-06：服务侧会话守卫校验
        lease_err, lease_msg = require_session_lease(ctx)
        if lease_err is not None:
            return (ExecOutcome(False, lease_err, lease_msg), update)
        # doc 6.3 §6.1：执行前任务准入（probe 独立 None 作用域）
        if purpose in ("query", "pushdown"):
            _task_record, terr, tmsg, thint = require_task_ready_for_work(
                ledger, ctx, sub_task_id, sub_task_revision
            )
            if terr is not None:
                message = (tmsg or "") + (f"（{thint}）" if thint else "")
                return (ExecOutcome(False, terr, message, thint), update)
        key = auth_key(
            ctx,
            text,
            purpose=purpose,
            sub_task_id=sub_task_id,
            sub_task_revision=sub_task_revision,
        )
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
                    hint="校验与执行需分步串行调用；确认使用同一 sub_task_id/"
                    "sub_task_revision 与 purpose",
                ),
                update,
            )
        try:
            expanded, ph_refs, _ = expand_placeholders(
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
        # doc 6.3 §6.3 + R1-03：占位符输入血缘由共享解析器核验
        # （同任务当前 revision/声明依赖固定版本/唯一归属），失败即拒绝。
        input_lineage, lineage_err, lineage_msg = resolve_placeholder_lineage(
            ledger, ctx, ph_refs, sub_task_id
        )
        if lineage_err is not None:
            return (
                ExecOutcome(
                    False,
                    lineage_err,
                    lineage_msg or "占位符输入血缘不可核验",
                    hint="直接消费该结果的任务应改用 analyze_result 声明输入",
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
            source_task=(
                {
                    "session_id": ctx.session_id,
                    "thread_id": ctx.thread_id,
                    "run_id": ctx.run_id,
                    "sub_task_id": sub_task_id,
                    "revision": sub_task_revision,
                }
                if sub_task_id and sub_task_revision is not None
                else None
            ),
        )
        evidence_id = (
            "E-" + hashlib.sha256(f"{tool_call_id}:{purpose}".encode()).hexdigest()[:8]
        )
        record_out = ExecutionRecord(
            evidence_id=evidence_id,
            tool_call_id=tool_call_id,
            run_id=ctx.run_id,
            request_id=ctx.request_id,
            # R2-10：记录自身完整可信 scope（轮换携带沿用记录自身身份）
            session_id=ctx.session_id,
            thread_id=ctx.thread_id,
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
            sub_task_id=sub_task_id if purpose != "probe" else None,
            sub_task_revision=sub_task_revision if purpose != "probe" else None,
            input_result_refs=input_lineage,
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
        sub_task_id: str | None = None,
        sub_task_revision: int | None = None,
    ) -> tuple[tuple[ValidationOutcome, ExecOutcome], LedgerUpdate]:
        """§8.4：服务生成 SQL（探针／下推）的统一入口。

        doc 6.3 §6.1：透传任务版本；pushdown 继承分析任务；probe 使用
        独立 None 作用域。
        """
        outcome, record, update = self.validate(
            sql,
            ctx,
            ledger,
            purpose,
            probe_spec=probe_spec,
            source_metas=source_metas,
            tool_call_id=tool_call_id,
            sub_task_id=sub_task_id,
            sub_task_revision=sub_task_revision,
        )
        if record is None:
            return (
                outcome,
                ExecOutcome(
                    False,
                    (
                        outcome.error_codes[0]
                        if outcome.error_codes
                        else "validation_failed"
                    ),
                    outcome.errors[0] if outcome.errors else "校验失败",
                ),
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
            sub_task_id=sub_task_id,
            sub_task_revision=sub_task_revision,
        )
        return (outcome, exec_outcome), update.merge(exec_update)
