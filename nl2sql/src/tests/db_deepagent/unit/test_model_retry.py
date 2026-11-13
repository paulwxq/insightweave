"""主模型有限重试单元测试（doc 6.2 §7.6/§7.8）

三层验证：
1. 白名单函数 is_transient_model_error 的异常分类；
2. BudgetAwareModelRetryMiddleware + 真实 OpenAI SDK（MockTransport）——
   HTTP 次数口径（单层重试：客户端 max_retries=0）、停止/到期行为；
3. 装配级：真实 create_deep_agent 中间件链上，同一步重试成功、预算按
   步骤计一次、关闭开关不重试。

全部离线：不访问真实模型或数据库。
"""

from __future__ import annotations

import time
import types
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from langchain_core.messages import AIMessage
from openai import OpenAI

from src.modules.db_deepagent.config import ModelRetryConfig
from src.modules.db_deepagent.middleware.model_retry import (
    _STATE_KEY,
    BudgetAwareModelRetryMiddleware,
    is_transient_model_error,
)

pytestmark = pytest.mark.unit


# ============================================================================
# 1. 白名单
# ============================================================================


def _status_error(code: int) -> Exception:
    from openai import APIStatusError

    return APIStatusError(
        message=f"code {code}",
        response=httpx.Response(code, request=httpx.Request("POST", "http://x")),
        body=None,
    )


def _register_add_call():
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": "record_sub_tasks",
                "args": {
                    "expected_plan_version": 0,
                    "operations": [
                        {
                            "op": "add",
                            "sub_task_id": "T1",
                            "content": "查询订单",
                            "dependencies": [],
                        }
                    ],
                },
                "id": "tc-reg1",
                "type": "tool_call",
            }
        ],
    )


def _register_failed_call():
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": "record_sub_tasks",
                "args": {
                    "expected_plan_version": 1,
                    "operations": [
                        {
                            "op": "status",
                            "sub_task_id": "T1",
                            "status": "failed",
                            "reason_code": "agent_abandoned",
                            "reason": "脚本无法推进",
                        }
                    ],
                },
                "id": "tc-reg2",
                "type": "tool_call",
            }
        ],
    )


class TestWhitelist:
    @pytest.mark.parametrize("code", [408, 409, 429, 500, 502, 503, 504], ids=str)
    def test_retryable_status_codes(self, code):
        assert is_transient_model_error(_status_error(code)) is True

    @pytest.mark.parametrize("code", [400, 401, 403, 404, 422], ids=str)
    def test_non_retryable_status_codes(self, code):
        assert is_transient_model_error(_status_error(code)) is False

    def test_connection_and_timeout_retryable(self):
        from openai import APIConnectionError, APITimeoutError

        conn = APIConnectionError(request=httpx.Request("POST", "http://x"))
        assert is_transient_model_error(conn) is True
        # 超时属于 APIConnectionError 子类（doc 6.2 §7.6）
        assert issubclass(APITimeoutError, APIConnectionError)
        timeout = APITimeoutError(request=httpx.Request("POST", "http://x"))
        assert is_transient_model_error(timeout) is True

    def test_rate_limit_retryable(self):
        from openai import RateLimitError

        rl = RateLimitError(
            message="rl",
            response=httpx.Response(429, request=httpx.Request("POST", "http://x")),
            body=None,
        )
        assert is_transient_model_error(rl) is True

    def test_plain_runtime_and_persistence_not_retryable(self):
        import psycopg

        assert is_transient_model_error(RuntimeError("boom")) is False
        assert is_transient_model_error(psycopg.OperationalError("db down")) is False


# ============================================================================
# 2. 中间件 + 真实 OpenAI SDK（MockTransport 计 HTTP 次数）
# ============================================================================


class FakeBudget:
    """记录调用的预算替身（stop_reason/remaining_seconds/stop/counters 对齐真实账本）。"""

    def __init__(self, reason: str | None = None, remaining: float = 1e9):
        self._reason = reason
        self._remaining = remaining
        self._counter = 1  # 当前逻辑步骤序号（model_call 计数）
        self.stop_calls: list[str] = []
        self.stop_reason_calls = 0
        self.remaining_calls = 0

    def counters(self, run_id: str) -> dict[str, int]:
        return {"model_call": self._counter}

    def stop_reason(self, run_id: str) -> str | None:
        self.stop_reason_calls += 1
        return self._reason

    def remaining_seconds(self, run_id: str) -> float:
        self.remaining_calls += 1
        return self._remaining

    def stop(self, run_id: str, reason: str) -> None:
        self.stop_calls.append(reason)
        self._reason = reason


class _Ctx:
    run_id = "run-retry"
    request_id = "req-retry"


class _Req:
    runtime = SimpleNamespace(context=_Ctx())


def _mock_client(handler) -> tuple[OpenAI, dict[str, int]]:
    """真实 openai.OpenAI + httpx.MockTransport；客户端 max_retries=0（单层前提）。"""
    counts = {"http": 0}

    def transport(request: httpx.Request) -> httpx.Response:
        counts["http"] += 1
        return handler(request, counts["http"])

    client = OpenAI(
        api_key="test-key",
        max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(transport)),
    )
    return client, counts


def _ok_response() -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "chatcmpl-test",
            "object": "chat.completion",
            "created": 0,
            "model": "test",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "ok"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        },
    )


def _middleware(reason=None, remaining=1e9, max_retries=2):
    return BudgetAwareModelRetryMiddleware(
        FakeBudget(reason, remaining),
        ModelRetryConfig(enabled=True, max_retries=max_retries),
    )


