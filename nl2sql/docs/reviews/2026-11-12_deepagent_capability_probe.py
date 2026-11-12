"""Offline review probes; run with PYTHONPATH=. from the nl2sql directory.

These characterize current behavior, including gaps. They are not acceptance
tests for the proposed improvements. No model or database is contacted.
"""

from dataclasses import replace
import hashlib
import json
from pathlib import Path
import threading

from langchain_core.tools import tool
from langgraph.prebuilt import ToolNode

from src.modules.db_deepagent.runner import DbAgentRunner, _RunFailures
from src.modules.db_deepagent.services.run_budget import BudgetSnapshot
from src.modules.db_deepagent.services.time_resolver import TimeResolver
from src.modules.db_deepagent.tools._envelope import serialize_tool_output
from src.modules.db_deepagent.tools.final_answer import make_submit_final_answer_tool
from src.tests.db_deepagent.conftest import db_agent_context, invoke_tool_node
from src.tests.db_deepagent.unit.test_final_answer import EXEC, _args


def emit(name, **fields):
    print(json.dumps({"probe": name, **fields}, ensure_ascii=False))


def main():
    from importlib.metadata import version

    root = Path(__file__).resolve().parents[2]
    source_paths = [
        "src/modules/db_deepagent/agent.py",
        "src/modules/db_deepagent/runner.py",
        "src/modules/db_deepagent/state.py",
        "src/modules/db_deepagent/tools/_envelope.py",
        "src/modules/db_deepagent/tools/final_answer.py",
        "src/modules/db_deepagent/services/sql_pipeline.py",
        "src/modules/db_deepagent/services/time_resolver.py",
    ]
    emit(
        "baseline",
        versions={p: version(p) for p in ("deepagents", "langchain", "langgraph")},
        source_sha256={p: hashlib.sha256((root / p).read_bytes()).hexdigest() for p in source_paths},
    )
    ctx = db_agent_context.__wrapped__()

    # The former 12000-character envelope cap no longer exists.
    payload = {
        "status": "ok",
        "data": {"tables": [{"columns": [
            {"name": f"c{i}", "comment": "列说明" * 80} for i in range(300)
        ]}]},
        "warnings": [],
    }
    encoded = serialize_tool_output(payload)
    assert len(encoded) > 12000 and json.loads(encoded) == payload
    emit("large_json", chars=len(encoded), json_valid=True, columns_preserved=300)

    # Evidence validity is enforced; coverage of a plan is not.
    # Two execution records, only one cited, no registered task state.
    updates, _ = invoke_tool_node(
        ToolNode([make_submit_final_answer_tool()]),
        [("submit_final_answer", _args(), "review-submit")],
        ctx,
        extra_state={"db_executions": [EXEC, {**EXEC, "evidence_id": "E-second"}]},
    )
    record = updates["db_final_answers"][ctx.run_id]
    assert record["answer"]["status"] == "completed"
    emit("completion_without_task_ledger", executions=2, cited=1, accepted_status="completed")

    # Call the real evaluator using real EvidenceItem projection.
    # The existing full runner injection tests cover the P1 -> P2 -> P3 path.
    runner = DbAgentRunner.__new__(DbAgentRunner)
    values = {"db_executions": [EXEC]}
    evidence = runner._assemble_evidence(values, ctx.run_id)
    failures = _RunFailures()
    failures.add("runtime", "P1", "offline simulated model error after SQL")
    status, _, answer, _ = runner._evaluate(
        failures, BudgetSnapshot(), values, None, evidence, []
    )
    assert status == "failed" and answer == "" and len(evidence) == 1
    emit("sql_success_model_failure", status=status, answer=answer, evidence_count=len(evidence))

    resolver = TimeResolver()
    for text, relative, n in [
        ("前天", "yesterday", None),
        ("最近三周", "last_n_days", 21),
        ("最近三个月", "last_n_days", 90),
        ("最近一年", "last_n_days", 365),
    ]:
        outcome = resolver.resolve(
            replace(ctx, user_texts=(text,)), source_text=text,
            kind="relative", relative=relative, n=n,
        )
        assert not outcome.ok
        emit("time_expression", text=text, accepted=outcome.ok, error=outcome.error_code)

    # A barrier proves overlapping ToolNode calls without timing heuristics.
    barrier = threading.Barrier(2, timeout=5)
    lock = threading.Lock()
    activity = {"active": 0, "peak": 0}

    @tool
    def concurrent_probe(value: int) -> str:
        """Offline concurrency probe with a two-party barrier."""
        with lock:
            activity["active"] += 1
            activity["peak"] = max(activity["peak"], activity["active"])
        try:
            barrier.wait()
            return str(value)
        finally:
            with lock:
                activity["active"] -= 1

    invoke_tool_node(
        ToolNode([concurrent_probe]),
        [("concurrent_probe", {"value": 1}, "review-par-1"),
         ("concurrent_probe", {"value": 2}, "review-par-2")],
        ctx,
    )
    assert activity["peak"] == 2
    emit("tool_node_concurrency", peak_active_calls=activity["peak"])


if __name__ == "__main__":
    main()
