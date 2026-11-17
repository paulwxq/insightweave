"""Final-round offline evidence; process-local test instrumentation only.

Run from nl2sql: PYTHONPATH=. .venv/bin/python docs/reviews/codex_doc62_impl_review_r3_probe.py
No production files are patched, and no model or database is contacted.
"""

import gc
import json
from pathlib import Path
import runpy
import time
from types import SimpleNamespace
import weakref

import httpx
import pytest
from langchain.agents.middleware.types import ModelResponse
from langchain_core.messages import AIMessage
from openai import APIConnectionError

from src.modules.db_deepagent.config import ModelRetryConfig
from src.modules.db_deepagent.middleware.model_retry import BudgetAwareModelRetryMiddleware
from src.modules.db_deepagent.services.run_budget import BudgetLimits, RunBudgetLedger


class Payload:
    pass


def request_for(run_id):
    return SimpleNamespace(runtime=SimpleNamespace(context=SimpleNamespace(run_id=run_id)))


def verify_registered_sequential_runs():
    budget = RunBudgetLedger()
    middleware = BudgetAwareModelRetryMiddleware(budget, ModelRetryConfig(enabled=True))
    references = []

    def failing_handler(_request):
        payload = Payload()
        references.append(weakref.ref(payload))
        raise APIConnectionError(request=httpx.Request("POST", "https://offline.invalid"))

    budget.open_run("registered-one", BudgetLimits(10, 10, 10), time.monotonic() + 60)
    budget.reserve("registered-one", "model_call")
    try:
        middleware.wrap_model_call(request_for("registered-one"), failing_handler)
    except APIConnectionError:
        pass
    else:
        raise AssertionError("the scripted failures must propagate")
    assert budget.run_shared_state("registered-one")["model_retry_step"]["used"] == 3
    assert middleware._local_steps == {}
    budget.close_run("registered-one")
    gc.collect()
    assert all(ref() is None for ref in references)
    assert budget.run_shared_state("registered-one") is None

    budget.open_run("registered-two", BudgetLimits(10, 10, 10), time.monotonic() + 60)
    budget.reserve("registered-two", "model_call")
    calls = 0

    def recovering_handler(_request):
        nonlocal calls
        calls += 1
        if calls < 3:
            payload = Payload()
            references.append(weakref.ref(payload))
            raise APIConnectionError(request=httpx.Request("POST", "https://offline.invalid"))
        return ModelResponse(result=[AIMessage(content="recovered")])

    result = middleware.wrap_model_call(request_for("registered-two"), recovering_handler)
    assert result.result[0].content == "recovered" and calls == 3
    assert budget.run_shared_state("registered-two")["model_retry_step"]["last_error"] is None
    gc.collect()
    assert all(ref() is None for ref in references)
    budget.close_run("registered-two")
    assert budget.run_shared_state("registered-two") is None
    assert middleware._local_steps == {}
    print(json.dumps({"two_registered_runs_independent": True, "tracebacks_released": True, "local_fallback_unused": True}))


def audit_submitted_sequential_test():
    instances = []
    original = BudgetAwareModelRetryMiddleware.__init__

    def capture(self, *args, **kwargs):
        original(self, *args, **kwargs)
        instances.append(self)

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(BudgetAwareModelRetryMiddleware, "__init__", capture)
        code = pytest.main([
            "src/tests/db_deepagent/unit/test_model_retry.py::TestRunLifecycleRelease::test_sequential_runs_independent_states",
            "-q", "--disable-warnings", "--tb=short",
        ])
    assert len(instances) == 1
    keys = list(instances[0]._local_steps)
    print(json.dumps({"submitted_sequential_test_exit_code": int(code), "leftover_unregistered_run_ids": keys}))


if __name__ == "__main__":
    verify_registered_sequential_runs()
    audit_submitted_sequential_test()
    # R2's intentional False -> 0 mutation must now FAIL both preservation tests.
    helper = runpy.run_path(str(Path(__file__).with_name("codex_doc62_impl_review_r2_probe.py")))
    helper["check_exact_type_test_strength"]()
