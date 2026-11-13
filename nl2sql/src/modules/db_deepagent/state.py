"""扩展状态 DbDeepAgentState（docs/v4/5 §5.2；doc 6.3 §4.1/§5.1/§13.7）

按概要设计 §9.3 与 K11，继承 DeepAgentState，不覆盖 messages。新增字段全部标注
PrivateStateAttr（K13）：调用方不能通过 invoke 输入注入，模型也看不到；
Runner 通过 get_state().values 读取。

勘误（相对 §5.2 代码示例）：metadata 顺序必须为 [PrivateStateAttr, reducer]。
langgraph `_is_field_binop` 只把 metadata 末元素识别为 reducer（state.py:1904-1910），
而 PrivateStateAttr 是 OmitFromSchema 实例（非 callable）——若垫在末位，reducer 被
静默丢弃，通道退化为 LastValue；langchain 侧 OmitFromSchema 识别扫描全部 metadata
（factory.py:_resolve_schema），marker 在前不影响输入/输出 Schema 剔除。

doc 6.3：新增 db_sub_tasks（按 (run_id, sub_task_id, revision) 规范编码键）与
db_task_plans（按 run_id 键）两个专用 reducer 通道；db_tool_errors 由
operator.add 改为 append_dedup_tool_errors()（完整 error_ref 去重、异事实报错、
左右输入与单侧内部均校验）。
"""

from __future__ import annotations

import json
import operator
from typing import Annotated, Any, NotRequired

from deepagents import DeepAgentState
from langchain.agents.middleware.types import PrivateStateAttr

from src.modules.db_deepagent.schemas import (
    AnalysisRecord,
    DimEvidence,
    ExecutionRecord,
    FinalAnswerRecord,
    RunStatusRecord,
    SubTaskRecord,
    TaskPlanRecord,
    TimeEvidence,
    ToolErrorRecord,
    ValidationRecord,
)


def merge_by_key(left: dict | None, right: dict | None) -> dict:
    """浅合并字典，右值覆盖左值（授权记录的创建与撤销、运行状态与最终答案）。"""
    if left is None and right is None:
        return {}
    if left is None:
        return dict(right)
    if right is None:
        return dict(left)
    merged = dict(left)
    merged.update(right)
    return merged


class LedgerConsistencyError(Exception):
    """同 evidence_id 两次登记的事实字段不一致（reuse 幂等冲突，§7.12.1）"""


def _append_dedup(
    left: list | None,
    right: list | None,
    fact_fields: tuple[str, ...],
) -> list:
    """按 evidence_id 去重追加：同 ID 同事实保留首条；同 ID 异事实抛一致性错误。

    比较只针对事实字段，排除 tool_call_id 等登记调用字段（允许不同）；
    其余字段（run_id／request_id）在同 ID 下本就相同。
    """
    if not left:
        return list(right or [])
    if not right:
        return list(left)
    merged = list(left)
    seen = {r["evidence_id"]: r for r in merged}
    for rec in right:
        existing = seen.get(rec["evidence_id"])
        if existing is None:
            merged.append(rec)
            seen[rec["evidence_id"]] = rec
            continue
        for field in fact_fields:
            if existing.get(field) != rec.get(field):
                raise LedgerConsistencyError(
                    f"evidence_id={rec['evidence_id']} 的 {field} 不一致："
                    f"{existing.get(field)!r} != {rec.get(field)!r}"
                )
        # 同 ID 同事实：保留首条（含其登记调用 tool_call_id）
    return merged


# §7.12.1：执行／分析记录分别定义事实比较字段
# doc 6.3 §6：任务归属、结构化输入血缘与直接沿用来源同为事实字段
_EXECUTION_FACT_FIELDS = (
    "reused_from",
    "result_ref",
    "executed_sql_sha256",
    "saved_row_count",
    "source_truncated",
    "columns",
    "result_generated_at",
    "sub_task_id",
    "sub_task_revision",
    "input_result_refs",
    "direct_reuse_from",
)
_ANALYSIS_FACT_FIELDS = (
    "reused_from",
    "output_result_ref",
    "operation",
    "input_result_refs",
    "executed_sql",
    "saved_row_count",
    "source_truncated",
    "result_generated_at",
    "sub_task_id",
    "sub_task_revision",
    "direct_reuse_from",
)


