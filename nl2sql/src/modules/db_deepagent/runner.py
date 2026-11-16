"""DbAgentRunner：一次请求的五个阶段（docs/v4/5 §14.3）

P0 准备（线程确定／轮换／携带证据）→ P1 运行（invoke）→ P2 读取（get_state）
→ P3 归档（Store）→ P4 本地记录。各阶段异常按发生顺序记入 RunFailures，
最终状态按优先级判定；primary 标在决定状态那一类中最早的一条。
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional, TypedDict

from langchain_core.messages import HumanMessage

from src.modules.db_deepagent.agent import DbAgentHandle, read_summary_for_handoff
from src.modules.db_deepagent.config import (
    DbAgentConfig,
    compute_validation_fingerprint,
)
from src.modules.db_deepagent.context import DbAgentContext
from src.modules.db_deepagent.errors import is_persistence_error
from src.modules.db_deepagent.middleware.model_observer import (
    ModelCallObserver,
    ModelObservationSnapshot,
)
from src.modules.db_deepagent.persistence.archive import (
    ARCHIVE_SCHEMA_VERSION,
    QaArchive,
    RUN_SNAPSHOT_SCHEMA_VERSION,
)
from src.modules.db_deepagent.schemas import (
    DbAgentResult,
    EvidenceItem,
    FindingItem,
    RunFailureRecord,
    RunStats,
    extract_raw_final_answer_payload,
    normalize_final_answer_record,
)
from src.modules.db_deepagent.services.result_facts import (
    aggregate_known_saved_row_counts,
    aggregate_proven_untruncated,
)
from src.modules.db_deepagent.services.run_budget import (
    BudgetSnapshot,
    limits_from_config,
)
from src.modules.db_deepagent.services.sql_pipeline import (
    LedgerView,
    TaskAccessError,
)
from src.modules.db_deepagent.services.task_completion import (
    TASK_COMPLETION_RULES_VERSION,
    CompletionDecision,
    CompletionSnapshot,
    GateError,
    evaluate_submission,
    merge_delivery_open_questions,
)
from src.modules.db_deepagent.services.task_ledger import (
    SUB_TASK_ID_PATTERN,
    current_task_view,
)
from src.modules.db_deepagent.services.reuse_qualification import (
    TASK_LEDGER_PROTOCOL_VERSION,
    SourceUnqualified,
)
from src.utils.logger import get_module_logger

logger = get_module_logger("nl2sql.db_deepagent.runner")

_BUDGET_STOP_REASONS = (
    "model_call_limit",
    "tool_call_limit",
    "execute_limit",
    "run_timeout",
)


def _mark_primary(failures: _RunFailures, status: str) -> list[RunFailureRecord]:
    """primary 标在与最终状态对应的那一类中最早的一条（§14.3）。

    failed 可由 runtime／tool_set_mismatch／no_final_answer 任一触发，按该
    优先顺序取最早匹配，使 primary 总有可标记的记录。Web 前置拒绝的结构化
    类型（session_busy／lock_backend_error 等，doc 2 §4.2）优先于普通
    runtime，保证主错误可被 Web 层机器读取。
    """
    kinds_by_status: dict[str, tuple[str, ...]] = {
        "persistence_error": ("persistence",),
        "failed": (
            "session_busy",
            "lock_backend_error",
            "knowledge_changed",
            "dependency_unavailable",
            "result_encoding_error",
            "runtime",
            "tool_set_mismatch",
            "task_completion_rejected",
            "no_final_answer",
        ),
        "budget_exhausted": ("budget_exhausted",),
    }
    targets = kinds_by_status.get(status)
    records = list(failures.records)
    if targets:
        for kind in targets:
            for record in records:
                if record.kind == kind:
                    record.primary = True
                    return records
    return records


@dataclass
class _RunFailures:
    """按发生顺序记录失败；不覆盖、不去重"""

    records: list[RunFailureRecord] = field(default_factory=list)

    def add(self, kind: str, phase: str, message: str) -> None:
        self.records.append(RunFailureRecord(phase=phase, kind=kind, message=message))  # type: ignore[arg-type]

    def has(self, kind: str) -> bool:
        return any(r.kind == kind for r in self.records)


class DbAgentRunner:
    """单会话 Runner：持有 agent 与归档，run() 每轮一 run_id（§14.3）"""

    def __init__(
        self,
        *,
        services,
        handle: DbAgentHandle,
        archive: QaArchive,
        persistence=None,
        fixed_now: datetime | None = None,
        finalize_hook: Optional[Callable[..., Optional["FinalizeHookOutcome"]]] = None,
    ):
        self._services = services
        self._handle = handle
        self._archive = archive
        self._persistence = persistence
        self._cfg: DbAgentConfig = services.config
        self._fixed_now = fixed_now or _parse_fixed_now(services.config)
        self._fingerprint = compute_validation_fingerprint(services.config)
        self._lock_conninfo = (
            _platform_conninfo(services.config) if persistence is not None else None
        )
        # 锁内终态收尾钩子（doc 2 §4.4）：P3 归档尝试后、释放会话锁前执行一次，
        # 由 Web 装配传入（Web 文本日志 + pending 收尾 + 终态复核）；默认无。
        self._finalize_hook = finalize_hook

    # ------------------------------------------------------------ 入口

    def run(
        self,
        question: str,
        session_id: str,
        thread_id: str | None = None,
        request_id: str | None = None,
        force_rotate: bool = False,
        source_id: str | None = None,
        session_lock=None,
    ) -> DbAgentResult:
        # ID 校验先于一切副作用（doc 2 §4.2）：目录、锁、本地记录、Store、
        # checkpoint 均不得用非法 ID 创建；非法值直接拒绝。Runner 未传
        # request_id 时先生成（req_ + 完整 UUID4 hex），再校验。
        from src.modules.db_deepagent.persistence.checkpoint import valid_web_id

        if not valid_web_id(session_id):
            raise ValueError(
                f"session_id 非法（允许 [A-Za-z0-9_-]，长度 1–64，禁冒号）: "
                f"{session_id!r}"
            )
        request_id = request_id or ("req_" + uuid.uuid4().hex)
        if not valid_web_id(request_id):
            raise ValueError(
                f"request_id 非法（允许 [A-Za-z0-9_-]，长度 1–64）: {request_id!r}"
            )
        # 会话级互斥（doc 7 §2.3）：P0 读取前取锁，P3 完成后外层 finally 释放；
        # memory 持久化模式使用进程内会话 guard（doc 6.3 §6.5）。取锁失败：
        # 不写任何元数据（防覆盖运行中进程指针），仅返回失败结果与 P4 本地记录。
        # session_lock（Web 执行链锁交接，doc 2 §4.4）：外层已持有同一会话
        # 锁并负责释放；沿用可消除“释放-重取”间隙内他请求把在途 pending
        # 误判为遗留而失效会话。非本层获取的锁不在本层释放。
        lock = None
        guard_lease = None
        memory_lease = None
        if session_lock is not None:
            lock = session_lock
        elif self._lock_conninfo is not None:
            from src.modules.db_deepagent.persistence.checkpoint import (
                LockBackendError,
                SessionAdvisoryLock,
            )

            lock = SessionAdvisoryLock(self._lock_conninfo, session_id)
            acquired = False
            lock_backend_error: LockBackendError | None = None
            try:
                acquired = lock.try_acquire()
            except LockBackendError as exc:
                # 锁后端异常 ≠ 忙碌（doc 2 §4.2）：映射依赖不可用，不误报占用
                lock_backend_error = exc
                acquired = False
            if lock_backend_error is not None or not acquired:
                run_id = "run_" + uuid.uuid4().hex
                failures = _RunFailures()
                failures.add(
                    (
                        "lock_backend_error"
                        if lock_backend_error is not None
                        else "session_busy"
                    ),
                    "P0",
                    (
                        str(lock_backend_error)
                        if lock_backend_error is not None
                        else "会话正在另一进程运行或清理中（advisory lock）"
                    ),
                )
                return self._finalize(
                    time.monotonic(),
                    request_id,
                    run_id,
                    session_id,
                    None,
                    failures,
                    BudgetSnapshot(),
                    ModelObservationSnapshot.not_started(request_id, run_id),
                    values=None,
                    evidence=[],
                    warnings=[],
                    archived=False,
                    rotated=False,
                    question=question,
                )
        else:
            # memory 模式（doc 6.3 §6.5）：进程内会话 guard，Runner 与清理共用
            from src.modules.db_deepagent.persistence.session_guard import (
                acquire_memory_lease,
            )

            memory_lease = acquire_memory_lease(session_id)
            if memory_lease is None:
                run_id = "run_" + uuid.uuid4().hex
                failures = _RunFailures()
                failures.add(
                    "session_busy",
                    "P0",
                    "会话正在本进程另一运行/清理中（memory guard）",
                )
                return self._finalize(
                    time.monotonic(),
                    request_id,
                    run_id,
                    session_id,
                    None,
                    failures,
                    BudgetSnapshot(),
                    ModelObservationSnapshot.not_started(request_id, run_id),
                    values=None,
                    evidence=[],
                    warnings=[],
                    archived=False,
                    rotated=False,
                    question=question,
                )
        # doc 6.3 §6.5：由已持有的锁构造运行凭据（工具消费同一凭据，不重复取锁）
        if lock is not None:
            from src.modules.db_deepagent.persistence.session_guard import (
                lease_from_advisory_lock,
            )

            guard_lease = lease_from_advisory_lock(lock, session_id)
        else:
            guard_lease = memory_lease
        try:
            return self._run_locked(
                question,
                session_id,
                thread_id,
                request_id,
                lock,
                force_rotate,
                source_id,
                guard_lease=guard_lease,
            )
        finally:
            if lock is not None and session_lock is None:
                lock.release()
            if memory_lease is not None:
                from src.modules.db_deepagent.persistence.session_guard import (
                    release_memory_lease,
                )

                release_memory_lease(memory_lease)

    def _run_locked(
        self,
        question: str,
        session_id: str,
        thread_id: str | None,
        request_id: str | None,
        lock,
        force_rotate: bool = False,
        source_id: str | None = None,
        guard_lease: object | None = None,
    ) -> DbAgentResult:
        started = time.monotonic()
        request_id = request_id or "req_" + uuid.uuid4().hex[:12]
        run_id = "run_" + uuid.uuid4().hex
        failures = _RunFailures()
        budget_snapshot = BudgetSnapshot()
        model_snapshot = ModelObservationSnapshot.not_started(request_id, run_id)
        thread_id_resolved: str | None = None
        values: dict[str, Any] | None = None
        user_texts: tuple[str, ...] = (question,)
        carried: tuple[Any, ...] = ()
        handoff_message: HumanMessage | None = None
        observer: ModelCallObserver | None = None
        budget_opened = False
        invoke_started = False
        rotated = False
        max_seq = 1
        source_binding: str | None = None

        # ---------------------------------------------------------------- P0
        try:
            if not _valid_session(session_id):
                raise _RejectedSession(
                    "session_id 含非法字符（允许 [A-Za-z0-9_-]，禁冒号）"
                )
            # 锁内检查已有本地诊断（doc 2 §4.2）：同会话复用已有 request_id
            # 直接拒绝，不覆盖诊断/归档/文本日志，不当作幂等重放
            if self._local_record_exists(session_id, request_id):
                raise RequestIdConflictError(
                    f"request_id 已存在本地运行记录: {request_id}"
                )
            session_rec = self._archive.get_session(session_id)
            source_binding = _resolve_source_binding(
                session_rec, source_id, self._services.source_id
            )
            current_thread, thread_seq, carried, old_values, max_seq = (
                self._resolve_thread(session_id, thread_id, session_rec)
            )
            if session_rec is None and old_values and old_values.get("messages"):
                # 孤立 checkpoint 拒绝（doc 2 §4.4）：sessions 记录不存在而将
                # 要恢复的 checkpoint 已有状态时，不推断来源、不复用旧上下文
                # （复用 _resolve_thread 已读取的 values，不额外探测）
                raise _RejectedSession(
                    "会话记录缺失但存在遗留 checkpoint，不能续聊；请新建会话"
                )
            thread_id_resolved = current_thread
            if old_values is not None:
                texts, messages = _thread_user_texts(old_values)
                # 强制轮换（/rotate）：有线程内容才轮换，空线程轮换无意义
                should_rotate = bool(messages) and (
                    force_rotate
                    or len(messages) >= self._cfg.thread.rotate_after_messages
                )
            else:
                texts, should_rotate = [], False
            if should_rotate:
                # 锁内分配（doc 7 §2.1）：会话已分配最大序号 +1，目标线程须不存在
                new_seq = max(max_seq, thread_seq) + 1
                new_thread = _thread_id_of(session_id, new_seq)
                while self._thread_has_state(new_thread):
                    new_seq += 1
                    new_thread = _thread_id_of(session_id, new_seq)
                carried, handoff_message = self._handoff(
                    old_values, carried, new_thread, session_id
                )
                thread_id_resolved = new_thread
                thread_seq = new_seq
                max_seq = max(max_seq, new_seq)
                user_texts = (question,)  # 新线程原文重置；旧口径走携带证据
                rotated = True
            else:
                user_texts = (*texts, question)
        except _RejectedSession as exc:
            failures.add("runtime", "P0", str(exc))
            return self._finalize(
                started,
                request_id,
                run_id,
                session_id,
                None,
                failures,
                budget_snapshot,
                model_snapshot,
                values=None,
                evidence=[],
                warnings=[],
                archived=False,
                rotated=False,
                question=question,
            )
        except Exception as exc:
            kind = "persistence" if is_persistence_error(exc) else "runtime"
            failures.add(kind, "P0", f"{type(exc).__name__}: {exc}")
            return self._finalize(
                started,
                request_id,
                run_id,
                session_id,
                None,
                failures,
                budget_snapshot,
                model_snapshot,
                values=None,
                evidence=[],
                warnings=[],
                archived=False,
                rotated=rotated,
                question=question,
            )

        ctx = DbAgentContext(
            session_id=session_id,
            thread_id=thread_id_resolved,
            request_id=request_id,
            run_id=run_id,
            datasource_id=self._services.business_db.datasource_id or "",
            validation_fingerprint=self._fingerprint,
            now=self._fixed_now or datetime.now().astimezone(),
            timezone=self._cfg.clock.timezone,
            user_texts=user_texts,
            carried_evidence=carried,
            deadline_monotonic=time.monotonic() + self._cfg.budget.run_timeout_seconds,
            session_guard_lease=guard_lease,
        )

        # ---------------------------------------------------------------- P1
        try:
            input_messages = (
                [handoff_message, HumanMessage(content=question)]
                if handoff_message is not None
                else [HumanMessage(content=question)]
            )
            audit_path = (
                Path(self._cfg.workspace.root) / "sessions" / session_id / "audit.jsonl"
            )
            observer = ModelCallObserver(request_id, run_id, audit_path)
            config = {
                "configurable": {"thread_id": thread_id_resolved},
                "recursion_limit": 200,
                "callbacks": [observer],
                "metadata": {
                    "request_id": request_id,
                    "dbda_run_id": run_id,
                    "dbda_call_kind": "agent",
                },
            }
            self._services.budget.open_run(
                run_id,
                limits_from_config(self._cfg.budget),
                deadline_monotonic=ctx.deadline_monotonic,
            )
            budget_opened = True
            invoke_started = True
            self._handle.agent.invoke(
                {"messages": input_messages}, config=config, context=ctx
            )
        except Exception as exc:
            kind = "persistence" if is_persistence_error(exc) else "runtime"
            failures.add(kind, "P1", f"{type(exc).__name__}: {exc}")
        finally:
            if budget_opened:
                try:
                    budget_snapshot = self._services.budget.close_run(run_id)
                except Exception as exc:  # noqa: BLE001（收尾独立捕获，不覆盖原错误）
                    failures.add(
                        "runtime", "P1.close_run", f"{type(exc).__name__}: {exc}"
                    )
            if observer is not None:
                try:
                    model_snapshot = observer.finalize()
                except Exception as exc:  # noqa: BLE001
                    failures.add(
                        "runtime",
                        "P1.observer_finalize",
                        f"{type(exc).__name__}: {exc}",
                    )

        # ---------------------------------------------------------------- P2
        if invoke_started and not failures.has("persistence"):
            try:
                values = self._handle.agent.get_state(
                    {"configurable": {"thread_id": thread_id_resolved}}
                ).values
            except Exception as exc:
                kind = "persistence" if is_persistence_error(exc) else "runtime"
                failures.add(kind, "P2", f"{type(exc).__name__}: {exc}")
                values = None

        # 评估：invoke 未开始、P1 runtime 异常或 P2 失败时不读取最终答案（§14.3）；
        # P2 诊断账本（values）仍然保留——归档条件 a 与执行轨迹依赖它，
        # “SQL 成功后模型失败”的轮次按 §13.3 应进 qa 并保留真实状态
        evaluate = invoke_started and not failures.has("runtime") and values is not None
        result = self._finalize(
            started,
            request_id,
            run_id,
            session_id,
            thread_id_resolved,
            failures,
            budget_snapshot,
            model_snapshot,
            values,
            evidence=[],
            warnings=[],
            archived=False,
            rotated=rotated,
            ctx=ctx,
            carried=carried,
            question=question,
            read_final_answer=evaluate,
            source_binding=source_binding,
            invoke_started=invoke_started,
        )
        # 锁内终态收尾钩子（doc 2 §4.4）：仍在会话 advisory lock 内执行；
        # 失败不抹去运行结果（history_saved 由 Web 侧按回读判定）
        if self._finalize_hook is not None:
            try:
                outcome = self._finalize_hook(
                    question=question,
                    request_id=request_id,
                    run_id=run_id,
                    session_id=session_id,
                    result=result,
                )
            except Exception as exc:  # noqa: BLE001（收尾失败不覆盖运行结果）
                logger.warning(
                    "终态收尾钩子失败 session=%s request=%s: %s: %s",
                    session_id,
                    request_id,
                    type(exc).__name__,
                    exc,
                )
            else:
                replacement = (outcome or {}).get("result")
                if replacement is not None:
                    result = replacement
        return result

    # ------------------------------------------------------------ P0 辅助

    def _resolve_thread(
        self, session_id: str, thread_id: str | None, session_rec: dict | None
    ) -> tuple[str, int, tuple[Any, ...], dict[str, Any] | None, int]:
        """确定线程与序号：显式传入 > sessions 记录；无则新建序号 1。

        归属校验（doc 7 §2.1）：显式 thread_id 与 sessions.current_thread_id
        都必须经 parse_thread_id 解析并归属本会话，跨会话即拒绝。
        返回 (thread_id, seq, carried, old_values, max_seq)；max_seq 为会话
        已分配最大序号（轮换编号不复用既有线程）。
        """
        from src.modules.db_deepagent.persistence.checkpoint import parse_thread_id

        def _require_owned(candidate: str) -> int:
            parsed = parse_thread_id(candidate)
            if parsed is None or parsed[0] != session_id:
                raise _RejectedSession(
                    f"thread_id {candidate!r} 不属于本会话 {session_id!r}"
                )
            return parsed[1]

        if thread_id is not None:
            seq = _require_owned(thread_id)
            chosen, carried = thread_id, ()
        elif session_rec is not None:
            chosen = session_rec.get("current_thread_id") or _thread_id_of(
                session_id, 1
            )
            seq = _require_owned(chosen)
            carried = tuple(session_rec.get("carried_evidence") or [])
        else:
            chosen, seq, carried = _thread_id_of(session_id, 1), 1, ()
        max_seq = int((session_rec or {}).get("thread_seq", seq) or seq)
        old_values = self._handle.agent.get_state(
            {"configurable": {"thread_id": chosen}}
        ).values
        return chosen, seq, carried, old_values, max_seq

    def _thread_has_state(self, thread_id: str) -> bool:
        """线程是否已有 checkpoint 状态（轮换编号分配的存在性校验）。

        读取失败原样抛出，交 P0 分类处理：持续故障时若按“存在”处理，
        轮换编号会无限递增探测，且此时尚未进入运行预算、不受时限保护。
        只有明确读到状态（有消息）才递增编号继续探测。
        """
        values = self._handle.agent.get_state(
            {"configurable": {"thread_id": thread_id}}
        ).values
        return bool(values.get("messages"))

    def _handoff(
        self,
        old_values: dict[str, Any] | None,
        prev_carried: tuple[Any, ...],
        new_thread: str,
        session_id: str = "",
    ) -> tuple[tuple[Any, ...], HumanMessage]:
        """§13.4：结构化携带证据 + 交接消息（不回查更早线程）。"""

        def _load_meta(result_ref: str | None, _run_id: str | None):
            if not result_ref or self._services.results is None or not session_id:
                return None
            try:
                return self._services.results.load_meta(
                    result_ref, session_id=session_id
                )
            except Exception:  # noqa: BLE001（来源补齐失败不阻塞轮换）
                return None

        summary_text = read_summary_for_handoff(old_values or {})
        carried = _collect_carried(
            old_values or {}, prev_carried, self._cfg, meta_loader=_load_meta
        )
        parts = ["【会话交接】"]
        if summary_text:
            parts.append(f"上一线程摘要：\n{summary_text}")
        listing = _carried_listing(carried)
        if listing:
            parts.append("携带证据清单（需要沿用时调用 reuse_evidence）：\n" + listing)
        parts.append("请基于以上口径继续回答本轮问题。")
        message = HumanMessage(
            content="\n\n".join(parts),
            additional_kwargs={"dbda_kind": "handoff"},
        )
        return carried, message

    # ------------------------------------------------------------ 收尾

    def _finalize(
        self,
        started: float,
        request_id: str,
        run_id: str,
        session_id: str,
        thread_id: str | None,
        failures: _RunFailures,
        budget_snapshot: BudgetSnapshot,
        model_snapshot: ModelObservationSnapshot,
        values: dict[str, Any] | None,
        evidence: list[EvidenceItem],
        warnings: list[str],
        archived: bool,
        rotated: bool,
        ctx: DbAgentContext | None = None,
        carried: tuple[Any, ...] | None = None,
        question: str = "",
        read_final_answer: bool = True,
        source_binding: str | None = None,
        invoke_started: bool = False,
    ) -> DbAgentResult:
        # 行数映射一次计算，证据投影与兜底模板复用同一份（doc 6.2 §7.2.2；
        # doc 6.3 §7.4：与任务门控共用 result_facts 同一映射）。
        known_counts = self._known_saved_row_counts(values, run_id)
        evidence = evidence or (
            self._assemble_evidence(values, run_id, known_row_counts=known_counts)
            if values
            else []
        )
        # 最终答案仅在评估允许时读取（P1 runtime 异常／P2 失败时不读）；
        # 账本（evidence／归档判定）不受此开关影响
        answer_record = (
            (values.get("db_final_answers") or {}).get(run_id)
            if (values is not None and read_final_answer)
            else None
        )
        # 归一提交视图一次生成，供评估、交付与归档复用（doc 6.2 §7.2.1）：
        # 严格核对外层 run_id/request_id 与本轮一致，结构非法或正文空白均无效。
        normalized_submission = normalize_final_answer_record(
            answer_record, run_id=run_id, request_id=request_id
        )
        # doc 6.3 §7.1/§7.5：一次结构归一 + 一次语义判定；eligible_submission
        # 贯穿评估、正文构建与归档条件 b。
        # R4-01：最终来源装配/门控 I/O 的受控异常边界。来源快照读取位于
        # P3 归档异常处理之前，若后端故障穿透将不返回 DbAgentResult、破坏
        # 6.2 兜底。此处按既有异常分类收敛，不关闭 services.store、不跳过
        # 必要的来源核验。
        try:
            decision = self._task_completion_decision(
                values,
                run_id,
                normalized_submission,
                known_counts,
                session_id=session_id,
                thread_id=thread_id,
                carried=carried,
            )
        except SourceUnqualified as exc:
            # 确定性协议损坏/坏来源：受控业务门控拒绝（R4-01 要求 5），
            # 不记为 persistence/runtime，也不让异常穿透。
            decision = CompletionDecision(
                accepted=False,
                expected_status=None,
                errors=[
                    GateError(
                        "stale_task_inputs",
                        f"最终来源资格核验失败：{exc.reason}",
                    )
                ],
            )
        except Exception as exc:  # noqa: BLE001
            # 已识别持久化故障记 persistence（§8.1 最高优先级）；未知运行
            # 错误按既有 runtime 规则收敛。二者都令提交无交付资格、继续
            # 走 _evaluate 的系统兜底，保留已取得的 evidence。绝不吞成
            # 普通 task_ledger_corrupt / task_completion_rejected。
            kind = "persistence" if is_persistence_error(exc) else "runtime"
            failures.add(
                kind,
                "P3",
                f"最终来源装配/门控 I/O 失败：{type(exc).__name__}: {exc}",
            )
            decision = CompletionDecision(
                accepted=False,
                expected_status=None,
                errors=[
                    GateError(
                        "task_completion_rejected",
                        "来源装配不可用，提交无交付资格",
                    )
                ],
            )
        eligible_submission = (
            normalized_submission
            if normalized_submission is not None and decision.accepted
            else None
        )
        status, findings, answer, delivery_questions, warnings = self._evaluate(
            failures,
            budget_snapshot,
            values,
            run_id,
            decision,
            eligible_submission,
            evidence,
            warnings,
            has_submission=normalized_submission is not None,
        )
        submitted_payload = (
            normalized_submission if normalized_submission is not None else {}
        )
        # 无资格提交的正文/findings/模型自报口径/补充问题均不进入交付
        # （doc 6.3 §7.5）；合法提交为 model，前置拒绝为 none。
        if eligible_submission is None:
            findings = []
        answer_source = "model" if answer.strip() else "none"
        # doc 6.3 §7.5 兜底谓词：Agent 已调用且没有可合法交付的正文（缺失、
        # 空白、结构/归属无效或 decision.accepted=False 均在内）。
        should_fallback = invoke_started and (
            eligible_submission is None or not answer.strip()
        )
        if not should_fallback and eligible_submission is not None:
            # R1-13：合法交付（model 来源）附加确定性分项段落；原始提交
            # payload 按原值归档，交付正文实时/归档/历史/CLI 一致。
            # doc 17 §4：用户正文用精简模式（仅失败/澄清），完整分项诊断
            # 写 DEBUG 日志，不拼入交付正文。
            from src.modules.db_deepagent.services.task_completion import (
                build_delivery_answer_section,
            )

            section = build_delivery_answer_section(
                decision, include_audit_details=False
            )
            if section:
                answer = f"{answer}\n\n{section}"
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug(
                    "[request_id=%s run_id=%s] 完整分项诊断（不进交付正文）:\n%s",
                    request_id,
                    run_id,
                    build_delivery_answer_section(decision),
                )
        if should_fallback:
            answer = self._build_fallback_answer(status, evidence, known_counts)
            task_section = self._fallback_task_section(decision)
            if task_section:
                answer = answer + "\n" + task_section
            answer_source = "system_fallback"
            warnings.append("最终说明由服务端兜底生成，非模型最终提交。")
        errors = _mark_primary(failures, status)
        if not model_snapshot.token_usage_complete and model_snapshot.total_model_calls:
            warnings.append("模型用量统计不完整")
        # 对外交付 payload：不是 FinalAnswerRecord，也不是 DbAgentFinalAnswer；
        # 其 status 为当前（P3 前）Runner 评估状态。
        # doc 6.3 §8.3：open_questions 为 §7.2 合并列表（权威在前、补充按原
        # 提交序、strip 后精确去重），不是原提交列表的拷贝。
        delivery_payload = {
            "status": status,
            "answer": answer,
            "findings": [item.model_dump() for item in findings],
            "metric_definitions": list(
                (eligible_submission or {}).get("metric_definitions") or []
            ),
            "limitations": list((eligible_submission or {}).get("limitations") or []),
            "open_questions": delivery_questions,
        }
        stats = RunStats(
            elapsed_ms=int((time.monotonic() - started) * 1000),
            model_calls=(budget_snapshot.counters or {}).get("model_call", 0),
            summary_model_calls=model_snapshot.summary_model_calls,
            total_model_calls=model_snapshot.total_model_calls,
            tool_calls=(budget_snapshot.counters or {}).get("tool_call", 0),
            input_tokens=model_snapshot.input_tokens,
            output_tokens=model_snapshot.output_tokens,
            token_usage_complete=model_snapshot.token_usage_complete,
        )
        # P3 归档（状态非 persistence_error 时；§13.3 条件 a/b）
        if status != "persistence_error":
            try:
                archived = self._archive_run(
                    session_id,
                    request_id,
                    run_id,
                    thread_id,
                    status,
                    answer_record,
                    evidence,
                    stats,
                    failures,
                    carried=carried,
                    question=question,
                    values=values,
                    source_binding=source_binding,
                    normalized_submission=normalized_submission,
                    delivery_payload=delivery_payload,
                    answer_source=answer_source,
                    eligible_submission=eligible_submission,
                    decision=decision,
                )
            except Exception as exc:
                if is_persistence_error(exc):
                    failures.add("persistence", "P3", f"{type(exc).__name__}: {exc}")
                    status = "persistence_error"
                    errors = _mark_primary(failures, status)
                    archived = False
                    warnings.append("本次结果未归档")
                else:
                    failures.add("runtime", "P3", f"{type(exc).__name__}: {exc}")
                    if status in (
                        "completed",
                        "partial",
                        "needs_clarification",
                        "no_data",
                    ):
                        status = "failed"
                        errors = _mark_primary(failures, status)
        result = DbAgentResult(
            session_id=session_id,
            thread_id=thread_id,
            request_id=request_id,
            run_id=run_id,
            status=status,  # type: ignore[arg-type]
            answer=answer,
            findings=findings,
            evidence=evidence,
            metric_definitions=delivery_payload["metric_definitions"],
            limitations=delivery_payload["limitations"],
            open_questions=delivery_payload["open_questions"],
            errors=errors,
            warnings=warnings,
            archived=archived,
            stats=stats,
        )
        self._write_local_record(session_id, request_id, result, failures, rotated)
        return result

    def _task_completion_decision(
        self,
        values: dict[str, Any] | None,
        run_id: str,
        normalized_submission: dict[str, Any] | None,
        known_counts: dict[str, int | None],
        *,
        session_id: str = "",
        thread_id: str | None = None,
        carried: tuple[Any, ...] | None = None,
    ) -> CompletionDecision:
        """装配只读快照并执行共享业务规则（doc 6.3 §7.1）。

        无账本可核验（values 缺失）时仍执行任务视图规则——空任务集合将
        被拒绝；纯规则不读文件，来源缺失作为拒绝依据传入。
        """
        try:
            tasks = current_task_view((values or {}).get("db_sub_tasks") or {}, run_id)
        except Exception as exc:  # noqa: BLE001（账本损坏 → 门控拒绝）
            decision = CompletionDecision(
                accepted=False,
                expected_status=None,
                errors=[],
                plan_version=0,
            )
            from src.modules.db_deepagent.services.task_completion import GateError

            decision.errors.append(
                GateError("task_ledger_corrupt", f"任务账本不可核验: {exc}")
            )
            return decision
        try:
            view = LedgerView(values or {})
            plan_version = view.task_plan_version(run_id)
            executions = view.executions_of_run(run_id)
            analyses = view.analyses_of_run(run_id)
        except TaskAccessError as exc:
            decision = CompletionDecision(
                accepted=False,
                expected_status=None,
                errors=[],
                plan_version=0,
            )
            from src.modules.db_deepagent.services.task_completion import GateError

            decision.errors.append(GateError(exc.error_type, exc.message))
            return decision
        # R1-02：沿用登记证据的来源资格核验（与提交工具共享同一规则）
        from src.modules.db_deepagent.services.reuse_qualification import (
            unqualified_reuse_evidence,
        )

        reuse_archive = self._archive if self._services_is_store_wired() else None
        # R2-01：会话/线程身份由 _finalize 显式传入，不从 state 推断。
        # 无显式传入（合成/旧测试）时仅从任务记录回退。
        if not session_id:
            session_id = next(
                (r.get("session_id") for r in tasks.values() if r.get("session_id")),
                "",
            )
        resolved_thread = thread_id or next(
            (r.get("thread_id") for r in tasks.values() if r.get("thread_id")),
            "",
        )
        reuse_failures = (
            unqualified_reuse_evidence(
                [*executions, *analyses],
                session_id=session_id,
                thread_id=resolved_thread or "",
                run_id=run_id,
                state=values or {},
                archive=reuse_archive,
                carried=carried or (),
            )
            if tasks
            else {}
        )
        from src.modules.db_deepagent.services.task_completion import (
            all_task_versions,
        )

        snapshot = CompletionSnapshot(
            run_id=run_id,
            tasks=tasks,
            all_task_records=all_task_versions(values or {}),
            executions=executions,
            analyses=analyses,
            errors=list((values or {}).get("db_tool_errors") or []),
            known_counts=known_counts,
            proven_untruncated=aggregate_proven_untruncated(values, run_id),
            reuse_failures=reuse_failures,
            plan_version=plan_version,
        )
        return evaluate_submission(normalized_submission, snapshot)

    def _services_is_store_wired(self) -> bool:
        """store 已注入（装配层）时启用快照读取的来源核验。"""
        return getattr(self._services, "store", None) is not None

    def _fallback_task_section(self, decision: CompletionDecision | None) -> str:
        """兜底正文的确定性任务摘要（doc 6.3 §7.5；R1-14 分级）。

        completed 声明只在门控校验走到状态映射（expected_status 已计算，
        即成果/覆盖核验通过）后才作为"已完成"展示；此前（结构错误/门控
        拒绝）只作"已登记完成（未核验）"。pending/in_progress 一律披露为
        未完成事项。
        """
        if decision is None:
            return ""
        # R2-08：逐项核验结论，不能以 expected_status 存在与否代替——
        # 无提交/预算停止/模型异常时，未核验的 completed 声明一律标注
        # "未核验"，缺证据/坏来源/部分可信成果都不确认为成功。
        lines: list[str] = []
        for summary in decision.outcome_summaries:
            if summary.status == "completed":
                if summary.sub_task_id in decision.verified_completed_refs:
                    lines.append(f"- 已完成：{summary.content}")
                else:
                    lines.append(f"- 已登记完成（未核验）：{summary.content}")
            elif summary.status in ("pending", "in_progress"):
                lines.append(f"- 未完成：{summary.content}（{summary.status}）")
            elif summary.status == "failed":
                reason = summary.reason or "未提供原因"
                lines.append(f"- 未完成：{summary.content}（原因：{reason}）")
            elif summary.status == "needs_clarification":
                question = summary.clarification_question or "需要澄清"
                lines.append(f"- 待澄清：{summary.content}（问题：{question}）")
            elif summary.status == "voided":
                reason = summary.reason or "计划调整"
                lines.append(f"- 已作废：{summary.content}（原因：{reason}）")
        for question in decision.authoritative_questions[:5]:
            lines.append(f"- 需要澄清：{question}")
        return "\n".join(lines) if lines else ""

    def _evaluate(
        self,
        failures: _RunFailures,
        budget_snapshot: BudgetSnapshot,
        values: dict[str, Any] | None,
        run_id: str,
        decision: CompletionDecision,
        eligible_submission: dict[str, Any] | None,
        evidence: list[EvidenceItem],
        warnings: list[str],
        *,
        has_submission: bool = False,
    ) -> tuple[str, list[FindingItem], str, list[str], list[str]]:
        """状态判定（§8.1 优先级）与业务状态合流（doc 6.3 §7.5）。

        持久化 → 运行/工具集合 → 预算停止优先于有效提交的业务状态；结构
        合法但 decision.accepted=False 的提交记 task_completion_rejected →
        failed；未提交/结构无效仍是 no_final_answer（6.2 语义保留）；不先
        丢弃一部分 ID 再把剩余答案当作通过。
        """
        payload = eligible_submission or {}
        findings = [FindingItem(**f) for f in payload.get("findings") or []]
        stop_reason = budget_snapshot.stop_reason
        if failures.has("persistence"):
            status = "persistence_error"
        elif failures.has("runtime") or stop_reason == "tool_set_mismatch":
            if stop_reason == "tool_set_mismatch" and not failures.has("runtime"):
                failures.add(
                    "tool_set_mismatch", "P1", "运行期工具集合与期望不一致，运行终止"
                )
            status = "failed"
        elif stop_reason in _BUDGET_STOP_REASONS:
            failures.add("budget_exhausted", "P1", f"预算或时限停止：{stop_reason}")
            status = "budget_exhausted"
        elif eligible_submission is not None and decision.accepted:
            status = decision.expected_status or payload.get("status", "failed")
        elif has_submission:
            # 结构合法但被语义门控拒绝（doc 6.3 §7.5）
            primary = decision.primary_error
            failures.add(
                "task_completion_rejected",
                "P2",
                (
                    f"最终提交被任务门控拒绝（{primary.code}）：{primary.message}"
                    if primary is not None
                    else "最终提交被任务门控拒绝"
                ),
            )
            status = "failed"
        else:
            failures.add(
                "no_final_answer",
                "P2",
                "未提交有效最终答案（缺失、归属不符、结构非法或正文空白）",
            )
            status = "failed"
        # 交付问题列表（§7.2 固定算法）：权威问题 + 原提交补充（有资格时）
        delivery_questions = merge_delivery_open_questions(
            decision.authoritative_questions,
            list(payload.get("open_questions") or []),
        )
        return status, findings, payload.get("answer", ""), delivery_questions, warnings

    @staticmethod
    def _known_saved_row_counts(
        values: dict[str, Any] | None,
        run_id: str,
    ) -> dict[str, int | None]:
        """本轮原始账本的可靠行数映射（doc 6.2 §7.2.2；doc 6.3 §7.4 复用共享实现）。"""
        return aggregate_known_saved_row_counts(values, run_id)

    @staticmethod
    def _build_fallback_answer(
        status: str,
        evidence: list[EvidenceItem],
        known_row_counts: dict[str, int | None],
    ) -> str:
        """确定性兜底正文（doc 6.2 §7.2.3）：面向业务用户的简版说明。

        只陈述账本可核验事实：不读取结果文件、不展示内部证据编号或
        “保存行数未知”（未知行数整句省略，不把未知写成零）、不合计多查
        询行数、不替模型解释指标。最多逐项五条，完整 evidence 不截短。
        """
        header = {
            "failed": "本轮未正常完成，未能生成有效的最终答案。",
            "budget_exhausted": "本轮已达到运行次数或时间限制，完整答案尚未生成。",
            "persistence_error": "本轮出现保存异常，最终交付或保存未完成。",
        }.get(status, "本轮未能生成有效的最终答案。")
        lines = ["系统兜底说明：" + header]
        if not evidence:
            lines.append("未取得可确认的查询或分析结果。")
            return "\n".join(lines)

        for index, item in enumerate(evidence[:5], start=1):
            kind = "查询" if item.kind == "sql_query" else "分析"
            origin = "使用此前的" if item.reused_from is not None else "本轮已记录"
            lines.append(f"- 结果 {index}：{origin}{kind}结果。")
            count = known_row_counts.get(item.evidence_id)
            if count == 0:
                # 可靠零行按结果类型区分（doc 6.2 §7.2.3 M1 定稿）；
                # 不把单项零行推广为整个问题无数据。
                lines.append(
                    "  该查询没有返回记录。"
                    if item.kind == "sql_query"
                    else "  该分析没有输出记录。"
                )
            elif count is not None:
                lines.append(f"  已记录 {count} 行结果。")
            if item.reused_from is not None:
                lines.append("  这项结果来自历史记录，不代表本轮重新查询的最新数据。")
            if item.source_truncated:
                lines.append("  结果仅包含部分数据，已记录行数不代表全部数据量。")
        if len(evidence) > 5:
            lines.append(f"另有 {len(evidence) - 5} 份结果记录，未在此逐项列出。")
        lines.append("以上仅为执行情况说明，完整答案尚未生成。")
        return "\n".join(lines)

    def _assemble_evidence(
        self,
        values: dict[str, Any],
        run_id: str,
        *,
        known_row_counts: dict[str, int | None],
    ) -> list[EvidenceItem]:
        """run_id==本次 的 query 执行与分析记录（含沿用登记）→ EvidenceItem。

        行数取 `_finalize()` 计算的可信映射的兼容投影：未知（None）与非法
        值均投影为外部既有整数零，避免 None 使 EvidenceItem 校验失败；但该
        兼容值不得被当作真实零行使用（兜底模板只读映射本身）。
        """
        items: list[EvidenceItem] = []
        for record in values.get("db_executions") or []:
            if record.get("run_id") != run_id or record.get("purpose") != "query":
                continue
            sql_template = record.get("sql_template")
            executed = record.get("executed_sql")
            items.append(
                EvidenceItem(
                    evidence_id=record["evidence_id"],
                    kind="sql_query",
                    tool_call_id=record.get("tool_call_id", ""),
                    source=record.get("datasource_id", ""),
                    sql=executed,
                    sql_template=(sql_template if sql_template != executed else None),
                    result_ref=record.get("result_ref"),
                    row_count=known_row_counts.get(record["evidence_id"]) or 0,
                    preview_truncated=False,
                    source_truncated=record.get("source_truncated", False),
                    warnings=record.get("warnings") or [],
                    reused_from=record.get("reused_from"),
                    result_generated_at=record.get("result_generated_at", ""),
                )
            )
        for record in values.get("db_analyses") or []:
            if record.get("run_id") != run_id:
                continue
            items.append(
                EvidenceItem(
                    evidence_id=record["evidence_id"],
                    kind="result_analysis",
                    tool_call_id=record.get("tool_call_id", ""),
                    source=record.get("datasource_id", ""),
                    sql=record.get("executed_sql"),
                    result_ref=record.get("output_result_ref"),
                    # doc 6.3 §8.3：私有结构化血缘只留在原始记录/快照，
                    # 公开投影继续为 list[str]（从条目提取 ref）。
                    input_result_refs=[
                        entry
                        for entry in (
                            item.get("result_ref") if isinstance(item, dict) else item
                            for item in (record.get("input_result_refs") or [])
                        )
                        if isinstance(entry, str) and entry
                    ],
                    operation=dict(record.get("operation") or {}),
                    row_count=known_row_counts.get(record["evidence_id"]) or 0,
                    preview_truncated=False,
                    source_truncated=record.get("source_truncated", False),
                    warnings=record.get("warnings") or [],
                    reused_from=record.get("reused_from"),
                    result_generated_at=record.get("result_generated_at", ""),
                )
            )
        return items

    def _archive_run(
        self,
        session_id: str,
        request_id: str,
        run_id: str,
        thread_id: str | None,
        status: str,
        answer_record: dict[str, Any] | None,
        evidence: list[EvidenceItem],
        stats: RunStats,
        failures: _RunFailures,
        carried: tuple[Any, ...] | None = None,
        question: str = "",
        values: dict[str, Any] | None = None,
        source_binding: str | None = None,
        *,
        normalized_submission: dict[str, Any] | None,
        delivery_payload: dict[str, Any],
        answer_source: str,
        eligible_submission: dict[str, Any] | None = None,
        decision: CompletionDecision | None = None,
    ) -> bool:
        """§13.3 写入判定：条件 a（成功真实执行）或 b（资格提交＋有效 E/A 沿用引用）。

        doc 6.2 §7.4 + doc 6.3 §7.5：资格判断与保存内容分离——条件 b 复用
        收尾入口的 eligible_submission（结构合法且 decision.accepted），原始
        payload 只按原值保存（answer 键）；门控拒绝时条件 b 必为假，不能因
        结构合法或原 payload 自报 completed 就通过。doc 6.3 §8.2：写
        QA/run_errors 前先封存运行来源快照并保存 snapshot_ref。
        """
        # 条件 a 从本运行执行账本判断（§13.3：query 或 pushdown、非沿用）；
        # evidence 里的 sql_query 只含 purpose="query"，纯下推分析会被漏判
        has_real_execution = any(
            record.get("run_id") == run_id
            and record.get("purpose") in ("query", "pushdown")
            and record.get("reused_from") is None
            for record in ((values or {}).get("db_executions") or [])
        )
        raw_submitted_payload = extract_raw_final_answer_payload(answer_record)
        eligible_payload = (
            eligible_submission if eligible_submission is not None else {}
        )
        reused_ids = {e.evidence_id for e in evidence if e.reused_from is not None}
        cited_reused = any(
            eid in reused_ids
            for finding in eligible_payload.get("findings") or []
            for eid in finding.get("evidence_ids") or []
        )
        condition_b = (
            eligible_submission is not None
            and eligible_payload.get("status") in ("completed", "partial")
            and cited_reused
        )
        # 条件 c（doc 19 §6）：零任务直接回答（本 run 从未登记任何任务，
        # 含作废记录）的合法提交 → 正常 QA 归档，不写 run_errors。复用
        # decision.outcome_summaries（由全部任务记录生成）判定，与门控
        # 直接分支口径一致；仅作废时该列表非空，不满足本条件。
        condition_c = (
            decision is not None
            and decision.accepted
            and not decision.outcome_summaries
        )
        # doc 6.3 §8.2：封存运行来源快照（写 QA/run_errors 前；失败轮次
        # 同样封存以保留解释依据）。封存异常原样上抛，由 _finalize 的
        # P3 分类处理（持久化/运行时）。
        snapshot_ref = self._seal_run_snapshot(
            session_id,
            thread_id,
            run_id,
            request_id,
            status,
            values,
            decision,
        )
        snapshot_field = {
            "session_id": session_id,
            "thread_id": thread_id,
            "run_id": run_id,
        }
        if has_real_execution or condition_b or condition_c:
            self._archive.write_qa(
                session_id,
                request_id,
                {
                    "run_id": run_id,
                    "thread_id": thread_id,
                    "question": question,
                    "status": status,
                    "archive_schema_version": ARCHIVE_SCHEMA_VERSION,
                    "answer": raw_submitted_payload,
                    "delivery_answer": delivery_payload,
                    "answer_source": answer_source,
                    "evidence": [e.model_dump() for e in evidence],
                    "stats": stats.model_dump(),
                    "snapshot_ref": snapshot_field,
                },
            )
        else:
            self._archive.write_run_errors(
                session_id,
                request_id,
                {
                    "run_id": run_id,
                    "question": question,
                    "status": status,
                    "stop_reason": None,  # 本阶段保持原字段规则。
                    "errors": [r.model_dump() for r in failures.records],
                    "archive_schema_version": ARCHIVE_SCHEMA_VERSION,
                    "answer": raw_submitted_payload,
                    "delivery_answer": delivery_payload,
                    "answer_source": answer_source,
                    "snapshot_ref": snapshot_field,
                },
            )
        # sessions／threads 轨迹落库（doc 7 §2.1；与归档同批）
        self._write_thread_tracks(session_id, thread_id, carried, source_binding)
        return True

    def _seal_run_snapshot(
        self,
        session_id: str,
        thread_id: str | None,
        run_id: str,
        request_id: str,
        run_status: str,
        values: dict[str, Any] | None,
        decision: CompletionDecision | None,
    ) -> dict[str, Any]:
        """doc 6.3 §8.2：装配并封存完整运行快照（不可变、幂等、冲突报错）。

        保存：服务端 scope、plan_version、全部任务全部 revision、原始
        executions/analyses（含结构化 input_result_refs、direct_reuse_from）、
        任务关联 tool_errors、覆盖结果、门控结论与 interrupted_tasks。
        """
        tasks_raw = dict((values or {}).get("db_sub_tasks") or {})
        # R1-02：只封存本 run 的任务（线程中其它 run 的任务留在原线程账本；
        # 同线程两轮同名任务按完整 run 身份区分，互不串读）
        run_tasks = [
            dict(record)
            for record in tasks_raw.values()
            if isinstance(record, dict) and record.get("run_id") == run_id
        ]
        executions = [
            dict(r)
            for r in (values or {}).get("db_executions") or []
            if r.get("run_id") == run_id
        ]
        analyses = [
            dict(r)
            for r in (values or {}).get("db_analyses") or []
            if r.get("run_id") == run_id
        ]
        tool_errors = [
            dict(r)
            for r in (values or {}).get("db_tool_errors") or []
            if r.get("run_id") == run_id
        ]
        # R2-12：current_task_view 要求**任务记录 mapping**（键为账本键），
        # 不是 {"db_sub_tasks": ...} 包装；此前包装形状错误导致异常被吞、
        # interrupted_tasks 恒为空。只传本 run 过滤后的任务 mapping。
        try:
            tasks_view = current_task_view(
                {
                    k: v
                    for k, v in tasks_raw.items()
                    if isinstance(v, dict) and v.get("run_id") == run_id
                },
                run_id,
            )
        except Exception:  # noqa: BLE001（真实损坏账本照实封存，读取时再拒绝）
            tasks_view = {}
        interrupted = sorted(
            tid
            for tid, rec in tasks_view.items()
            if rec.get("status") in ("pending", "in_progress")
        )
        record = {
            "run_snapshot_schema_version": RUN_SNAPSHOT_SCHEMA_VERSION,
            "ledger_protocol_version": TASK_LEDGER_PROTOCOL_VERSION,
            "completion_rules_version": TASK_COMPLETION_RULES_VERSION,
            "session_id": session_id,
            "thread_id": thread_id,
            "request_id": request_id,
            "run_id": run_id,
            "run_status_at_seal": run_status,
            "plan_version": (
                (LedgerView(values or {}).task_plan_version(run_id))
                if values is not None
                else 0
            ),
            "tasks": run_tasks,
            "executions": executions,
            "analyses": analyses,
            "tool_errors": tool_errors,
            "coverage": (
                {
                    "accepted": decision.accepted,
                    "expected_status": decision.expected_status,
                    "coverage_map": decision.coverage_map,
                    "uncovered_task_refs": decision.uncovered_task_refs,
                    "pending_task_refs": decision.pending_task_refs,
                    "errors": [
                        {
                            "code": e.code,
                            "message": e.message,
                            "task_refs": e.task_refs,
                        }
                        for e in decision.errors
                    ],
                }
                if decision is not None
                else None
            ),
            "interrupted_tasks": interrupted,
            "sealed_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        }
        self._archive.write_run_snapshot(session_id, thread_id, run_id, record)
        return record

    def _write_thread_tracks(
        self,
        session_id: str,
        thread_id: str | None,
        carried: tuple[Any, ...] | None = None,
        source_binding: str | None = None,
    ) -> None:
        """P3：sessions upsert（当前线程/最大序号/携带证据/源绑定）＋threads 轨迹。

        carried 为当前线程的携带证据清单（轮换时由 §13.4 收集；普通运行
        沿用会话记录中的既有清单）；None 表示调用方未知，保留已存值不覆盖。
        source_binding 为本会话绑定的 source_id（doc 4 §5.2）：首轮写入，
        后续 upsert 整体覆盖时沿用同一绑定。
        """
        if thread_id is None:
            return
        now_iso = datetime.now().astimezone().isoformat(timespec="seconds")
        existing = self._archive.get_session(session_id) or {}
        if carried is None:
            carried = tuple(existing.get("carried_evidence") or [])
        if source_binding is None:
            # 调用方未知（旧路径）：保留已存绑定不覆盖
            source_binding = existing.get("source_id")
        self._archive.upsert_session(
            session_id,
            self._archive.session_record(
                session_id=session_id,
                thread_id=thread_id,
                thread_seq=max(
                    _seq_of(thread_id),
                    int(existing.get("thread_seq", 0) or 0),
                ),
                created_at=datetime.now().astimezone(),
                last_active_at=datetime.now().astimezone(),
                active=False,
                carried_evidence=list(carried),
                source_id=source_binding,
            ),
        )
        record = self._archive.get_thread(session_id, thread_id) or {
            "thread_id": thread_id,
            "session_id": session_id,
            "seq": _seq_of(thread_id),
        }
        self._archive.upsert_thread(
            session_id,
            {
                **record,
                "thread_id": thread_id,
                "session_id": session_id,
                "seq": _seq_of(thread_id),
                "last_active_at": now_iso,
            },
        )

    def _local_record_path(self, session_id: str, request_id: str) -> Path:
        return (
            Path(self._cfg.workspace.root)
            / "sessions"
            / session_id
            / "runs"
            / f"{request_id}.json"
        )

    def _local_record_exists(self, session_id: str, request_id: str) -> bool:
        """锁内已有同 request_id 的本地诊断（doc 2 §4.2 防覆盖检查）。"""
        try:
            return self._local_record_path(session_id, request_id).exists()
        except OSError:
            return False

    def _write_local_record(
        self,
        session_id: str,
        request_id: str,
        result: DbAgentResult,
        failures: _RunFailures,
        rotated: bool,
    ) -> None:
        """P4：<workspace>/sessions/<session_id>/runs/<request_id>.json；失败只记日志。

        排他创建（doc 2 §4.2）：已有文件不覆盖，报告冲突后跳过该次落盘。
        """
        try:
            path = self._local_record_path(session_id, request_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = result.model_dump()
            payload["failures"] = [r.model_dump() for r in failures.records]
            payload["rotated"] = rotated
            with open(path, "x", encoding="utf-8") as fh:
                fh.write(json.dumps(payload, ensure_ascii=False, indent=1))
        except FileExistsError:
            logger.warning(
                "本地运行记录已存在，跳过覆盖: session=%s request=%s",
                session_id,
                request_id,
            )
        except OSError as exc:
            logger.warning("本地运行记录写入失败: %s", exc)


# ----------------------------------------------------------------------------
# 携带证据收集（§13.4）
# ----------------------------------------------------------------------------


def _collect_carried(
    old_values: dict[str, Any],
    prev_carried: tuple[Any, ...],
    cfg: DbAgentConfig,
    meta_loader: Any = None,
) -> tuple[Any, ...]:
    """使用中的时间／维度证据（授权记录 guard_evidence 口径）＋既有携带
    ＋最近执行／分析记录（链首去重）。

    去重键按 §13.4：时间证据 (start, end, grain, timezone)；维度证据
    (表, 列, 取值集合)——同一列不同取值口径不互相挤掉。同键后写覆盖
    先写并把该条移到序列末尾（授权记录按时间顺序遍历，序列即“最近
    使用优先”顺序），上限截断取序列前 N——最近使用的口径优先保留。
    """
    carried_map: dict[tuple, dict[str, Any]] = {}  # 保序：插入序＝最近使用序

    def _time_key(record: dict[str, Any]) -> tuple:
        return (
            record.get("start"),
            record.get("end"),
            record.get("grain"),
            record.get("timezone"),
        )

    def _dim_key(record: dict[str, Any]) -> tuple:
        return (
            record.get("table"),
            record.get("column"),
            tuple(sorted(record.get("values") or [])),
        )

    def _put(key: tuple, item: dict[str, Any]) -> None:
        if key in carried_map:  # 同键：最近使用的覆盖并移到末尾（§13.4）
            del carried_map[key]
        carried_map[key] = item

    for item in prev_carried:  # 既有携带先占位（多次轮换逐代传递）
        record = item.get("record") or {}
        if item.get("kind") == "time":
            _put(_time_key(record), item)
        elif item.get("kind") == "dim":
            _put(_dim_key(record), item)
    # 来源：本线程成功执行（query/pushdown）的授权记录中的 guard_evidence
    # （按登记顺序遍历，越靠后越是最近使用）
    for validation in (old_values.get("db_validations") or {}).values():
        if validation.get("status") != "valid":
            continue
        snapshot = validation.get("guard_evidence") or {}
        for record in snapshot.get("time") or []:
            _put(
                _time_key(record),
                {
                    "kind": "time",
                    "record": record,
                    "origin": record.get("inherited_from")
                    or _origin_of(record, "time_evidence_id"),
                },
            )
        for record in snapshot.get("dim") or []:
            _put(
                _dim_key(record),
                {
                    "kind": "dim",
                    "record": record,
                    "origin": record.get("inherited_from")
                    or _origin_of(record, "dim_evidence_id"),
                },
            )
    # 序列即“最后触碰”（最近使用）顺序——覆盖与新增都移到末尾；
    # 上限截断保留最近的 N 条（§13.4 优先保留最近使用的）
    carried = list(carried_map.values())[-cfg.thread.carry_max_evidence :]
    # 最近执行／分析记录（§7.12.1：链首去重；doc 6.3 §6.4：携带完整来源引用，
    # thread_id 经 ResultMeta 补齐，供后续 run 以完整身份沿用）
    limit = getattr(cfg.thread, "carry_recent_executions", 3)
    if limit:
        picked: list[Any] = []
        seen_root: set[str] = set()
        for kind, records in (
            ("execution", old_values.get("db_executions") or []),
            ("analysis", old_values.get("db_analyses") or []),
        ):
            for record in reversed(records):
                if kind == "execution" and record.get("purpose") != "query":
                    continue
                root = (record.get("reused_from") or {}).get(
                    "evidence_id"
                ) or record.get("evidence_id")
                if root in seen_root:
                    continue
                seen_root.add(root)
                # R1-07：origin 一律为记录自身完整身份（含沿用登记产生的新
                # 记录——run/evidence 属当前记录）；链首 reused_from 仅作
                # 展示并保留在 record 内，不再顶替直接来源身份。
                origin = _origin_of_record(record, meta_loader=meta_loader)
                picked.append(
                    {
                        "kind": kind,
                        "record": record,
                        "origin": origin,
                    }
                )
                if len(picked) >= limit:
                    break
            if len(picked) >= limit:
                break
        carried.extend(picked)
    return tuple(carried)


def _origin_of(record: dict[str, Any], id_key: str) -> dict[str, Any]:
    return {
        "evidence_id": record.get(id_key, ""),
        "thread_id": "",
        "request_id": record.get("request_id", ""),
        "run_id": record.get("run_id", ""),
        "datasource_id": record.get("datasource_id", ""),
        "user_text": record.get("source_text"),
    }


def _origin_of_record(
    record: dict[str, Any], meta_loader: Any = None
) -> dict[str, Any]:
    """执行/分析记录的携带来源（doc 6.3 §6.4；R2-10）。

    记录自身的完整可信 scope（thread/run/request/evidence）优先；原结果
    文件的生成身份与链首 reused_from 只作展示，不用于补造新来源身份。
    仅当旧记录确实没有 thread_id 时（旧协议）才回退 ResultMeta 补齐，
    绝不覆盖已有身份。
    """
    origin = _origin_of(record, "evidence_id")
    origin["user_text"] = None
    own_thread = record.get("thread_id")
    if isinstance(own_thread, str) and own_thread:
        origin["thread_id"] = own_thread
        return origin
    if meta_loader is not None:
        try:
            meta = meta_loader(
                record.get("result_ref") or record.get("output_result_ref"),
                record.get("run_id"),
            )
        except Exception:  # noqa: BLE001（来源补齐失败不阻塞轮换）
            meta = None
        if meta is not None and getattr(meta, "thread_id", ""):
            origin["thread_id"] = meta.thread_id
            origin["request_id"] = getattr(meta, "request_id", origin["request_id"])
    return origin


def _carried_listing(carried: tuple[Any, ...]) -> str:
    lines = []
    for item in carried:
        record = item.get("record") or {}
        origin = item.get("origin") or {}
        kind = item.get("kind")
        if kind == "time":
            lines.append(
                f"- 时间证据 {record.get('time_evidence_id')}："
                f"[{record.get('start')}, {record.get('end')})，原文“{record.get('source_text')}”"
            )
        elif kind == "dim":
            lines.append(
                f"- 维度证据 {record.get('dim_evidence_id')}："
                f"{record.get('table')}.{record.get('column')} ∈ {record.get('values')}"
            )
        elif kind == "execution":
            lines.append(
                f"- 执行证据 {record.get('evidence_id')}（原运行 {origin.get('run_id')}，"
                f"result_ref={record.get('result_ref')}，"
                f"生成于 {record.get('result_generated_at') or '未知时间'}）"
                f"来源引用：thread={origin.get('thread_id')}, "
                f"run={origin.get('run_id')}, evidence={origin.get('evidence_id')}"
            )
        elif kind == "analysis":
            lines.append(
                f"- 分析证据 {record.get('evidence_id')}（原运行 {origin.get('run_id')}，"
                f"result_ref={record.get('output_result_ref')}）"
                f"来源引用：thread={origin.get('thread_id')}, "
                f"run={origin.get('run_id')}, evidence={origin.get('evidence_id')}"
            )
    return "\n".join(lines)


def _thread_user_texts(values: dict[str, Any]) -> tuple[list[str], list[Any]]:
    """线程用户原文（排除 dbda_kind 注入）与消息列表（轮换判定用）。"""
    messages = values.get("messages") or []
    texts = [
        m.content
        for m in messages
        if isinstance(m, HumanMessage)
        and isinstance(m.content, str)
        and not (m.additional_kwargs or {}).get("dbda_kind")
    ]
    return texts, messages


def _thread_id_of(session_id: str, seq: int) -> str:
    return f"dbda:{session_id}:{seq}"


class _RejectedSession(Exception):
    """入口校验拒绝（会话/线程标识不合规、跨会话或来源绑定不一致）"""


class RequestIdConflictError(Exception):
    """同会话复用已有 request_id（doc 2 §4.2）：锁内拒绝，不覆盖既有
    诊断/归档/文本日志，也不当作幂等重放。"""


class FinalizeHookOutcome(TypedDict, total=False):
    """锁内终态收尾钩子的返回（doc 2 §4.4）：可选替换结果（不可信终态的
    安全投影）及 Web 层附加信息；键缺失表示不替换。"""

    result: "DbAgentResult"


def _resolve_source_binding(
    session_rec: dict | None,
    request_source: str | None,
    services_source: str | None,
) -> str:
    """三方核对会话来源绑定（doc 4 §3.2/§5.2），返回生效绑定。

    - 服务绑定（build_services 构造）必须存在且为合法短 ID（db_src_N，
      doc 5.1 §1；doc 12.2 M-1）；
    - 请求显式携带 source_id 时仅作一致性检查，不能覆盖会话绑定；
    - 会话记录已有绑定且与服务绑定不一致 → 明确拒绝（防 A 源会话发
      B 源问题导致串源）；
    - **会话记录已存在却缺少来源绑定**（改造前创建的旧会话）→ 拒绝
      续聊：此时 _resolve_thread 会恢复旧 checkpoint，旧源对话上下文
      不能进入新源会话；提示新建会话（无自动迁移）；
    - 首轮（无会话记录）以服务绑定为会话绑定并在 P3 持久化。
    """
    if not isinstance(services_source, str) or not services_source:
        raise _RejectedSession("服务未绑定 source_id，拒绝执行（无源过滤检索被禁止）")
    try:
        from src.modules.db_deepagent.source_registry import (
            normalize_short_source_id,
        )

        normalized_services = normalize_short_source_id(services_source)
    except ValueError:
        raise _RejectedSession("服务绑定的 source_id 不是合法短 ID") from None

    if request_source is not None:
        try:
            normalized_request = normalize_short_source_id(request_source)
        except (ValueError, AttributeError):
            raise _RejectedSession(
                f"请求携带的 source_id 不是合法短 ID: {request_source!r}"
            ) from None
        if normalized_request != normalized_services:
            raise _RejectedSession(
                f"请求 source_id（{normalized_request}）与服务绑定"
                f"（{normalized_services}）不一致，拒绝执行"
            )

    if session_rec is None:
        return normalized_services  # 首轮：绑定服务来源
    existing = session_rec.get("source_id")
    if not existing:
        raise _RejectedSession(
            "会话缺少来源绑定（多源改造前创建），不能直接切换数据源续聊；" "请新建会话"
        )
    if str(existing) != normalized_services:
        raise _RejectedSession(
            f"会话已绑定其他 source_id（{existing}），当前服务绑定为"
            f"{normalized_services}；请换用原绑定来源或新建会话"
        )
    return normalized_services


def _valid_session(session_id: str) -> bool:
    from src.modules.db_deepagent.persistence.checkpoint import valid_session_id

    return valid_session_id(session_id)


def _platform_conninfo(cfg: DbAgentConfig) -> str:
    from src.modules.db_deepagent.persistence.platform_pool import (
        make_platform_conninfo,
    )

    return make_platform_conninfo(cfg.platform_db)


def _seq_of(thread_id: str) -> int:
    try:
        return int(thread_id.rsplit(":", 1)[-1])
    except ValueError:
        return 1


def _parse_fixed_now(cfg: DbAgentConfig) -> datetime | None:
    if cfg.clock.fixed_now is None:
        return None
    return datetime.fromisoformat(cfg.clock.fixed_now)
