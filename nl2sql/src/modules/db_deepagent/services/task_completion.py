"""共享完成判定与答案覆盖（doc 6.3 §7）

纯规则模块：结构校验、任务成果有效性、依赖血缘、覆盖集合、no_data 判定
与整体业务状态计算集中于此。I/O 由调用方预先装配为一致的只读快照；
规则函数不查库、不调用模型、不写账本。

提交工具与 Runner 复用同一入口 `evaluate_submission()`；`accepted` 表示
提交合法，不表示业务成功（完整交代失败任务、业务状态 failed 的合法提交
仍可 accepted=true）。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

# 完成规则版本：随判定语义变化递增，写入最终记录供审计
TASK_COMPLETION_RULES_VERSION = 1


@dataclass
class GateError:
    """一条门控错误（doc 6.3 §7.3 错误码 + 定位任务）"""

    code: str
    message: str
    task_refs: list[str] = field(default_factory=list)


@dataclass
class TaskOutcomeSummary:
    """交付用的任务结果摘要（确定性、面向用户）"""

    sub_task_id: str
    revision: int
    status: str
    content: str
    reason: str | None = None
    clarification_question: str | None = None


@dataclass
class CompletionDecision:
    """共享规则的判定结果（§7.1）"""

    accepted: bool
    expected_status: str | None
    errors: list[GateError] = field(default_factory=list)
    pending_task_refs: list[str] = field(default_factory=list)
    uncovered_task_refs: list[str] = field(default_factory=list)
    stale_evidence_ids: list[str] = field(default_factory=list)
    coverage_map: dict[str, list[str]] = field(default_factory=dict)
    authoritative_questions: list[str] = field(default_factory=list)
    unresolved_task_refs: list[dict[str, int | str]] = field(default_factory=list)
    outcome_summaries: list[TaskOutcomeSummary] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    plan_version: int = 0
    no_data_proven: bool = False
    # R2-08：逐项核验通过的 completed 任务（兜底不得用 expected_status
    # 存在与否代替明确的任务级成果结论）。
    verified_completed_refs: set[str] = field(default_factory=set)
    # R2-07：合法 findings 的实际结论（交付确定性分项正文的输入）。
    finding_summaries: list[dict[str, Any]] = field(default_factory=list)

    @property
    def primary_error(self) -> GateError | None:
        return self.errors[0] if self.errors else None


@dataclass
class CompletionSnapshot:
    """调用方装配的只读快照（§7.1：纯规则不隐式读文件）"""

    run_id: str
    tasks: dict[str, dict[str, Any]]  # 当前视图（含 voided），按 sub_task_id
    executions: list[dict[str, Any]]  # 本轮执行记录
    analyses: list[dict[str, Any]]  # 本轮分析记录
    errors: list[dict[str, Any]]  # 错误账本（读取侧已核验唯一性）
    known_counts: dict[str, int | None]  # result_facts 聚合
    proven_untruncated: dict[str, bool]  # result_facts 聚合
    # R1-02：本轮沿用登记证据的来源资格核验结果（{evidence_id: 原因}；
    # 由调用方装配时经 reuse_qualification 计算）
    reuse_failures: dict[str, str] = field(default_factory=dict)
    # R1-11：全部任务版本记录 {(run_id, sub_task_id, revision): record}——
    # replacement 历史复核按完整身份，不要求目标仍是最新版本
    all_task_records: dict[tuple, dict] = field(default_factory=dict)
    plan_version: int = 0


def _is_nonblank(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _error_ref_key(error_ref: Mapping[str, Any]) -> tuple:
    return (
        error_ref.get("session_id"),
        error_ref.get("thread_id"),
        error_ref.get("run_id"),
        error_ref.get("error_id"),
    )


def _build_error_index(errors: list[dict[str, Any]]) -> dict[tuple, dict[str, Any]]:
    """error_by_ref 读取语义（§4.3）：同完整引用重复即损坏。

    调用方（LedgerView）已做过重复/冲突检查；此处重建索引时同样拒绝重复，
    不取第一条掩盖损坏。
    """
    index: dict[tuple, dict[str, Any]] = {}
    for record in errors:
        if not isinstance(record, Mapping):
            # 账本损坏检测（非调用方可修正的类型误用）
            raise ValueError("错误账本存在非映射记录")  # noqa: TRY004
        key = (
            record.get("session_id"),
            record.get("thread_id"),
            record.get("run_id"),
            record.get("error_id"),
        )
        if not all(isinstance(part, str) and part for part in key):
            raise ValueError("错误记录缺失完整身份")
        if key in index:
            raise ValueError(f"错误账本同完整引用重复: {key[3]}")
        index[key] = dict(record)
    return index


def _dep_state_snapshot(rec: Mapping[str, Any], key: tuple) -> dict[str, Any]:
    snap: dict[str, Any] = {
        "sub_task_id": key[0],
        "revision": key[1],
        "status": rec.get("status", "unknown"),
    }
    if rec.get("reason_code") is not None:
        snap["reason_code"] = rec["reason_code"]
    if rec.get("reason") is not None:
        snap["reason"] = rec["reason"]
    return snap


# ---------------------------------------------------------------------------
# R3-04：决策时事实（生成端 TaskLedger 与复核端共享同一规则）
# ---------------------------------------------------------------------------


def terminal_facts(rec: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """固定版本的不可变决策时终态事实。

    R3-04 A：固定 revision 不代表状态不可变。同 revision 后来被 void 时，
    服务端在转换时封存 `pre_void_terminal`；历史复核读取它，而不是把
    voided status/reason 强行当作决策时的 NC/failed 事实。
    """
    if not isinstance(rec, Mapping):
        return None
    if rec.get("status") == "voided" and isinstance(
        rec.get("pre_void_terminal"), Mapping
    ):
        pv = rec["pre_void_terminal"]
        return {
            "status": pv.get("status"),
            "reason_code": pv.get("reason_code"),
            "reason": pv.get("reason"),
        }
    return {
        "status": rec.get("status"),
        "reason_code": rec.get("reason_code"),
        "reason": rec.get("reason"),
    }


def _root_from_facts(key: tuple, facts: Mapping[str, Any]) -> dict[str, Any]:
    snap: dict[str, Any] = {
        "sub_task_id": key[0],
        "revision": key[1],
        "status": facts.get("status", "unknown"),
    }
    if facts.get("reason_code") is not None:
        snap["reason_code"] = facts["reason_code"]
    if facts.get("reason") is not None:
        snap["reason"] = facts["reason"]
    return snap


def derive_decision_root(
    lookup: Any, start_key: tuple, *, max_hops: int = 64
) -> dict[str, Any] | None:
    """沿 blocked_by 的**固定版本**（不按逻辑 ID 追最新版）推导阻塞根因。

    lookup(sub_task_id, revision) -> 该固定版本的记录或 None。R3-04 B：遇到
    已封存的 blocked 节点时按其固定决策依据继续，不重新绑定到最新版本。
    """
    visited: set[tuple] = set()
    key = (start_key[0], start_key[1])
    hops = 0
    while key not in visited and hops < max_hops:
        hops += 1
        visited.add(key)
        rec = lookup(key[0], key[1])
        facts = terminal_facts(rec)
        if facts is None or facts.get("status") == "voided":
            return None
        if facts.get("status") == "needs_clarification":
            return _root_from_facts(key, facts)
        if facts.get("reason_code") == "dependency_blocked":
            chain = rec.get("blocked_by") or []
            if not chain or not isinstance(chain[0], Mapping):
                return None
            key = (chain[0].get("sub_task_id"), chain[0].get("revision"))
            continue
        return _root_from_facts(key, facts)
    return None


def root_matches(saved: Any, derived: Mapping[str, Any] | None) -> bool:
    """已保存根因与固定历史推导逐项一致（伪造根因拒绝）。"""
    if derived is None or not isinstance(saved, Mapping):
        return False
    for key in ("sub_task_id", "revision", "status", "reason_code", "reason"):
        if saved.get(key) != derived.get(key):
            return False
    return True


def validate_blocked_decision(
    rec: Mapping[str, Any],
    *,
    lookup: Any,
    plan_version: int,
    run_id: str,
) -> list[str]:
    """逐项核验 dependency_blocked 的决策时依据（R3-04 C/D / R4-02）。

    - decision_plan_version 必须是合法且不超当前计划的整数；
    - dependency_snapshots 必须非空、条目身份合法且唯一，并逐项覆盖
      全部 blocked_by；每项必须与固定历史版本的决策时事实一致；
    - 每个 blocked_by 都要逐项核验固定身份、决策时依赖快照、决策时
      事实与可推导根因（保留后来追加伪造来源、缺快照、错误计划版本的
      拒绝，R3-04 C/D）；
    - blocked_by_root 只是首项的主要根因展示摘要，仅要求它与首项的
      可推导根因一致；其余独立阻塞项各自核验（身份/快照/事实/根可推导
      已完成），不强制与首项同根（R4-02），也不新增多根展示协议。
    返回错误文本列表（空表示通过）。
    """
    errors: list[str] = []
    blocked_by = rec.get("blocked_by")
    if (
        not isinstance(blocked_by, list)
        or not blocked_by
        or not all(isinstance(d, Mapping) for d in blocked_by)
    ):
        return ["dependency_blocked 缺少合法的 blocked_by"]
    ctx = rec.get("termination_context")
    if not isinstance(ctx, Mapping):
        return ["dependency_blocked 缺少 termination_context（决策时终止依据）"]
    dpv = ctx.get("decision_plan_version")
    if type(dpv) is not int or dpv < 0 or dpv > plan_version:
        errors.append("decision_plan_version 非整数或超过当前计划版本")
    snapshots = ctx.get("dependency_snapshots")
    if not isinstance(snapshots, list) or not snapshots:
        return errors + ["dependency_snapshots 缺失、为空或类型非法"]
    snap_by_key: dict[tuple, Mapping[str, Any]] = {}
    for item in snapshots:
        if not isinstance(item, Mapping):
            errors.append("dependency_snapshots 含非映射条目")
            continue
        key = (item.get("sub_task_id"), item.get("revision"))
        if not isinstance(key[0], str) or type(key[1]) is not int:
            errors.append("dependency_snapshots 条目身份非字符串/整数")
            continue
        if key in snap_by_key:
            errors.append(f"dependency_snapshots 重复身份 {key[0]}@{key[1]}")
            continue
        snap_by_key[key] = item
    deps_fixed = {
        (d.get("sub_task_id"), d.get("revision")) for d in rec.get("dependencies") or []
    }
    for key in snap_by_key:
        if key not in deps_fixed:
            errors.append(f"dependency_snapshots {key[0]}@{key[1]} 不属于声明依赖")
    saved_root = ctx.get("blocked_by_root")
    if not isinstance(saved_root, Mapping):
        errors.append("blocked_by_root 缺失或类型非法")
    for index, dep in enumerate(blocked_by):
        key = (dep.get("sub_task_id"), dep.get("revision"))
        if key not in deps_fixed:
            errors.append(f"blocked_by {key[0]}@{key[1]} 不属于声明直接依赖")
            continue
        snap = snap_by_key.get(key)
        if snap is None:
            errors.append(f"blocked_by {key[0]}@{key[1]} 未被决策时依赖快照覆盖")
            continue
        facts = terminal_facts(lookup(key[0], key[1]))
        if facts is None or facts.get("status") not in (
            "failed",
            "needs_clarification",
        ):
            errors.append(
                f"blocked_by {key[0]}@{key[1]} 的固定历史版本在决策时"
                "并非 failed/needs_clarification"
            )
            continue
        if (
            snap.get("status") != facts.get("status")
            or snap.get("reason_code") != facts.get("reason_code")
            or snap.get("reason") != facts.get("reason")
        ):
            errors.append(f"blocked_by {key[0]}@{key[1]} 的依赖快照与决策时事实不符")
            continue
        derived = derive_decision_root(lookup, key)
        if derived is None:
            errors.append(f"blocked_by {key[0]}@{key[1]} 的阻塞根因不可推导")
            continue
        # R4-02：blocked_by_root 只绑定首项主要根因摘要；其余独立阻塞项
        # 已完成上述逐项核验，不再要求与首项同根。
        if index == 0 and not root_matches(saved_root, derived):
            errors.append("blocked_by 首项的根因与已封存 blocked_by_root 不符")
    return errors


def _task_outcome_summaries(
    tasks: Mapping[str, Mapping[str, Any]],
) -> list[TaskOutcomeSummary]:
    out: list[TaskOutcomeSummary] = []
    for sub_task_id in sorted(tasks):
        rec = tasks[sub_task_id]
        out.append(
            TaskOutcomeSummary(
                sub_task_id=sub_task_id,
                revision=int(rec.get("revision", 0) or 0),
                status=str(rec.get("status", "")),
                content=str(rec.get("content", "")),
                reason=rec.get("reason"),
                clarification_question=rec.get("clarification_question"),
            )
        )
    return out


def evaluate_submission(
    normalized_submission: Mapping[str, Any] | None,
    snapshot: CompletionSnapshot,
) -> CompletionDecision:
    """共享门控入口（§7.3 校验顺序 / §7.4 状态映射）。"""
    decision = CompletionDecision(
        accepted=False,
        expected_status=None,
        plan_version=snapshot.plan_version,
        counts={},
    )
    tasks = snapshot.tasks
    non_void = {tid: rec for tid, rec in tasks.items() if rec.get("status") != "voided"}
    decision.outcome_summaries = _task_outcome_summaries(tasks)
    decision.counts = {
        "completed": sum(
            1 for r in non_void.values() if r.get("status") == "completed"
        ),
        "failed": sum(1 for r in non_void.values() if r.get("status") == "failed"),
        "needs_clarification": sum(
            1 for r in non_void.values() if r.get("status") == "needs_clarification"
        ),
        "voided": sum(1 for r in tasks.values() if r.get("status") == "voided"),
        "dependency_blocked": sum(
            1
            for r in non_void.values()
            if r.get("status") == "failed"
            and r.get("reason_code") == "dependency_blocked"
        ),
    }

    # 步骤 2：任务视图——空集合与全部作废均拒绝（§7.4 第一行）
    if not tasks or not non_void:
        decision.errors.append(
            GateError(
                "task_not_registered",
                "本轮没有有效业务任务（未登记或全部作废），不能提交成功结论",
            )
        )
        return decision

    error_index: dict[tuple, dict[str, Any]] | None = None
    try:
        error_index = _build_error_index(snapshot.errors)
    except ValueError as exc:
        decision.errors.append(
            GateError("invalid_task_outcomes", f"错误账本不可核验: {exc}")
        )
        return decision

    # 步骤 3：任务级复核（必填文本、终止审计引用、执行依赖）
    for sub_task_id, rec in tasks.items():
        status = rec.get("status")
        if status in ("failed", "needs_clarification", "voided") and not _is_nonblank(
            rec.get("reason")
        ):
            decision.errors.append(
                GateError(
                    "invalid_task_outcomes",
                    f"任务 {sub_task_id}（{status}）缺少非空白 reason",
                    task_refs=[sub_task_id],
                )
            )
        if status == "needs_clarification" and not _is_nonblank(
            rec.get("clarification_question")
        ):
            decision.errors.append(
                GateError(
                    "invalid_task_outcomes",
                    f"任务 {sub_task_id} 缺少非空白 clarification_question",
                    task_refs=[sub_task_id],
                )
            )
        if status == "failed" and rec.get("reason_code") == "tool_failed":
            refs = rec.get("failure_refs") or []
            if not refs:
                decision.errors.append(
                    GateError(
                        "invalid_task_failure_ref",
                        f"任务 {sub_task_id} 声称工具失败但缺少 failure_refs",
                        task_refs=[sub_task_id],
                    )
                )
            for ref in refs:
                record = (error_index or {}).get(
                    _error_ref_key(ref.get("error_ref") or {})
                )
                if record is None:
                    decision.errors.append(
                        GateError(
                            "invalid_task_failure_ref",
                            f"任务 {sub_task_id} 引用的错误记录不存在或身份不全",
                            task_refs=[sub_task_id],
                        )
                    )
                    continue
                source = record.get("source_task_ref") or {}
                ref_source = ref.get("source_task_ref") or {}
                if (
                    source.get("sub_task_id") != sub_task_id
                    or source.get("revision") != rec.get("revision")
                    or source.get("run_id") != snapshot.run_id
                ):
                    decision.errors.append(
                        GateError(
                            "invalid_task_failure_ref",
                            f"任务 {sub_task_id} 引用的错误不属于其当前版本",
                            task_refs=[sub_task_id],
                        )
                    )
                    continue
                # R1-08：引用内自报的 source_task_ref/tool_call_id/error_type
                # 与权威记录逐项一致（防御绕过登记的伪造）
                if (
                    ref_source.get("sub_task_id") != source.get("sub_task_id")
                    or ref_source.get("revision") != source.get("revision")
                    or ref_source.get("run_id") != source.get("run_id")
                    or ref.get("tool_call_id") != record.get("tool_call_id")
                    or ref.get("error_type") != record.get("error_type")
                ):
                    decision.errors.append(
                        GateError(
                            "invalid_task_failure_ref",
                            f"任务 {sub_task_id} 引用的错误不属于其当前版本",
                            task_refs=[sub_task_id],
                        )
                    )
        # voided 的 replacement 历史复核（R1-11）：按完整历史身份与决策时
        # replacement_snapshot，不要求目标后续仍为当前/未作废版本
        if status == "voided":
            replacement = rec.get("replacement")
            if replacement is not None:
                target = snapshot.all_task_records.get(
                    (
                        snapshot.run_id,
                        replacement.get("sub_task_id"),
                        replacement.get("revision"),
                    )
                )
                if target is None and tasks.get(replacement.get("sub_task_id"), {}).get(
                    "revision"
                ) == replacement.get("revision"):
                    target = tasks.get(replacement.get("sub_task_id"))
                if target is None:
                    decision.errors.append(
                        GateError(
                            "invalid_task_replacement",
                            f"任务 {sub_task_id} 的替代目标历史版本不存在",
                            task_refs=[sub_task_id],
                        )
                    )
                else:
                    snap = (rec.get("termination_context") or {}).get(
                        "replacement_snapshot"
                    )
                    if not isinstance(snap, Mapping) or (
                        snap.get("sub_task_id") != replacement.get("sub_task_id")
                        or snap.get("revision") != replacement.get("revision")
                        or snap.get("run_id") != snapshot.run_id
                    ):
                        decision.errors.append(
                            GateError(
                                "invalid_task_replacement",
                                f"任务 {sub_task_id} 缺少合法的 replacement_snapshot"
                                "（决策时快照缺失或身份不符）",
                                task_refs=[sub_task_id],
                            )
                        )
        # R3-04：dependency_blocked 完整审计复核。生成端与复核端共用
        # validate_blocked_decision 的决策时事实规则；逐项核验全部
        # blocked_by、依赖快照与决策版本，同 revision 后来 void 由
        # pre_void_terminal 保留原终态事实，不反向否定历史决策。
        if status == "failed" and rec.get("reason_code") == "dependency_blocked":

            def _lookup(tid: Any, rev: Any) -> Mapping[str, Any] | None:
                hist = snapshot.all_task_records.get((snapshot.run_id, tid, rev))
                if hist is not None:
                    return hist
                current = tasks.get(tid)
                if current is not None and current.get("revision") == rev:
                    return current
                return None

            for message in validate_blocked_decision(
                rec,
                lookup=_lookup,
                plan_version=snapshot.plan_version,
                run_id=snapshot.run_id,
            ):
                decision.errors.append(
                    GateError(
                        "invalid_task_dependency",
                        f"任务 {sub_task_id} 的阻塞决策依据不成立：{message}",
                        task_refs=[sub_task_id],
                    )
                )
        # 执行依赖复核：执行图任务的依赖须为当前、未作废；completed 任务的
        # 依赖还须已 completed（使用成果前必须全部完成，§5.2）
        if status in ("pending", "in_progress", "completed"):
            for dep in rec.get("dependencies") or []:
                target = tasks.get(dep.get("sub_task_id"))
                if (
                    target is None
                    or target.get("revision") != dep.get("revision")
                    or target.get("status") == "voided"
                ):
                    decision.errors.append(
                        GateError(
                            "invalid_task_dependency",
                            f"任务 {sub_task_id} 的执行依赖 "
                            f"{dep.get('sub_task_id')}@{dep.get('revision')}"
                            " 已失效（非当前版本或已作废）",
                            task_refs=[sub_task_id],
                        )
                    )
                elif status == "completed" and target.get("status") != "completed":
                    decision.errors.append(
                        GateError(
                            "invalid_task_dependency",
                            f"任务 {sub_task_id} 已完成但其依赖 "
                            f"{dep.get('sub_task_id')}@{dep.get('revision')}"
                            " 尚未 completed",
                            task_refs=[sub_task_id],
                        )
                    )

    if decision.errors:
        return decision

    # 步骤 5：pending/in_progress 拒绝普通提交
    pending = sorted(
        tid
        for tid, rec in non_void.items()
        if rec.get("status") in ("pending", "in_progress")
    )
    decision.pending_task_refs = pending
    if pending:
        decision.errors.append(
            GateError(
                "task_not_terminal",
                "以下任务仍处于 pending/in_progress，不能普通提交："
                + ", ".join(pending),
                task_refs=pending,
            )
        )
        return decision

    # 权威问题（§7.2 固定算法第 1 步）：最新未作废 needs_clarification 按
    # (sub_task_id Unicode 码点序, revision 整数) 升序
    nc_tasks = sorted(
        (
            rec
            for rec in non_void.values()
            if rec.get("status") == "needs_clarification"
        ),
        key=lambda r: (r["sub_task_id"], int(r.get("revision", 0) or 0)),
    )
    decision.authoritative_questions = [
        rec["clarification_question"].strip()
        for rec in nc_tasks
        if _is_nonblank(rec.get("clarification_question"))
    ]
    decision.unresolved_task_refs = [
        {"sub_task_id": rec["sub_task_id"], "revision": int(rec.get("revision", 0))}
        for rec in sorted(
            (
                rec
                for rec in non_void.values()
                if rec.get("status") in ("failed", "needs_clarification")
            ),
            key=lambda r: (r["sub_task_id"], int(r.get("revision", 0) or 0)),
        )
    ]

    # 步骤 6：成果资格视图与覆盖检查。资格视图与任务级成果核验独立于是否
    # 存在最终提交（R2-08）：无提交的异常收尾也须逐项核验，不能把状态映射
    # 的存在当成"成果已核验"。
    eligible_evidence: dict[str, dict[str, Any]] = {}
    for record in snapshot.executions:
        if record.get("run_id") == snapshot.run_id and record.get("purpose") == "query":
            eligible_evidence[record["evidence_id"]] = record
    for record in snapshot.analyses:
        if record.get("run_id") == snapshot.run_id:
            eligible_evidence[record["evidence_id"]] = record

    # R2-02/R2-08：每个 completed 任务选定的**全部**主成果逐项核验——
    # 存在、归属本任务当前版本、沿用来源资格成立；缺一即拒绝，并且该任务
    # 不计入"已核验"（不能以一个合法主证据掩盖其余，也不能让兜底误报成功）。
    for tid, rec in non_void.items():
        if rec.get("status") != "completed":
            continue
        selected = rec.get("evidence_ids") or []
        task_ok = bool(selected)
        for eid in selected:
            record = eligible_evidence.get(eid)
            if record is None:
                task_ok = False
                decision.errors.append(
                    GateError(
                        "task_evidence_mismatch",
                        f"任务 {tid} 选定成果 {eid} 不存在于本轮原始记录",
                        task_refs=[tid],
                    )
                )
            elif record.get("sub_task_id") != tid or record.get(
                "sub_task_revision"
            ) != rec.get("revision"):
                task_ok = False
                decision.errors.append(
                    GateError(
                        "task_evidence_mismatch",
                        f"任务 {tid} 选定成果 {eid} 不属于其当前版本",
                        task_refs=[tid],
                    )
                )
            elif eid in snapshot.reuse_failures:
                task_ok = False
                decision.errors.append(
                    GateError(
                        "stale_task_inputs",
                        f"任务 {tid} 选定成果 {eid} 的沿用来源资格不成立："
                        f"{snapshot.reuse_failures[eid]}",
                        task_refs=[tid],
                    )
                )
        if task_ok:
            decision.verified_completed_refs.add(tid)

    if normalized_submission is None:
        # 提交工具不会走到这里（结构非法在参数层拒绝）；Runner 用 None 表达
        # 无有效提交。任务级成果核验已在上方独立执行，expected_status 仅在
        # 没有任何门控错误时给出，供状态展示；不能当成果已核验标志。
        decision.accepted = False
        if not decision.errors:
            decision.expected_status = _map_business_status(
                non_void, snapshot, decision
            )
        return decision

    findings = list(normalized_submission.get("findings") or [])
    # R2-07：合法 findings 的实际结论进入确定性交付正文输入。
    decision.finding_summaries = [
        {
            "finding_id": finding.get("finding_id"),
            "text": (finding.get("text") or "").strip(),
            "task_refs": finding.get("task_refs") or [],
        }
        for finding in findings
        if _is_nonblank(finding.get("text"))
    ]

    def _owner_completed_current(evidence: Mapping[str, Any]) -> str | None:
        owner = evidence.get("sub_task_id")
        if not isinstance(owner, str):
            return None
        owner_rec = tasks.get(owner)
        if owner_rec is None or owner_rec.get("status") != "completed":
            return None
        if owner_rec.get("revision") != evidence.get("sub_task_revision"):
            return None
        return owner

    # 所有引用证据必须合法（主证据或合法旁证，规则 4）
    for finding in findings:
        for eid in finding.get("evidence_ids") or []:
            record = eligible_evidence.get(eid)
            if record is None:
                decision.stale_evidence_ids.append(eid)
                decision.errors.append(
                    GateError(
                        "task_evidence_mismatch",
                        f"finding {finding.get('finding_id')} 引用的证据 {eid}"
                        " 不在本轮可引用记录中",
                    )
                )
                continue
            if eid in snapshot.reuse_failures:
                decision.stale_evidence_ids.append(eid)
                decision.errors.append(
                    GateError(
                        "stale_task_inputs",
                        f"证据 {eid} 的沿用来源资格不成立："
                        f"{snapshot.reuse_failures[eid]}",
                    )
                )
            if _owner_completed_current(record) is None:
                decision.stale_evidence_ids.append(eid)
                decision.errors.append(
                    GateError(
                        "task_evidence_mismatch",
                        f"证据 {eid} 不属于任何最新、未作废且 completed 的任务版本，"
                        "不能作为主证据或附加旁证",
                    )
                )

    # 覆盖规则 1–4：每个 task_ref 至少引用一条该任务已选定的主证据
    covered: dict[str, list[str]] = {}
    for finding in findings:
        finding_id = finding.get("finding_id", "")
        for ref in finding.get("task_refs") or []:
            tid = ref.get("sub_task_id")
            revision = ref.get("revision")
            target = tasks.get(tid)
            if (
                target is None
                or target.get("status") != "completed"
                or target.get("status") == "voided"
                or target.get("revision") != revision
            ):
                decision.errors.append(
                    GateError(
                        "task_revision_mismatch",
                        f"finding {finding_id} 的 task_ref {tid}@{revision}"
                        " 不是本轮最新、未作废、completed 的任务版本",
                        task_refs=[tid] if tid else [],
                    )
                )
                continue
            selected = set(target.get("evidence_ids") or [])
            primary_hit = [
                eid
                for eid in finding.get("evidence_ids") or []
                if eid in selected
                and (eligible_evidence.get(eid) or {}).get("sub_task_id") == tid
                and (eligible_evidence.get(eid) or {}).get("sub_task_revision")
                == revision
            ]
            if not primary_hit:
                decision.errors.append(
                    GateError(
                        "task_evidence_mismatch",
                        f"finding {finding_id} 引用任务 {tid}@{revision}"
                        " 但未引用任何已选入其成果列表的主证据",
                        task_refs=[tid],
                    )
                )
                continue
            covered.setdefault(f"{tid}@{revision}", []).append(finding_id)
    decision.coverage_map = covered

    # R1-02：全部主成果逐项核验已在上方独立执行（与是否存在提交无关），
    # 沿用来源资格失败/缺源/归属不符的成果即使未被 finding 引用也拒绝。
    # 规则 4：每个 completed 任务至少被一条合法 finding 覆盖
    uncovered = sorted(
        tid
        for tid, rec in non_void.items()
        if rec.get("status") == "completed"
        and f"{tid}@{rec.get('revision')}" not in covered
    )
    decision.uncovered_task_refs = uncovered
    if uncovered:
        decision.errors.append(
            GateError(
                "task_answer_uncovered",
                "以下 completed 任务未被任何合法 finding 覆盖: " + ", ".join(uncovered),
                task_refs=uncovered,
            )
        )

    # 规则 5：unresolved 恰覆盖 failed/NC 最新任务
    submitted_list = [
        (ref.get("sub_task_id"), ref.get("revision"))
        for ref in normalized_submission.get("unresolved_task_refs") or []
    ]
    expected_unresolved = {
        (ref["sub_task_id"], ref["revision"]) for ref in decision.unresolved_task_refs
    }
    # R1-17：多重集合精确比较——重复提交（set 化后恰好相等）也拒绝
    submitted_unresolved = set(submitted_list)
    if submitted_unresolved != expected_unresolved or len(submitted_list) != len(
        submitted_unresolved
    ):
        missing = expected_unresolved - submitted_unresolved
        extra = submitted_unresolved - expected_unresolved
        parts = []
        if missing:
            parts.append(
                "遗漏: " + ", ".join(f"{tid}@{rev}" for tid, rev in sorted(missing))
            )
        if extra:
            parts.append(
                "多余/重复/旧版: "
                + ", ".join(f"{tid}@{rev}" for tid, rev in sorted(extra))
            )
        decision.errors.append(
            GateError(
                "invalid_task_outcomes",
                "unresolved_task_refs 未恰覆盖本轮 failed/needs_clarification 任务（"
                + "；".join(parts)
                + "）",
            )
        )

    if decision.errors:
        return decision

    # 步骤 7：唯一业务状态（§7.4 自上而下）
    expected_status = _map_business_status(non_void, snapshot, decision)
    decision.expected_status = expected_status
    submitted_status = normalized_submission.get("status")
    if submitted_status != expected_status:
        decision.errors.append(
            GateError(
                "final_status_mismatch",
                f"提交 status={submitted_status!r} 与业务判定不符，"
                f"expected_status={expected_status!r}",
            )
        )
        return decision
    decision.accepted = True
    return decision


def _map_business_status(
    non_void: Mapping[str, Mapping[str, Any]],
    snapshot: CompletionSnapshot,
    decision: CompletionDecision,
) -> str:
    """§7.4 状态映射（自上而下；结构性拒绝已在前置步骤处理）。"""
    statuses = [rec.get("status") for rec in non_void.values()]
    if any(s == "needs_clarification" for s in statuses):
        return "needs_clarification"
    if statuses and all(s == "failed" for s in statuses):
        return "failed"
    if any(s == "completed" for s in statuses) and any(s == "failed" for s in statuses):
        return "partial"
    if statuses and all(s == "completed" for s in statuses):
        if _proven_all_empty(non_void, snapshot):
            decision.no_data_proven = True
            return "no_data"
        return "completed"
    return "failed"


def _proven_all_empty(
    non_void: Mapping[str, Mapping[str, Any]], snapshot: CompletionSnapshot
) -> bool:
    """严格空结果判定（§7.4）：每项选定的全部主成果可靠行数为 0 且证明未截断。"""
    for rec in non_void.values():
        if rec.get("status") != "completed":
            continue
        selected = rec.get("evidence_ids") or []
        if not selected:
            return False
        for eid in selected:
            if snapshot.known_counts.get(eid) != 0:
                return False
            if not snapshot.proven_untruncated.get(eid, False):
                return False
    return True


def all_task_versions(state: Any) -> dict[tuple[str, int], dict]:
    """全部任务版本记录（R1-11：按 (id, rev) 键；跨 run 不混——键含 run）。
    返回 {(run_id, sub_task_id, revision): record}。"""
    from src.modules.db_deepagent.state import parse_task_ledger_key

    out: dict[tuple, dict] = {}
    for key, record in (state.get("db_sub_tasks") or {}).items():
        parsed = parse_task_ledger_key(key)
        if parsed is None or not isinstance(record, Mapping):
            continue
        out[parsed] = dict(record)
    return out


# ------------------------------------------------ §7.2 确定性分项正文


def build_delivery_answer_section(decision: CompletionDecision) -> str:
    """确定性"分项结果/未完成事项"段落（§7.2；R1-13）。

    逐项展示合法 finding 文本、失败原因、澄清问题与作废说明；完全相同
    条目去重（保序）。合法交付（model 来源）与系统兜底共用；原始提交
    payload 由调用方保真归档，不因此改写。
    """
    lines: list[str] = []
    seen: set[str] = set()

    def _add(text: str | None) -> None:
        if not isinstance(text, str) or not text.strip():
            return
        key = text.strip()
        if key in seen:
            return
        seen.add(key)
        lines.append(f"- {key}")

    # R2-07：合法 findings 的实际结论必须进入交付正文（模型 answer 可能
    # 只写"已处理"）。逐项展示 finding 文本及对应分项任务，原始 payload
    # 由调用方保真归档，不因此改写。
    for finding in decision.finding_summaries:
        refs = ", ".join(
            f"{r.get('sub_task_id')}@{r.get('revision')}"
            for r in finding.get("task_refs") or []
            if isinstance(r, Mapping)
        )
        label = finding.get("finding_id") or "分项"
        suffix = f"（{refs}）" if refs else ""
        _add(f"结论 {label}{suffix}：{finding.get('text')}")
    for summary in decision.outcome_summaries:
        if summary.status == "completed":
            _add(f"已完成：{summary.content}")
        elif summary.status == "failed":
            reason = (summary.reason or "").strip() or "未提供原因"
            _add(f"未完成：{summary.content}（原因：{reason}）")
        elif summary.status == "needs_clarification":
            question = (summary.clarification_question or "").strip() or "需要澄清"
            _add(f"待澄清：{summary.content}（问题：{question}）")
        elif summary.status == "voided":
            reason = (summary.reason or "").strip() or "计划调整"
            _add(f"已作废：{summary.content}（原因：{reason}）")
    for question in decision.authoritative_questions:
        _add(f"需要澄清：{question}")
    return "\n".join(lines)


# ------------------------------------------------ §7.2 交付问题列表固定算法


def merge_delivery_open_questions(
    authoritative: list[str], supplementary: list[str]
) -> list[str]:
    """权威问题在前（账本排序已定）、补充按原提交序在后；strip 后精确去重。

    不做大小写折叠或 Unicode 归一化，不按文本再排序；空条目丢弃；保留内部
    空白。去重键为清理后的完整字符串（str.strip()）。
    """
    merged: list[str] = []
    seen: set[str] = set()
    for text in [*authoritative, *(supplementary or [])]:
        if not isinstance(text, str):
            continue
        cleaned = text.strip()
        if not cleaned:
            continue
        if cleaned in seen:
            continue
        seen.add(cleaned)
        merged.append(cleaned)
    return merged