def _sdk_handler(client: OpenAI) -> Any:
    def handler(_request):
        completion = client.chat.completions.create(
            model="test", messages=[{"role": "user", "content": "q"}]
        )
        # 生产链路中 handler 由框架提供并返回 ModelResponse；此处对齐该契约
        from langchain.agents.middleware.types import ModelResponse

        return ModelResponse(
            result=[AIMessage(content=completion.choices[0].message.content)]
        )

    return handler


class TestMiddlewareHttpCounts:
    def test_consecutive_retryable_errors_max_three_http(self):
        """连续 429：首次 + 两次重试 = 最多三次 HTTP，随后原异常抛出。"""

        def handler(_req, n):
            return httpx.Response(429, text="rate limited", request=_req)

        client, counts = _mock_client(handler)
        mw = _middleware()
        with pytest.raises(Exception) as excinfo:
            mw.wrap_model_call(_Req(), _sdk_handler(client))
        assert counts["http"] == 3  # max_retries=2 单层
        assert "429" in str(excinfo.value) or "rate" in str(excinfo.value).lower()

    def test_third_attempt_recovers(self):
        """前两次 429、第三次成功：三次 HTTP 后恢复。"""

        def handler(_req, n):
            return (
                httpx.Response(429, text="rl", request=_req)
                if n < 3
                else _ok_response()
            )

        client, counts = _mock_client(handler)
        mw = _middleware()
        result = mw.wrap_model_call(_Req(), _sdk_handler(client))
        assert counts["http"] == 3
        assert result.result[0].content == "ok"

    def test_non_retryable_single_http(self):
        """401 不重试：一次 HTTP 后原样抛出。"""

        def handler(_req, n):
            return httpx.Response(401, text="unauthorized", request=_req)

        client, counts = _mock_client(handler)
        mw = _middleware()
        with pytest.raises(Exception):
            mw.wrap_model_call(_Req(), _sdk_handler(client))
        assert counts["http"] == 1

    def test_already_stopped_no_http(self):
        """已有停止原因：返回停止消息，handler 0 次调用。"""
        calls = {"handler": 0}

        def handler(_request):
            calls["handler"] += 1
            raise AssertionError("不应发起模型调用")

        mw = _middleware(reason="tool_set_mismatch")
        result = mw.wrap_model_call(_Req(), handler)
        assert calls["handler"] == 0
        assert "停止条件" in result.result[0].content
        assert isinstance(result.result[0], AIMessage)

    def test_deadline_reached_stops_without_handler(self):
        """到期（remaining<=0）：stop(run_timeout) 后返回停止消息，0 次 handler。"""
        budget = FakeBudget(remaining=-1.0)
        mw = BudgetAwareModelRetryMiddleware(
            budget, ModelRetryConfig(enabled=True, max_retries=2)
        )
        calls = {"handler": 0}

        def handler(_request):
            calls["handler"] += 1
            raise AssertionError("不应发起模型调用")

        result = mw.wrap_model_call(_Req(), handler)
        assert calls["handler"] == 0
        assert budget.stop_calls == ["run_timeout"]
        assert "停止条件" in result.result[0].content

    def test_deadline_after_first_failure_no_further_http(self):
        """首次 429 后到期：不再有第二次 HTTP（协作式停止）。"""
        budget = FakeBudget(remaining=1e9)

        def handler(_req, n):
            if n >= 1:
                budget._remaining = -1.0  # 首个请求后到期
            return httpx.Response(429, text="rl", request=_req)

        client, counts = _mock_client(handler)
        mw = BudgetAwareModelRetryMiddleware(
            budget, ModelRetryConfig(enabled=True, max_retries=2)
        )
        result = mw.wrap_model_call(_Req(), _sdk_handler(client))
        assert counts["http"] == 1  # 到期后未再发请求
        assert "停止条件" in result.result[0].content

    def test_inflight_request_not_cancelled_by_deadline(self):
        """在途请求可晚于时限完成（非硬 deadline）：handler 完成即成功。"""
        budget = FakeBudget(remaining=0.15)

        def handler(_request):
            time.sleep(0.3)  # 超过检查时的剩余时限
            from langchain.agents.middleware.types import ModelResponse

            return ModelResponse(result=[AIMessage(content="late ok")])

        mw = BudgetAwareModelRetryMiddleware(
            budget, ModelRetryConfig(enabled=True, max_retries=2)
        )
        result = mw.wrap_model_call(_Req(), handler)
        assert result.result[0].content == "late ok"

    def test_precheck_runs_before_every_attempt(self):
        """每次尝试前都检查停止/时限：首次失败并置停止后，第二次预检查拦截。"""
        budget = FakeBudget(remaining=1e9)
        attempts = {"n": 0}

        def handler(_request):
            from openai import APIConnectionError

            attempts["n"] += 1
            budget._reason = "run_timeout"  # 第一次尝试后置停止
            raise APIConnectionError(request=httpx.Request("POST", "http://x"))

        mw = BudgetAwareModelRetryMiddleware(
            budget, ModelRetryConfig(enabled=True, max_retries=2)
        )
        result = mw.wrap_model_call(_Req(), handler)
        assert attempts["n"] == 1  # 第二次尝试在预检查被拦，handler 未再执行
        assert "停止条件" in result.result[0].content

    def test_zero_project_level_backoff(self):
        """项目层零退避：两次失败尝试间无 sleep（连续快速重试）。"""
        stamps: list[float] = []

        def handler(_request):
            from langchain.agents.middleware.types import ModelResponse
            from openai import APIConnectionError

            stamps.append(time.monotonic())
            if len(stamps) == 1:
                raise APIConnectionError(request=httpx.Request("POST", "http://x"))
            return ModelResponse(result=[AIMessage(content="ok")])

        mw = _middleware()
        result = mw.wrap_model_call(_Req(), handler)
        assert result.result[0].content == "ok"
        assert len(stamps) == 2
        assert stamps[1] - stamps[0] < 0.5  # 无 SDK 默认指数退避


