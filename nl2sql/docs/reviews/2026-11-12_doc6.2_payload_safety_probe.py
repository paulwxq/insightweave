"""Check doc 6.2 proposals on in-memory classes; never edit production code.

Run from nl2sql with PYTHONPATH=. .venv/bin/python docs/reviews/<this file>.
All services/model responses are fakes. No network or database is contacted.
"""

import ast
import asyncio
import copy
from importlib.metadata import version as package_version
import json
import logging
from pathlib import Path
import re
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import src.modules.db_deepagent.config as config_module
import src.modules.db_deepagent.persistence.archive as archive_module
import src.modules.db_deepagent.runner as runner_module
import src.modules.db_deepagent.schemas as schemas
from src.modules.db_deepagent.agent import DbAgentHandle
from src.tests.db_deepagent.conftest import (
    FakeAgent,
    FakeStore,
    assembly_services,
    db_agent_config,
)
from src.tests.db_deepagent.unit.test_runner_persistence import ANSWER, _exec


def compile_nodes(nodes, namespace):
    module = ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[]))
    exec(compile(module, "doc62-memory-proposal", "exec"), namespace)


def main():
    logging.disable(logging.CRITICAL)
    root = Path(__file__).resolve().parents[2]
    document = root / "docs/6.2_db_deepagent最终答案兜底问题与补齐建议.md"
    blocks = re.findall(r"```python\n(.*?)```", document.read_text(), re.S)
    for index, block in enumerate(blocks, 1):
        compile(block, f"doc62-snippet-{index}", "exec")

    # Install the proposed field validator only in a new class namespace.
    validation = next(b for b in blocks if "def has_nonblank_answer" in b)
    schema_ns = dict(vars(schemas))
    exec(validation, schema_ns)
    schema_tree = ast.parse((root / "src/modules/db_deepagent/schemas.py").read_text())
    answer_class = copy.deepcopy(next(
        n for n in schema_tree.body
        if isinstance(n, ast.ClassDef) and n.name == "DbAgentFinalAnswer"
    ))
    answer_class.body.append(copy.deepcopy(next(
        n for n in ast.parse(validation).body
        if isinstance(n, ast.FunctionDef) and n.name == "_require_nonblank_answer"
    )))
    compile_nodes([answer_class], schema_ns)
    extract = schema_ns["extract_raw_final_answer_payload"]
    for raw in (None, "", [], False, 0):
        assert extract({"answer": raw}) is raw
    assert extract({}) == {}

    tree = ast.parse((root / "src/modules/db_deepagent/runner.py").read_text())
    runner_class = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "DbAgentRunner")
    methods = {n.name: n for n in runner_class.body if isinstance(n, ast.FunctionDef)}
    env = dict(vars(runner_module))
    for name in ("normalize_final_answer_record", "extract_raw_final_answer_payload"):
        env[name] = schema_ns[name]
    for name in ("_known_saved_row_counts", "_build_fallback_answer"):
        exec(next(b for b in blocks if f"def {name}" in b), env)

    # Keep the actual evidence loops, adding the shared map parameter and
    # replacing both row_count projections as specified in the document.
    assemble = copy.deepcopy(methods["_assemble_evidence"])
    signature_block = next(b for b in blocks if "def _assemble_evidence(" in b)
    assemble.args = ast.parse(signature_block).body[0].args
    for node in ast.walk(assemble):
        if isinstance(node, ast.keyword) and node.arg == "row_count":
            node.value = ast.parse('known_row_counts.get(record["evidence_id"]) or 0', mode="eval").body

    # Apply the documented evaluator prelude and invalid-submission branch.
    evaluate = copy.deepcopy(methods["_evaluate"])
    next(arg for arg in evaluate.args.args if arg.arg == "answer_record").arg = "normalized_submission"
    prefix = next(b for b in blocks if "valid_submission = normalized_submission is not None" in b and "cited_reused" not in b)
    position = next(i for i, n in enumerate(evaluate.body) if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "payload" for t in n.targets))
    evaluate.body[position:position + 1] = ast.parse(prefix).body

    class ValidityBranch(ast.NodeTransformer):
        def visit_If(self, node):
            self.generic_visit(node)
            if ast.unparse(node.test) == "answer_record is None":
                node.test = ast.parse("not valid_submission", mode="eval").body
            elif ast.unparse(node.test) == "answer_record is not None":
                node.test = ast.Name(id="valid_submission", ctx=ast.Load())
            return node

    evaluate = ValidityBranch().visit(evaluate)

    # Preserve the real finalizer/P3/DbAgentResult path, replacing only the
    # documented delivery prelude, payload source and archive arguments.
    finalize = copy.deepcopy(methods["_finalize"])
    finalize.args.args.append(ast.arg(arg="invoke_started", annotation=ast.Name(id="bool", ctx=ast.Load())))
    finalize.args.defaults.append(ast.Constant(value=False))
    delivery = next(b for b in blocks if "delivery_payload = {" in b and "self._evaluate(" in b)
    start = next(i for i, n in enumerate(finalize.body) if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "evidence" for t in n.targets))
    position = next(i for i, n in enumerate(finalize.body) if isinstance(n, ast.Assign) and isinstance(n.value, ast.Call) and isinstance(n.value.func, ast.Attribute) and n.value.func.attr == "_evaluate")
    finalize.body[start:position + 1] = ast.parse(delivery).body
    for node in ast.walk(finalize):
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "answer_payload" for t in node.targets):
            node.value = ast.Name(id="submitted_payload", ctx=ast.Load())
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "_archive_run":
            node.keywords.extend([
                ast.keyword(arg="normalized_submission", value=ast.Name(id="normalized_submission", ctx=ast.Load())),
                ast.keyword(arg="delivery_payload", value=ast.Name(id="delivery_payload", ctx=ast.Load())),
                ast.keyword(arg="answer_source", value=ast.Name(id="answer_source", ctx=ast.Load())),
            ])

    locked = copy.deepcopy(methods["_run_locked"])
    for node in ast.walk(locked):
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "result" for t in node.targets) and isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Attribute) and node.value.func.attr == "_finalize":
            node.value.keywords.append(ast.keyword(arg="invoke_started", value=ast.Name(id="invoke_started", ctx=ast.Load())))

    archive = copy.deepcopy(methods["_archive_run"])
    archive.args.kwonlyargs.extend([
        ast.arg(arg="normalized_submission"), ast.arg(arg="delivery_payload"), ast.arg(arg="answer_source"),
    ])
    archive.args.kw_defaults.extend([None, None, None])
    real_execution = copy.deepcopy(next(n for n in archive.body if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "has_real_execution" for t in n.targets)))
    archive_block = next(b for b in blocks if "cited_reused = any(" in b)
    archive.body = [real_execution, *ast.parse(archive_block).body, *copy.deepcopy(methods["_archive_run"].body[-2:])]

    compile_nodes([assemble, evaluate, finalize, locked, archive], env)
    candidate = type("Doc62Runner", (runner_module.DbAgentRunner,), {
        name: env[name] for name in (
            "_assemble_evidence", "_evaluate", "_finalize", "_run_locked", "_archive_run",
            "_known_saved_row_counts", "_build_fallback_answer",
        )
    })

    # Inspect the real boundary calls without replacing their implementation.
    original_evaluate = candidate._evaluate
    original_archive = candidate._archive_run
    original_counts = candidate._known_saved_row_counts
    original_assemble = candidate._assemble_evidence
    original_fallback = candidate._build_fallback_answer

    def observed_counts(self, values, run_id):
        self.doc62_row_count_scans = getattr(self, "doc62_row_count_scans", 0) + 1
        result = original_counts(values, run_id)
        self.doc62_known_counts = result
        self.doc62_counts_snapshot = dict(result)
        return result

    def observed_assemble(self, values, run_id, *, known_row_counts):
        assert known_row_counts is self.doc62_known_counts
        return original_assemble(self, values, run_id, known_row_counts=known_row_counts)

    def observed_fallback(self, status, evidence, known_row_counts):
        assert known_row_counts is self.doc62_known_counts
        assert known_row_counts == self.doc62_counts_snapshot
        return original_fallback(status, evidence, known_row_counts)

    def observed_evaluate(self, failures, budget_snapshot, values, normalized_submission, evidence, warnings):
        self.doc62_normalized_submission = normalized_submission
        self.doc62_submission_snapshot = copy.deepcopy(normalized_submission)
        return original_evaluate(self, failures, budget_snapshot, values, normalized_submission, evidence, warnings)

    def observed_archive(self, *args, **kwargs):
        assert kwargs["normalized_submission"] is self.doc62_normalized_submission
        assert kwargs["normalized_submission"] == self.doc62_submission_snapshot
        assert self.doc62_row_count_scans == 1
        assert self.doc62_known_counts == self.doc62_counts_snapshot
        self.doc62_shared_view_checked = True
        return original_archive(self, *args, **kwargs)

    candidate._evaluate = observed_evaluate
    candidate._archive_run = observed_archive
    candidate._known_saved_row_counts = observed_counts
    candidate._assemble_evidence = observed_assemble
    candidate._build_fallback_answer = observed_fallback

    class ScopeAgent(FakeAgent):
        def __init__(self, state, scope_change):
            super().__init__(state)
            self.scope_change = scope_change

        def invoke(self, inputs, config=None, context=None):
            result = super().invoke(inputs, config=config, context=context)
            record = self.state_values["db_final_answers"][context.run_id]
            field, value = self.scope_change
            if value is None:
                record.pop(field)
            else:
                record[field] = value
            return result

    invalid_values = [
        "not-an-object", ["not-an-object"], None, "", [], False, 0,
        {**ANSWER, "findings": "not-a-list"},
        {**ANSWER, "findings": {"text": "not-a-list"}},
        {**ANSWER, "findings": ["not-a-dict"]},
    ]
    total = 0
    normal_cases = 0
    scope_cases = 0
    cfg = db_agent_config.__wrapped__()
    with tempfile.TemporaryDirectory(prefix="doc62-payload-") as directory:
        with pytest.MonkeyPatch.context() as monkeypatch:
            services = assembly_services.__wrapped__(cfg, Path(directory), monkeypatch)
            # Only the proposed version constant is added to this process.
            with patch.object(archive_module, "ARCHIVE_SCHEMA_VERSION", 2, create=True):
                for reused in (False, True):
                    for index, raw in enumerate(invalid_values):
                        state = {
                            "messages": [],
                            "db_executions": [_exec(reused=reused)],
                            "db_analyses": [],
                            "db_final_answers": {"run-x": {
                                "run_id": "run-x", "request_id": f"r-{int(reused)}-{index}",
                                "answer": copy.deepcopy(raw),
                            }},
                        }
                        store = FakeStore()
                        agent = FakeAgent(state)
                        handle = DbAgentHandle(agent, None, [], "offline", Path(directory))
                        runner = candidate(services=services, handle=handle, archive=archive_module.QaArchive(store))
                        result = runner.run("离线审核", f"s-{int(reused)}-{index}", request_id=f"r-{int(reused)}-{index}")
                        assert result.status == "failed", result
                        assert result.answer.startswith("系统兜底说明：")
                        assert not any(error.phase == "P3" for error in result.errors), result.errors
                        assert len(agent.invoke_calls) == 1
                        rows = [(key[0][1], value) for key, value in store.items.items() if key[0][1] in ("qa", "run_errors")]
                        assert len(rows) == 1
                        kind, record = rows[0]
                        assert kind == ("run_errors" if reused else "qa")
                        assert type(record["answer"]) is type(raw) and record["answer"] == raw
                        assert record["delivery_answer"]["answer"] == result.answer
                        assert record["answer_source"] == "system_fallback"
                        assert runner.doc62_shared_view_checked
                        total += 1

                    # The dictionary key stays current while the outer IDs are
                    # changed AFTER the fixture remaps its placeholder run ID.
                    for index, scope_change in enumerate((
                        ("run_id", "another-run"), ("request_id", "another-request"),
                        ("run_id", None), ("request_id", None),
                    )):
                        request_id = f"scope-{int(reused)}-{index}"
                        state = {
                            "messages": [], "db_executions": [_exec(reused=reused)],
                            "db_analyses": [],
                            "db_final_answers": {"run-x": {
                                "run_id": "run-x", "request_id": request_id,
                                "answer": copy.deepcopy(ANSWER),
                            }},
                        }
                        store = FakeStore()
                        agent = ScopeAgent(state, scope_change)
                        handle = DbAgentHandle(agent, None, [], "offline", Path(directory))
                        runner = candidate(services=services, handle=handle, archive=archive_module.QaArchive(store))
                        with patch.object(schema_ns["DbAgentFinalAnswer"], "model_validate", wraps=schema_ns["DbAgentFinalAnswer"].model_validate) as validate:
                            result = runner.run("归属核对", request_id, request_id=request_id)
                            assert validate.call_count == 0
                        assert result.status == "failed", result
                        assert result.answer.startswith("系统兜底说明：")
                        assert any(error.kind == "no_final_answer" for error in result.errors), result.errors
                        assert not any(error.phase == "P3" for error in result.errors), result.errors
                        assert len(agent.invoke_calls) == 1
                        assert runner.doc62_normalized_submission is None
                        assert runner.doc62_shared_view_checked
                        current_run_id = agent.invoke_calls[0]["context"].run_id
                        original_record = agent.state_values["db_final_answers"][current_run_id]
                        field, value = scope_change
                        if value is None:
                            assert field not in original_record
                        else:
                            assert original_record[field] == value
                        rows = [(key[0][1], value) for key, value in store.items.items() if key[0][1] in ("qa", "run_errors")]
                        assert len(rows) == 1
                        kind, record = rows[0]
                        assert kind == ("run_errors" if reused else "qa")
                        assert record["answer"] == ANSWER
                        assert record["delivery_answer"]["answer"] == result.answer
                        assert record["answer_source"] == "system_fallback"
                        scope_cases += 1

                    # Positive baseline for each archive qualification path.
                    state = {
                        "messages": [], "db_executions": [_exec(reused=reused)],
                        "db_analyses": [],
                        "db_final_answers": {"run-x": {
                            "run_id": "run-x", "request_id": f"normal-{int(reused)}",
                            "answer": copy.deepcopy(ANSWER),
                        }},
                    }
                    store = FakeStore()
                    agent = FakeAgent(state)
                    handle = DbAgentHandle(agent, None, [], "offline", Path(directory))
                    runner = candidate(services=services, handle=handle, archive=archive_module.QaArchive(store))
                    with patch.object(schema_ns["DbAgentFinalAnswer"], "model_validate", wraps=schema_ns["DbAgentFinalAnswer"].model_validate) as validate:
                        result = runner.run("正常基线", f"normal-{int(reused)}", request_id=f"normal-{int(reused)}")
                        assert validate.call_count == 1
                    assert result.status == "completed" and result.answer == ANSWER["answer"]
                    assert runner.doc62_shared_view_checked
                    rows = [value for key, value in store.items.items() if key[0][1] == "qa"]
                    assert len(rows) == 1 and rows[0]["answer"] == ANSWER
                    assert rows[0]["answer_source"] == "model"
                    normal_cases += 1

    # Verify omission defaults, provider migration error, and shallow-reader scope.
    config_ns = dict(vars(config_module))
    exec(next(b for b in blocks if "class ModelRetryConfig" in b), config_ns)
    config_tree = ast.parse((root / "src/modules/db_deepagent/config.py").read_text())
    model_config = copy.deepcopy(next(n for n in config_tree.body if isinstance(n, ast.ClassDef) and n.name == "ModelConfig"))
    model_config.body.append(ast.parse("retry: ModelRetryConfig = Field(default_factory=ModelRetryConfig)").body[0])
    compile_nodes([model_config], config_ns)
    model_cfg = config_ns["ModelConfig"](llm_profile="native", request_timeout_seconds=120)
    assert model_cfg.retry.enabled is False
    build_block = next(b for b in blocks if "model = get_llm(" in b)
    seen = []
    scope = dict(
        cfg=SimpleNamespace(model=model_cfg), provider="dashscope", overrides={},
        get_llm=lambda profile, **kwargs: (seen.append(kwargs) or SimpleNamespace(llm=object())),
        DbAgentConfigError=config_module.DbAgentConfigError,
    )
    exec(build_block, scope)
    assert seen == [{}]
    model_cfg.retry.enabled = True
    try:
        exec(build_block, {**scope, "overrides": {}})
    except config_module.DbAgentConfigError as exc:
        assert "model.retry.enabled=false" in str(exc)
    else:
        raise AssertionError("explicit native retry should be rejected")

    # Exercise the public client attributes on the installed ChatOpenAI. Only
    # construction occurs; fake credentials and no model/network invocation.
    from langchain_openai import ChatOpenAI

    sdk_models = []

    def make_offline_model(profile, **overrides):
        # Do not let the host's SOCKS settings add unrelated optional-package
        # requirements to an offline construction check. Restore on exit.
        with pytest.MonkeyPatch.context() as constructor_env:
            for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
                constructor_env.delenv(name, raising=False)
            model = ChatOpenAI(model="offline-model", api_key="offline-placeholder", **overrides)
        sdk_models.append(model)
        return SimpleNamespace(llm=model)

    try:
        sdk_scope = {
            **scope, "provider": "openai", "overrides": {},
            "get_llm": make_offline_model,
        }
        exec(build_block, sdk_scope)
        model = sdk_scope["model"]
        assert model.root_client.max_retries == 0
        assert model.root_async_client.max_retries == 0
        for attribute in ("root_client", "root_async_client"):
            with patch.object(model, attribute, None):
                try:
                    exec(build_block, {
                        **sdk_scope, "overrides": {},
                        "get_llm": lambda *args, **kwargs: SimpleNamespace(llm=model),
                    })
                except config_module.DbAgentConfigError as exc:
                    assert "无法确认" in str(exc)
                else:
                    raise AssertionError("unconstructed client must not silently pass")
    finally:
        for model in sdk_models:
            model.root_client.close()
            asyncio.run(model.root_async_client.close())

    reader_ns = {"Any": object}
    exec(next(b for b in blocks if "LEGACY_ARCHIVE_SCHEMA_VERSION = 1" in b), reader_ns)
    exec(next(b for b in blocks if "def read_delivery_answer" in b), reader_ns)
    read = reader_ns["read_delivery_answer"].__func__
    bad_content = {"unexpected": "reader is not a content validator"}
    assert read({"archive_schema_version": 2, "delivery_answer": bad_content}) == bad_content
    assert read({"answer": None}) == {}
    # Preserve the model-vs-runner distinction in historical projections.
    legacy = {"status": "failed", "answer": {"status": "completed", "answer": "旧正文"}}
    snapshot = copy.deepcopy(legacy)
    view = read(legacy)
    assert "status" not in view and view["submitted_status"] == "completed"
    assert legacy == snapshot
    assert view["answer"] == legacy["answer"]["answer"]
    assert "submitted_status" not in read({"answer": {"answer": "无状态正文"}})
    assert read({"answer": {"status": "partial", "submitted_status": "other"}})["submitted_status"] == "partial"
    current = {"archive_schema_version": 2, "status": "failed", "delivery_answer": {"status": "failed", "answer": "兜底正文"}}
    assert read(current)["status"] == "failed" and "submitted_status" not in read(current)
    assert [record["status"] for record in (legacy, current)] == ["failed", "failed"]

    # Reliable zero and unknown remain distinct even when external row_count
    # uses zero for both. The user-facing text omits internal IDs and jargon.
    template_cases = 0
    missing = object()
    for raw_count in (0, missing, None, False, -1, "3"):
        record = _exec(reused=True)
        record["source_truncated"] = True
        if raw_count is missing:
            record.pop("saved_row_count", None)
        else:
            record["saved_row_count"] = raw_count
        values = {"db_executions": [record], "db_analyses": []}
        counts = original_counts(values, "run-x")
        evidence = original_assemble(None, values, "run-x", known_row_counts=counts)
        assert len(evidence) == 1 and evidence[0].row_count == 0
        text = original_fallback("failed", evidence, counts)
        assert ("该查询没有返回记录。" in text) == (type(raw_count) is int and raw_count == 0)
        assert "已记录 0 行结果。" not in text
        assert text.startswith("系统兜底说明：")
        assert record["evidence_id"] not in text
        assert "账本" not in text and "保存行数未知" not in text
        assert "来自历史记录" in text and "部分数据" in text
        assert "完整答案尚未生成" in text
        template_cases += 1
    assert "未取得可确认" in original_fallback("failed", [], {})
    analysis = evidence[0].model_copy(update={
        "kind": "result_analysis", "sql": None, "operation": {"type": "offline"},
        "input_result_refs": ["offline-input"],
    })
    analysis_text = original_fallback("failed", [analysis], {analysis.evidence_id: 0})
    assert "该分析没有输出记录。" in analysis_text
    assert "该查询没有返回记录。" not in analysis_text
    assert analysis_text.startswith("系统兜底说明：")
    many = [evidence[0].model_copy(update={"evidence_id": f"E-internal-{i}"}) for i in range(7)]
    assert original_fallback("failed", many, {}).count("- 结果 ") == 5
    assert "另有 2 份结果记录" in original_fallback("failed", many, {})
    for bad in (None, "bad", []):
        try:
            read({"archive_schema_version": 2, "delivery_answer": bad})
        except ValueError:
            pass
        else:
            raise AssertionError("non-dict v2 container should be rejected")

    print(json.dumps({
        "result": "PASS", "full_runner_cases": total,
        "normal_runner_baselines": normal_cases,
        "scope_mismatch_runner_cases": scope_cases,
        "single_runner_validation_and_shared_view": True,
        "single_row_count_scan_and_shared_map": True,
        "historical_status_semantics_explicit": True,
        "simple_template_cases": template_cases,
        "langchain_openai_version": package_version("langchain-openai"),
        "public_clients_constructed_and_retries_zero": True,
        "missing_public_client_rejected": True,
        "invalid_payloads": len(invalid_values), "real_execution_and_reuse": True,
        "raw_values_preserved": True, "no_extra_p3_errors": True,
        "omitted_retry_defaults_false": True, "native_disable_hint": True,
        "reader_is_shallow_projection": True, "python_snippets": len(blocks),
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
