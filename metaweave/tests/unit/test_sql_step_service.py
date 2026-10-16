"""doc 25.1：受管 SQL 输入、执行与失败状态的关键边界。"""

import asyncio
import json
from contextlib import contextmanager
from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4

import pytest
from starlette.requests import Request

from backend.data_sources import sql_step_service as service
from backend.data_sources.db_step_service import DbStepConfigError
from backend.jobs.collection_run import (
    normalize_collection_steps,
    _blocked_by_same_request_upstream,
)


SOURCE_ID = uuid4()
JOB_ID = uuid4()


class FakeLLM:
    model = "fake"

    def __init__(self, *_args, **_kwargs):
        self.calls = []
        self.on_call = None

    def call_llm(self, prompt, system_message=None):
        self.calls.append(prompt)
        if self.on_call:
            self.on_call()
        return '[{"question":"有多少订单？","sql":"SELECT count(*) FROM orders"}]'


def _config(**validation):
    return {
        "sql_rag": {
            "generation": {"questions_per_domain": 1, "skip_uncategorized": False},
            "validation": {"enabled": False, **validation},
        },
        "llm": {},
    }


def _files(tmp_path, monkeypatch, domain_tables=None):
    workspace = tmp_path / str(SOURCE_ID) / str(JOB_ID)
    for name in ("md", "domain", "rel", "sql-rag"):
        (workspace / name).mkdir(parents=True)
    (workspace / "md" / "demo.public.orders.md").write_text(
        "# 订单表 orders\n字段 id", encoding="utf-8"
    )
    import yaml

    domains = [
        {"name": "空域", "tables": []},
        {
            "name": "订单",
            "tables": (
                ["demo.public.orders"] if domain_tables is None else domain_tables
            ),
        },
    ]
    (workspace / "domain" / "db_domains.yaml").write_text(
        yaml.safe_dump(
            {"database": {"name": "demo"}, "domains": domains}, allow_unicode=True
        ),
        encoding="utf-8",
    )
    (workspace / "rel" / "demo.relationships_global.md").write_text(
        "# 表间关系发现报告\ndatabase: demo\n零关系", encoding="utf-8"
    )
    for name, func in (
        ("md", "resolve_md_output_dir"),
        ("domain", "resolve_domain_output_dir"),
        ("rel", "resolve_rel_output_dir"),
        ("sql-rag", "resolve_sql_rag_output_dir"),
    ):
        monkeypatch.setattr(service, func, lambda *_args, name=name: workspace / name)
    return workspace


def _install(monkeypatch, workspace, config=None):
    config = deepcopy(config or _config())
    rows = {
        step: SimpleNamespace(last_status="success", updated_at=1)
        for step in service.UPSTREAM
    }
    source = SimpleNamespace(
        source_id=SOURCE_ID,
        db_name="demo",
        db_type="postgresql",
        username="tester",
        host="localhost",
        is_active=True,
        selected_schemas=["public"],
    )
    state = SimpleNamespace(
        rows=rows, source=source, started=[], finished=[], llm=FakeLLM()
    )

    @contextmanager
    def session(_settings):
        yield state

    monkeypatch.setattr(service, "load_main_config_snapshot", lambda _path: config)
    monkeypatch.setattr(service, "load_platform_settings_from_env", lambda: object())
    monkeypatch.setattr(service, "platform_session", session)
    monkeypatch.setattr(service, "get_db_source", lambda _session, _id: source)
    monkeypatch.setattr(service, "assert_workspace_owner", lambda *_args: None)
    monkeypatch.setattr(
        service, "get_step_row", lambda _session, _job, step: rows.get(step)
    )
    monkeypatch.setattr(service, "lock_db_source_row", lambda _session, _id: source)
    monkeypatch.setattr(
        service,
        "start_step",
        lambda _session, job, source_id, step: state.started.append(
            (job, source_id, step)
        ),
    )
    monkeypatch.setattr(
        service,
        "finish_step",
        lambda _session, job, step, status: state.finished.append((job, step, status)),
    )
    monkeypatch.setattr(
        service, "build_managed_output_snapshot", lambda *_args: deepcopy(config)
    )
    monkeypatch.setattr(service, "resolve_module_llm_config", lambda *_args: {})
    monkeypatch.setattr(service, "LLMService", lambda *_args: state.llm)
    monkeypatch.setattr(
        service,
        "DatabaseConnector",
        lambda *_args: pytest.fail("disabled validation must not connect"),
    )
    return state