# ============================================================================
# 3. 装配级：真实 create_deep_agent 中间件链
# ============================================================================


class TestAssemblyRetry:
    def _patched_model(self, monkeypatch, responses):
        """脚本模型：responses 项为异常实例则抛出，否则作为 AIMessage 返回。"""
        from langchain_core.language_models import BaseChatModel
        from langchain_core.outputs import ChatGeneration, ChatResult
        from pydantic import Field

        class ScriptedRaiseModel(BaseChatModel):
            model_name: str = "scripted-retry"
            responses: list = Field(default_factory=list)
            attempts: int = 0

            @property
            def _llm_type(self) -> str:
                return "scripted-retry"

            def bind_tools(self, tools, **kwargs):
                return self

            def _generate(self, messages, stop=None, run_manager=None, **kwargs):
                index = self.attempts
                object.__setattr__(self, "attempts", index + 1)
                item = self.responses[min(index, len(self.responses) - 1)]
                if isinstance(item, BaseException):
                    raise item
                if isinstance(item, str):
                    item = AIMessage(content=item)
                return ChatResult(generations=[ChatGeneration(message=item)])

        return ScriptedRaiseModel(responses=responses)

    def _submit_call(self):
        return AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "submit_final_answer",
                    "args": {
                        "status": "failed",
                        "answer": "脚本提交",
                        "findings": [],
                        "metric_definitions": [],
                        "limitations": [],
                        "open_questions": [],
                        "unresolved_task_refs": [{"sub_task_id": "T1", "revision": 1}],
                    },
                    "id": "tc-sub",
                    "type": "tool_call",
                }
            ],
        )

    _register_add_call = staticmethod(_register_add_call)
    _register_failed_call = staticmethod(_register_failed_call)

    def test_same_step_retry_recovers_within_agent(
        self, assembly_services, monkeypatch
    ):
        """开启重试：首步首次 APIConnectionError 失败，同一步重试提交答案。

        预算按模型步骤计（2 步 = 2 次 model_call），第二次模型调用是同一步
        内的重试，不新增步骤额度；最终拿到有效提交。
        """
        import src.modules.db_deepagent.agent as agent_mod
        from src.modules.db_deepagent.agent import build_db_agent
        from openai import APIConnectionError

        cfg = assembly_services.config.model_copy(deep=True)
        assert cfg.model.retry.enabled is True  # 标准 YAML 显式开启
        services = type(assembly_services)(
            **{**assembly_services.__dict__, "config": cfg}
        )
        model = self._patched_model(
            monkeypatch,
            [
                APIConnectionError(request=httpx.Request("POST", "http://x")),
                self._register_add_call(),
                self._register_failed_call(),
                self._submit_call(),
            ],
        )
        monkeypatch.setattr(agent_mod, "build_model", lambda _cfg: model)

        from src.modules.db_deepagent.context import DbAgentContext
        from src.modules.db_deepagent.services.run_budget import limits_from_config
        from datetime import datetime, timezone

        from langgraph.checkpoint.memory import InMemorySaver

        handle = build_db_agent(
            services, session_id="t-retry", checkpointer=InMemorySaver()
        )
        budget = services.budget
        run_id = "run_retry_asm"
        budget.open_run(
            run_id,
            limits_from_config(cfg.budget),
            deadline_monotonic=time.monotonic() + 60,
        )
        ctx = DbAgentContext(
            session_id="t-retry",
            thread_id="dbda:t-retry:1",
            request_id="req_retry_asm",
            run_id=run_id,
            datasource_id="orders",
            validation_fingerprint="fp",
            now=datetime.now(timezone.utc),
            timezone="UTC",
            user_texts=("问",),
            carried_evidence=(),
            deadline_monotonic=time.monotonic() + 60,
        )
        handle.agent.invoke(
            {"messages": [("user", "问")]},
            config={
                "configurable": {"thread_id": "dbda:t-retry:1"},
                "recursion_limit": 50,
            },
            context=ctx,
        )
        state = handle.agent.get_state(
            {"configurable": {"thread_id": "dbda:t-retry:1"}}
        )
        answers = (state.values or {}).get("db_final_answers") or {}
        assert run_id in answers, "重试后模型在同一步完成有效提交"
        assert model.attempts == 4  # 首步失败+同一步重试成功，再加登记×2+提交
        counters = budget.counters(run_id)
        # 预算按模型步骤计：实际尝试 4 次（1 次失败重试 + 登记两步 + 提交），
        # model_call 只按步骤计 3 次；提交后 FinalAnswerGate 在下一模型步骤
        # 预占前即结束运行，重试不新增步骤额度。
        assert counters.get("model_call", 0) == 3
        assert counters.get("tool_call", 0) == 3

    def test_disabled_no_retry_raises(self, assembly_services, monkeypatch):
        """关闭重试：同一失败原样抛出（不装配项目层包装器）。"""
        import src.modules.db_deepagent.agent as agent_mod
        from src.modules.db_deepagent.agent import build_db_agent
        from openai import APIConnectionError

        cfg = assembly_services.config.model_copy(deep=True)
        cfg.model.retry.enabled = False
        services = type(assembly_services)(
            **{**assembly_services.__dict__, "config": cfg}
        )
        model = self._patched_model(
            monkeypatch,
            [APIConnectionError(request=httpx.Request("POST", "http://x"))],
        )
        monkeypatch.setattr(agent_mod, "build_model", lambda _cfg: model)
        from langgraph.checkpoint.memory import InMemorySaver

        handle = build_db_agent(
            services, session_id="t-noretry", checkpointer=InMemorySaver()
        )
        from src.modules.db_deepagent.context import DbAgentContext
        from datetime import datetime, timezone

        ctx = DbAgentContext(
            session_id="t-noretry",
            thread_id="dbda:t-noretry:1",
            request_id="req_nr",
            run_id="run_nr",
            datasource_id="orders",
            validation_fingerprint="fp",
            now=datetime.now(timezone.utc),
            timezone="UTC",
            user_texts=("问",),
            carried_evidence=(),
            deadline_monotonic=time.monotonic() + 60,
        )
        with pytest.raises(APIConnectionError):
            handle.agent.invoke(
                {"messages": [("user", "问")]},
                config={
                    "configurable": {"thread_id": "dbda:t-noretry:1"},
                    "recursion_limit": 50,
                },
                context=ctx,
            )


