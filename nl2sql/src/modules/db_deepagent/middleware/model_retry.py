"""主模型有限重试（doc 6.2 §7.6）

封装公开 `ModelRetryMiddleware`（langchain 1.4.9）：明确暂时性错误白名单，
每次实际尝试前检查运行停止条件（协作式，非硬 deadline）。前置条件：
`build_model()` 已确保主模型 SDK 客户端 max_retries=0（单层重试，doc 6.2
§7.5.2），项目层不再叠加 sleep——底层客户端的请求超时仍保留。

只覆盖同步主 Agent 模型调用（生产 Runner 为同步 invoke()）；不承诺异步
入口或摘要模型内部调用（摘要经 SummarizationMiddleware 直调，不在本
wrapper 链内）。
"""

from __future__ import annotations

from collections.abc import Callable

from langchain.agents.middleware import AgentMiddleware, ModelRetryMiddleware
from langchain.agents.middleware.types import ModelRequest, ModelResponse
from langchain_core.messages import AIMessage
from openai import APIConnectionError, APIStatusError, RateLimitError

from src.modules.db_deepagent.config import ModelRetryConfig
from src.modules.db_deepagent.context import DbAgentContext
from src.modules.db_deepagent.services.run_budget import RunBudgetLedger
from src.utils.logger import get_module_logger

logger = get_module_logger("nl2sql.db_deepagent.middleware.model_retry")


_STATE_KEY = "model_retry_step"  # 账本运行条目中的步骤额度状态键


class ModelStepAttemptsExhausted(RuntimeError):
    """同一逻辑模型步骤的主模型尝试额度耗尽（doc 6.2 §7.6，R1 修复）。

    含上下文压缩（SummarizationMiddleware）恢复后的再请求；__cause__ 保留
    该步骤最后一次原始异常。由 P1 捕获后按 runtime 失败收敛，不伪造成功。
    """


def is_transient_model_error(exc: Exception) -> bool:
    """暂时性错误白名单（doc 6.2 §7.6）。

    覆盖当前主模型 OpenAI 兼容路径（dashscope_openai → ChatOpenAI）：
    - APIConnectionError（含其子类 APITimeoutError）与 RateLimitError 重试；
    - APIStatusError 仅 408/409/429/5xx 重试；鉴权（401）、参数（400）、
      模型拒绝（4xx）不重试；普通 RuntimeError、持久化异常不重试。
    原生 Tongyi 等其他 provider 的异常不在白名单，直接交给 Runner；
    切换主模型路径须先补该 provider 的明确适配和测试。
    """
    if isinstance(exc, (APIConnectionError, RateLimitError)):
        return True
    return isinstance(exc, APIStatusError) and (
        exc.status_code in (408, 409, 429) or exc.status_code >= 500
    )