def test_sql_step_uses_job_paths_skips_empty_domain_and_cleans_top_level(
    tmp_path, monkeypatch
):
    workspace = _files(tmp_path, monkeypatch)
    global_dir = tmp_path / "global-sql-rag"
    global_dir.mkdir()
    (global_dir / "decoy.json").write_text("global", encoding="utf-8")
    config = _config()
    config["output"] = {"markdown_directory": str(tmp_path / "global-md")}
    config["sql_rag"]["generation"]["output_dir"] = str(global_dir)
    state = _install(monkeypatch, workspace, config)
    output_dir = workspace / "sql-rag"
    (output_dir / "old.json").write_text("old", encoding="utf-8")
    (output_dir / "subdir").mkdir()

    result = service.run_sql_step("unused.yaml", SOURCE_ID, JOB_ID)

    assert result["success"] is True
    assert state.started == [(JOB_ID, SOURCE_ID, "sql")]
    assert state.finished == [(JOB_ID, "sql", "success")]
    assert not (output_dir / "old.json").exists()
    assert (output_dir / "subdir").is_dir()
    assert (global_dir / "decoy.json").read_text(encoding="utf-8") == "global"
    assert len(state.llm.calls) == 1
    assert "订单表 orders" in state.llm.calls[0]
    assert "空域（没有表）" in result["message"]
    assert "SQL 校验已跳过" in result["message"]
    assert (
        json.loads((output_dir / "qs_demo_pair.json").read_text())[0]["question"]
        == "有多少订单？"
    )


def test_no_usable_domain_does_not_clear_previous_output(tmp_path, monkeypatch):
    workspace = _files(tmp_path, monkeypatch, domain_tables=[])
    state = _install(monkeypatch, workspace)
    old = workspace / "sql-rag" / "old.json"
    old.write_text("old", encoding="utf-8")

    with pytest.raises(DbStepConfigError, match="没有可生成 SQL 的业务域"):
        service.run_sql_step("unused.yaml", SOURCE_ID, JOB_ID)
    assert old.read_text(encoding="utf-8") == "old"
    assert not state.started


@pytest.mark.parametrize(
    "problem", ["wrong_md_name", "unknown_domain_table", "bad_rel_database"]
)
def test_invalid_managed_input_is_rejected_before_start(tmp_path, monkeypatch, problem):
    workspace = _files(tmp_path, monkeypatch)
    state = _install(monkeypatch, workspace)
    old = workspace / "sql-rag" / "old.json"
    old.write_text("old", encoding="utf-8")
    if problem == "wrong_md_name":
        (workspace / "md" / "other.public.orders.md").write_text(
            "# wrong", encoding="utf-8"
        )
    elif problem == "unknown_domain_table":
        domain = workspace / "domain" / "db_domains.yaml"
        domain.write_text(
            domain.read_text(encoding="utf-8").replace(
                "demo.public.orders", "demo.public.unknown"
            ),
            encoding="utf-8",
        )
    else:
        rel = workspace / "rel" / "demo.relationships_global.md"
        rel.write_text("# 表间关系发现报告\ndatabase: demo_other", encoding="utf-8")
    with pytest.raises(DbStepConfigError, match="SQL 输入未就绪"):
        service.run_sql_step("unused.yaml", SOURCE_ID, JOB_ID)
    assert old.read_text(encoding="utf-8") == "old"
    assert not state.started


@pytest.mark.parametrize(
    "change", ["domain", "rel", "md_add", "md_delete", "md_rewrite"]
)
def test_input_edit_during_generation_marks_sql_failed(tmp_path, monkeypatch, change):
    workspace = _files(tmp_path, monkeypatch)
    state = _install(monkeypatch, workspace)
    original_markers = {step: row.updated_at for step, row in state.rows.items()}

    def edit_input():
        if change == "md_add":
            (workspace / "md" / "demo.public.extra.md").write_text(
                "# 新增表", encoding="utf-8"
            )
            return
        paths = {
            "domain": workspace / "domain" / "db_domains.yaml",
            "rel": workspace / "rel" / "demo.relationships_global.md",
            "md_delete": workspace / "md" / "demo.public.orders.md",
            "md_rewrite": workspace / "md" / "demo.public.orders.md",
        }
        path = paths[change]
        if change == "md_delete":
            path.unlink()
        else:
            path.write_text(
                path.read_text(encoding="utf-8") + "\n# 编辑\n", encoding="utf-8"
            )

    state.llm.on_call = edit_input

    result = service.run_sql_step("unused.yaml", SOURCE_ID, JOB_ID)

    assert result["success"] is False
    assert "上游输入在生成期间发生变化" in result["message"]
    assert state.finished == [(JOB_ID, "sql", "failed")]
    assert {
        step: row.updated_at for step, row in state.rows.items()
    } == original_markers