# ============================================================================
# R1：尝试额度绑定逻辑模型步骤（含摘要恢复重入）
# ============================================================================


def _status_error(code: int, msg: str = "boom") -> Exception:
    from openai import APIStatusError

    return APIStatusError(
        message=msg,
        response=httpx.Response(code, request=httpx.Request("POST", "http://x")),
        body=None,
    )


class TestStepSharedBudget:
    def test_reentry_same_step_shares_allowance(self):
        """同一步重入（模拟摘要恢复）：两次进入合计不超过 1+max_retries。"""
        from langchain.agents.middleware.types import ModelResponse

        budget = FakeBudget()
        mw = BudgetAwareModelRetryMiddleware(
            budget, ModelRetryConfig(enabled=True, max_retries=2)
        )
        attempts = {"n": 0}

        def fail_401(_request):
            attempts["n"] += 1
            raise _status_error(401)  # 非重试：entry1 消耗 1 次后原样抛出

        with pytest.raises(Exception):
            mw.wrap_model_call(_Req(), fail_401)
        assert attempts["n"] == 1

        # 摘要恢复重入（同一步，counters 未前进）：剩余 2 次
        def retry_then_ok(_request):
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise _status_error(429)
            return ModelResponse(result=[AIMessage(content="ok")])

        result = mw.wrap_model_call(_Req(), retry_then_ok)
        assert result.result[0].content == "ok"
        assert attempts["n"] == 3  # 1（entry1）+ 2（entry2）= 总额 3

    def test_exhausted_reentry_raises_with_original_cause(self):
        """额度用尽后再重入：ModelStepAttemptsExhausted，cause 保留最后原异常。"""
        from src.modules.db_deepagent.middleware.model_retry import (
            ModelStepAttemptsExhausted,
        )

        budget = FakeBudget()
        mw = BudgetAwareModelRetryMiddleware(
            budget, ModelRetryConfig(enabled=True, max_retries=2)
        )

        def always_429(_request):
            raise _status_error(429)

        with pytest.raises(Exception):
            mw.wrap_model_call(_Req(), always_429)  # 3 次耗尽
        with pytest.raises(ModelStepAttemptsExhausted) as ei:
            mw.wrap_model_call(_Req(), always_429)  # 重入即拒
        assert ei.value.__cause__ is not None
        assert "额度已用尽" in str(ei.value)

    def test_new_step_resets_allowance(self):
        """counters 前进（新模型步骤）后额度重置。"""
        budget = FakeBudget()
        mw = BudgetAwareModelRetryMiddleware(
            budget, ModelRetryConfig(enabled=True, max_retries=2)
        )
        attempts = {"n": 0}

        def fail_401(_request):
            attempts["n"] += 1
            raise _status_error(401)

        with pytest.raises(Exception):
            mw.wrap_model_call(_Req(), fail_401)
        budget._counter = 2  # 新步骤（before_model 预占后计数前进）

        def fresh_401s(_request):
            attempts["n"] += 1
            raise _status_error(401)

        with pytest.raises(Exception):
            mw.wrap_model_call(_Req(), fresh_401s)
        # 新步骤仍可重试：401 非重试只耗 1 次，但额度键已重置
        assert attempts["n"] == 2
        # 新步骤内可重试满 3 次（429 场景）
        attempts["n"] = 0

        def always_429(_request):
            attempts["n"] += 1
            raise _status_error(429)

        budget._counter = 3
        with pytest.raises(Exception):
            mw.wrap_model_call(_Req(), always_429)
        assert attempts["n"] == 3

    def test_stop_precedes_budget_exhaustion(self):
        """停止条件优先于额度检查：即使额度已用尽也返回停止消息。"""
        budget = FakeBudget()
        mw = BudgetAwareModelRetryMiddleware(
            budget, ModelRetryConfig(enabled=True, max_retries=0)
        )

        def once_429(_request):
            raise _status_error(429)

        with pytest.raises(Exception):
            mw.wrap_model_call(_Req(), once_429)  # 总额 1 次用尽
        budget._reason = "tool_set_mismatch"
        result = mw.wrap_model_call(_Req(), once_429)  # 不抛 exhausted
        assert "停止条件" in result.result[0].content


