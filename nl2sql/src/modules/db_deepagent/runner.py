"""DbAgentRunner：一次请求的五个阶段（docs/v4/5 §14.3）

P0 准备（线程确定／轮换／携带证据）→ P1 运行（invoke）→ P2 读取（get_state）
→ P3 归档（Store）→ P4 本地记录。各阶段异常按发生顺序记入 RunFailures，
最终状态按优先级判定；primary 标在决定状态那一类中最早的一条。
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from langchain_core.messages import HumanMessage

from src.modules.db_deepagent.agent import DbAgentHandle, read_summary_for_handoff
from src.modules.db_deepagent.config import (
    DbAgentConfig,
    compute_validation_fingerprint,
)
from src.modules.db_deepagent.context import DbAgentContext
from src.modules.db_deepagent.middleware.model_observer import (
    ModelCallObserver,
    ModelObservationSnapshot,
)
from src.modules.db_deepagent.persistence.archive import QaArchive
from src.modules.db_deepagent.schemas import (
    DbAgentResult,
    EvidenceItem,
    FindingItem,
    RunFailureRecord,
    RunStats,
)
from src.modules.db_deepagent.services.run_budget import (
    BudgetSnapshot,
    limits_from_config,
)
from src.utils.logger import get_module_logger

logger = get_module_logger("nl2sql.db_deepagent.runner")

_BUDGET_STOP_REASONS = (
    "model_call_limit",
    "tool_call_limit",
    "execute_limit",
    "run_timeout",
)


def _persistence_error_types() -> tuple[type[BaseException], ...]:
    import psycopg
    import psycopg_pool

    from src.modules.db_deepagent.errors import PlatformSchemaError

    return (psycopg.Error, psycopg_pool.PoolTimeout, PlatformSchemaError)


def _mark_primary(failures: _RunFailures, status: str) -> list[RunFailureRecord]:
    """primary 标在与最终状态对应的那一类中最早的一条（§14.3）。

    failed 可由 runtime／tool_set_mismatch／no_final_answer 任一触发，按该
    优先顺序取最早匹配，使 primary 总有可标记的记录。
    """
    kinds_by_status: dict[str, tuple[str, ...]] = {
        "persistence_error": ("persistence",),
        "failed": ("runtime", "tool_set_mismatch", "no_final_answer"),
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


def is_persistence_error(exc: BaseException) -> bool:
    return isinstance(exc, _persistence_error_types())


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

    # ------------------------------------------------------------ 入口

    def run(
        self,
        question: str,
        session_id: str,
        thread_id: str | None = None,
        request_id: str | None = None,
        force_rotate: bool = False,
        source_id: str | None = None,
    ) -> DbAgentResult:
        # 会话级互斥（doc 7 §2.3）：P0 读取前取锁，P3 完成后外层 finally 释放；
        # memory 持久化模式无 PG，跳过互斥。取锁失败：不写任何元数据（防覆盖
        # 运行中进程指针），仅返回失败结果与 P4 本地记录。
        lock = None
        if self._lock_conninfo is not None:
            from src.modules.db_deepagent.persistence.checkpoint import (
                SessionAdvisoryLock,
            )

            lock = SessionAdvisoryLock(self._lock_conninfo, session_id)
            acquired = False
            try:
                acquired = lock.try_acquire()
            except Exception:  # noqa: BLE001（连接失败按取锁失败处理）
                acquired = False
            if not acquired:
                request_id = request_id or "req_" + uuid.uuid4().hex[:12]
                run_id = "run_" + uuid.uuid4().hex
                failures = _RunFailures()
                failures.add(
                    "runtime", "P1", "会话正在另一进程运行或清理中（advisory lock）"
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
        try:
            return self._run_locked(
                question,
                session_id,
                thread_id,
                request_id,
                lock,
                force_rotate,
                source_id,
            )
        finally:
            if lock is not None:
                lock.release()

    def _run_locked(
        self,
        question: str,
        session_id: str,
        thread_id: str | None,
        request_id: str | None,
        lock,
        force_rotate: bool = False,
        source_id: str | None = None,
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
            session_rec = self._archive.get_session(session_id)
            source_binding = _resolve_source_binding(
                session_rec, source_id, self._services.source_id
            )
            current_thread, thread_seq, carried, old_values, max_seq = (
                self._resolve_thread(session_id, thread_id, session_rec)
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
                    old_values, carried, new_thread
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
        return self._finalize(
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
        )

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
    ) -> tuple[tuple[Any, ...], HumanMessage]:
        """§13.4：结构化携带证据 + 交接消息（不回查更早线程）。"""
        summary_text = read_summary_for_handoff(old_values or {})
        carried = _collect_carried(old_values or {}, prev_carried, self._cfg)
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
    ) -> DbAgentResult:
        evidence = evidence or (
            self._assemble_evidence(values, run_id) if values else []
        )
        # 最终答案仅在评估允许时读取（P1 runtime 异常／P2 失败时不读）；
        # 账本（evidence／归档判定）不受此开关影响
        answer_record = (
            (values.get("db_final_answers") or {}).get(run_id)
            if (values is not None and read_final_answer)
            else None
        )
        status, findings, answer, warnings = self._evaluate(
            failures, budget_snapshot, values, answer_record, evidence, warnings
        )
        errors = _mark_primary(failures, status)
        if not model_snapshot.token_usage_complete and model_snapshot.total_model_calls:
            warnings.append("模型用量统计不完整")
        answer_payload = (answer_record or {}).get("answer") or {}
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
            metric_definitions=answer_payload.get("metric_definitions", []),
            limitations=answer_payload.get("limitations", []),
            open_questions=answer_payload.get("open_questions", []),
            errors=errors,
            warnings=warnings,
            archived=archived,
            stats=stats,
        )
        self._write_local_record(session_id, request_id, result, failures, rotated)
        return result

    def _evaluate(
        self,
        failures: _RunFailures,
        budget_snapshot: BudgetSnapshot,
        values: dict[str, Any] | None,
        answer_record: dict[str, Any] | None,
        evidence: list[EvidenceItem],
        warnings: list[str],
    ) -> tuple[str, list[FindingItem], str, list[str]]:
        """状态判定（优先级）与 findings 后备核对（§14.3）。"""
        payload = (answer_record or {}).get("answer") or {}
        findings = [FindingItem(**f) for f in payload.get("findings") or []]
        valid_ids = {e.evidence_id for e in evidence}
        if answer_record is not None:
            kept: list[FindingItem] = []
            for finding in findings:
                remaining = [eid for eid in finding.evidence_ids if eid in valid_ids]
                dropped = [eid for eid in finding.evidence_ids if eid not in valid_ids]
                if dropped:
                    warnings.append(
                        f"finding 引用的证据不存在，已移除: {', '.join(dropped)}"
                    )
                if remaining:
                    kept.append(FindingItem(text=finding.text, evidence_ids=remaining))
                else:
                    warnings.append(f"finding 无有效证据支持：{finding.text[:50]}")
            findings = kept
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
        elif answer_record is None:
            failures.add("no_final_answer", "P2", "未提交最终答案")
            status = "failed"
        else:
            status = payload.get("status", "failed")
            if status == "completed" and not evidence:
                status = "failed"
                warnings.append("模型报 completed 但本次无任何成功证据，判为 failed")
        return status, findings, payload.get("answer", ""), warnings  # type: ignore[return-value]

    def _assemble_evidence(
        self, values: dict[str, Any], run_id: str
    ) -> list[EvidenceItem]:
        """run_id==本次 的 query 执行与分析记录（含沿用登记）→ EvidenceItem。"""
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
                    row_count=record.get("saved_row_count", 0),
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
                    input_result_refs=list(record.get("input_result_refs") or []),
                    operation=dict(record.get("operation") or {}),
                    row_count=record.get("saved_row_count", 0),
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
    ) -> bool:
        """§13.3 写入判定：条件 a（成功真实执行）或 b（被接受答案＋有效 E/A 沿用引用）。"""
        # 条件 a 从本运行执行账本判断（§13.3：query 或 pushdown、非沿用）；
        # evidence 里的 sql_query 只含 purpose="query"，纯下推分析会被漏判
        has_real_execution = any(
            record.get("run_id") == run_id
            and record.get("purpose") in ("query", "pushdown")
            and record.get("reused_from") is None
            for record in ((values or {}).get("db_executions") or [])
        )
        reused_ids = {e.evidence_id for e in evidence if e.reused_from is not None}
        payload = (answer_record or {}).get("answer") or {}
        cited_reused = any(
            eid in reused_ids
            for finding in payload.get("findings") or []
            for eid in finding.get("evidence_ids") or []
        )
        condition_b = (
            answer_record is not None
            and payload.get("status") in ("completed", "partial")
            and cited_reused
        )
        if has_real_execution or condition_b:
            self._archive.write_qa(
                session_id,
                request_id,
                {
                    "run_id": run_id,
                    "thread_id": thread_id,
                    "question": question,
                    "status": status,
                    "answer": payload,
                    "evidence": [e.model_dump() for e in evidence],
                    "stats": stats.model_dump(),
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
                    "stop_reason": None,
                    "errors": [r.model_dump() for r in failures.records],
                },
            )
        # sessions／threads 轨迹落库（doc 7 §2.1；与归档同批）
        self._write_thread_tracks(session_id, thread_id, carried, source_binding)
        return True

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

    def _write_local_record(
        self,
        session_id: str,
        request_id: str,
        result: DbAgentResult,
        failures: _RunFailures,
        rotated: bool,
    ) -> None:
        """P4：<workspace>/sessions/<session_id>/runs/<request_id>.json；失败只记日志。"""
        try:
            path = Path(self._cfg.workspace.root) / "sessions" / session_id / "runs"
            path.mkdir(parents=True, exist_ok=True)
            payload = result.model_dump()
            payload["failures"] = [r.model_dump() for r in failures.records]
            payload["rotated"] = rotated
            (path / f"{request_id}.json").write_text(
                json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
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
    # 最近执行／分析记录（§7.12.1：链首去重）
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
                picked.append(
                    {
                        "kind": kind,
                        "record": record,
                        "origin": record.get("reused_from")
                        or _origin_of_record(record),
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


def _origin_of_record(record: dict[str, Any]) -> dict[str, Any]:
    origin = _origin_of(record, "evidence_id")
    origin["user_text"] = None
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
            )
        elif kind == "analysis":
            lines.append(
                f"- 分析证据 {record.get('evidence_id')}（原运行 {origin.get('run_id')}，"
                f"result_ref={record.get('output_result_ref')}）"
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


def _resolve_source_binding(
    session_rec: dict | None,
    request_source: str | None,
    services_source: str | None,
) -> str:
    """三方核对会话来源绑定（doc 4 §3.2/§5.2），返回生效绑定。

    - 服务绑定（build_services 构造）必须存在且为合法 UUID；
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
        normalized_services = str(uuid.UUID(services_source))
    except ValueError:
        raise _RejectedSession("服务绑定的 source_id 不是合法 UUID") from None

    if request_source is not None:
        try:
            normalized_request = str(uuid.UUID(str(request_source).strip()))
        except (ValueError, AttributeError):
            raise _RejectedSession(
                f"请求携带的 source_id 不是合法 UUID: {request_source!r}"
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
