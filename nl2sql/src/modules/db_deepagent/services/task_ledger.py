"""业务任务账本规则（doc 6.3 §4–§5）

整批候选计划、状态转移、版本选择、完整规范化操作比较与 no-op 识别、
执行 DAG 与终止审计校验、终止依据与替代快照生成。纯规则模块：

- 不直接读写 runtime.state（调用方传入状态映射，返回应写记录）；
- 不查库、不调用模型；
- 任何操作失败整批零写入（返回值层面即不产生任何更新）。

幂等采用版本匹配前提（§5.3）：先检查 expected_plan_version，不符一律
task_plan_conflict（即使 operations 完全相同）；通过后才在整批候选视图中
识别无变化操作，整批全为合法 no-op 时不递增 plan_version。
"""

from __future__ import annotations

import copy
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from src.modules.db_deepagent.state import (
    LedgerConsistencyError,
    is_legal_void_transition,
    parse_task_ledger_key,
    task_ledger_key,
)

SUB_TASK_ID_PATTERN = r"^T[1-9][0-9]{0,5}$"
TASK_STATUSES = (
    "pending",
    "in_progress",
    "completed",
    "failed",
    "needs_clarification",
    "voided",
)
_EXECUTION_GRAPH_STATUSES = ("pending", "in_progress", "completed")
TERMINAL_STATUSES = ("completed", "failed", "needs_clarification", "voided")


class TaskLedgerError(Exception):
    """任务账本操作失败（error_type 对应 doc 6.3 §7.3 错误码）"""

    def __init__(
        self,
        error_type: str,
        message: str,
        hint: str | None = None,
        task_refs: list[str] | None = None,
        current_plan_version: int | None = None,
        tasks_summary: list[dict[str, Any]] | None = None,
    ):
        self.error_type = error_type
        self.message = message
        self.hint = hint
        self.task_refs = task_refs or []
        self.current_plan_version = current_plan_version
        self.tasks_summary = tasks_summary or []
        super().__init__(message)


def is_nonblank_str(value: Any) -> bool:
    """按状态必填的文本约束（doc 6.3 §4.1/§13.3）：字符串且 strip 后非空。"""
    return isinstance(value, str) and bool(value.strip())


def current_task_view(
    records: Mapping[str, Any], run_id: str, *, include_voided: bool = True
) -> dict[str, dict[str, Any]]:
    """先取最大 revision、再判断状态（doc 6.3 §4.2）。

    不先过滤 voided 再取最大值；缺失版本/键不可解码/记录身份不符按
    LedgerConsistencyError 上抛，不静默忽略损坏账本。
    """
    latest: dict[str, dict[str, Any]] = {}
    for key, record in (records or {}).items():
        parsed = parse_task_ledger_key(key)
        if parsed is None or not isinstance(record, Mapping):
            raise LedgerConsistencyError(f"任务账本键不可解码: {key!r}")
        r_run, sub_task_id, revision = parsed
        if r_run != run_id:
            continue
        if (
            record.get("run_id") != r_run
            or record.get("sub_task_id") != sub_task_id
            or record.get("revision") != revision
        ):
            raise LedgerConsistencyError(f"任务记录身份与键不符: {key!r}")
        existing = latest.get(sub_task_id)
        if existing is None or revision > existing["revision"]:
            latest[sub_task_id] = dict(record)
    if not include_voided:
        return {
            tid: rec for tid, rec in latest.items() if rec.get("status") != "voided"
        }
    return latest