class TestSummaryReentryCombo:
    """装配级组合：摘要恢复重入消费同一逻辑步骤额度（真实 SDK 栈）。"""

    def _build(self, assembly_services, monkeypatch, responses):
        import src.modules.db_deepagent.agent as agent_mod
        from src.modules.db_deepagent.agent import build_db_agent
        from langgraph.checkpoint.memory import InMemorySaver

        cfg = assembly_services.config.model_copy(deep=True)
        assert cfg.model.retry.enabled is True
        # 压缩保留窗口，使上下文超限后的摘要恢复路径有可压缩前缀
        # （否则 cutoff=0 直接 ContextOverflowError，无法复现重入）。
        cfg.summarization.recent_context_tokens = 5
        cfg.summarization.trim_tokens_to_summarize = 1
        services = type(assembly_services)(
            **{**assembly_services.__dict__, "config": cfg}
        )
        model = self._scripted(responses)
        monkeypatch.setattr(agent_mod, "build_model", lambda _cfg: model)
        handle = build_db_agent(
            services, session_id="t-combo", checkpointer=InMemorySaver()
        )
        return handle, services, model

    def _scripted(self, responses):
        from langchain_core.language_models import BaseChatModel
        from langchain_core.outputs import ChatGeneration, ChatResult
        from pydantic import Field

        class ScriptedRaiseModel(BaseChatModel):
            model_name: str = "scripted-combo"
            responses: list = Field(default_factory=list)
            attempts: int = 0

            @property
            def _llm_type(self) -> str:
                return "scripted-combo"

            def bind_tools(self, tools, **kwargs):
                return self

            def _generate(self, messages, stop=None, run_manager=None, **kwargs):
                index = self.attempts
                object.__setattr__(self, "attempts", index + 1)
                item = self.responses[min(index, len(self.responses) - 1)]
                if isinstance(item, BaseException):
                    raise item
                return ChatResult(generations=[ChatGeneration(message=item)])

        return ScriptedRaiseModel(responses=responses)

    @staticmethod
    def _ctx_err():
        """上下文超限形态：400 + context_length_exceeded（摘要恢复触发器）。"""
        from openai import BadRequestError

        return BadRequestError(
            message=(
                "This model's maximum context length is 128000 tokens. "
                "However, your messages resulted in 999999 tokens "
                "(context_length_exceeded)"
            ),
            response=httpx.Response(400, request=httpx.Request("POST", "http://x")),
            body=None,
        )

    def _invoke(self, handle, ctx):
        # 摘要恢复需要可压缩历史：单条消息无法降载会直接 ContextOverflowError
        history = []
        for i in range(12):
            history.append(("user", f"历史问题 {i}：请统计 {i} 月订单"))
            history.append(("assistant", f"历史回答 {i}：{i} 月订单共 {i * 10} 单。"))
        history.append(("user", "本轮问题"))
        return handle.agent.invoke(
            {"messages": history},
            config={
                "configurable": {"thread_id": ctx.thread_id},
                "recursion_limit": 50,
            },
            context=ctx,
        )

    def _mk_ctx(self, run_id, services=None):
        from datetime import datetime, timezone

        from src.modules.db_deepagent.context import DbAgentContext
        from src.modules.db_deepagent.services.run_budget import limits_from_config

        if services is not None:
            services.budget.open_run(
                run_id,
                limits_from_config(services.config.budget),
                deadline_monotonic=time.monotonic() + 60,
            )
        return DbAgentContext(
            session_id="t-combo",
            thread_id="dbda:t-combo:1",
            request_id=f"req_{run_id}",
            run_id=run_id,
            datasource_id="orders",
            validation_fingerprint="fp",
            now=datetime.now(timezone.utc),
            timezone="UTC",
            user_texts=("问",),
            carried_evidence=(),
            deadline_monotonic=time.monotonic() + 60,
        )

    def test_reentry_capped_to_step_total(self, assembly_services, monkeypatch):
        """429→429→上下文超限→摘要恢复→429→429→成功：整步最多 3 次主模型尝试。

        R1 修复前同一步出现 6 次；修复后第 4 次前抛 ModelStepAttemptsExhausted。
        """
        from openai import APIConnectionError
        from src.modules.db_deepagent.middleware.model_retry import (
            ModelStepAttemptsExhausted,
        )

        def _conn_err():
            return APIConnectionError(request=httpx.Request("POST", "http://x"))

        handle, services, model = self._build(
            assembly_services,
            monkeypatch,
            [
                _conn_err(),
                _conn_err(),
                self._ctx_err(),
                _conn_err(),
                _conn_err(),
                AIMessage(content="不应到达"),
            ],
        )
        ctx = self._mk_ctx("run_combo_cap", services)
        with pytest.raises(ModelStepAttemptsExhausted):
            self._invoke(handle, ctx)
        assert model.attempts == 3  # 整个逻辑步骤的总额度
        counters = services.budget.counters("run_combo_cap")
        assert counters.get("model_call") == 1  # 仍是同一个模型步骤

    def test_recovery_within_total_allowance(self, assembly_services, monkeypatch):
        """429→上下文超限→摘要恢复→提交成功：总尝试 3 次，运行正常完成。"""
        from openai import APIConnectionError

        conn = APIConnectionError(request=httpx.Request("POST", "http://x"))
        submit = AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "submit_final_answer",
                    "args": {
                        "status": "failed",
                        "answer": "恢复后提交",
                        "findings": [],
                        "metric_definitions": [],
                        "limitations": [],
                        "open_questions": [],
                        "unresolved_task_refs": [{"sub_task_id": "T1", "revision": 1}],
                    },
                    "id": "tc-sub",
                    "type": "tool_call",
                }
            ],
        )
        handle, services, model = self._build(
            assembly_services,
            monkeypatch,
            [
                conn,
                self._ctx_err(),
                _register_add_call(),
                _register_failed_call(),
                submit,
            ],
        )
        ctx = self._mk_ctx("run_combo_ok", services)
        self._invoke(handle, ctx)
        state = handle.agent.get_state({"configurable": {"thread_id": ctx.thread_id}})
        assert ctx.run_id in (state.values.get("db_final_answers") or {})
        # 1(429) + 1(超限) + 恢复后登记×2 + 提交；恢复与重试共享同一逻辑步骤
        assert model.attempts == 5
        assert services.budget.counters(ctx.run_id).get("model_call") == 3

    def test_six_http_with_real_sdk_mocktransport(self, assembly_services, monkeypatch):
        """真实 OpenAI SDK + MockTransport：主模型 HTTP 次数与额度一致（≤3）。

        重现 codex R1 探针场景：此前同一栈会打出 6 次主模型 HTTP。
        """
        import src.modules.db_deepagent.agent as agent_mod
        from src.modules.db_deepagent.agent import build_db_agent
        from langchain_core.language_models import BaseChatModel
        from langchain_core.outputs import ChatGeneration, ChatResult
        from langgraph.checkpoint.memory import InMemorySaver
        from openai import BadRequestError

        counts = {"http": 0}
        seq = ["429", "429", "ctx", "429", "429", "ok"]
        seen = {"n": 0}

        def transport(request: httpx.Request) -> httpx.Response:
            counts["http"] += 1
            item = seq[min(seen["n"], len(seq) - 1)]
            seen["n"] += 1
            if item == "429":
                return httpx.Response(429, text="rate limited", request=request)
            if item == "ctx":
                return httpx.Response(
                    400,
                    json={
                        "error": {
                            "message": (
                                "maximum context length is 128000 tokens "
                                "(context_length_exceeded)"
                            )
                        }
                    },
                    request=request,
                )
            return httpx.Response(
                200,
                json={
                    "id": "c",
                    "object": "chat.completion",
                    "created": 0,
                    "model": "t",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "unused"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 1,
                        "completion_tokens": 1,
                        "total_tokens": 2,
                    },
                },
            )

        client = OpenAI(
            api_key="test",
            max_retries=0,
            http_client=httpx.Client(transport=httpx.MockTransport(transport)),
        )

        class SdkBackedModel(BaseChatModel):
            model_name: str = "sdk-backed"
            attempts: int = 0

            @property
            def _llm_type(self) -> str:
                return "sdk-backed"

            def bind_tools(self, tools, **kwargs):
                return self

            def _generate(self, messages, stop=None, run_manager=None, **kwargs):
                object.__setattr__(self, "attempts", self.attempts + 1)
                if self.attempts == 6:
                    # 第 6 次本不该发生：额度修复后应在此之前收敛
                    raise AssertionError("超过步骤总额度：出现第 6 次主模型请求")
                text = (
                    client.chat.completions.create(
                        model="t", messages=[{"role": "user", "content": "q"}]
                    )
                    .choices[0]
                    .message.content
                )
                return ChatResult(
                    generations=[ChatGeneration(message=AIMessage(content=text))]
                )

        cfg = assembly_services.config.model_copy(deep=True)
        cfg.summarization.recent_context_tokens = 5
        cfg.summarization.trim_tokens_to_summarize = 1
        services = type(assembly_services)(
            **{**assembly_services.__dict__, "config": cfg}
        )
        model = SdkBackedModel()
        monkeypatch.setattr(agent_mod, "build_model", lambda _cfg: model)
        handle = build_db_agent(
            services, session_id="t-sdk", checkpointer=InMemorySaver()
        )
        from src.modules.db_deepagent.middleware.model_retry import (
            ModelStepAttemptsExhausted,
        )

        ctx = self._mk_ctx("run_sdk_cap", services)
        with pytest.raises((ModelStepAttemptsExhausted, BadRequestError)):
            self._invoke(handle, ctx)
        assert counts["http"] <= 3  # 单层重试 + 步骤共享额度
        assert model.attempts == counts["http"]


