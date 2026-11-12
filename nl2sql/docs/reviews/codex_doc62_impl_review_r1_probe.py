"""doc 6.2 implementation review R1: offline evidence, not a passing regression suite.

Run from nl2sql: PYTHONPATH=. .venv/bin/python docs/reviews/codex_doc62_impl_review_r1_probe.py
Exercises production build_db_agent with fake services/models, plus the real
OpenAI SDK with MockTransport. No production files are patched on disk. This
captures the pre-fix six-request behavior; adjust into regression tests when fixed.
"""

import copy
import json
import logging
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from openai import APIConnectionError, BadRequestError

import src.modules.db_deepagent.agent as agent_mod
from src.modules.db_deepagent.context import DbAgentContext
from src.modules.db_deepagent.services.run_budget import BudgetLimits
from src.tests.db_deepagent.conftest import assembly_services, db_agent_config, FakeAgent, FakeStore
from src.tests.db_deepagent.unit.test_model_retry import TestAssemblyRetry
from src.tests.db_deepagent.unit.test_model_retry import _mock_client, _ok_response
from src.tests.db_deepagent.unit.test_runner_persistence import ANSWER, _exec, _runner

logging.disable(logging.CRITICAL)
cfg = db_agent_config.__wrapped__()
with tempfile.TemporaryDirectory(prefix="doc62-impl-r1-") as directory:
    with pytest.MonkeyPatch.context() as mp:
        services = assembly_services.__wrapped__(cfg, Path(directory), mp)
        cfg = services.config.model_copy(deep=True)
        cfg.summarization.summary_trigger_tokens = 999_999
        cfg.summarization.recent_context_tokens = 50
        services.config = cfg
        request = httpx.Request("POST", "https://offline.invalid")
        transient = lambda: APIConnectionError(request=request)
        context_error = BadRequestError(
            "context_length_exceeded",
            response=httpx.Response(400, request=request),
            body={"error": {"code": "context_length_exceeded"}},
        )
        helper = TestAssemblyRetry()
        model = helper._patched_model(mp, [
            transient(), transient(), context_error,
            transient(), transient(), helper._submit_call(),
        ])
        mp.setattr(agent_mod, "build_model", lambda _: model)
        handle = agent_mod.build_db_agent(services, session_id="r1-overflow", checkpointer=InMemorySaver())
        deadline = time.monotonic() + 60
        services.budget.open_run("r1-overflow", BudgetLimits(1, 10, 10), deadline)
        ctx = DbAgentContext(
            session_id="r1-overflow", thread_id="dbda:r1-overflow:1",
            request_id="r1-overflow", run_id="r1-overflow", datasource_id="orders",
            validation_fingerprint="fp", now=datetime.now(timezone.utc), timezone="UTC",
            user_texts=("问",), carried_evidence=(), deadline_monotonic=deadline,
        )
        handle.agent.invoke({"messages": [
            HumanMessage(content="旧问题 " * 1000),
            AIMessage(content="旧回复 " * 1000),
            HumanMessage(content="现在的问题"),
        ]}, config={"configurable": {"thread_id": ctx.thread_id}, "recursion_limit": 50}, context=ctx)
        print(json.dumps({"primary_attempts": model.attempts, "budget": services.budget.counters(ctx.run_id)}, ensure_ascii=False))

        # Same assembled graph path, backed by real OpenAI SDK with internal
        # retries zero and MockTransport. Count actual primary HTTP requests.
        def transport(req, index):
            if index in (1, 2, 4, 5):
                return httpx.Response(429, json={"error": {"message": "rate limited"}}, request=req)
            if index == 3:
                return httpx.Response(400, json={"error": {"message": "context_length_exceeded", "code": "context_length_exceeded"}}, request=req)
            return _ok_response()

        client, counts = _mock_client(transport)
        assert client.max_retries == 0

        def sdk_generate(self, messages, stop=None, run_manager=None, **kwargs):
            from langchain_core.outputs import ChatGeneration, ChatResult
            object.__setattr__(self, "attempts", self.attempts + 1)
            client.chat.completions.create(model="offline", messages=[{"role": "user", "content": "offline"}])
            return ChatResult(generations=[ChatGeneration(message=helper._submit_call())])

        mp.setattr(type(model), "_generate", sdk_generate)
        object.__setattr__(model, "attempts", 0)
        handle = agent_mod.build_db_agent(services, session_id="r1-http", checkpointer=InMemorySaver())
        ctx = ctx.__class__(**{**ctx.__dict__, "session_id": "r1-http", "thread_id": "dbda:r1-http:1", "run_id": "r1-http", "request_id": "r1-http"})
        services.budget.open_run(ctx.run_id, BudgetLimits(1, 10, 10), deadline)
        handle.agent.invoke({"messages": [HumanMessage(content="旧问题 " * 1000), AIMessage(content="旧回复 " * 1000), HumanMessage(content="现在的问题")]}, config={"configurable": {"thread_id": ctx.thread_id}, "recursion_limit": 50}, context=ctx)
        print(json.dumps({"sdk_internal_max_retries": client.max_retries, "actual_primary_http_requests": counts["http"], "budget": services.budget.counters(ctx.run_id)}, ensure_ascii=False))
        client.close()

        # Whole payload values missing from the submitted persistent test matrix.
        raw_cases = 0
        for reused in (False, True):
            for index, raw in enumerate((None, "", [], "not-a-dict", ["not-a-dict"], False, 0, {**ANSWER, "findings": {"bad": "shape"}})):
                state = {"messages": [], "db_executions": [_exec(reused=reused)], "db_analyses": [],
                         "db_final_answers": {"run-x": {"run_id": "run-x", "answer": copy.deepcopy(raw)}}}
                store = FakeStore()
                runner = _runner(services, FakeAgent(state), store, state)
                result = runner.run("问", f"raw-{int(reused)}-{index}", request_id=f"raw-{index}")
                records = [(key[0][1], record) for key, record in store.items.items() if key[0][1] in ("qa", "run_errors")]
                assert result.status == "failed" and result.answer.startswith("系统兜底说明："), result
                assert all(error.phase != "P3" for error in result.errors), result.errors
                kind, record = records[0]
                assert kind == ("run_errors" if reused else "qa")
                assert type(record["answer"]) is type(raw) and record["answer"] == raw
                raw_cases += 1
        print(json.dumps({"raw_payload_probe_cases": raw_cases, "passed": True}))
