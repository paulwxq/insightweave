"""受管 SQL 步骤 worker 用例（doc 25.1、doc 6 §4.1）。

断代改造适配：上游就绪判定读 `md/`、`domain/`、`rel/` 顶层唯一成功采集
marker 与文件指纹；产物目录 `sql/`（原 sql-rag 更名）；worker 签名
`run_sql_step(ctx) -> StepOutcome`，不再接触平台库。
"""

from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api.errors import register_exception_handlers
from backend.data_sources import sql_step_service as service
from backend.data_sources.sql_step_service import SqlPreparationError
from backend.data_sources.step_context import DbStepConfigError, StepContext
from backend.jobs.collection_run import (
    UnsupportedCollectionStepError,
    _blocked_by_same_request_upstream,
    normalize_collection_steps,
)
from backend.persistence.models import DbSourceInfo
from metaweave.core.sql_rag.execution import SqlRagRunResult

SOURCE_ID = "db_src_1"
WORKSPACE_ID = "ws_1"
RUN_ID = UUID("11111111-2222-4333-8444-555555555555")
MD_RUN_ID = UUID("aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee")
DOMAIN_RUN_ID = UUID("cccccccc-dddd-4eee-8fff-000000000000")
REL_RUN_ID = UUID("eeeeeeee-ffff-4aaa-8bbb-111111111111")
DB_NAME = "demo"


class FakeLLM:
    model = "fake"

    def __init__(self, *_args, **_kwargs):
        self.calls = []

    def call_llm(self, prompt, system_message=None):
        self.calls.append(prompt)
        return '[{"question":"有多少订单？","sql":"SELECT count(*) FROM orders"}]'


def _config(**validation):
    return {
        "sql_rag": {
            "generation": {"questions_per_domain": 1, "skip_uncategorized": False},
            "validation": {"enabled": False, **validation},
        },
        "llm": {},
    }


def _files(tmp_path, monkeypatch, *, domain_tables=None, domains=None):
    """构造工作区上游产物（md/domain/rel + 各自成功 marker），项目根指向 tmp_path。"""
    from backend.workspace_files.markers import write_collection_marker

    monkeypatch.setattr("metaweave.utils.file_utils.get_project_root", lambda: tmp_path)
    import backend.security.source_password as crypto

    monkeypatch.setattr(crypto, "decrypt_password", lambda _cipher: "secret")
    workspace = tmp_path / "output" / SOURCE_ID / WORKSPACE_ID
    for name in ("md", "domain", "rel", "sql"):
        (workspace / name).mkdir(parents=True)
    (workspace / "md" / "demo.public.orders.md").write_text(
        "# 订单表 orders\n字段 id", encoding="utf-8"
    )
    if domains is None:
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
            {"database": {"name": DB_NAME}, "domains": domains}, allow_unicode=True
        ),
        encoding="utf-8",
    )
    (workspace / "rel" / "demo.relationships_global.md").write_text(
        "# 表间关系发现报告\ndatabase: demo\n零关系", encoding="utf-8"
    )
    write_collection_marker(workspace / "md", MD_RUN_ID, "success")
    write_collection_marker(workspace / "domain", DOMAIN_RUN_ID, "success")
    write_collection_marker(workspace / "rel", REL_RUN_ID, "success")
    return workspace


def _install(monkeypatch, workspace, config, *, on_generate=None, generation=None):
    """LLM 与生成执行换替身；inspect/备份/核验走真实实现。"""
    import json as _json

    llm = FakeLLM()
    monkeypatch.setattr(service, "resolve_module_llm_config", lambda *_args: {})
    monkeypatch.setattr(service, "LLMService", lambda *_args: llm)
    state = SimpleNamespace(llm=llm, generation_calls=[], on_generate=on_generate)

    def fake_run_generation(
        managed_config,
        *,
        domains_path,
        md_dir,
        rel_dir,
        output_dir,
        llm_service,
        connector=None,
    ):
        state.generation_calls.append({"output_dir": output_dir})
        pairs = [{"question": "有多少订单？", "sql": "SELECT count(*) FROM orders"}]
        (output_dir / f"qs_{DB_NAME}_pair.json").write_text(
            _json.dumps(pairs), encoding="utf-8"
        )
        # 生成落盘后的钩子：模拟"生成完成后"对产物或上游的并发改动
        if state.on_generate is not None:
            state.on_generate()
        if generation is not None:
            return SqlRagRunResult(generation=generation, validation=None)
        return SqlRagRunResult(
            generation=SimpleNamespace(
                success=True,
                total_generated=1,
                skipped_domains=["空域"],
                failed_domains=[],
                zero_output_domains=[],
            ),
            validation=None,
        )

    monkeypatch.setattr(service, "run_sql_rag_generation", fake_run_generation)
    monkeypatch.setattr(
        service,
        "DatabaseConnector",
        lambda *_args: pytest.fail("disabled validation must not connect"),
    )
    return state