class BudgetAwareModelRetryMiddleware(AgentMiddleware):
    """每次模型尝试前检查停止条件，再用 SDK 重试环执行（doc 6.2 §7.6）。

    停止条件触发时返回普通模型响应（不是正式答案），由既有 gate/Runner
    按停止原因收敛，Runner 仍生成系统兜底；重试耗尽或错误不在白名单时
    重新抛出原异常，由 P1 捕获与状态优先级处理。

    尝试额度绑定到**逻辑模型步骤**（R1 修复）：SummarizationMiddleware 在
    本中间件外层，上下文超限恢复会带着压缩后的上下文再次进入本包装器——
    这不是新的 before_model 步骤。额度按 run_id + 已预占的 model_call 步骤
    序号共享：同一逻辑步骤内（含摘要恢复重入）总尝试数不超过
    1 + max_retries；新步骤（counters 前进）额度重置。额度耗尽抛
    ModelStepAttemptsExhausted（链上保留最后一次原异常），按既定失败
    语义收敛，不伪造成功。
    """

    def __init__(self, budget: RunBudgetLedger, cfg: ModelRetryConfig):
        self._budget = budget
        self._cfg = cfg
        # 回退存储：仅用于未注册预算账本的运行（离线测试直调中间件）。
        # 已注册运行的步骤状态存于 RunBudgetLedger.run_shared_state(run_id)，
        # 随 close_run() 释放（doc 6.2 §7.6 / R2-01），不在运行结束后滞留。
        self._local_steps: dict[str, dict] = {}

    def _step_state(self, run_id: str) -> dict:
        step_key = (self._budget.counters(run_id) or {}).get("model_call", 0)
        # 主存储：账本运行条目的共享状态（close_run 移除，含异常回溯一并释放）
        run_state_getter = getattr(self._budget, "run_shared_state", None)
        store = run_state_getter(run_id) if callable(run_state_getter) else None
        if store is not None:
            state = store.get(_STATE_KEY)
            if state is None or state["key"] != step_key:
                state = {"key": step_key, "used": 0, "last_error": None}
                store[_STATE_KEY] = state
            return state
        # 回退：未注册运行（测试直调）；步骤键前进即重置，小容量兜底
        state = self._local_steps.get(run_id)
        if state is None or state["key"] != step_key:
            state = {"key": step_key, "used": 0, "last_error": None}
            if len(self._local_steps) > 64:
                self._local_steps.clear()
            self._local_steps[run_id] = state
        return state

    def _stop_response(self, run_id: str) -> ModelResponse | None:
        """停止/时限预检：已停止时返回停止消息（不发请求、不耗额度）。"""
        reason = self._budget.stop_reason(run_id)
        if reason is None and self._budget.remaining_seconds(run_id) <= 0:
            self._budget.stop(run_id, "run_timeout")
            reason = self._budget.stop_reason(run_id)
        if reason is None:
            return None
        logger.info(
            "[run_id=%s] 停止条件已触发（%s），不再发起后续模型尝试",
            run_id,
            reason,
        )
        return ModelResponse(
            result=[AIMessage(content="本轮已达到停止条件，停止后续模型尝试。")]
        )

    def wrap_model_call(
        self,
        request: ModelRequest[DbAgentContext],
        handler: Callable[[ModelRequest[DbAgentContext]], ModelResponse],
    ) -> ModelResponse:
        ctx: DbAgentContext = request.runtime.context
        state = self._step_state(ctx.run_id)
        total = 1 + self._cfg.max_retries  # 本逻辑步骤总尝试上限

        def attempt(
            inner_request: ModelRequest[DbAgentContext],
        ) -> ModelResponse:
            stopped = self._stop_response(ctx.run_id)
            if stopped is not None:
                return stopped
            if state["used"] >= total:
                # 步骤额度耗尽（含摘要恢复重入）：保留最后错误，按失败收敛。
                raise ModelStepAttemptsExhausted(
                    f"本模型步骤的主模型尝试额度已用尽（上限 {total} 次，"
                    f"含上下文压缩恢复后的再请求）；run_id={ctx.run_id}"
                ) from state["last_error"]
            state["used"] += 1
            try:
                response = handler(inner_request)
            except BaseException as exc:  # noqa: BLE001（记录后原样上抛）
                state["last_error"] = exc
                raise
            # 成功即释放原异常及其回溯（R2-01）：额度状态本身随运行关闭清理
            state["last_error"] = None
            return response

        remaining = total - state["used"]
        if remaining <= 0:
            # 停止条件优先于额度拒绝：运行已在收敛，不再抛额度错误。
            stopped = self._stop_response(ctx.run_id)
            if stopped is not None:
                return stopped
            raise ModelStepAttemptsExhausted(
                f"本模型步骤的主模型尝试额度已用尽（上限 {total} 次，"
                f"含上下文压缩恢复后的再请求）；run_id={ctx.run_id}"
            ) from state["last_error"]
        # 每次进入构造独立包装器，不修改共享实例上的运行专属闭包；
        # 本进入最多再尝试 remaining 次（跨重入共享同一逻辑步骤额度）。
        # 零项目层退避（initial_delay/max_delay=0、无 jitter），避免与
        # 底层客户端重试或等待叠加。
        retry = ModelRetryMiddleware(
            max_retries=remaining - 1,
            retry_on=is_transient_model_error,
            on_failure="error",
            initial_delay=0.0,
            max_delay=0.0,
            jitter=False,
        )
        return retry.wrap_model_call(request, attempt)

    # 说明：未定义 awrap_model_call——异步路径不在首版承诺范围（doc 6.2 §7.6）。