@pytest.mark.parametrize(
    "failure_mode,original_reason",
    [
        ("exception", "SQL 样例生成或校验失败（ValueError）：SQL 校验报告写入失败"),
        ("zero_output", "所有业务域均未生成 SQL 样例"),
    ],
)
def test_final_input_change_preserves_original_generation_failure(
    tmp_path, monkeypatch, failure_mode, original_reason
):
    workspace = _files(tmp_path, monkeypatch)
    state = _install(monkeypatch, workspace)
    original_generate = service.run_sql_rag_generation
    domain = workspace / "domain" / "db_domains.yaml"

    def generate(*args, **kwargs):
        domain.write_text(
            domain.read_text(encoding="utf-8") + "# 编辑\n", encoding="utf-8"
        )
        if failure_mode == "exception":
            raise ValueError("SQL 校验报告写入失败")
        return original_generate(*args, **kwargs)

    state.llm.call_llm = lambda *_args, **_kwargs: "[]"
    monkeypatch.setattr(service, "run_sql_rag_generation", generate)

    result = service.run_sql_step("unused.yaml", SOURCE_ID, JOB_ID)

    assert result["success"] is False
    assert result["message"].startswith("SQL 上游输入在生成期间发生变化：")
    assert f"；{original_reason}" in result["message"]
    assert state.finished == [(JOB_ID, "sql", "failed")]


def test_uncategorized_uses_own_question_count_and_only_its_tables(
    tmp_path, monkeypatch
):
    import yaml

    workspace = _files(tmp_path, monkeypatch)
    domain_path = workspace / "domain" / "db_domains.yaml"
    document = yaml.safe_load(domain_path.read_text(encoding="utf-8"))
    document["domains"] = [{"name": "_未分类_", "tables": ["demo.public.orders"]}]
    domain_path.write_text(
        yaml.safe_dump(document, allow_unicode=True), encoding="utf-8"
    )
    (workspace / "md" / "demo.public.other.md").write_text(
        "OTHER_TABLE_MUST_NOT_APPEAR", encoding="utf-8"
    )
    config = _config()
    config["sql_rag"]["generation"].update(
        questions_per_domain=7, uncategorized_questions=2, skip_uncategorized=False
    )
    state = _install(monkeypatch, workspace, config)
    original_call = state.llm.call_llm

    def two_pairs(prompt, system_message=None):
        first = json.loads(original_call(prompt, system_message))
        return json.dumps(
            first + [{"question": "查看订单", "sql": "SELECT * FROM orders"}]
        )

    state.llm.call_llm = two_pairs

    result = service.run_sql_step("unused.yaml", SOURCE_ID, JOB_ID)

    assert result["success"] is True
    assert state.finished == [(JOB_ID, "sql", "success")]
    assert len(state.llm.calls) == 1
    prompt = state.llm.calls[0]
    assert "主题名称：_未分类_" in prompt
    assert "生成 2 组" in prompt
    assert "生成 7 组" not in prompt
    assert "订单表 orders" in prompt
    assert "OTHER_TABLE_MUST_NOT_APPEAR" not in prompt
    pairs = json.loads((workspace / "sql-rag" / "qs_demo_pair.json").read_text())
    assert len(pairs) == 2
    assert all(pair["domain"] == "_未分类_" for pair in pairs)


def test_sql_final_check_and_finish_share_source_lock(tmp_path, monkeypatch):
    workspace = _files(tmp_path, monkeypatch)
    state = _install(monkeypatch, workspace)
    events = []
    original_inspect = service.inspect_managed_sql_inputs

    def lock(*_args):
        events.append("lock")
        return state.source

    def inspect(*args):
        events.append("inspect")
        return original_inspect(*args)

    def finish(_session, job, step, status):
        events.append("finish")
        state.finished.append((job, step, status))

    monkeypatch.setattr(service, "lock_db_source_row", lock)
    monkeypatch.setattr(service, "inspect_managed_sql_inputs", inspect)
    monkeypatch.setattr(service, "finish_step", finish)
    result = service.run_sql_step("unused.yaml", SOURCE_ID, JOB_ID)

    assert result["success"] is True
    assert events[-3:] == ["lock", "inspect", "finish"]


