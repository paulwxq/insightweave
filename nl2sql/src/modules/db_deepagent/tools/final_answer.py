"""submit_final_answer：最终答案提交工具（docs/v4/5 §7.11、§14.1；doc 6.3 §7.3）

检查顺序（doc 6.3 §7.3）：参数结构（含 6.2 非空白正文 validator，参数层）
→ 单独调用 → 未重复提交 → 共享任务门控（结构归一后一次语义判定：
任务视图/终态/证据/覆盖/unresolved 恰覆盖/status 严格匹配）。全部通过才
登记 FinalAnswerRecord（含 plan_version/规则版本/覆盖映射审计）；拒绝不写
记录、不结束运行（K19），Runner 从最终 checkpoint 再执行同一规则。
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from langchain.tools import ToolRuntime
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import InjectedToolArg, tool
from langgraph.types import Command
from pydantic import Field

from src.modules.db_deepagent.context import DbAgentContext
from src.modules.db_deepagent.errors import (
    TOOL_ERROR_FINAL_ANSWER_ALREADY_SUBMITTED,
    TOOL_ERROR_FINAL_ANSWER_NOT_ALONE,
    is_persistence_error,
)
from src.modules.db_deepagent.schemas import DbAgentFinalAnswer, FinalAnswerRecord
from src.modules.db_deepagent.services.result_facts import (
    aggregate_known_saved_row_counts,
    aggregate_proven_untruncated,
)
from src.modules.db_deepagent.services.sql_pipeline import LedgerView
from src.modules.db_deepagent.services.task_completion import (
    TASK_COMPLETION_RULES_VERSION,
    CompletionSnapshot,
    evaluate_submission,
)
from src.modules.db_deepagent.services.task_ledger import current_task_view
from src.modules.db_deepagent.state import DbDeepAgentState
from src.modules.db_deepagent.tools._envelope import error_envelope
from src.modules.db_deepagent.tools.schema_tools import (
    _attach_validation_handler,
)
from src.utils.logger import get_module_logger

logger = get_module_logger("nl2sql.db_deepagent.tools.final_answer")


def build_completion_snapshot(
    state: Any,
    run_id: str,
    *,
    services: Any = None,
    session_id: str | None = None,
    thread_id: str | None = None,
    carried: tuple[Any, ...] = (),
) -> tuple[CompletionSnapshot | None, str | None]:
    """装配共享规则快照（doc 6.3 §7.1：I/O 由调用方预先装配）。

    R2-01：会话/线程身份由调用方显式传入可信 ctx，不从不存在的
    state.session_id 推断；无显式传入时仅从任务记录回退（合成/旧测试状态）。
    """
    try:
        tasks = current_task_view(state.get("db_sub_tasks") or {}, run_id)
        view = LedgerView(state)
        executions = view.executions_of_run(run_id)
        analyses = view.analyses_of_run(run_id)
        if session_id is None:
            session_id = state.get("session_id") or next(
                (t.get("session_id") for t in tasks.values() if t.get("session_id")),
                "",
            )
        if thread_id is None:
            thread_id = state.get("thread_id") or next(
                (t.get("thread_id") for t in tasks.values() if t.get("thread_id")),
                "",
            )
        # R2-01/02：完整证据图核验（快照优先，工具/登记/提交/Runner 共享）
        from src.modules.db_deepagent.services.reuse_qualification import (
            unqualified_reuse_evidence,
        )

        reuse_archive = None
        if getattr(services, "store", None) is not None:
            from src.modules.db_deepagent.persistence.archive import QaArchive

            reuse_archive = QaArchive(services.store)
        reuse_failures = unqualified_reuse_evidence(
            [*executions, *analyses],
            session_id=session_id or "",
            thread_id=thread_id or "",
            run_id=run_id,
            state=state,
            archive=reuse_archive,
            carried=carried,
        )
        from src.modules.db_deepagent.services.task_completion import (
            all_task_versions,
        )

        return (
            CompletionSnapshot(
                run_id=run_id,
                tasks=tasks,
                all_task_records=all_task_versions(state),
                executions=executions,
                analyses=analyses,
                errors=list(state.get("db_tool_errors") or []),
                known_counts=aggregate_known_saved_row_counts(state, run_id),
                proven_untruncated=aggregate_proven_untruncated(state, run_id),
                reuse_failures=reuse_failures,
                plan_version=view.task_plan_version(run_id),
            ),
            None,
        )
    except Exception as exc:  # noqa: BLE001
        # R4-01：已识别的持久化 I/O 故障不伪装成 task_ledger_corrupt
        # 业务门控拒绝（模型无法据此修正、会误判为可重试）。原样上抛，
        # 由运行层 P1 既有 is_persistence_error 分类收敛为 persistence_error
        # 与系统兜底；确定性协议损坏/账本损坏仍按受控业务拒绝返回。
        if is_persistence_error(exc):
            raise
        return None, f"任务账本不可核验: {exc}"


def make_submit_final_answer_tool(services: Any = None):
    """构造 submit_final_answer 工具（services 可选：store 供来源资格核验）。"""

    class SubmitFinalAnswerArgs(DbAgentFinalAnswer):
        runtime: Annotated[Any, InjectedToolArg] = Field(exclude=True)

    @tool(args_schema=SubmitFinalAnswerArgs)
    def submit_final_answer(
        status: str,
        answer: str,
        findings: list[dict[str, Any]],
        metric_definitions: list[str],
        limitations: list[str],
        open_questions: list[str],
        unresolved_task_refs: list[dict[str, Any]],
        runtime: ToolRuntime[DbAgentContext, DbDeepAgentState],
    ):
        """提交本运行的最终答案；必须单独调用（同一条消息不调用其他工具）。

        每条 finding 带 finding_id（F-1、F-2…）与 task_refs，并引用
        execute_sql／analyze_result／reuse_evidence 返回过、且已选入对应任务
        成果的 evidence_id；探针执行记录不能引用。unresolved_task_refs 恰列
        本轮 failed/needs_clarification 任务；open_questions 只放非阻塞补充。
        提交后运行由系统结束。
        """
        ctx = runtime.context
        run_id = ctx.run_id
        submitted = DbAgentFinalAnswer(
            status=status,  # type: ignore[arg-type]
            answer=answer,
            findings=findings,  # type: ignore[arg-type]
            metric_definitions=metric_definitions,
            limitations=limitations,
            open_questions=open_questions,
            unresolved_task_refs=unresolved_task_refs,  # type: ignore[arg-type]
        )
        # 1. 单独调用：最后一条 AIMessage（发出本调用的消息）不得还有别的工具（K20）
        messages = runtime.state.get("messages") or []
        last = messages[-1] if messages else None
        if isinstance(last, AIMessage):
            others = [
                tc
                for tc in (last.tool_calls or [])
                if tc.get("id") != runtime.tool_call_id
            ]
            if others:
                return error_envelope(
                    TOOL_ERROR_FINAL_ANSWER_NOT_ALONE,
                    "submit_final_answer 必须单独调用，同一条消息不能调用其他工具",
                    retryable=True,
                    hint="其他工具的结果会在下一轮可见，之后单独提交",
                )
        # 2. 未重复提交
        if run_id in (runtime.state.get("db_final_answers") or {}):
            return error_envelope(
                TOOL_ERROR_FINAL_ANSWER_ALREADY_SUBMITTED,
                "本运行已提交过最终答案",
                retryable=False,
            )
        # 3. 共享任务门控（doc 6.3 §7.3）：结构归一后一次语义判定
        snapshot, snapshot_error = build_completion_snapshot(
            runtime.state,
            run_id,
            services=services,
            session_id=ctx.session_id,
            thread_id=ctx.thread_id,
            carried=tuple(ctx.carried_evidence or ()),
        )
        if snapshot is None:
            return error_envelope(
                "task_ledger_corrupt",
                snapshot_error or "任务账本不可核验，拒绝提交",
                retryable=False,
            )
        decision = evaluate_submission(submitted.model_dump(), snapshot)
        if not decision.accepted:
            primary = decision.primary_error
            data: dict[str, Any] = {
                "task_refs": primary.task_refs if primary else [],
                "submitted_status": submitted.status,
            }
            if decision.expected_status is not None:
                data["expected_status"] = decision.expected_status
            if decision.pending_task_refs:
                data["pending_tasks"] = decision.pending_task_refs
            if decision.uncovered_task_refs:
                data["uncovered_tasks"] = decision.uncovered_task_refs
            logger.info(
                "[request_id=%s run_id=%s] 最终答案被门控拒绝: %s",
                ctx.request_id,
                run_id,
                primary.code if primary else "unknown",
            )
            return error_envelope(
                primary.code if primary else "task_completion_rejected",
                primary.message if primary else "任务门控拒绝",
                retryable=True,
                hint=(
                    "修正提交（登记终态/覆盖 findings/unresolved 恰覆盖/"
                    "status 与业务判定一致）；不需要重新执行 SQL"
                ),
                data=data,
            )
        # 4. 通过：保存原提交与服务端规范化答案记录（doc 6.3 §7.1）
        record = FinalAnswerRecord(
            run_id=run_id,
            request_id=ctx.request_id,
            tool_call_id=runtime.tool_call_id or "",
            answer=submitted.model_dump(),
            submitted_at=datetime.now().astimezone().isoformat(timespec="seconds"),
        )
        logger.info(
            "[request_id=%s run_id=%s] 最终答案已接受: status=%s findings=%d",
            ctx.request_id,
            run_id,
            submitted.status,
            len(submitted.findings),
        )
        return Command(
            update={
                "db_final_answers": {
                    run_id: {
                        **record,
                        "plan_version": snapshot.plan_version,
                        "completion_rules_version": TASK_COMPLETION_RULES_VERSION,
                        "coverage_map": decision.coverage_map,
                    }
                },
                "messages": [
                    ToolMessage(
                        content="已接收最终答案",
                        tool_call_id=runtime.tool_call_id or "",
                    )
                ],
            }
        )

    _attach_validation_handler(submit_final_answer)
    return submit_final_answer