def append_dedup_execution(left, right) -> list:
    return _append_dedup(left, right, _EXECUTION_FACT_FIELDS)


def append_dedup_analysis(left, right) -> list:
    return _append_dedup(left, right, _ANALYSIS_FACT_FIELDS)


# ------------------------------------------------ doc 6.3 §4.1：账本键编码


def task_ledger_key(run_id: str, sub_task_id: str, revision: int) -> str:
    """(run_id, sub_task_id, revision) 的规范化 JSON 编码（不能直接用 tuple 键）。"""
    return json.dumps(
        [run_id, sub_task_id, int(revision)],
        ensure_ascii=False,
        separators=(",", ":"),
    )


def parse_task_ledger_key(key: str) -> tuple[str, str, int] | None:
    """解析账本键；不可解码返回 None（调用方按账本损坏拒绝）。"""
    try:
        parts = json.loads(key)
    except (TypeError, ValueError):
        return None
    if (
        not isinstance(parts, list)
        or len(parts) != 3
        or not all(isinstance(p, str) for p in parts[:2])
        or type(parts[2]) is not int
    ):
        return None
    return parts[0], parts[1], parts[2]


# 任务记录的不可变事实字段（同版本更新中必须相等；R1-15）
_TASK_IMMUTABLE_FIELDS = (
    "session_id",
    "thread_id",
    "request_id",
    "run_id",
    "sub_task_id",
    "revision",
    "content",
    "dependencies",
    "creation_operation",
    "created_at",
)

# 同版本内允许的**普通 status** 转移（§5.1；R1-15）。终态无出边——
# 终态通过普通 status 原地恢复一律拒绝；void 是独立操作，另行判定。
_TASK_STATUS_TRANSITIONS: dict[str, set[str]] = {
    "pending": {"in_progress", "completed", "failed", "needs_clarification"},
    "in_progress": {"completed", "failed", "needs_clarification"},
    "completed": set(),
    "failed": set(),
    "needs_clarification": set(),
    "voided": set(),
}

# void 独立操作允许从这些状态转入 voided（§5.1：作废已完成下游，再修订上游）
_VOIDABLE_STATUSES = frozenset(
    {"pending", "in_progress", "completed", "failed", "needs_clarification"}
)


def is_legal_void_transition(old_status: Any, new_record: dict) -> bool:
    """void 合法转移的共享规则（TaskLedger 与 reducer 一致；R2-04）。

    普通 status 的终态无出边，但 void 允许 pending/in_progress/completed/
    failed/needs_clarification → voided。它必须由服务端写入完整
    void_operation 与非空白 reason，且不改写任何不可变事实；不能把任意
    status=voided 的写入都当成合法作废。
    """
    if old_status not in _VOIDABLE_STATUSES or new_record.get("status") != "voided":
        return False
    if not isinstance(new_record.get("void_operation"), dict):
        return False
    reason = new_record.get("reason")
    return isinstance(reason, str) and bool(reason.strip())


def task_record_update_allowed(old: dict, new: dict) -> bool:
    """同一版本记录的合法状态更新判定（R1-15 / R2-04：reducer 层防护）。

    - 不可变事实字段必须完全相等（content/dependencies/creation_operation
      等不可在同版本内改写；revise 产生新键）。
    - void 合法转移单独判定（允许终态→voided），仍保留不可变事实检查。
    - 普通 status 沿 §5.1 允许方向转移；相同 status 要求整条记录相等
      （幂等重放）。终态不得通过普通 status 原地恢复。
    """
    for field in _TASK_IMMUTABLE_FIELDS:
        if old.get(field) != new.get(field):
            return False
    old_status = old.get("status")
    new_status = new.get("status")
    if new_status == "voided" and old_status != "voided":
        return is_legal_void_transition(old_status, new)
    if new_status not in _TASK_STATUS_TRANSITIONS.get(old_status, set()):
        return old == new  # 无合法转移：仅完全相等（重放）允许
    if old_status == new_status:
        return old == new
    # 合法转移：不可变字段之外的变化允许（服务层已完成业务校验）
    return True