def test_output_symlink_stops_before_deleting_any_file(tmp_path):
    directory = tmp_path / "sql-rag"
    directory.mkdir()
    old = directory / "old.json"
    old.write_text("old", encoding="utf-8")
    (directory / "link").symlink_to(old)

    with pytest.raises(ValueError, match="符号链接"):
        service._clean_sql_top_level_files(directory)
    assert old.read_text(encoding="utf-8") == "old"


def test_output_symlink_marks_started_sql_failed(tmp_path, monkeypatch):
    workspace = _files(tmp_path, monkeypatch)
    state = _install(monkeypatch, workspace)
    output_dir = workspace / "sql-rag"
    old = output_dir / "old.json"
    old.write_text("old", encoding="utf-8")
    (output_dir / "link").symlink_to(old)

    result = service.run_sql_step("unused.yaml", SOURCE_ID, JOB_ID)

    assert result["success"] is False
    assert "SQL-RAG 输出目录含符号链接" in result["message"]
    assert state.finished == [(JOB_ID, "sql", "failed")]
    assert old.read_text(encoding="utf-8") == "old"


@pytest.mark.parametrize(
    "target,error,expected_message",
    [
        (
            "_clean_sql_top_level_files",
            ValueError("SQL-RAG 输出目录含非常规条目：pipe"),
            "SQL-RAG 输出目录含非常规条目：pipe",
        ),
        (
            "_clean_sql_top_level_files",
            PermissionError("旧 SQL 文件无法删除"),
            "旧 SQL 文件无法删除",
        ),
        (
            "run_sql_rag_generation",
            ValueError("enable_repair=true 时必须提供 llm_service"),
            "enable_repair=true 时必须提供 llm_service",
        ),
        (
            "run_sql_rag_generation",
            RuntimeError("internal failure details"),
            "SQL 样例生成或校验失败（RuntimeError）",
        ),
    ],
)
def test_runtime_failure_keeps_expected_error_details(
    tmp_path, monkeypatch, target, error, expected_message
):
    workspace = _files(tmp_path, monkeypatch)
    state = _install(monkeypatch, workspace)

    def fail(*_args, **_kwargs):
        raise error

    monkeypatch.setattr(service, target, fail)

    result = service.run_sql_step("unused.yaml", SOURCE_ID, JOB_ID)

    assert result["success"] is False
    assert expected_message in result["message"]
    assert "internal failure details" not in result["message"]
    assert state.finished == [(JOB_ID, "sql", "failed")]


def test_removed_validation_key_rejected_before_clean(tmp_path, monkeypatch):
    workspace = _files(tmp_path, monkeypatch)
    state = _install(
        monkeypatch, workspace, _config(enabled=True, modify_original_file=True)
    )
    old = workspace / "sql-rag" / "old.json"
    old.write_text("old", encoding="utf-8")

    with pytest.raises(DbStepConfigError, match="modify_original_file"):
        service.run_sql_step("unused.yaml", SOURCE_ID, JOB_ID)
    assert old.exists()
    assert not state.started


