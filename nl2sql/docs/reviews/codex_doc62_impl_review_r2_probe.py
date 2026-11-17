"""Offline review evidence for run cleanup and exact-type regression coverage.

Run from nl2sql with PYTHONPATH=. .venv/bin/python docs/reviews/<this file>.
Mutations are process-local and deliberately simulate an incorrect archive value;
no production files, model requests, or database connections are used.
"""

import gc
import json
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


def check_run_cleanup():
    budget = RunBudgetLedger()
    budget.open_run("cleanup-probe", BudgetLimits(10, 10, 10), time.monotonic() + 60)
    budget.reserve("cleanup-probe", "model_call")
    middleware = BudgetAwareModelRetryMiddleware(budget, ModelRetryConfig(enabled=True))
    context = SimpleNamespace(run_id="cleanup-probe", request_id="cleanup-probe")
    request = SimpleNamespace(runtime=SimpleNamespace(context=context))
    attempts = 0
    references = []

    def handler(_request):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            payload = Payload()  # A request-local object, retained only by its failed frame.
            references.append(weakref.ref(payload))
            raise APIConnectionError(request=httpx.Request("POST", "https://offline.invalid"))
        return ModelResponse(result=[AIMessage(content="recovered")])

    result = middleware.wrap_model_call(request, handler)
    assert result.result[0].content == "recovered" and attempts == 2
    budget.close_run(context.run_id)
    gc.collect()
    retained = context.run_id in middleware._steps
    payload_retained = references[0]() is not None
    middleware._steps.clear()  # Evidence only: releasing the owner releases the traceback.
    gc.collect()
    released = references[0]() is None
    print(json.dumps({
        "closed_run_retained_in_middleware": retained,
        "recovered_error_traceback_retains_payload": payload_retained,
        "released_after_manual_clear": released,
    }))


def check_exact_type_test_strength():
    import src.modules.db_deepagent.runner as runner_module

    original = runner_module.extract_raw_final_answer_payload

    def deliberately_lossy_extraction(record):
        raw = original(record)
        return 0 if raw is False else raw  # Violates the exact-type preservation contract.

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(runner_module, "extract_raw_final_answer_payload", deliberately_lossy_extraction)
        exit_code = pytest.main([
            "src/tests/db_deepagent/unit/test_runner_persistence.py::TestRawPayloadPreservation::test_raw_illegal_payload_full_tail",
            "-k", "payload_false", "-q", "--disable-warnings", "--tb=short",
        ])
    print(json.dumps({"false_to_zero_mutation_test_exit_code": int(exit_code)}))


if __name__ == "__main__":
    check_run_cleanup()
    check_exact_type_test_strength()