def merge_task_records(left: dict | None, right: dict | None) -> dict:
    """db_sub_tasks 专用 reducer（doc 6.3 §5.3；R1-15 强化）。

    - 键不可解码、记录身份与键不符或缺失必填身份 → LedgerConsistencyError
      （读取侧不掩盖损坏）。
    - 同键更新必须满足 task_record_update_allowed：不可变事实不可改写、
      状态转移沿 §5.1 方向、相同状态要求整条记录相等。绕过服务层的
      非法 Command（如同 revision 改 content、completed 回退 pending）
      在此拒绝。
    - 同键同事实（Command 重放）→ 整条记录相等，天然幂等。
    """
    merged = dict(left or {})
    for source in (left, right):
        for key, record in (source or {}).items():
            parsed = parse_task_ledger_key(key)
            if parsed is None or not isinstance(record, dict):
                raise LedgerConsistencyError(f"任务账本键不可解码: {key!r}")
            run_id, sub_task_id, revision = parsed
            if (
                record.get("run_id") != run_id
                or record.get("sub_task_id") != sub_task_id
                or record.get("revision") != revision
            ):
                raise LedgerConsistencyError(f"任务记录身份与键不符: {key!r}")
    for key, record in (right or {}).items():
        existing = merged.get(key)
        if existing is not None and existing != record:
            if not task_record_update_allowed(existing, record):
                raise LedgerConsistencyError(
                    f"任务账本同版本非法覆盖: {key!r} "
                    f"({existing.get('status')}→{record.get('status')})"
                )
        merged[key] = record
    return merged


def merge_task_plans(left: dict | None, right: dict | None) -> dict:
    """db_task_plans 专用 reducer（doc 6.3 §5.3）。

    按 run_id 键控：新写入的 plan_version 必须恰为既有值 +1（整批真实修改），
    或与既有记录完全一致（Command 重放幂等）；否则拒绝，禁止最后写者静默覆盖。
    """
    merged = dict(left or {})
    for key, record in (right or {}).items():
        if not isinstance(record, dict) or record.get("run_id") != key:
            raise LedgerConsistencyError(f"计划记录身份与键不符: {key!r}")
        existing = merged.get(key)
        if existing is None:
            merged[key] = record
            continue
        if existing == record:
            continue  # 幂等重放
        old_version = existing.get("plan_version")
        new_version = record.get("plan_version")
        if (
            type(old_version) is int
            and type(new_version) is int
            and new_version == old_version + 1
        ):
            merged[key] = record
            continue
        raise LedgerConsistencyError(
            f"计划版本非法变化 run_id={key}: {old_version!r} -> {new_version!r}"
        )
    return merged


# ------------------------------------------------ doc 6.3 §5.1/§13.7：错误账本

# 错误事实比较字段：完整 error_ref 为键，事实含登记调用与任务归属
_TOOL_ERROR_FACT_FIELDS = (
    "request_id",
    "tool_call_id",
    "tool",
    "error_type",
    "message",
    "retryable",
    "source_task_ref",
)


def _tool_error_identity(record: Any) -> tuple[str, str, str, str]:
    """记录的完整 error_ref 身份；缺失或非法必填身份抛一致性错误。"""
    if not isinstance(record, dict):
        raise LedgerConsistencyError("错误记录必须是映射")
    parts = []
    for field in ("session_id", "thread_id", "run_id", "error_id"):
        value = record.get(field)
        if not isinstance(value, str) or not value:
            raise LedgerConsistencyError(f"错误记录缺失合法 {field}")
        parts.append(value)
    return tuple(parts)  # type: ignore[return-value]


_TOOL_ERROR_REQUIRED_NONEMPTY = ("tool", "error_type", "message")


def _validate_tool_error_record(record: dict) -> None:
    """单条错误记录结构校验（R1-08）：必填非空字段拒绝四字段空壳；
    retryable 必须为布尔。"""
    for field in _TOOL_ERROR_REQUIRED_NONEMPTY:
        value = record.get(field)
        if not isinstance(value, str) or not value.strip():
            raise LedgerConsistencyError(f"错误记录 {field} 缺失或为空白")
    if not isinstance(record.get("retryable"), bool):
        raise LedgerConsistencyError("错误记录 retryable 必须为布尔")