@pytest.mark.parametrize(
    "repair,retained,expected_success,message",
    [
        (True, 1, True, "生成 2 条，保留 1 条，删除 1 条"),
        (True, 0, False, "没有可保留的样例"),
        (False, 2, False, "存在无效 SQL 且未启用修复"),
    ],
)
def test_validation_result_controls_status_and_closes_connector(
    tmp_path, monkeypatch, repair, retained, expected_success, message
):
    workspace = _files(tmp_path, monkeypatch)
    state = _install(
        monkeypatch, workspace, _config(enabled=True, enable_sql_repair=repair)
    )
    state.source.password_ciphertext = "encrypted"
    monkeypatch.setattr(service, "decrypt_password", lambda _cipher: "secret")

    def managed_config(config, *_args):
        snapshot = deepcopy(config)
        snapshot["database"] = {
            "host": "localhost",
            "port": 5432,
            "database": "demo",
            "user": "tester",
            "password": "secret",
            "sslmode": None,
        }
        return snapshot

    monkeypatch.setattr(service, "build_managed_ddl_config", managed_config)
    old = workspace / "sql-rag" / "old.json"
    old.write_text("old", encoding="utf-8")
    connector = SimpleNamespace(closed=False)

    @contextmanager
    def get_connection():
        assert old.read_text(encoding="utf-8") == "old"
        yield object()

    connector.get_connection = get_connection
    connector.close = lambda: setattr(connector, "closed", True)
    monkeypatch.setattr(service, "DatabaseConnector", lambda _cfg: connector)
    state.llm.call_llm = lambda *_args, **_kwargs: json.dumps(
        [
            {"question": "有效", "sql": "SELECT 1"},
            {"question": "无效", "sql": "SELECT bad"},
        ]
    )
    from metaweave.core.sql_rag import execution

    class FakeValidator:
        def __init__(self, *, connector, config, llm_service, md_dir, rel_dir):
            assert not old.exists()
            assert connector is not None
            assert md_dir == str(workspace / "md")
            assert rel_dir == str(workspace / "rel")
            assert llm_service is (state.llm if repair else None)

        def validate_file(self, input_file, enable_repair):
            assert enable_repair is repair
            path = workspace / "sql-rag" / "qs_demo_pair.json"
            assert input_file == str(path)
            pairs = json.loads(path.read_text(encoding="utf-8"))
            if repair:
                path.with_suffix(".json.backup").write_text(
                    json.dumps(pairs), encoding="utf-8"
                )
                path.write_text(json.dumps(pairs[:retained]), encoding="utf-8")
            (path.parent / "sql_validation_summary_fake.log").write_text(
                "ok", encoding="utf-8"
            )
            return {
                "total": 2,
                "valid": 0 if retained == 0 else 1,
                "invalid": 2 if retained == 0 else 1,
                "repair_apply_stats": {"deleted": 2 - retained},
            }

    monkeypatch.setattr(execution, "SQLValidator", FakeValidator)
    result = service.run_sql_step("unused.yaml", SOURCE_ID, JOB_ID)

    assert result["success"] is expected_success
    assert message in result["message"]
    assert connector.closed is True
    expected_status = "success" if expected_success else "failed"
    assert state.finished == [(JOB_ID, "sql", expected_status)]
    pairs = json.loads((workspace / "sql-rag" / "qs_demo_pair.json").read_text())
    assert len(pairs) == retained


@pytest.mark.parametrize("failure_stage", ["construction", "connection"])
def test_source_connection_failure_preserves_old_output(
    tmp_path, monkeypatch, failure_stage
):
    workspace = _files(tmp_path, monkeypatch)
    state = _install(monkeypatch, workspace, _config(enabled=True))
    state.source.password_ciphertext = "encrypted"
    monkeypatch.setattr(service, "decrypt_password", lambda _cipher: "secret")
    monkeypatch.setattr(
        service,
        "build_managed_ddl_config",
        lambda config, *_args: {
            **deepcopy(config),
            "database": {
                "host": "localhost",
                "port": 5432,
                "database": "demo",
                "user": "tester",
                "password": "secret",
            },
        },
    )
    old = workspace / "sql-rag" / "old.json"
    old.write_text("old", encoding="utf-8")
    connector = SimpleNamespace(closed=False)

    @contextmanager
    def get_connection():
        raise OSError("source unavailable")
        yield  # pragma: no cover

    connector.get_connection = get_connection
    connector.close = lambda: setattr(connector, "closed", True)

    def create_connector(_config):
        if failure_stage == "construction":
            raise ValueError("pool configuration invalid")
        return connector

    monkeypatch.setattr(service, "DatabaseConnector", create_connector)

    result = service.run_sql_step("unused.yaml", SOURCE_ID, JOB_ID)

    assert result["success"] is False
    assert result["job_id"] == str(JOB_ID)
    assert old.read_text(encoding="utf-8") == "old"
    assert state.finished == [(JOB_ID, "sql", "failed")]
    assert not state.llm.calls
    assert connector.closed is (failure_stage == "connection")


def test_start_step_failure_is_platform_error_and_preserves_output(
    tmp_path, monkeypatch
):
    from backend.jobs import collection_run

    workspace = _files(tmp_path, monkeypatch)
    state = _install(monkeypatch, workspace)
    old = workspace / "sql-rag" / "old.json"
    old.write_text("old", encoding="utf-8")

    def fail_start(*_args):
        raise OSError("state write failed")

    monkeypatch.setattr(service, "start_step", fail_start)
    result = service.run_sql_step("unused.yaml", SOURCE_ID, JOB_ID)

    assert result["success"] is False
    assert result["job_id"] == str(JOB_ID)
    assert result["message"] == "作业状态登记失败（OSError）"
    assert old.read_text(encoding="utf-8") == "old"
    assert not state.llm.calls
    assert not state.finished

    monkeypatch.setattr(collection_run, "run_sql_step", lambda *_args: result)
    with pytest.raises(service.PlatformUnavailableError):
        collection_run.start_db_collection(SOURCE_ID, ["sql"], JOB_ID)