def _lc_req():
    """run-lc 上下文的请求（状态应入账本运行条目而非本地回退）。"""
    return types.SimpleNamespace(
        runtime=types.SimpleNamespace(context=types.SimpleNamespace(run_id="run-lc"))
    )


class TestRunLifecycleRelease:
    """R2-01：步骤状态随 close_run 释放；成功、失败、额度耗尽三路径全覆盖。"""

    def _ledger_mw(self, max_retries=2):
        from src.modules.db_deepagent.services.run_budget import (
            BudgetLimits,
            RunBudgetLedger,
        )

        budget = RunBudgetLedger()
        limits = BudgetLimits(model_call=50, tool_call=50, db_execute=10)
        budget.open_run("run-lc", limits, deadline_monotonic=time.monotonic() + 60)
        mw = BudgetAwareModelRetryMiddleware(
            budget, ModelRetryConfig(enabled=True, max_retries=max_retries)
        )
        return budget, mw

    class _Payload:
        """可弱引用的哨兵对象（object() 不支持 weakref）。"""

    @staticmethod
    def _payload_error(payload):
        """抛出引用 payload 的异常：异常回溯帧持有 payload 局部引用。"""
        from openai import APIConnectionError

        def handler(_request):
            sentinel = payload  # 回溯帧保留该局部，异常链持有回溯
            raise APIConnectionError(
                request=httpx.Request("POST", "http://x"),
                message=f"boom {id(sentinel)}",
            )

        return handler

    def test_recovery_success_releases_traceback_promptly(self):
        """首试失败、第二次成功：成功即清 last_error（不等 close_run）。"""
        import gc
        import weakref

        from langchain.agents.middleware.types import ModelResponse
        from openai import APIConnectionError

        budget, mw = self._ledger_mw()
        box = {"payload": self._Payload()}
        ref = weakref.ref(box["payload"])
        attempts = {"n": 0}

        def handler(_request):
            attempts["n"] += 1
            if attempts["n"] == 1:
                sentinel = box["payload"]
                raise APIConnectionError(
                    request=httpx.Request("POST", "http://x"),
                    message=f"boom {id(sentinel)}",
                )
            return ModelResponse(result=[AIMessage(content="ok")])

        result = mw.wrap_model_call(_lc_req(), handler)
        assert result.result[0].content == "ok"
        state = budget.run_shared_state("run-lc")[_STATE_KEY]
        assert state["last_error"] is None  # 成功即释放
        box.clear()  # 丢弃闭包外的强引用；last_error 已清，回溯应可回收
        gc.collect()
        assert ref() is None  # 失败回溯不因成功而滞留
        budget.close_run("run-lc")

    def test_exhausted_run_state_released_on_close(self):
        """重试耗尽：last_error 保留至 close_run，关闭后随条目释放并可 GC。"""
        import gc
        import weakref

        budget, mw = self._ledger_mw()
        payload = self._Payload()
        ref = weakref.ref(payload)

        with pytest.raises(Exception):
            mw.wrap_model_call(_lc_req(), self._payload_error(payload))
        state = budget.run_shared_state("run-lc")[_STATE_KEY]
        assert state["last_error"] is not None  # 运行中保留 cause
        del payload
        gc.collect()
        assert ref() is not None  # 运行未结束：允许持有（供额度耗尽 cause）
        budget.close_run("run-lc")
        assert budget.run_shared_state("run-lc") is None  # 条目移除
        del state  # 测试自身不再引用状态字典（含 last_error 异常链）
        gc.collect()
        assert ref() is None  # 异常回溯及引用对象随运行结束释放

    def test_step_exhausted_exception_also_released_on_close(self):
        """上下文恢复额度耗尽路径：同样随 close_run 释放。"""
        import gc
        import weakref

        from src.modules.db_deepagent.middleware.model_retry import (
            ModelStepAttemptsExhausted,
        )

        budget, mw = self._ledger_mw()
        payload = self._Payload()
        ref = weakref.ref(payload)

        with pytest.raises(Exception):
            mw.wrap_model_call(_lc_req(), self._payload_error(payload))
        with pytest.raises(ModelStepAttemptsExhausted):
            mw.wrap_model_call(_lc_req(), self._payload_error(payload))  # 重入即拒
        budget.close_run("run-lc")
        del payload
        gc.collect()
        assert budget.run_shared_state("run-lc") is None
        assert ref() is None

    def test_sequential_runs_independent_states(self):
        """同账本连续两运行（CLI 复用中间件实例）：互不串额度，各自释放。"""
        from langchain.agents.middleware.types import ModelResponse

        budget, mw = self._ledger_mw()
        from src.modules.db_deepagent.services.run_budget import BudgetLimits

        budget.open_run(
            "run-lc2",
            BudgetLimits(model_call=50, tool_call=50, db_execute=10),
            deadline_monotonic=time.monotonic() + 60,
        )
        # run1：连接异常重试耗尽（R3-01 修正：用 run-lc 已注册上下文，
        # 状态须入账本运行条目而非本地回退）
        with pytest.raises(Exception):
            mw.wrap_model_call(_lc_req(), self._payload_error(self._Payload()))
        assert budget.run_shared_state("run-lc")[_STATE_KEY]["used"] == 3
        assert not mw._local_steps  # 已注册运行不得走本地回退
        budget.close_run("run-lc")
        # run2：全新额度，可重试恢复
        attempts = {"n": 0}

        def flaky_then_ok(_request):
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise _status_error(429)
            return ModelResponse(result=[AIMessage(content="ok2")])

        ctx2 = types.SimpleNamespace(
            runtime=types.SimpleNamespace(
                context=types.SimpleNamespace(run_id="run-lc2")
            )
        )
        # run2 已注册账本 → 状态入运行条目（新运行全新存储 → 额度重置）
        result = mw.wrap_model_call(ctx2, flaky_then_ok)
        assert result.result[0].content == "ok2"
        assert attempts["n"] == 3
        assert budget.run_shared_state("run-lc") is None  # 旧运行已释放
        assert budget.run_shared_state("run-lc2") is not None
        budget.close_run("run-lc2")
        assert budget.run_shared_state("run-lc2") is None