def _tool_error_facts(record: dict) -> dict:
    """事实视图＝全部字段（R1-08：结构化 details 等未知字段纳入事实比较，
    不得静默去重；无登记时间类豁免字段——本记录不保存服务端时间）。"""
    return dict(record)


def _merge_tool_errors_side(records: list) -> dict[tuple[str, str, str, str], dict]:
    """单侧输入内部校验：结构完整 + 同完整引用同事实去重、异事实拒绝。"""
    seen: dict[tuple[str, str, str, str], dict] = {}
    for record in records:
        _validate_tool_error_record(record)
        key = _tool_error_identity(record)
        existing = seen.get(key)
        if existing is None:
            seen[key] = record
            continue
        if _tool_error_facts(existing) != _tool_error_facts(record):
            raise LedgerConsistencyError(
                f"error_ref={key[3]} 事实不一致（含结构化字段）："
                f"{_tool_error_facts(existing)!r} != {_tool_error_facts(record)!r}"
            )
    return seen


def append_dedup_tool_errors(left, right) -> list:
    """db_tool_errors 专用 reducer（doc 6.3 §5.1/§13.7）。

    按完整 error_ref（session/thread/run/error_id）合并：左右输入与单侧内部
    都校验；同引用同事实保留首条，异事实抛 LedgerConsistencyError；不因一侧
    为空跳过对另一侧的内部校验。tool_call_id 属错误事实（与执行 reducer 的
    排除规则不同），不能照搬。
    """
    left_seen = _merge_tool_errors_side(list(left or []))
    right_seen = _merge_tool_errors_side(list(right or []))
    for key, record in right_seen.items():
        existing = left_seen.get(key)
        if existing is None:
            left_seen[key] = record
            continue
        if _tool_error_facts(existing) != _tool_error_facts(record):
            raise LedgerConsistencyError(
                f"error_ref={key[3]} 事实不一致（含结构化字段）"
            )
    # 保序输出：先左侧原序，再右侧新增
    out: list[ToolErrorRecord] = []
    emitted: set[tuple[str, str, str, str]] = set()
    for record in [*list(left or []), *list(right or [])]:
        key = _tool_error_identity(record)
        if key in emitted:
            continue
        emitted.add(key)
        out.append(record)
    return out


class DbDeepAgentState(DeepAgentState):
    """db_deepagent 图状态：账本字段全部为 PrivateStateAttr，只由本模块工具写入（§5.2）"""

    db_validations: NotRequired[
        Annotated[dict[str, ValidationRecord], PrivateStateAttr, merge_by_key]
    ]
    # §7.12.1：按 evidence_id 去重追加（reuse 幂等；同 ID 异事实抛一致性错误）
    db_executions: NotRequired[
        Annotated[list[ExecutionRecord], PrivateStateAttr, append_dedup_execution]
    ]
    db_analyses: NotRequired[
        Annotated[list[AnalysisRecord], PrivateStateAttr, append_dedup_analysis]
    ]
    db_time_evidence: NotRequired[
        Annotated[list[TimeEvidence], PrivateStateAttr, operator.add]
    ]
    db_dim_evidence: NotRequired[
        Annotated[list[DimEvidence], PrivateStateAttr, operator.add]
    ]
    # doc 6.3 §5.1/§13.7：专用去重 reducer（operator.add 已移除）
    db_tool_errors: NotRequired[
        Annotated[list[ToolErrorRecord], PrivateStateAttr, append_dedup_tool_errors]
    ]
    db_run_status: NotRequired[
        Annotated[dict[str, RunStatusRecord], PrivateStateAttr, merge_by_key]
    ]
    db_final_answers: NotRequired[
        Annotated[dict[str, FinalAnswerRecord], PrivateStateAttr, merge_by_key]
    ]
    # doc 6.3 §4.1：任务账本与计划通道（专用 reducer）
    db_sub_tasks: NotRequired[
        Annotated[dict[str, SubTaskRecord], PrivateStateAttr, merge_task_records]
    ]
    db_task_plans: NotRequired[
        Annotated[dict[str, TaskPlanRecord], PrivateStateAttr, merge_task_plans]
    ]