def _ctx(config, step_params=None):
    return StepContext(
        settings=SimpleNamespace(),
        source=DbSourceInfo(
            source_id=SOURCE_ID,
            display_name="测试源",
            description=None,
            db_type="postgresql",
            host="localhost",
            port=5432,
            db_name=DB_NAME,
            username="tester",
            password_ciphertext="cipher",
            sslmode=None,
            db_version=None,
            selected_schemas=["public"],
            is_active=True,
        ),
        main_config=config,
        config_path=Path("cfg.yaml"),
        source_id=SOURCE_ID,
        workspace_id=WORKSPACE_ID,
        run_id=RUN_ID,
        step_name="sql",
        step_params=step_params,
    )


# ---- 错误类型与 API 映射 ----


def test_sql_preparation_error_maps_to_dedicated_503():
    app = FastAPI()
    register_exception_handlers(app)

    @app.get("/boom")
    def _boom():
        raise SqlPreparationError("SQL 准备阶段故障")

    client = TestClient(app, raise_server_exceptions=False)
    response = client.get("/boom")
    assert response.status_code == 503
    assert response.json()["code"] == "sql_preparation_error"


# ---- inspect_managed_sql_inputs（就绪判定与指纹） ----


def test_inspect_ready_inputs_returns_markers_and_fingerprints(tmp_path, monkeypatch):
    workspace = _files(tmp_path, monkeypatch)
    snapshot, missing, reason = service.inspect_managed_sql_inputs(
        SOURCE_ID, WORKSPACE_ID, DB_NAME, _config()
    )
    assert snapshot is not None and missing == [] and reason == ""
    assert dict(snapshot.markers) == {
        "md": str(MD_RUN_ID),
        "domain": str(DOMAIN_RUN_ID),
        "rel": str(REL_RUN_ID),
    }
    assert [path for path, _ in snapshot.fingerprints] == [
        "domain/db_domains.yaml",
        "md/demo.public.orders.md",
        "rel/demo.relationships_global.md",
    ]
    assert snapshot.output_dir == workspace / "sql"


def test_inspect_reports_missing_markers(tmp_path, monkeypatch):
    """无任何 marker：三个上游全部列入 missing，不读产物内容兜底。"""
    workspace = _files(tmp_path, monkeypatch)
    for name in ("md", "domain", "rel"):
        for marker in (workspace / name).glob(".coll_run_id_*"):
            marker.unlink()
    snapshot, missing, reason = service.inspect_managed_sql_inputs(
        SOURCE_ID, WORKSPACE_ID, DB_NAME, _config()
    )
    assert snapshot is None
    assert missing == ["domain", "md", "rel"]
    assert "没有成功采集 marker" in reason


def test_inspect_failed_marker_blocks(tmp_path, monkeypatch):
    from backend.workspace_files.markers import write_collection_marker

    workspace = _files(tmp_path, monkeypatch)
    write_collection_marker(workspace / "rel", REL_RUN_ID, "failed")
    snapshot, missing, _ = service.inspect_managed_sql_inputs(
        SOURCE_ID, WORKSPACE_ID, DB_NAME, _config()
    )
    assert snapshot is None and missing == ["rel"]