def canonical_json(value: Any) -> str:
    """规范化编码：键排序、紧凑分隔、禁 ASCII 转义。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _normalized_dependencies(dependencies: Any) -> list[dict[str, Any]]:
    """依赖列表规范化：先拒绝重复，再按身份排序（§5.3）。"""
    if not isinstance(dependencies, list):
        raise TaskLedgerError("invalid_task_dependency", "dependencies 必须为列表")
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for dep in dependencies:
        if not isinstance(dep, Mapping):
            raise TaskLedgerError(
                "invalid_task_dependency", f"依赖项必须是映射: {dep!r}"
            )
        sub_task_id = dep.get("sub_task_id")
        revision = dep.get("revision")
        if (
            not isinstance(sub_task_id, str)
            or type(revision) is not int
            or revision < 1
        ):
            raise TaskLedgerError(
                "invalid_task_dependency", f"依赖项身份非法: {dict(dep)!r}"
            )
        identity = canonical_json([sub_task_id, revision])
        if identity in seen:
            raise TaskLedgerError(
                "invalid_task_dependency",
                f"依赖重复: {sub_task_id}@{revision}",
                task_refs=[sub_task_id],
            )
        seen.add(identity)
        out.append({"sub_task_id": sub_task_id, "revision": revision})
    out.sort(key=lambda d: (d["sub_task_id"], d["revision"]))
    return out


@dataclass
class LedgerOperation:
    """已由工具层解析的规范化操作（§5.1）"""

    op: str  # add / revise / status / void
    sub_task_id: str
    # add / revise
    content: str | None = None
    dependencies: list[dict[str, Any]] | None = None
    # status
    status: str | None = None
    evidence_ids: list[str] | None = None
    reason_code: str | None = None
    reason: str | None = None
    clarification_question: str | None = None
    failure_refs: list[dict[str, Any]] | None = None
    blocked_by: list[dict[str, Any]] | None = None
    # void
    replacement: dict[str, Any] | None = None

    def normalized_dict(self) -> dict[str, Any]:
        """模型可写参数的完整规范化比较视图（§5.3；不含服务端审计字段）。"""
        out: dict[str, Any] = {"op": self.op, "sub_task_id": self.sub_task_id}
        if self.op in ("add", "revise"):
            out["content"] = self.content
            out["dependencies"] = self.dependencies or []
        elif self.op == "status":
            out["status"] = self.status
            out["evidence_ids"] = list(self.evidence_ids or [])
            out["reason_code"] = self.reason_code
            out["reason"] = self.reason
            out["clarification_question"] = self.clarification_question
            out["failure_refs"] = _normalized_failure_refs(self.failure_refs or [])
            out["blocked_by"] = self.blocked_by or []
        elif self.op == "void":
            out["reason"] = self.reason
            out["replacement"] = self.replacement
        return out


def _normalized_failure_refs(refs: list[Any]) -> list[dict[str, Any]]:
    """failure_refs 规范化比较视图（键排序，不重排列表顺序）。"""
    out: list[dict[str, Any]] = []
    for ref in refs:
        if not isinstance(ref, Mapping):
            raise TaskLedgerError(
                "invalid_task_failure_ref", f"failure_ref 必须是映射: {ref!r}"
            )
        normalized = json.loads(canonical_json(dict(ref)))
        out.append(normalized)
    return out


@dataclass
class ApplyResult:
    """一次 record_sub_tasks 的应用结果"""

    plan_version: int
    plan_record: dict[str, Any] | None  # None=整批 no-op，不写计划通道
    task_records: dict[str, dict[str, Any]]  # 本次写入的任务记录（按键）
    no_op: bool
    op_receipts: list[dict[str, Any]] = field(default_factory=list)


def _task_summary(view: Mapping[str, Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "sub_task_id": tid,
            "revision": rec.get("revision"),
            "status": rec.get("status"),
        }
        for tid, rec in sorted(view.items())
    ]


def _find_error_record(
    errors: list[Any], error_ref: Mapping[str, Any]
) -> Mapping[str, Any] | None:
    """按完整 error_ref 查找错误记录；同引用重复/冲突由 error_by_ref 语义保证唯一。"""
    seen: dict[str, Mapping[str, Any]] = {}
    for record in errors or []:
        if not isinstance(record, Mapping):
            continue
        key = canonical_json(
            [
                record.get("session_id"),
                record.get("thread_id"),
                record.get("run_id"),
                record.get("error_id"),
            ]
        )
        seen.setdefault(key, record)
    key = canonical_json(
        [
            error_ref.get("session_id"),
            error_ref.get("thread_id"),
            error_ref.get("run_id"),
            error_ref.get("error_id"),
        ]
    )
    return seen.get(key)


def _validate_failure_refs(
    refs: list[dict[str, Any]] | None,
    *,
    task: Mapping[str, Any],
    errors: list[Any],
    ctx_scope: Mapping[str, Any],
) -> None:
    """§5.1：tool_failed 的 failure_refs 与服务端错误记录精确相符。"""
    if not refs:
        return
    for ref in refs:
        error_ref = ref.get("error_ref") or {}
        record = _find_error_record(errors, error_ref)
        if record is None:
            raise TaskLedgerError(
                "invalid_task_failure_ref",
                f"error_id={error_ref.get('error_id')!r} 不在服务端错误账本中",
                task_refs=[task.get("sub_task_id", "")],
            )
        if (
            error_ref.get("session_id") != ctx_scope.get("session_id")
            or error_ref.get("thread_id") != ctx_scope.get("thread_id")
            or error_ref.get("run_id") != ctx_scope.get("run_id")
        ):
            raise TaskLedgerError(
                "invalid_task_failure_ref",
                f"error_ref={error_ref.get('error_id')!r} 作用域与当前运行不符",
                task_refs=[task.get("sub_task_id", "")],
            )
        # 后三项事实必须与被引用记录精确相符（不信任模型自报）
        if ref.get("tool_call_id") != record.get("tool_call_id"):
            raise TaskLedgerError(
                "invalid_task_failure_ref",
                f"error_id={error_ref.get('error_id')!r} 的 tool_call_id 不符",
                task_refs=[task.get("sub_task_id", "")],
            )
        if ref.get("error_type") != record.get("error_type"):
            raise TaskLedgerError(
                "invalid_task_failure_ref",
                f"error_id={error_ref.get('error_id')!r} 的 error_type 不符",
                task_refs=[task.get("sub_task_id", "")],
            )
        # R1-08：引用内的 source_task_ref 也须与被引用记录精确一致
        ref_source = ref.get("source_task_ref") or {}
        record_source = record.get("source_task_ref") or {}
        for field_name in (
            "session_id",
            "thread_id",
            "run_id",
            "sub_task_id",
            "revision",
        ):
            if ref_source.get(field_name) != record_source.get(field_name):
                raise TaskLedgerError(
                    "invalid_task_failure_ref",
                    f"error_id={error_ref.get('error_id')!r} 的"
                    f" source_task_ref.{field_name} 不符",
                    task_refs=[task.get("sub_task_id", "")],
                )
        source_task = record.get("source_task_ref") or {}
        if (
            source_task.get("run_id") != task.get("run_id")
            or source_task.get("sub_task_id") != task.get("sub_task_id")
            or source_task.get("revision") != task.get("revision")
        ):
            raise TaskLedgerError(
                "invalid_task_failure_ref",
                f"error_id={error_ref.get('error_id')!r} 不属于任务"
                f" {task.get('sub_task_id')}@{task.get('revision')} 的当前版本",
                task_refs=[task.get("sub_task_id", "")],
            )


def _dependency_state_snapshot(
    version_index: Mapping[tuple[str, str, int], Mapping[str, Any]],
    candidate: Mapping[str, Mapping[str, Any]],
    run_id: str,
    dep: Mapping[str, Any],
) -> dict[str, Any]:
    """按固定 (run, id, revision) 取事实（R2-06）。

    若该固定版本正是候选中的当前版本，用最终候选（含本批服务端派生原因）；
    否则用历史版本原记录，绝不把新版本状态套到旧 revision 上。
    """
    tid, rev = dep["sub_task_id"], dep["revision"]
    current = candidate.get(tid)
    if current is not None and current.get("revision") == rev:
        rec: Mapping[str, Any] = current
    else:
        rec = version_index.get((run_id, tid, rev)) or {}
    snap: dict[str, Any] = {
        "sub_task_id": tid,
        "revision": rev,
        "status": rec.get("status", "unknown"),
    }
    if rec.get("reason_code") is not None:
        snap["reason_code"] = rec["reason_code"]
    if rec.get("reason") is not None:
        snap["reason"] = rec["reason"]
    return snap


def _resolve_blockage_root(
    version_index: Mapping[tuple[str, str, int], Mapping[str, Any]],
    candidate: Mapping[str, Mapping[str, Any]],
    run_id: str,
    start_key: tuple,
) -> dict[str, Any]:
    """生成端按**固定版本**推导阻塞根因（R3-04 B）。

    与复核端共用 derive_decision_root / terminal_facts：沿 blocked_by 的
    固定 (sub_task_id, revision) 前进，遇到已封存 blocked 节点按其固定
    决策依据继续，绝不按逻辑 ID 追到后来 revise 的新版本。
    """
    from src.modules.db_deepagent.services.task_completion import (
        derive_decision_root,
    )

    def lookup(tid: Any, rev: Any) -> Mapping[str, Any] | None:
        current = candidate.get(tid)
        if current is not None and current.get("revision") == rev:
            return current
        return version_index.get((run_id, tid, rev))

    root = derive_decision_root(lookup, start_key)
    if root is None:
        # 不构成合法阻塞链时回退到起点事实，由上层/复核端给出明确拒绝
        rec = lookup(start_key[0], start_key[1]) or {}
        root = _dependency_state_snapshot(
            version_index,
            candidate,
            run_id,
            {"sub_task_id": start_key[0], "revision": start_key[1]},
        )
        root["status"] = rec.get("status", "unknown")
    return root


class TaskLedger:
    """整批操作应用器（无状态；每次调用传入当前账本快照）"""

    def __init__(self, state: Mapping[str, Any], *, archive=None):
        self._archive = archive  # R1-02：沿用来源资格核验的快照读取
        self._records: dict[str, dict[str, Any]] = dict(state.get("db_sub_tasks") or {})
        self._plans: dict[str, dict[str, Any]] = dict(state.get("db_task_plans") or {})
        self._errors: list[Any] = list(state.get("db_tool_errors") or [])
        self._executions: list[Any] = list(state.get("db_executions") or [])
        self._analyses: list[Any] = list(state.get("db_analyses") or [])

    # ------------------------------------------------------------ 读取辅助

    def current_view(
        self, run_id: str, *, include_voided: bool = True
    ) -> dict[str, dict[str, Any]]:
        return current_task_view(self._records, run_id, include_voided=include_voided)

    def plan_version(self, run_id: str) -> int:
        record = self._plans.get(run_id) or {}
        version = record.get("plan_version", 0)
        return version if type(version) is int else 0

    # ------------------------------------------------------------ 应用入口

    def apply(
        self,
        *,
        run_id: str,
        session_id: str,
        thread_id: str,
        request_id: str,
        expected_plan_version: int,
        operations: list[LedgerOperation],
        now: datetime,
        tool_call_id: str,
    ) -> ApplyResult:
        """整批校验后一次提交（§5.2）；任一错误整批不写。"""
        current_version = self.plan_version(run_id)
        view = self.current_view(run_id)
        summary = _task_summary(view)
        if type(expected_plan_version) is not int or expected_plan_version < 0:
            raise TaskLedgerError(
                "invalid_argument",
                "expected_plan_version 必须为非负整数",
                current_plan_version=current_version,
                tasks_summary=summary,
            )
        if expected_plan_version != current_version:
            raise TaskLedgerError(
                "task_plan_conflict",
                f"计划版本冲突：expected={expected_plan_version}, "
                f"current={current_version}",
                hint="按返回的当前任务摘要重新确认计划后整批重提",
                current_plan_version=current_version,
                tasks_summary=summary,
            )
        if not operations:
            raise TaskLedgerError(
                "invalid_argument",
                "operations 不能为空",
                current_plan_version=current_version,
            )
        # 每批对同一逻辑 ID 至多一个修改操作（§5.2）
        seen_ids: dict[str, str] = {}
        for op in operations:
            if op.op not in ("add", "revise", "status", "void"):
                raise TaskLedgerError(
                    "invalid_argument",
                    f"未知操作类型: {op.op!r}",
                    current_plan_version=current_version,
                )
            if op.sub_task_id in seen_ids:
                raise TaskLedgerError(
                    "task_operation_conflict",
                    f"同一批次对 {op.sub_task_id} 有多个修改操作"
                    f"（{seen_ids[op.sub_task_id]} 与 {op.op}）",
                    task_refs=[op.sub_task_id],
                    current_plan_version=current_version,
                )
            seen_ids[op.sub_task_id] = op.op

        # 候选视图必须深拷贝：R2-05 嵌套 termination_context / replacement
        # 快照等历史依据不得与输入账本共享对象，成功与失败计算都不得就地
        # 改写输入。
        candidate: dict[str, dict[str, Any]] = copy.deepcopy(dict(view))
        written: dict[str, dict[str, Any]] = {}
        written_ids: set[str] = set()  # 本批修改的逻辑 ID（快照回填范围）
        receipts: list[dict[str, Any]] = []
        all_no_op = True

        for op in operations:
            no_op = self._apply_one(
                op,
                candidate=candidate,
                written=written,
                written_ids=written_ids,
                receipts=receipts,
                scope={
                    "run_id": run_id,
                    "session_id": session_id,
                    "thread_id": thread_id,
                    "request_id": request_id,
                },
                now=now,
                tool_call_id=tool_call_id,
                current_version=current_version,
            )
            if not no_op:
                all_no_op = False

        # 整批级校验：执行 DAG + 终止审计引用（先合并后核验，R1-09）
        self._validate_candidate(candidate, modified_ids=set(written_ids))

        if all_no_op:
            return ApplyResult(
                plan_version=current_version,
                plan_record=None,
                task_records={},
                no_op=True,
                op_receipts=receipts,
            )
        new_version = current_version + 1
        plan_record = {
            "session_id": session_id,
            "thread_id": thread_id,
            "request_id": request_id,
            "run_id": run_id,
            "plan_version": new_version,
            "updated_at": now.astimezone().isoformat(timespec="seconds"),
            "tool_call_id": tool_call_id,
        }
        return ApplyResult(
            plan_version=new_version,
            plan_record=plan_record,
            task_records=written,
            no_op=False,
            op_receipts=receipts,
        )

    # ------------------------------------------------------------ 单操作

    def _apply_one(
        self,
        op: LedgerOperation,
        *,
        candidate: dict[str, dict[str, Any]],
        written: dict[str, dict[str, Any]],
        written_ids: set[str],
        receipts: list[dict[str, Any]],
        scope: Mapping[str, Any],
        now: datetime,
        tool_call_id: str,
        current_version: int,
    ) -> bool:
        """应用单个操作到候选视图；返回是否 no-op。"""
        run_id = scope["run_id"]
        sub_task_id = op.sub_task_id
        existing = candidate.get(sub_task_id)
        now_iso = now.astimezone().isoformat(timespec="seconds")

        if op.op == "add":
            receipt: dict[str, Any] = {"op": "add", "sub_task_id": sub_task_id}
            if op.dependencies is None:
                op.dependencies = []  # 省略依赖按空列表规范化（§5.3）
            deps = _normalized_dependencies(op.dependencies)
            op.dependencies = deps  # 保存与比较共用排序规范化（R1-12）
            if existing is not None:
                # 已 revise/void 的 ID 返回冲突；仅 revision=1 未作废且创建操作
                # 一致时可 no-op（状态推进时回执明确当前状态）
                if existing.get("revision") != 1 or existing.get("status") == "voided":
                    raise TaskLedgerError(
                        "task_operation_conflict",
                        f"任务 {sub_task_id} 已存在（当前 revision="
                        f"{existing.get('revision')}，status={existing.get('status')}），"
                        "add 不能复用既有 ID；恢复需求请新增 ID 并记录关系",
                        task_refs=[sub_task_id],
                        current_plan_version=current_version,
                    )
                saved_creation = existing.get("creation_operation") or {}
                if saved_creation != op.normalized_dict():
                    raise TaskLedgerError(
                        "task_operation_conflict",
                        f"任务 {sub_task_id} 已存在且创建操作不一致"
                        "（同 ID 不同 content/dependencies 均冲突）",
                        task_refs=[sub_task_id],
                        current_plan_version=current_version,
                    )
                receipt["no_op"] = True
                receipt["current_status"] = existing.get("status")
                receipt["message"] = (
                    f"未新增：{sub_task_id} 已存在，当前状态为 "
                    f"{existing.get('status')}"
                )
                receipts.append(receipt)
                return True
            self._require_valid_content(op.content, sub_task_id)
            record = {
                "session_id": scope["session_id"],
                "thread_id": scope["thread_id"],
                "request_id": scope["request_id"],
                "run_id": run_id,
                "sub_task_id": sub_task_id,
                "revision": 1,
                "content": op.content,
                "dependencies": deps,
                "status": "pending",
                "evidence_ids": [],
                "creation_operation": op.normalized_dict(),
                "failure_refs": [],
                "decision_source": "agent",
                "created_at": now_iso,
                "updated_at": now_iso,
                "tool_call_id": tool_call_id,
            }
            candidate[sub_task_id] = record
            written_ids.add(sub_task_id)
            written[task_ledger_key(run_id, sub_task_id, 1)] = record
            receipt["no_op"] = False
            receipt["current_status"] = "pending"
            receipts.append(receipt)
            return False

        if existing is None:
            raise TaskLedgerError(
                "task_not_registered",
                f"任务 {sub_task_id} 不在本运行账本中",
                task_refs=[sub_task_id],
                current_plan_version=current_version,
            )

        if op.op == "revise":
            if existing.get("status") == "voided":
                raise TaskLedgerError(
                    "task_operation_conflict",
                    f"任务 {sub_task_id} 最新版本已作废，不能再 revise；"
                    "恢复需求请新增 ID 并记录关系",
                    task_refs=[sub_task_id],
                    current_plan_version=current_version,
                )
            if op.dependencies is None:
                op.dependencies = []
            self._require_valid_content(op.content, sub_task_id)
            deps = _normalized_dependencies(op.dependencies)
            op.dependencies = deps  # R1-12：规范化后保存
            new_revision = int(existing["revision"]) + 1
            record = {
                "session_id": scope["session_id"],
                "thread_id": scope["thread_id"],
                "request_id": scope["request_id"],
                "run_id": run_id,
                "sub_task_id": sub_task_id,
                "revision": new_revision,
                "content": op.content,
                "dependencies": deps,
                "status": "pending",
                "evidence_ids": [],
                "creation_operation": op.normalized_dict(),
                "failure_refs": [],
                "decision_source": "agent",
                "created_at": now_iso,
                "updated_at": now_iso,
                "tool_call_id": tool_call_id,
            }
            candidate[sub_task_id] = record
            written_ids.add(sub_task_id)
            written[task_ledger_key(run_id, sub_task_id, new_revision)] = record
            receipts.append(
                {
                    "op": "revise",
                    "sub_task_id": sub_task_id,
                    "new_revision": new_revision,
                    "status": "pending",
                }
            )
            return False

        if op.op == "status":
            return self._apply_status(
                op,
                existing=existing,
                candidate=candidate,
                written=written,
                written_ids=written_ids,
                receipts=receipts,
                scope=scope,
                now_iso=now_iso,
                tool_call_id=tool_call_id,
                current_version=current_version,
                errors=self._errors,
            )

        # void
        return self._apply_void(
            op,
            existing=existing,
            candidate=candidate,
            written=written,
            written_ids=written_ids,
            receipts=receipts,
            scope=scope,
            now_iso=now_iso,
            tool_call_id=tool_call_id,
            current_version=current_version,
        )

    def _require_valid_content(self, content: Any, sub_task_id: str) -> None:
        if not is_nonblank_str(content):
            raise TaskLedgerError(
                "invalid_argument",
                f"任务 {sub_task_id} 的 content 必须为字符串且 strip 后非空",
                task_refs=[sub_task_id],
            )

    def _apply_status(
        self,
        op: LedgerOperation,
        *,
        existing: dict[str, Any],
        candidate: dict[str, dict[str, Any]],
        written: dict[str, dict[str, Any]],
        written_ids: set[str],
        receipts: list[dict[str, Any]],
        scope: Mapping[str, Any],
        now_iso: str,
        tool_call_id: str,
        current_version: int,
        errors: list[Any],
    ) -> bool:
        sub_task_id = op.sub_task_id
        run_id = scope["run_id"]
        status = op.status
        if status not in (
            "in_progress",
            "completed",
            "failed",
            "needs_clarification",
        ):
            raise TaskLedgerError(
                "invalid_argument",
                f"status 操作的目标状态非法: {status!r}",
                task_refs=[sub_task_id],
                current_plan_version=current_version,
            )
        if existing.get("status") == "voided":
            raise TaskLedgerError(
                "task_operation_conflict",
                f"任务 {sub_task_id} 已作废，不接受 status/revise",
                task_refs=[sub_task_id],
                current_plan_version=current_version,
            )
        # 必填文本约束（§5.1/§13.3）
        if status in ("failed", "needs_clarification") and not is_nonblank_str(
            op.reason
        ):
            raise TaskLedgerError(
                "invalid_argument",
                f"任务 {sub_task_id} status={status} 必填 reason（字符串且非空白）",
                task_refs=[sub_task_id],
                current_plan_version=current_version,
            )
        if status == "needs_clarification" and not is_nonblank_str(
            op.clarification_question
        ):
            raise TaskLedgerError(
                "invalid_argument",
                f"任务 {sub_task_id} needs_clarification 必填 clarification_question",
                task_refs=[sub_task_id],
                current_plan_version=current_version,
            )
        reason_code = op.reason_code
        failure_refs: list[dict[str, Any]] = []
        blocked_by: list[dict[str, Any]] = []
        if status == "failed" and reason_code == "tool_failed":
            if not op.failure_refs:
                raise TaskLedgerError(
                    "invalid_task_failure_ref",
                    f"任务 {sub_task_id} reason_code=tool_failed 必填非空 failure_refs",
                    task_refs=[sub_task_id],
                    current_plan_version=current_version,
                )
            failure_refs = _normalized_failure_refs(op.failure_refs)
        if status == "failed" and reason_code not in (
            "tool_failed",
            "agent_abandoned",
            "dependency_blocked",
            None,
        ):
            raise TaskLedgerError(
                "invalid_argument",
                f"任务 {sub_task_id} reason_code 非法: {reason_code!r}",
                task_refs=[sub_task_id],
                current_plan_version=current_version,
            )
        if (
            status == "failed"
            and reason_code in ("agent_abandoned", None)
            and op.failure_refs
        ):
            raise TaskLedgerError(
                "invalid_task_failure_ref",
                f"任务 {sub_task_id} 非工具失败原因不得伪造 failure_refs",
                task_refs=[sub_task_id],
                current_plan_version=current_version,
            )
        if (
            status == "failed"
            and reason_code == "dependency_blocked"
            and op.failure_refs
        ):
            raise TaskLedgerError(
                "invalid_task_failure_ref",
                f"任务 {sub_task_id} dependency_blocked 不伪造 failure_refs",
                task_refs=[sub_task_id],
                current_plan_version=current_version,
            )
        if status == "failed" and reason_code == "dependency_blocked":
            blocked_by = _normalized_dependencies(op.blocked_by)
            if not blocked_by:
                raise TaskLedgerError(
                    "invalid_task_dependency",
                    f"任务 {sub_task_id} dependency_blocked 必填 blocked_by",
                    task_refs=[sub_task_id],
                    current_plan_version=current_version,
                )
            # R1-09/R1-10：blocked_by 的目标资格与"属于直接依赖"在整批
            # 候选构造后统一核验（_validate_candidate），不依赖操作排列顺序。

        evidence_ids = list(op.evidence_ids or [])
        if status == "completed" and not evidence_ids:
            raise TaskLedgerError(
                "task_evidence_mismatch",
                f"任务 {sub_task_id} completed 必须通过任务成果校验："
                "evidence_ids 不能为空",
                task_refs=[sub_task_id],
                current_plan_version=current_version,
            )
        if status != "completed" and evidence_ids:
            raise TaskLedgerError(
                "invalid_argument",
                f"任务 {sub_task_id} status={status} 不携带成果 evidence_ids",
                task_refs=[sub_task_id],
                current_plan_version=current_version,
            )

        # 幂等识别（§5.3；R1-12）：优先与保存的完整规范化操作比较
        # （status_operation），派生字段（服务端生成 reason 等）不参与；
        # 旧记录无该字段时退回保存状态事实比较。
        op_facts = {
            "status": status,
            "evidence_ids": evidence_ids,
            "reason_code": reason_code,
            "reason": op.reason,
            "clarification_question": (
                op.clarification_question if status == "needs_clarification" else None
            ),
            "failure_refs": failure_refs,
            "blocked_by": blocked_by,
        }
        saved_operation = existing.get("status_operation")
        if saved_operation is not None:
            is_no_op = saved_operation == op_facts
        else:
            saved_facts = {
                "status": existing.get("status"),
                "evidence_ids": existing.get("evidence_ids") or [],
                "reason_code": existing.get("reason_code"),
                "reason": existing.get("reason"),
                "clarification_question": existing.get("clarification_question"),
                "failure_refs": existing.get("failure_refs") or [],
                "blocked_by": existing.get("blocked_by") or [],
            }
            is_no_op = saved_facts == op_facts
        if is_no_op:
            receipts.append(
                {
                    "op": "status",
                    "sub_task_id": sub_task_id,
                    "no_op": True,
                    "current_status": existing.get("status"),
                    "message": (
                        f"状态未变化：{sub_task_id} 已是 "
                        f"{existing.get('status')}（完整事实一致）"
                    ),
                }
            )
            return True

        # 转移合法性：pending→in_progress；pending/in_progress→终态（§5.1）
        allowed: dict[str, set[str]] = {
            "pending": {"in_progress", "completed", "failed", "needs_clarification"},
            "in_progress": {"completed", "failed", "needs_clarification"},
        }
        from_status = existing.get("status")
        if status not in allowed.get(from_status, set()):
            raise TaskLedgerError(
                "task_operation_conflict",
                f"任务 {sub_task_id} 不允许 {from_status}→{status}；"
                "终态不通过普通 status 原地恢复，重做/纠正成果请 revise",
                task_refs=[sub_task_id],
                current_plan_version=current_version,
            )

        # completed 成果归属校验（§5.1：服务端核验，不信模型自报）
        if status == "completed":
            self._validate_outcome_evidence(
                evidence_ids,
                task=existing,
                scope=scope,
                current_version=current_version,
            )
        # tool_failed 引用核验（§5.1）
        _validate_failure_refs(
            failure_refs,
            task={**existing, "sub_task_id": sub_task_id},
            errors=errors,
            ctx_scope=scope,
        )

        updated = dict(existing)
        updated["status"] = status
        updated["evidence_ids"] = evidence_ids
        updated["failure_refs"] = failure_refs
        updated["updated_at"] = now_iso
        updated["tool_call_id"] = tool_call_id
        updated["decision_source"] = "agent"
        updated["status_operation"] = op_facts  # R1-12：完整规范化输入
        if status in ("failed", "needs_clarification"):
            updated["reason_code"] = reason_code
            updated["reason"] = op.reason
        if status == "needs_clarification":
            updated["clarification_question"] = op.clarification_question
        if status == "failed" and reason_code == "dependency_blocked":
            updated["blocked_by"] = blocked_by
            updated["reason"] = op.reason or "依赖阻塞"  # 占位；候选核验后重生成
            updated["decision_source"] = "system"
        # 终止依据（服务端生成，§4.1）：基础字段先写入；依赖快照、
        # blocked_by_root 与服务端 reason 在整批候选校验后统一生成
        # （R1-09：不依赖操作排列顺序）。
        updated["termination_context"] = {"decision_plan_version": current_version + 1}
        candidate[sub_task_id] = updated
        written_ids.add(sub_task_id)
        written[task_ledger_key(run_id, sub_task_id, int(existing["revision"]))] = (
            updated
        )
        receipts.append(
            {
                "op": "status",
                "sub_task_id": sub_task_id,
                "no_op": False,
                "status": status,
            }
        )
        return False

    def _validate_outcome_evidence(
        self,
        evidence_ids: list[str],
        *,
        task: Mapping[str, Any],
        scope: Mapping[str, Any],
        current_version: int,
    ) -> None:
        """completed 的主成果必须属于本任务当前版本（§6.2）。

        由调用方传入的账本中查本轮执行/分析记录；本方法在 TaskLedger 上
        通过注入的 state 读 db_executions/db_analyses。
        """
        run_id = scope["run_id"]
        sub_task_id = task["sub_task_id"]
        revision = task["revision"]
        owned: dict[str, dict[str, Any]] = {}
        for record in self._executions:
            if (
                record.get("run_id") == run_id
                and record.get("purpose") == "query"
                and record.get("sub_task_id") == sub_task_id
                and record.get("sub_task_revision") == revision
            ):
                owned[record.get("evidence_id")] = record
        for record in self._analyses:
            if (
                record.get("run_id") == run_id
                and record.get("sub_task_id") == sub_task_id
                and record.get("sub_task_revision") == revision
            ):
                owned[record.get("evidence_id")] = record
        missing = [eid for eid in evidence_ids if eid not in owned]
        if missing:
            raise TaskLedgerError(
                "task_evidence_mismatch",
                f"任务 {sub_task_id}@{revision} 的成果证据不属于其当前版本: "
                + ", ".join(missing),
                hint="只可选择该任务当前版本下产生的 query E- / 分析 A- /"
                "受控沿用登记的新证据",
                task_refs=[sub_task_id],
                current_plan_version=current_version,
            )
        # R2-01/02：选定成果须核验完整证据图（direct/input 全部边、缺源、
        # 冲突、环、最终有效来源资格），与提交门控和 Runner 共享同一规则。
        # 不再把本轮新登记的目标记录当作来源记录（R2-01 根因）。
        from src.modules.db_deepagent.services.reuse_qualification import (
            unqualified_reuse_evidence,
        )

        failures = unqualified_reuse_evidence(
            [owned[eid] for eid in evidence_ids if eid in owned],
            session_id=scope.get("session_id") or "",
            thread_id=scope.get("thread_id") or "",
            run_id=run_id,
            state={
                "db_sub_tasks": self._records,
                "db_executions": self._executions,
                "db_analyses": self._analyses,
            },
            archive=self._archive,
        )
        for eid, reason in failures.items():
            raise TaskLedgerError(
                "stale_task_inputs",
                f"任务 {sub_task_id} 选定的成果 {eid} 证据图不成立：{reason}",
                task_refs=[sub_task_id],
                current_plan_version=current_version,
            )

    def _apply_void(
        self,
        op: LedgerOperation,
        *,
        existing: dict[str, Any],
        candidate: dict[str, dict[str, Any]],
        written: dict[str, dict[str, Any]],
        written_ids: set[str],
        receipts: list[dict[str, Any]],
        scope: Mapping[str, Any],
        now_iso: str,
        tool_call_id: str,
        current_version: int,
    ) -> bool:
        sub_task_id = op.sub_task_id
        run_id = scope["run_id"]
        if not is_nonblank_str(op.reason):
            raise TaskLedgerError(
                "invalid_argument",
                f"任务 {sub_task_id} void 必填 reason（字符串且非空白）",
                task_refs=[sub_task_id],
                current_plan_version=current_version,
            )
        replacement = op.replacement
        if replacement is not None:
            if not isinstance(replacement, Mapping):
                raise TaskLedgerError(
                    "invalid_task_replacement",
                    f"任务 {sub_task_id} 的 replacement 必须是映射",
                    task_refs=[sub_task_id],
                    current_plan_version=current_version,
                )
            if replacement.get("sub_task_id") == sub_task_id:
                raise TaskLedgerError(
                    "invalid_task_replacement",
                    f"任务 {sub_task_id} 的替代目标不能是自身",
                    task_refs=[sub_task_id],
                    current_plan_version=current_version,
                )
            # R1-09：替代目标的存在/当前/未作废与快照生成在整批候选校验
            # （_validate_candidate）统一执行——同批 add 的替代目标此时
            # 尚未在候选中，不依赖操作排列顺序。
        # 幂等：当前版本已 voided 且事实一致
        if existing.get("status") == "voided":
            saved = {
                "reason": existing.get("reason"),
                "replacement": existing.get("replacement"),
            }
            now_facts = {"reason": op.reason, "replacement": replacement}
            if saved == now_facts:
                receipts.append(
                    {
                        "op": "void",
                        "sub_task_id": sub_task_id,
                        "no_op": True,
                        "message": f"任务 {sub_task_id} 已作废，事实一致",
                    }
                )
                return True
            raise TaskLedgerError(
                "task_operation_conflict",
                f"任务 {sub_task_id} 已作废且作废事实不一致，不能重复 void",
                task_refs=[sub_task_id],
                current_plan_version=current_version,
            )
        updated = dict(existing)
        # R3-04 A：固定 revision 不代表状态不可变。从终态作废时由服务端
        # 封存原终态事实，历史阻塞决策复核读取它，而不是把 voided 的
        # status/reason 强行当成决策时的 NC/failed 事实。
        if existing.get("status") in (
            "failed",
            "needs_clarification",
            "completed",
        ):
            updated["pre_void_terminal"] = {
                "status": existing.get("status"),
                "reason_code": existing.get("reason_code"),
                "reason": existing.get("reason"),
                "clarification_question": existing.get("clarification_question"),
            }
        updated["status"] = "voided"
        updated["reason"] = op.reason
        updated["updated_at"] = now_iso
        updated["tool_call_id"] = tool_call_id
        updated["decision_source"] = "agent"
        updated["void_operation"] = {  # R1-12：完整规范化输入
            "reason": op.reason,
            "replacement": (
                {
                    "sub_task_id": replacement.get("sub_task_id"),
                    "revision": replacement.get("revision"),
                }
                if replacement is not None
                else None
            ),
        }
        updated["termination_context"] = {"decision_plan_version": current_version + 1}
        if replacement is not None:
            updated["replacement"] = {
                "sub_task_id": replacement.get("sub_task_id"),
                "revision": replacement.get("revision"),
            }
        # R2-04：与 reducer 共用 void 合法转移规则（允许终态→voided），
        # 服务层成功的 Command 不得在 reducer 侧被误拒。
        if not is_legal_void_transition(existing.get("status"), updated):
            raise TaskLedgerError(
                "task_operation_conflict",
                f"任务 {sub_task_id} 的作废转移不被共享规则接受",
                task_refs=[sub_task_id],
                current_plan_version=current_version,
            )
        candidate[sub_task_id] = updated
        written_ids.add(sub_task_id)
        written[task_ledger_key(run_id, sub_task_id, int(existing["revision"]))] = (
            updated
        )
        receipts.append(
            {
                "op": "void",
                "sub_task_id": sub_task_id,
                "no_op": False,
                "replacement": (
                    {
                        "sub_task_id": replacement.get("sub_task_id"),
                        "revision": replacement.get("revision"),
                    }
                    if replacement is not None
                    else None
                ),
            }
        )
        return False

    # ------------------------------------------------------------ 整批校验

    def _validate_candidate(
        self,
        candidate: Mapping[str, Mapping[str, Any]],
        *,
        modified_ids: set[str] | None = None,
    ) -> None:
        """§5.2：执行依赖 DAG 与终止审计引用整批校验（先合并后核验，R1-09）。

        终止依据的 dependency_snapshots / blocked_by_root / replacement_snapshot
        一律从最终候选生成并只回填本批修改的记录（历史依据不随后续改写）。
        """
        modified_ids = modified_ids or set()
        # 1) 执行依赖：pending/in_progress/completed 的当前版本
        for sub_task_id, record in candidate.items():
            status = record.get("status")
            deps = record.get("dependencies") or []
            if status in _EXECUTION_GRAPH_STATUSES:
                for dep in deps:
                    if dep["sub_task_id"] == sub_task_id:
                        raise TaskLedgerError(
                            "invalid_task_dependency",
                            f"任务 {sub_task_id} 不能依赖自身",
                            task_refs=[sub_task_id],
                        )
                    target = candidate.get(dep["sub_task_id"])
                    if target is None:
                        raise TaskLedgerError(
                            "invalid_task_dependency",
                            f"任务 {sub_task_id} 依赖未知目标 "
                            f"{dep['sub_task_id']}@{dep['revision']}",
                            task_refs=[sub_task_id],
                        )
                    if target.get("revision") != dep["revision"]:
                        raise TaskLedgerError(
                            "invalid_task_dependency",
                            f"任务 {sub_task_id} 依赖旧版 "
                            f"{dep['sub_task_id']}@{dep['revision']}"
                            f"（当前 revision={target.get('revision')}）；"
                            "请 revise 重绑依赖或改用替代目标",
                            task_refs=[sub_task_id],
                        )
                    if target.get("status") == "voided":
                        raise TaskLedgerError(
                            "invalid_task_dependency",
                            f"任务 {sub_task_id} 的有效执行依赖 "
                            f"{dep['sub_task_id']}@{dep['revision']} 已作废；"
                            "下游须同批 revise 重绑、转非执行终态或作废",
                            task_refs=[sub_task_id],
                        )
            else:
                # 终止审计引用：仅核对历史版本存在性（不要求仍是当前依赖）
                for dep in deps:
                    target = candidate.get(dep["sub_task_id"])
                    if target is None:
                        raise TaskLedgerError(
                            "invalid_task_dependency",
                            f"已终止任务 {sub_task_id} 的历史依赖 "
                            f"{dep['sub_task_id']}@{dep['revision']} 不存在",
                            task_refs=[sub_task_id],
                        )
        # 2) 环检测（执行图）
        graph: dict[str, list[str]] = {}
        for sub_task_id, record in candidate.items():
            if record.get("status") in _EXECUTION_GRAPH_STATUSES:
                graph[sub_task_id] = [
                    dep["sub_task_id"]
                    for dep in record.get("dependencies") or []
                    if dep["sub_task_id"] in candidate
                    and (candidate[dep["sub_task_id"]] or {}).get("status")
                    in _EXECUTION_GRAPH_STATUSES
                ]
        visiting: set[str] = set()
        done: set[str] = set()

        def _visit(node: str, path: list[str]) -> None:
            if node in done:
                return
            if node in visiting:
                cycle = path[path.index(node) :] + [node]
                raise TaskLedgerError(
                    "invalid_task_dependency",
                    "执行依赖成环: " + " -> ".join(cycle),
                    task_refs=sorted(set(cycle)),
                )
            visiting.add(node)
            for nxt in graph.get(node, []):
                _visit(nxt, [*path, node])
            visiting.discard(node)
            done.add(node)

        for node in sorted(graph):
            _visit(node, [node])

        # 固定版本索引（R2-06）：历史全部版本 + 最终候选当前版本。
        # 依赖快照按 (run, sub_task_id, revision) 取事实，不按逻辑 ID 取
        # 最新候选再套用旧 revision（否则同批 revise 会把 A@1 completed
        # 误写成 A@1 pending）。
        run_id = next(
            (r.get("run_id") for r in candidate.values() if r.get("run_id")),
            "",
        )
        version_index: dict[tuple[str, str, int], dict[str, Any]] = {}
        for key, record in self._records.items():
            parsed = parse_task_ledger_key(key)
            if parsed is None:
                raise TaskLedgerError(
                    "invalid_task_dependency", "任务账本键不可解码（历史版本）"
                )
            version_index[parsed] = dict(record)
        for tid, record in candidate.items():
            version_index[(run_id, tid, record.get("revision"))] = record

        # 3) R2-05：只有**本批新决策**按最终候选校验并生成服务端依据；
        # 历史决策不再按当前候选重验、不要求目标仍当前、不重新生成快照。
        # 先派生全部服务端原因，再封存一致快照（R2-06：批次顺序无关）。
        new_modified = sorted(modified_ids)
        for sub_task_id in new_modified:
            record = candidate.get(sub_task_id)
            if record is None or not (
                record.get("status") == "failed"
                and record.get("reason_code") == "dependency_blocked"
            ):
                continue
            blocked_by = record.get("blocked_by") or []
            if not blocked_by:
                raise TaskLedgerError(
                    "invalid_task_dependency",
                    f"任务 {sub_task_id} dependency_blocked 必填 blocked_by",
                    task_refs=[sub_task_id],
                )
            deps_fixed = {
                (d.get("sub_task_id"), d.get("revision"))
                for d in record.get("dependencies") or []
            }
            for dep in blocked_by:
                if (dep["sub_task_id"], dep["revision"]) not in deps_fixed:
                    raise TaskLedgerError(
                        "invalid_task_dependency",
                        f"任务 {sub_task_id} 的 blocked_by "
                        f"{dep['sub_task_id']}@{dep['revision']}"
                        " 不属于其声明的直接依赖（固定版本）",
                        task_refs=[sub_task_id],
                    )
                target = candidate.get(dep["sub_task_id"])
                if target is None or target.get("revision") != dep["revision"]:
                    raise TaskLedgerError(
                        "invalid_task_dependency",
                        f"任务 {sub_task_id} 的 blocked_by 指向未知依赖版本 "
                        f"{dep['sub_task_id']}@{dep['revision']}",
                        task_refs=[sub_task_id],
                    )
                if target.get("status") not in ("failed", "needs_clarification"):
                    raise TaskLedgerError(
                        "invalid_task_dependency",
                        f"任务 {sub_task_id} 的 blocked_by 目标 "
                        f"{dep['sub_task_id']}@{dep['revision']}"
                        " 未处于 failed/needs_clarification，不能谎报已阻塞",
                        task_refs=[sub_task_id],
                    )
            first = blocked_by[0]
            root = _resolve_blockage_root(
                version_index,
                candidate,
                run_id,
                (first["sub_task_id"], first["revision"]),
            )
            record["termination_context"]["blocked_by_root"] = root
            root_desc = root.get("reason") or "上游未能完成"
            record["reason"] = (
                f"依赖 {first['sub_task_id']}@{first['revision']}"
                f" 阻塞（根因：{root_desc}）"
            )

        # 4) R2-05：replacement 整批复核仅针对本批新 void 决策（同批 add
        # 的替代目标此时已在候选中）；历史替代决策不重验、不重生成快照。
        for sub_task_id in new_modified:
            record = candidate.get(sub_task_id)
            if record is None:
                continue
            replacement = record.get("replacement")
            if record.get("status") != "voided" or replacement is None:
                continue
            rep_id = replacement.get("sub_task_id")
            rep_rev = replacement.get("revision")
            target = candidate.get(rep_id) if isinstance(rep_id, str) else None
            if target is None or target.get("revision") != rep_rev:
                raise TaskLedgerError(
                    "invalid_task_replacement",
                    f"任务 {sub_task_id} 的替代目标 {rep_id}@{rep_rev}"
                    " 不在候选计划中",
                    task_refs=[sub_task_id],
                )
            if target.get("status") == "voided":
                raise TaskLedgerError(
                    "invalid_task_replacement",
                    f"替代目标 {rep_id} 已作废",
                    task_refs=[sub_task_id],
                )
            scope = record
            record["termination_context"]["replacement_snapshot"] = {
                "session_id": scope.get("session_id"),
                "thread_id": scope.get("thread_id"),
                "run_id": scope.get("run_id"),
                "sub_task_id": rep_id,
                "revision": rep_rev,
                "status": target.get("status"),
            }

        # 5) R2-06：所有新决策的服务端原因派生完成后，再封存一致的依赖
        # 快照；按固定 revision 从版本索引取事实，保存真实历史状态。
        for sub_task_id in new_modified:
            record = candidate.get(sub_task_id)
            if record is None or record.get("status") not in TERMINAL_STATUSES:
                continue
            deps = record.get("dependencies") or []
            if deps:
                record["termination_context"]["dependency_snapshots"] = [
                    _dependency_state_snapshot(version_index, candidate, run_id, dep)
                    for dep in deps
                ]