def test_finish_step_failure_cannot_report_success(tmp_path, monkeypatch):
    workspace = _files(tmp_path, monkeypatch)
    _install(monkeypatch, workspace)

    def fail_finish(*_args):
        raise OSError("state write failed")

    monkeypatch.setattr(service, "finish_step", fail_finish)

    result = service.run_sql_step("unused.yaml", SOURCE_ID, JOB_ID)

    assert result["success"] is False
    assert "作业状态更新失败" in result["message"]


def test_readiness_reports_missing_files_and_platform_failure(tmp_path, monkeypatch):
    workspace = _files(tmp_path, monkeypatch)
    _install(monkeypatch, workspace)
    assert service.get_sql_readiness(object(), SOURCE_ID, JOB_ID)["ready"] is True
    (workspace / "rel" / "demo.relationships_global.md").unlink()
    answer = service.get_sql_readiness(object(), SOURCE_ID, JOB_ID)
    assert answer["ready"] is False
    assert answer["missing"] == ["rel"]

    def fail_source(*_args):
        raise OSError("database down")

    monkeypatch.setattr(service, "get_db_source", fail_source)
    with pytest.raises(service.PlatformUnavailableError):
        service.get_sql_readiness(object(), SOURCE_ID, JOB_ID)


def test_readiness_rejects_disabled_source_before_input_inspection(
    tmp_path, monkeypatch
):
    workspace = _files(tmp_path, monkeypatch)
    state = _install(monkeypatch, workspace)
    state.source.is_active = False
    monkeypatch.setattr(
        service,
        "inspect_managed_sql_inputs",
        lambda *_args: pytest.fail("disabled source must not inspect inputs"),
    )

    with pytest.raises(service.SourceDisabledError, match="该数据源已禁用"):
        service.get_sql_readiness(object(), SOURCE_ID, JOB_ID)


def test_upstream_row_read_failure_is_platform_failure(tmp_path, monkeypatch):
    workspace = _files(tmp_path, monkeypatch)
    state = _install(monkeypatch, workspace)

    def fail_row(*_args):
        raise OSError("platform read failed")

    monkeypatch.setattr(service, "get_step_row", fail_row)
    result = service.run_sql_step("unused.yaml", SOURCE_ID, JOB_ID)
    assert result["success"] is False
    assert result["message"] == "平台库连接或读取失败"
    assert "job_id" not in result
    assert not state.started


def test_sql_collection_order_and_same_request_block():
    chosen = normalize_collection_steps(["sql", "rel", "domain", "md", "json", "ddl"])
    assert chosen == ["ddl", "json", "md", "domain", "rel", "sql"]
    blocked = _blocked_by_same_request_upstream(
        "sql", chosen, [{"step": "rel", "status": "failed"}]
    )
    assert blocked and "Rel 未成功" in blocked


def test_sql_readiness_api_and_preparation_error_codes():
    from backend.api.main import create_app
    from backend.api.routers.data_sources import router
    from backend.persistence.platform_db import PlatformSettings

    settings = PlatformSettings(
        host="unused", port=5432, name="unused", user="unused", password="unused"
    )
    app = create_app(settings, cors_origins=[])

    paths = {route.path for route in router.routes}
    assert (
        "/api/data-sources/{source_id}/collection-jobs/{job_id}/sql-readiness" in paths
    )
    request = Request({"type": "http", "method": "GET", "path": "/test", "headers": []})
    handler = app.exception_handlers[service.PlatformUnavailableError]
    response = asyncio.run(handler(request, service.PlatformUnavailableError("down")))
    assert response.status_code == 503
    assert json.loads(response.body)["code"] == "platform_unavailable"
    handler = app.exception_handlers[service.SqlPreparationError]
    response = asyncio.run(handler(request, service.SqlPreparationError("local fault")))
    assert response.status_code == 503
    assert json.loads(response.body)["code"] == "sql_preparation_error"
    handler = app.exception_handlers[service.SourceDisabledError]
    response = asyncio.run(handler(request, service.SourceDisabledError("disabled")))
    assert response.status_code == 409
    assert json.loads(response.body)["code"] == "source_disabled"