@pytest.mark.parametrize(
    "problem",
    [
        "md_bad_name",
        "domain_db_mismatch",
        "domain_table_without_md",
        "rel_header_mismatch",
        "no_participating_domain",
    ],
)
def test_inspect_rejects_invalid_content(tmp_path, monkeypatch, problem):
    workspace = _files(tmp_path, monkeypatch)
    if problem == "md_bad_name":
        (workspace / "md" / "demo.public.orders.md").rename(
            workspace / "md" / "bad-name.md"
        )
        expect_step = "md"
    elif problem == "domain_db_mismatch":
        (workspace / "domain" / "db_domains.yaml").write_text(
            yaml.safe_dump(
                {
                    "database": {"name": "other"},
                    "domains": [{"name": "d", "tables": ["demo.public.orders"]}],
                }
            ),
            encoding="utf-8",
        )
        expect_step = "domain"
    elif problem == "domain_table_without_md":
        (workspace / "domain" / "db_domains.yaml").write_text(
            yaml.safe_dump(
                {
                    "database": {"name": DB_NAME},
                    "domains": [{"name": "d", "tables": ["demo.public.ghost"]}],
                }
            ),
            encoding="utf-8",
        )
        expect_step = "domain"
    elif problem == "rel_header_mismatch":
        (workspace / "rel" / "demo.relationships_global.md").write_text(
            "# 其他报告\ndatabase: other\n", encoding="utf-8"
        )
        expect_step = "rel"
    else:
        (workspace / "domain" / "db_domains.yaml").write_text(
            yaml.safe_dump(
                {
                    "database": {"name": DB_NAME},
                    "domains": [{"name": "空域", "tables": []}],
                }
            ),
            encoding="utf-8",
        )
        expect_step = "domain"
    snapshot, missing, reason = service.inspect_managed_sql_inputs(
        SOURCE_ID, WORKSPACE_ID, DB_NAME, _config()
    )
    assert snapshot is None
    assert expect_step in missing
    assert reason


def test_inspect_skip_uncategorized_controls_participation(tmp_path, monkeypatch):
    """仅 _未分类_ 域：skip_uncategorized=true 时无参与域，false 时参与。"""
    workspace = _files(
        tmp_path,
        monkeypatch,
        domains=[{"name": "_未分类_", "tables": ["demo.public.orders"]}],
    )
    snapshot, missing, _ = service.inspect_managed_sql_inputs(
        SOURCE_ID, WORKSPACE_ID, DB_NAME, _config()
    )
    assert snapshot is not None  # 默认 skip_uncategorized=False → 参与域存在
    skip_config = deepcopy(_config())
    skip_config["sql_rag"]["generation"]["skip_uncategorized"] = True
    snapshot2, missing2, reason2 = service.inspect_managed_sql_inputs(
        SOURCE_ID, WORKSPACE_ID, DB_NAME, skip_config
    )
    assert snapshot2 is None and "domain" in missing2
    assert "没有可生成 SQL 的业务域" in reason2


# ---- run_sql_step：准备与成功路径 ----


def test_run_sql_step_success_skips_validation_and_connects_nothing(
    tmp_path, monkeypatch
):
    workspace = _files(tmp_path, monkeypatch)
    config = _config()
    state = _install(monkeypatch, workspace, config)
    # 预置旧产物：随整目录备份移走
    sql_dir = workspace / "sql"
    (sql_dir / "qs_demo_pair.json.bak").write_text("old", encoding="utf-8")

    outcome = service.run_sql_step(_ctx(config))

    assert outcome.success is True, outcome.message
    assert "生成 1 条，保留 1 条" in outcome.message
    assert "SQL 校验已跳过" in outcome.message
    # 输入清单：三个上游文件各带采集来源运行（doc 6.1 §11）
    assert outcome.input_files == [
        ("domain/db_domains.yaml", str(DOMAIN_RUN_ID)),
        ("md/demo.public.orders.md", str(MD_RUN_ID)),
        ("rel/demo.relationships_global.md", str(REL_RUN_ID)),
    ]
    assert outcome.output_files == ["sql/qs_demo_pair.json"]
    # 旧产物进入备份
    (backup,) = list((sql_dir / ".backup").iterdir())
    assert (backup / "qs_demo_pair.json.bak").exists()


def test_run_sql_step_rejects_when_upstream_not_ready(tmp_path, monkeypatch):
    workspace = _files(tmp_path, monkeypatch)
    for marker in (workspace / "domain").glob(".coll_run_id_*"):
        marker.unlink()
    config = _config()
    _install(monkeypatch, workspace, config)
    # 预置旧产物：准备失败不动旧内容
    (workspace / "sql" / "old.json").write_text("keep", encoding="utf-8")
    with pytest.raises(DbStepConfigError, match="SQL 输入未就绪（domain）"):
        service.run_sql_step(_ctx(config))
    assert (workspace / "sql" / "old.json").exists()
    assert not (workspace / "sql" / ".backup").exists()


