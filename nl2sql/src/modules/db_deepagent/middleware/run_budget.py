"""RunBudgetMiddleware：主 Agent 模型步骤与工具调用的预算预占（docs/v4/5 §11.1）

判定用进程内 RunBudgetLedger（加锁预占），状态只记录结果轨迹；预占后即使
工具失败也不退还额度。db_execute 的预占在 SqlPipeline.execute 内（§8.3）。
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from typing import Any

from langchain.agents.middleware import AgentMiddleware, hook_config
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.types import Command

from src.modules.db_deepagent.config import BudgetConfig
from src.modules.db_deepagent.context import DbAgentContext
from src.modules.db_deepagent.schemas import RunStatusRecord
from src.modules.db_deepagent.services.run_budget import RunBudgetLedger
from src.modules.db_deepagent.state import DbDeepAgentState
from src.utils.logger import get_module_logger

logger = get_module_logger("nl2sql.db_deepagent.middleware.run_budget")
audit_logger = get_module_logger("nl2sql.db_deepagent.audit")

_VALID_CALL_KINDS = ("model_call", "tool_call", "db_execute")


def _status_update(
    budget: RunBudgetLedger, run_id: str, reminders: int
) -> RunStatusRecord:
    """当前预算计数快照 → db_run_status 轨迹记录（§11.1：判定以 ledger 为准）。"""
    counters = budget.counters(run_id)
    return RunStatusRecord(
        run_id=run_id,
        stop_reason=budget.stop_reason(run_id),
        counters={k: counters.get(k, 0) for k in _VALID_CALL_KINDS},
        final_answer_reminders=reminders,
    )


def _existing_reminders(state: Any, run_id: str) -> int:
    record = (state.get("db_run_status") or {}).get(run_id) or {}
    return record.get("final_answer_reminders", 0)


def _safe_log(level: int, message: str, *args, **kwargs) -> None:
    """观测日志失败不得改变工具返回或异常。"""
    try:
        log_method = getattr(audit_logger, logging.getLevelName(level).lower())
        log_method(message, *args, **kwargs)
    except Exception:  # noqa: BLE001 - 日志自身失败不得改变工具行为
        return


def _tool_message(
    outcome: ToolMessage | Command, tool_call_id: str
) -> ToolMessage | None:
    if isinstance(outcome, ToolMessage):
        return outcome
    update = outcome.update or {}
    messages = update.get("messages") or []
    for message in messages:
        if isinstance(message, ToolMessage) and message.tool_call_id == tool_call_id:
            return message
    return None


def _summarize_tool_outcome(
    outcome: ToolMessage | Command, tool_call_id: str
) -> dict[str, Any]:
    message = _tool_message(outcome, tool_call_id)
    summary: dict[str, Any] = {
        "result_kind": "command" if isinstance(outcome, Command) else "tool_message",
        "status": "-",
        "error_type": "-",
        "error_source": "-",
        "output_chars": "-",
    }
    if message is None:
        return summary
    content = message.content
    if isinstance(content, str):
        summary["output_chars"] = len(content)
        try:
            envelope = json.loads(content)
        except (json.JSONDecodeError, TypeError, ValueError, RecursionError):
            envelope = None
        status = envelope.get("status") if isinstance(envelope, dict) else None
        if isinstance(status, str) and status in {"ok", "empty", "error"}:
            summary["status"] = status
            if status == "error":
                error = envelope.get("error") or {}
                if not isinstance(error, dict):
                    error = {}
                error_type = error.get("error_type")
                summary["error_type"] = (
                    error_type if isinstance(error_type, str) and error_type else "-"
                )
                summary["error_source"] = "business"
            return summary
    if message.status == "error":
        summary["status"] = "error"
        summary["error_source"] = "framework"
        summary["error_type"] = (
            "invalid_argument"
            if isinstance(content, str) and content.startswith("invalid_argument:")
            else "tool_error"
        )
    return summary


def _debug_value(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


def _debug_block(
    name: str,
    ctx: DbAgentContext,
    tool_name: str,
    tool_call_id: str,
    body: str,
) -> None:
    start_marker = (
        f"===== {name} request_id={ctx.request_id} run_id={ctx.run_id} "
        f"tool={tool_name} id={tool_call_id} ====="
    )
    end_marker = start_marker.replace("_BEGIN", "_END")
    _safe_log(logging.DEBUG, "%s\n%s\n%s", start_marker, body, end_marker)


def _debug_input(
    ctx: DbAgentContext, tool_name: str, tool_call_id: str, args: Any
) -> None:
    try:
        if not audit_logger.isEnabledFor(logging.DEBUG):
            return
        body = _debug_value(args)
    except Exception:  # noqa: BLE001 - 任意参数排版失败均不得中断工具
        body = f"<debug serialization failed; type={type(args).__name__}>"
    _debug_block("TOOL_INPUT_BEGIN", ctx, tool_name, tool_call_id, body)


def _nontext_summary(content: Any) -> str:
    if isinstance(content, list):
        block_types = [
            (
                str(block.get("type", "unknown"))
                if isinstance(block, dict)
                else type(block).__name__
            )
            for block in content
        ]
        return f"block_count={len(content)} block_types={','.join(block_types)}"
    return f"content_type={type(content).__name__}"


def _debug_output(
    ctx: DbAgentContext,
    tool_name: str,
    tool_call_id: str,
    outcome: ToolMessage | Command,
) -> None:
    try:
        if not audit_logger.isEnabledFor(logging.DEBUG):
            return
        message = _tool_message(outcome, tool_call_id)
        if message is None:
            body = "matching ToolMessage not found"
        elif isinstance(message.content, str):
            body = message.content
        else:
            body = _nontext_summary(message.content)
    except Exception:  # noqa: BLE001 - 任意返回排版失败均不得中断工具
        body = "<debug output formatting failed>"
    _debug_block("TOOL_OUTPUT_BEGIN", ctx, tool_name, tool_call_id, body)


def _log_tool_summary(
    event: str,
    elapsed_ms: int,
    ctx: DbAgentContext,
    tool_name: str,
    tool_call_id: str,
    summary: dict[str, Any],
    *,
    stop_reason: str | None = None,
) -> None:
    suffix = f" stop_reason={stop_reason}" if stop_reason else ""
    _safe_log(
        logging.INFO,
        "[request_id=%s run_id=%s] tool=%s id=%s event=%s elapsed_ms=%d "
        "result_kind=%s status=%s error_type=%s error_source=%s output_chars=%s%s",
        ctx.request_id,
        ctx.run_id,
        tool_name,
        tool_call_id,
        event,
        elapsed_ms,
        summary["result_kind"],
        summary["status"],
        summary["error_type"],
        summary["error_source"],
        summary["output_chars"],
        suffix,
    )


class RunBudgetMiddleware(AgentMiddleware[DbDeepAgentState, DbAgentContext]):
    """模型步骤／工具调用预占；失败即终止或跳过该次调用（§11.1）"""

    def __init__(self, budget: RunBudgetLedger, cfg: BudgetConfig):
        self._budget = budget
        self._cfg = cfg

    @hook_config(can_jump_to=["end"])
    def before_model(self, state, runtime) -> dict[str, Any] | None:
        ctx: DbAgentContext = runtime.context
        reservation = self._budget.reserve(ctx.run_id, "model_call")
        if reservation.ok:
            audit_logger.info(
                "[request_id=%s run_id=%s] model_call 预占成功 remaining=%d",
                ctx.request_id,
                ctx.run_id,
                reservation.remaining,
            )
            return None
        run_id = ctx.run_id
        logger.warning(
            "[request_id=%s run_id=%s] model_call 预占失败：%s",
            ctx.request_id,
            run_id,
            reservation.stop_reason,
        )
        return {
            "db_run_status": {
                run_id: _status_update(
                    self._budget, run_id, _existing_reminders(state, run_id)
                )
            },
            "messages": [
                AIMessage(
                    content=(
                        f"运行预算已耗尽（{reservation.stop_reason}），"
                        "本轮停止生成，已有结果将由系统整理返回。"
                    )
                )
            ],
            "jump_to": "end",
        }

    def wrap_tool_call(
        self,
        request,
        handler: Callable[[Any], ToolMessage | Command],
    ) -> ToolMessage | Command:
        ctx: DbAgentContext = request.runtime.context
        run_id = ctx.run_id
        state = request.state
        tool_call = request.tool_call
        tool_name = tool_call.get("name", "<unknown>")
        tool_call_id = tool_call.get("id", "")
        started = time.monotonic()
        _safe_log(
            logging.INFO,
            "[request_id=%s run_id=%s] tool=%s id=%s event=started",
            ctx.request_id,
            run_id,
            tool_name,
            tool_call_id,
        )
        reservation = self._budget.reserve(run_id, "tool_call")
        if not reservation.ok:
            logger.warning(
                "[request_id=%s run_id=%s] tool_call 预占失败：%s",
                ctx.request_id,
                run_id,
                reservation.stop_reason,
            )
            content = json.dumps(
                {
                    "status": "error",
                    "data": {},
                    "warnings": [],
                    "error": {
                        "error_type": "budget_exhausted",
                        "message": f"工具调用预算已耗尽（{reservation.stop_reason}）",
                        "retryable": False,
                    },
                },
                ensure_ascii=False,
            )
            result: ToolMessage | Command = Command(
                update={
                    "messages": [
                        ToolMessage(content=content, tool_call_id=tool_call_id)
                    ],
                    "db_run_status": {
                        run_id: _status_update(
                            self._budget,
                            run_id,
                            _existing_reminders(state, run_id),
                        )
                    },
                }
            )
            elapsed_ms = int((time.monotonic() - started) * 1000)
            summary = _summarize_tool_outcome(result, tool_call_id)
            summary["error_source"] = "middleware"
            _log_tool_summary(
                "rejected",
                elapsed_ms,
                ctx,
                tool_name,
                tool_call_id,
                summary,
                stop_reason=reservation.stop_reason,
            )
            _debug_output(ctx, tool_name, tool_call_id, result)
            return result
        _debug_input(ctx, tool_name, tool_call_id, tool_call.get("args", {}))
        try:
            outcome = handler(request)
        except Exception as exc:
            # 预占不退还；ERROR 只留摘要，原文和堆栈仅在 DEBUG 中记录。
            elapsed_ms = int((time.monotonic() - started) * 1000)
            _safe_log(
                logging.ERROR,
                "[request_id=%s run_id=%s] tool=%s id=%s event=failed "
                "elapsed_ms=%d exception_type=%s",
                ctx.request_id,
                run_id,
                tool_name,
                tool_call_id,
                elapsed_ms,
                type(exc).__name__,
            )
            _safe_log(
                logging.DEBUG,
                "[request_id=%s run_id=%s] tool=%s id=%s exception traceback",
                ctx.request_id,
                run_id,
                tool_name,
                tool_call_id,
                exc_info=True,
            )
            raise
        elapsed_ms = int((time.monotonic() - started) * 1000)
        summary = _summarize_tool_outcome(outcome, tool_call_id)
        _log_tool_summary("finished", elapsed_ms, ctx, tool_name, tool_call_id, summary)
        _debug_output(ctx, tool_name, tool_call_id, outcome)
        return outcome
