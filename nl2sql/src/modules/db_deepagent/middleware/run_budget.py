"""RunBudgetMiddleware：主 Agent 模型步骤与工具调用的预算预占（docs/v4/5 §11.1）

判定用进程内 RunBudgetLedger（加锁预占），状态只记录结果轨迹；预占后即使
工具失败也不退还额度。db_execute 的预占在 SqlPipeline.execute 内（§8.3）。
"""

from __future__ import annotations

import json
from typing import Any, Callable

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
        reservation = self._budget.reserve(run_id, "tool_call")
        status = "reserved"
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
            status = f"rejected:{reservation.stop_reason}"
            audit_logger.info(
                "[request_id=%s run_id=%s] tool=%s id=%s 预占=%s",
                ctx.request_id,
                run_id,
                tool_name,
                tool_call_id,
                status,
            )
            return result
        try:
            outcome = handler(request)
        except Exception:
            # 预占不退还（计数表示尝试次数）；异常原样上抛，由框架错误策略处理
            audit_logger.info(
                "[request_id=%s run_id=%s] tool=%s id=%s 预占=ok 结果=exception",
                ctx.request_id,
                run_id,
                tool_name,
                tool_call_id,
            )
            raise
        result_kind = "command" if isinstance(outcome, Command) else "tool_message"
        audit_logger.info(
            "[request_id=%s run_id=%s] tool=%s id=%s 预占=ok 结果=%s",
            ctx.request_id,
            run_id,
            tool_name,
            tool_call_id,
            result_kind,
        )
        return outcome