def test_no_usable_domain_does_not_clear_previous_output(tmp_path, monkeypatch):
    workspace = _files(
        tmp_path,
        monkeypatch,
        domains=[{"name": "空域", "tables": []}],
    )
    config = _config()
    _install(monkeypatch, workspace, config)
    (workspace / "sql" / "qs_demo_pair.json").write_text(
        '[{"q":"旧","sql":"SELECT 1"}]', encoding="utf-8"
    )
    with pytest.raises(DbStepConfigError):
        service.run_sql_step(_ctx(config))
    assert (workspace / "sql" / "qs_demo_pair.json").exists()


def test_input_edit_during_generation_marks_sql_failed(tmp_path, monkeypatch):
    """生成期间上游文件被编辑（指纹变化）：本次产物无效（既有冲突检查）。"""
    workspace = _files(tmp_path, monkeypatch)
    config = _config()

    def on_generate():
        (workspace / "md" / "demo.public.orders.md").write_text(
            "# 订单表 orders\n字段 id, amount", encoding="utf-8"
        )

    _install(monkeypatch, workspace, config, on_generate=on_generate)
    outcome = service.run_sql_step(_ctx(config))
    assert outcome.success is False
    assert "上游输入在生成期间发生变化" in outcome.message


def test_input_marker_rewrite_during_generation_marks_sql_failed(tmp_path, monkeypatch):
    """生成期间上游被重跑（marker 变化）：同样判无效。"""
    from backend.workspace_files.markers import write_collection_marker

    workspace = _files(tmp_path, monkeypatch)
    config = _config()

    def on_generate():
        write_collection_marker(
            workspace / "rel", UUID("99999999-9999-4999-8999-999999999999"), "success"
        )

    _install(monkeypatch, workspace, config, on_generate=on_generate)
    outcome = service.run_sql_step(_ctx(config))
    assert outcome.success is False
    assert "上游输入在生成期间发生变化" in outcome.message


def test_all_domains_failing_generation_reports_details(tmp_path, monkeypatch):
    workspace = _files(tmp_path, monkeypatch)
    config = _config()
    generation = SimpleNamespace(
        success=False,
        total_generated=0,
        skipped_domains=[],
        failed_domains=["订单"],
        zero_output_domains=[],
    )
    _install(monkeypatch, workspace, config, generation=generation)
    outcome = service.run_sql_step(_ctx(config))
    assert outcome.success is False
    assert "所有业务域均未生成 SQL 样例" in outcome.message
    assert "生成失败域：订单" in outcome.message


@pytest.mark.parametrize(
    "config",
    [
        {
            **_config(),
            "sql_rag": {
                "generation": {"questions_per_domain": "x"},
                "validation": {"enabled": False},
            },
        },
        {**_config(), "sql_rag": {"generation": {}, "validation": {"enabled": "yes"}}},
        {
            **_config(),
            "sql_rag": {
                "generation": {},
                "validation": {"enabled": True, "max_retries": 2},  # 已废弃键名
            },
        },
        {
            **_config(),
            "sql_rag": {
                "generation": {},
                "validation": {"enabled": True, "timeout": 0},  # 低于最小值 1
            },
        },
    ],
)
def test_invalid_generation_config_rejected_before_any_side_effect(
    tmp_path, monkeypatch, config
):
    workspace = _files(tmp_path, monkeypatch)
    _install(monkeypatch, workspace, config)
    with pytest.raises(DbStepConfigError):
        service.run_sql_step(_ctx(config))
    assert not (workspace / "sql" / ".backup").exists()


def test_llm_failure_in_preparation_is_preparation_error(tmp_path, monkeypatch):
    workspace = _files(tmp_path, monkeypatch)
    config = _config()
    _install(monkeypatch, workspace, config)

    class _Boom(Exception):
        pass

    def boom(*_args):
        raise _Boom("llm broken")

    monkeypatch.setattr(service, "LLMService", boom)
    with pytest.raises(SqlPreparationError, match="SQL 准备阶段故障"):
        service.run_sql_step(_ctx(config))


def test_main_file_missing_after_generation_fails(tmp_path, monkeypatch):
    workspace = _files(tmp_path, monkeypatch)
    config = _config()

    def on_generate():
        (workspace / "sql" / f"qs_{DB_NAME}_pair.json").unlink()

    _install(monkeypatch, workspace, config, on_generate=on_generate)
    outcome = service.run_sql_step(_ctx(config))
    assert outcome.success is False
    assert "SQL 样例主文件缺失" in outcome.message