class TestRetryNoSqlReexecution:
    """K1+K2（Kimi R1）：重试恢复不重复执行 SQL；步骤/尝试两种计数口径固定。

    真实装配（build_db_agent + 假后端）+ 完整 DbAgentRunner：
    模型步骤 1 校验 SQL → 步骤 2 执行 SQL → 步骤 3 首试可重试异常、
    同一步重试后提交最终答案。SQL 全程只执行一次，预算按步骤计 3，
    观察器按实际尝试计 4。
    """

    SQL = "SELECT id, status FROM public.orders"

    @staticmethod
    def _tool_call(name, args, call_id):
        return AIMessage(
            content="",
            tool_calls=[
                {"name": name, "args": args, "id": call_id, "type": "tool_call"}
            ],
        )

    def test_sql_once_and_observer_counts_across_retry(
        self, assembly_services, monkeypatch, tmp_path
    ):
        import src.modules.db_deepagent.agent as agent_mod
        from src.modules.db_deepagent.agent import build_db_agent
        from langchain_core.language_models import BaseChatModel
        from langchain_core.outputs import ChatGeneration, ChatResult
        from langgraph.checkpoint.memory import InMemorySaver
        from openai import APIConnectionError
        from pydantic import Field

        from src.modules.db_deepagent.persistence.archive import QaArchive
        from src.modules.db_deepagent.runner import DbAgentRunner
        from src.tests.db_deepagent.conftest import FakeStore

        class ScriptedFlowModel(BaseChatModel):
            model_name: str = "scripted-flow"
            responses: list = Field(default_factory=list)
            attempts: int = 0

            @property
            def _llm_type(self) -> str:
                return "scripted-flow"

            def bind_tools(self, tools, **kwargs):
                return self

            def _generate(self, messages, stop=None, run_manager=None, **kwargs):
                index = self.attempts
                object.__setattr__(self, "attempts", index + 1)
                item = self.responses[min(index, len(self.responses) - 1)]
                if isinstance(item, BaseException):
                    raise item
                return ChatResult(generations=[ChatGeneration(message=item)])

        import hashlib as _hashlib

        from src.tests.db_deepagent.conftest import FakeExecDb

        cfg = assembly_services.config.model_copy(deep=True)
        cfg.workspace.root = str(tmp_path)
        # 空结果库：no_data 需全部主成果为未截断零行（doc 6.3 §7.4）
        zero_db = FakeExecDb(rows=[])
        zero_db.datasource_id = assembly_services.business_db.datasource_id
        services = type(assembly_services)(
            **{
                **assembly_services.__dict__,
                "config": cfg,
                "business_db": zero_db,
                "pipeline": type(assembly_services.pipeline)(
                    config=cfg,
                    validator=type(assembly_services.validator)(
                        cfg.validation,
                        zero_db,
                        explain_timeout_s=cfg.business_db.explain_timeout_seconds,
                    ),
                    guard=type(assembly_services.guard)(
                        cfg.validation.guard, zero_db, assembly_services.probe
                    ),
                    db=zero_db,
                    results=assembly_services.results,
                    budget=assembly_services.budget,
                ),
            }
        )
        # doc 6.3 §11 五步最短脚本：登记→校验→执行→终态→提交
        evidence_id = "E-" + _hashlib.sha256(b"tc-exe:query").hexdigest()[:8]
        model = ScriptedFlowModel(
            responses=[
                self._tool_call(
                    "record_sub_tasks",
                    {
                        "expected_plan_version": 0,
                        "operations": [
                            {
                                "op": "add",
                                "sub_task_id": "T1",
                                "content": "查询订单",
                                "dependencies": [],
                            }
                        ],
                    },
                    "tc-reg",
                ),
                self._tool_call(
                    "validate_sql",
                    {"sql": self.SQL, "sub_task_id": "T1", "sub_task_revision": 1},
                    "tc-val",
                ),
                self._tool_call(
                    "execute_sql",
                    {"sql": self.SQL, "sub_task_id": "T1", "sub_task_revision": 1},
                    "tc-exe",
                ),
                self._tool_call(
                    "record_sub_tasks",
                    {
                        "expected_plan_version": 1,
                        "operations": [
                            {
                                "op": "status",
                                "sub_task_id": "T1",
                                "status": "completed",
                                "evidence_ids": [evidence_id],
                            }
                        ],
                    },
                    "tc-fin",
                ),
                APIConnectionError(request=httpx.Request("POST", "http://x")),
                self._tool_call(
                    "submit_final_answer",
                    {
                        "status": "no_data",
                        "answer": "查询完成：没有符合条件的记录。",
                        "findings": [
                            {
                                "text": "查询无记录",
                                "finding_id": "F-1",
                                "task_refs": [{"sub_task_id": "T1", "revision": 1}],
                                "evidence_ids": [evidence_id],
                            }
                        ],
                        "metric_definitions": [],
                        "limitations": [],
                        "open_questions": [],
                    },
                    "tc-sub",
                ),
            ]
        )
        monkeypatch.setattr(agent_mod, "build_model", lambda _cfg: model)
        handle = build_db_agent(
            services, session_id="t-k12", checkpointer=InMemorySaver()
        )
        store = FakeStore()
        runner = DbAgentRunner(
            services=services, handle=handle, archive=QaArchive(store), fixed_now=None
        )
        result = runner.run("查订单", "t-k12", request_id="req-k12")

        # K1：SQL 恰好执行一次；重试恢复没有重跑 Agent 或 SQL
        business_db = services.business_db
        assert len(business_db.executed) == 1, "重试不得重复执行 SQL"
        assert business_db.executed[0][0] == self.SQL
        assert len(result.evidence) == 1, "执行证据不得因重试重复"
        assert result.status == "no_data"
        assert result.answer.startswith("查询完成：没有符合条件的记录。")
        # K1：五步脚本 + 1 次失败重试 = 6 次实际尝试（doc 6.3 §11）
        assert model.attempts == 6
        # K2：两种计数口径——预算按模型步骤计 5，观察器按实际尝试计 6
        assert result.stats.model_calls == 5
        assert result.stats.total_model_calls == 6
        assert result.stats.summary_model_calls == 0
        # 归档：真实执行按条件 a 进 qa，来源为模型提交
        qa = [v for k, v in store.items.items() if k[0][1] == "qa"]
        assert qa and qa[0]["answer_source"] == "model"
