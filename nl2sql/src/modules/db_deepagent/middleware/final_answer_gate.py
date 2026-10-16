"""FinalAnswerGateMiddleware：最终答案门控（docs/v4/5 §11.3）

已接受提交 → 下一次模型调用前结束运行；模型无工具调用且未提交时按配置
提醒一次。被拒的提交不写记录，不会在此结束运行（K19）。
"""

from __future__ import annotations

from typing import Any

from langchain.agents.middleware import AgentMiddleware, hook_config
from langchain_core.messages import AIMessage, HumanMessage

from src.modules.db_deepagent.config import BudgetConfig
from src.modules.db_deepagent.context import DbAgentContext
from src.modules.db_deepagent.services.run_budget import RunBudgetLedger
from src.modules.db_deepagent.state import DbDeepAgentState
from src.utils.logger import get_module_logger

logger = get_module_logger("nl2sql.db_deepagent.middleware.final_answer_gate")


class FinalAnswerGateMiddleware(AgentMiddleware[DbDeepAgentState, DbAgentContext]):
    """提交后结束运行；漏提交提醒（§11.3）"""

    def __init__(self, budget: RunBudgetLedger, cfg: BudgetConfig):
        self._budget = budget
        self._cfg = cfg

    @hook_config(can_jump_to=["end"])
    def before_model(self, state, runtime) -> dict[str, Any] | None:
        ctx: DbAgentContext = runtime.context
        answers = state.get("db_final_answers") or {}
        if ctx.run_id in answers:
            logger.info(
                "[request_id=%s run_id=%s] 最终答案已登记，运行结束",
                ctx.request_id,
                ctx.run_id,
            )
            return {"jump_to": "end"}
        return None

    @hook_config(can_jump_to=["model", "end"])
    def after_model(self, state, runtime) -> dict[str, Any] | None:
        messages = state.get("messages") or []
        last = messages[-1] if messages else None
        if not isinstance(last, AIMessage) or last.tool_calls:
            return None
        ctx: DbAgentContext = runtime.context
        run_id = ctx.run_id
        if run_id in (state.get("db_final_answers") or {}):
            return None  # 已提交：下一轮 before_model 结束运行
        if self._budget.stop_reason(run_id) is not None:
            return None  # 预算耗尽／工具集合异常时不提醒（§11.3）
        record = (state.get("db_run_status") or {}).get(run_id) or {}
        reminders = record.get("final_answer_reminders", 0)
        if reminders >= self._cfg.final_answer_reminders:
            return None  # 不再干预，运行自然结束，Runner 按“未提交”处理
        logger.info(
            "[request_id=%s run_id=%s] 模型未调用工具且未提交，发送提醒（第 %d 次）",
            ctx.request_id,
            run_id,
            reminders + 1,
        )
        counters = self._budget.counters(run_id)
        return {
            "db_run_status": {
                run_id: {
                    "run_id": run_id,
                    "stop_reason": self._budget.stop_reason(run_id),
                    "counters": {
                        k: counters.get(k, 0)
                        for k in ("model_call", "tool_call", "db_execute")
                    },
                    "final_answer_reminders": reminders + 1,
                }
            },
            "messages": [
                HumanMessage(
                    content=(
                        "请调用 submit_final_answer 单独提交最终答案，"
                        "并引用已有 evidence_id。"
                    ),
                    additional_kwargs={"dbda_kind": "final_answer_reminder"},
                )
            ],
            "jump_to": "model",
        }