def test_main_file_symlink_fails(tmp_path, monkeypatch):
    workspace = _files(tmp_path, monkeypatch)
    config = _config()

    def on_generate():
        target = workspace / "sql" / f"qs_{DB_NAME}_pair.json"
        target.unlink()
        outside = workspace / "sql_outside.json"
        outside.write_text("[]", encoding="utf-8")
        target.symlink_to(outside)

    _install(monkeypatch, workspace, config, on_generate=on_generate)
    outcome = service.run_sql_step(_ctx(config))
    assert outcome.success is False
    assert "主文件缺失或位置错误" in outcome.message


# ---- 校验启用路径 ----


class _FakeConnector:
    def __init__(self, state, fail=False):
        self._state = state
        self._fail = fail
        self.closed = False

    def get_connection(self):
        state = self._state

        class _Ctx:
            def __enter__(self):
                if state.connect_fail:
                    raise RuntimeError("pool unreachable")
                return object()

            def __exit__(self, *_):
                return False

        return _Ctx()

    def close(self):
        self.closed = True
        self._state.connectors.append("closed")


def test_validation_enabled_connects_and_closes_connector(tmp_path, monkeypatch):
    workspace = _files(tmp_path, monkeypatch)
    config = _config(enabled=True)
    state = SimpleNamespace(connect_fail=False, connectors=[])
    _install(monkeypatch, workspace, config)
    made = []

    def fake_connector(*_args):
        connector = _FakeConnector(state)
        made.append(connector)
        return connector

    monkeypatch.setattr(service, "DatabaseConnector", fake_connector)
    # 校验统计与生成数量一致、无删除；替身同时落盘主文件与校验报告
    generation = SimpleNamespace(
        success=True,
        total_generated=1,
        skipped_domains=[],
        failed_domains=[],
        zero_output_domains=[],
    )

    def generation_with_validation(managed_config, *, output_dir, **_kwargs):
        import json as _json

        (output_dir / f"qs_{DB_NAME}_pair.json").write_text(
            _json.dumps([{"question": "q", "sql": "SELECT 1"}]),
            encoding="utf-8",
        )
        (output_dir / "sql_validation_summary_demo.log").write_text(
            "summary", encoding="utf-8"
        )
        return SqlRagRunResult(
            generation=generation,
            validation={
                "total": 1,
                "invalid": 0,
                "repair_apply_stats": {"deleted": 0},
            },
        )

    monkeypatch.setattr(service, "run_sql_rag_generation", generation_with_validation)
    outcome = service.run_sql_step(_ctx(config))
    assert outcome.success is True, outcome.message
    assert "SQL 校验已执行" in outcome.message
    assert made and made[0].closed


def test_validation_enabled_connection_failure_preserves_old_output(
    tmp_path, monkeypatch
):
    """连接失败发生在备份之前：旧产物保持原状。"""
    workspace = _files(tmp_path, monkeypatch)
    config = _config(enabled=True)
    state = SimpleNamespace(connect_fail=True, connectors=[])
    _install(monkeypatch, workspace, config)
    monkeypatch.setattr(
        service, "DatabaseConnector", lambda *_args: _FakeConnector(state)
    )
    (workspace / "sql" / "old.json").write_text("keep", encoding="utf-8")

    outcome = service.run_sql_step(_ctx(config))

    assert outcome.success is False
    assert (workspace / "sql" / "old.json").exists()
    assert not (workspace / "sql" / ".backup").exists()


# ---- 采集步骤排序与同请求阻断（SQL 相关） ----


def test_sql_collection_order_and_dim_rename():
    assert normalize_collection_steps(["sql", "md"]) == ["md", "sql"]
    # dim_config 已随断代改造更名为 dim（doc 6 §2）
    with pytest.raises(UnsupportedCollectionStepError):
        normalize_collection_steps(["dim_config"])
    assert normalize_collection_steps(["dim"]) == ["dim"]


def test_sql_blocked_by_same_request_upstream_failure():
    results = [{"step": "md", "status": "failed"}]
    message = _blocked_by_same_request_upstream("sql", ["md", "sql"], results)
    assert message is not None and "MD 未成功" in message
    # 上游成功则不阻断
    assert (
        _blocked_by_same_request_upstream(
            "sql", ["md", "sql"], [{"step": "md", "status": "success"}]
        )
        is None
    )
