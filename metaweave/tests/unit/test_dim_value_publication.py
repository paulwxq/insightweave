"""dim_value 发布（doc 22.2、6.2 §8）：三态拦截、预检载荷与执行链。

三态拦截（配置无效/零表/未配置 embedding_col）落在集中预检：整单
PublishPrecheckError、不创建运行；`classify_dim_tables_yaml` 的归一配置
经 `check_step` payload 传给执行链（同一份配置，不经文件重读）。
"""

from uuid import UUID

import pytest
import yaml

from backend.profile_loading.db_loader import MetaLoadConfigError
from backend.publishing import publish_service
from backend.publishing.precheck import PublishPrecheckError, check_step
from backend.publishing.publish_service import (
    _dim_value_failure_summary,
    create_publish_run,
    execute_publish_run,
)
from tests.unit.test_publish_service import (
    DB_NAME,
    FakeSession,
    PUBLISH_JOB_ID,
    RUN,
    SETTINGS,
    SOURCE_ID,
    WORKSPACE_ID,
    install,
    make_ready_db,
    run_step_row,
    step_dir,
    write_marker,
)


def write_dim_yaml(tmp_path, content):
    dim_dir = step_dir(tmp_path, "dim")
    dim_dir.mkdir(parents=True, exist_ok=True)
    if content is not None:
        (dim_dir / "dim_tables.yaml").write_text(content, encoding="utf-8")
    return dim_dir


def dim_yaml(tables):
    return yaml.safe_dump({"databases": {DB_NAME: {"tables": tables}}})


CONFIGURED_YAML = dim_yaml({"public.t1": {"embedding_col": "name"}})


def dim_ready(tmp_path, content=CONFIGURED_YAML):
    write_marker(tmp_path, "dim")
    return write_dim_yaml(tmp_path, content)


# ---- check_step 三态与载荷 ----


class TestCheckStepDimValue:
    def test_ready_returns_normalized_config_payload(self, monkeypatch, tmp_path):
        install(monkeypatch, tmp_path)
        dim_ready(tmp_path)
        result = check_step(None, SOURCE_ID, WORKSPACE_ID, "dim_value", db_name=DB_NAME)
        assert result.ready is True and result.reason is None
        assert result.input_files == (("dim/dim_tables.yaml", str(RUN)),)
        payload = result.payload
        assert payload.database == DB_NAME
        assert payload.tables["public.t1"].embedding_col == "name"

    def test_missing_yaml(self, monkeypatch, tmp_path):
        install(monkeypatch, tmp_path)
        write_marker(tmp_path, "dim")
        write_dim_yaml(tmp_path, None)
        result = check_step(None, SOURCE_ID, WORKSPACE_ID, "dim_value", db_name=DB_NAME)
        assert result.ready is False
        assert result.reason == "dim/dim_tables.yaml 缺失或不可读"

    def test_invalid_yaml(self, monkeypatch, tmp_path):
        install(monkeypatch, tmp_path)
        dim_ready(tmp_path, "not: [valid\n")
        result = check_step(None, SOURCE_ID, WORKSPACE_ID, "dim_value", db_name=DB_NAME)
        assert result.ready is False
        assert "配置文件无效" in result.reason

    def test_zero_tables(self, monkeypatch, tmp_path):
        install(monkeypatch, tmp_path)
        dim_ready(tmp_path, "databases: {}\n")
        result = check_step(None, SOURCE_ID, WORKSPACE_ID, "dim_value", db_name=DB_NAME)
        assert result.ready is False
        assert result.reason == "dim_tables.yaml 没有任何维度表"

    def test_unconfigured_embedding_col_lists_tables(self, monkeypatch, tmp_path):
        install(monkeypatch, tmp_path)
        content = dim_yaml(
            {
                "public.t1": {"embedding_col": "name"},
                "public.t2": {"embedding_col": None},
            }
        )
        dim_ready(tmp_path, content)
        result = check_step(None, SOURCE_ID, WORKSPACE_ID, "dim_value", db_name=DB_NAME)
        assert result.ready is False
        assert "未配置有效 embedding_col" in result.reason
        assert "public.t2" in result.reason and "public.t1" not in result.reason

    def test_marker_missing_beats_file_check(self, monkeypatch, tmp_path):
        install(monkeypatch, tmp_path)
        write_dim_yaml(tmp_path, CONFIGURED_YAML)  # 有文件无 marker
        result = check_step(None, SOURCE_ID, WORKSPACE_ID, "dim_value", db_name=DB_NAME)
        assert result.ready is False
        assert result.reason == "dim/ 目录没有采集 marker，产物不可用"


# ---- 集中预检整单拦截（A24/A15） ----


class TestPrecheckInterception:
    def test_dim_not_configured_rejects_whole_request(self, monkeypatch, tmp_path):
        """同选 db/dim_value/sql：db、sql 就绪，dim 未配置 → 整单 400、无业务记录。"""
        from tests.unit.test_sql_publication import sql_ready

        make_ready_db(tmp_path)
        sql_ready(tmp_path)
        dim_ready(tmp_path, "databases: {}\n")
        session = install(
            monkeypatch, tmp_path, FakeSession(steps=("db", "dim_value", "sql"))
        )
        with pytest.raises(PublishPrecheckError) as excinfo:
            create_publish_run(
                SETTINGS,
                PUBLISH_JOB_ID,
                revision_no=1,
                mode="default",
                steps=None,
                step_config=None,
            )
        failures = excinfo.value.failures
        assert [item["step"] for item in failures] == ["dim_value"]
        assert "没有任何维度表" in failures[0]["reason"]
        assert session.runs == [] and session.run_steps == []

    def test_unselected_dim_not_ready_does_not_block(self, monkeypatch, tmp_path):
        """只选 db/sql：dim 未就绪不阻断本次发布（预检只查所选步骤）。"""
        from tests.unit.test_sql_publication import sql_ready

        make_ready_db(tmp_path)
        sql_ready(tmp_path)
        dim_ready(tmp_path, "databases: {}\n")
        session = install(
            monkeypatch, tmp_path, FakeSession(steps=("db", "dim_value", "sql"))
        )
        result = create_publish_run(
            SETTINGS,
            PUBLISH_JOB_ID,
            revision_no=1,
            mode="custom",
            steps=["db", "sql"],
            step_config=None,
        )
        assert result["steps"] == ["db", "sql"]
        assert len(session.run_steps) == 2


# ---- 执行链（_publish_dim_value，受管 loader 注入） ----


class FakeLoader:
    """记录 build_dim_value_loader(source, password, workspace, run, dim_tables) 实参。"""

    ctor_args: tuple = ()
    next_result: dict = {}
    validate_result: bool = True
    calls: list = []

    def __init__(self, *args, **kwargs):
        FakeLoader.ctor_args = args

    def validate(self):
        return FakeLoader.validate_result

    def load(self):
        FakeLoader.calls.append("load")
        return dict(FakeLoader.next_result)

    def close(self):
        FakeLoader.calls.append("close")


@pytest.fixture(autouse=True)
def _reset_fake_loader():
    FakeLoader.ctor_args = ()
    FakeLoader.next_result = {"success": True, "records_loaded": 3}
    FakeLoader.validate_result = True
    FakeLoader.calls = []
    yield


def _install_loader(monkeypatch):
    monkeypatch.setattr(
        "backend.security.source_password.decrypt_password", lambda _c: "plain-pw"
    )
    monkeypatch.setattr(
        publish_service,
        "build_dim_value_loader",
        lambda *a, **kwargs: FakeLoader(*a, **kwargs),
    )
    monkeypatch.setattr(
        publish_service, "_milvus_collection", lambda _key: "dim_value_embeddings"
    )


def _publish_dim_value(monkeypatch, tmp_path):
    dim_ready(tmp_path)
    session = install(monkeypatch, tmp_path, FakeSession(steps=("dim_value",)))
    _install_loader(monkeypatch)
    result = create_publish_run(
        SETTINGS,
        PUBLISH_JOB_ID,
        revision_no=1,
        mode="custom",
        steps=["dim_value"],
        step_config=None,
    )
    run_id = UUID(result["publish_run_id"])
    execute_publish_run(SETTINGS, run_id)
    return session, run_id


class TestPublishDimValueExecution:
    def test_success_uses_precheck_payload_not_file(self, monkeypatch, tmp_path):
        session, run_id = _publish_dim_value(monkeypatch, tmp_path)
        step = run_step_row(session, run_id, "dim_value")
        assert step.status == "success"
        assert step.message == "维度值已发布到 Milvus（3 条）"
        # 注入的是预检归一后的同一份配置 + 解密密码（doc 22.2 §7.1）
        source, password, workspace_id, publish_run_id, dim_tables = (
            FakeLoader.ctor_args
        )
        assert source.db_name == DB_NAME
        assert password == "plain-pw"
        assert workspace_id == WORKSPACE_ID
        assert publish_run_id == run_id
        assert dim_tables.database == DB_NAME
        assert dim_tables.tables["public.t1"].embedding_col == "name"
        assert "close" in FakeLoader.calls
        assert step.target_manifest["targets"][0]["type"] == "milvus"

    def test_validate_failure_message(self, monkeypatch, tmp_path):
        FakeLoader.validate_result = False
        session, run_id = _publish_dim_value(monkeypatch, tmp_path)
        step = run_step_row(session, run_id, "dim_value")
        assert step.status == "failed"
        assert step.message == "维度值发布失败：Milvus、源库或 Embedding 服务不可用"
        assert "close" in FakeLoader.calls

    def test_load_failure_summary(self, monkeypatch, tmp_path):
        FakeLoader.next_result = {
            "success": False,
            "error_kind": "embedding",
            "records_loaded": 1,
            "records_failed": 2,
            "records_unconfirmed": 0,
        }
        session, run_id = _publish_dim_value(monkeypatch, tmp_path)
        step = run_step_row(session, run_id, "dim_value")
        assert step.status == "failed"
        assert step.message == (
            "维度值未全部发布到 Milvus（成功 1 条 / 失败 2 条），请重新发布"
        )

    def test_loader_crash_is_generic_failure(self, monkeypatch, tmp_path):
        class ExplodingLoader(FakeLoader):
            def load(self):
                raise RuntimeError("milvus down")

        monkeypatch.setattr(
            "backend.security.source_password.decrypt_password", lambda _c: "plain-pw"
        )
        monkeypatch.setattr(
            publish_service,
            "build_dim_value_loader",
            lambda *a, **kwargs: ExplodingLoader(**kwargs),
        )
        monkeypatch.setattr(
            publish_service, "_milvus_collection", lambda _key: "dim_value_embeddings"
        )
        dim_ready(tmp_path)
        session = install(monkeypatch, tmp_path, FakeSession(steps=("dim_value",)))
        result = create_publish_run(
            SETTINGS,
            PUBLISH_JOB_ID,
            revision_no=1,
            mode="custom",
            steps=["dim_value"],
            step_config=None,
        )
        execute_publish_run(SETTINGS, UUID(result["publish_run_id"]))
        step = run_step_row(session, UUID(result["publish_run_id"]), "dim_value")
        assert step.status == "failed"
        assert step.message == "发布执行失败（RuntimeError）"

    def test_disabled_source_blocks_at_execution(self, monkeypatch, tmp_path):
        dim_ready(tmp_path)
        session = install(monkeypatch, tmp_path, FakeSession(steps=("dim_value",)))
        _install_loader(monkeypatch)
        result = create_publish_run(
            SETTINGS,
            PUBLISH_JOB_ID,
            revision_no=1,
            mode="custom",
            steps=["dim_value"],
            step_config=None,
        )
        run_id = UUID(result["publish_run_id"])
        session.source.is_active = False
        execute_publish_run(SETTINGS, run_id)
        step = run_step_row(session, run_id, "dim_value")
        assert step.status == "blocked"
        assert "禁用" in step.message
        assert FakeLoader.calls == []

    def test_workspace_mismatch_fails_requirement(self, monkeypatch, tmp_path):
        from types import SimpleNamespace

        from backend.profile_loading.db_loader import require_workspace_source

        session = FakeSession()
        session.workspace = SimpleNamespace(
            workspace_id=WORKSPACE_ID, source_id="db_src_9"
        )
        install(monkeypatch, tmp_path, session)
        with pytest.raises(MetaLoadConfigError, match="工作区不属于该数据源"):
            require_workspace_source(session, WORKSPACE_ID, SOURCE_ID)


# ---- 失败文案分派（doc 22.2 §11 A28 六行表） ----


class TestFailureSummary:
    def test_schema_mismatch_hint(self):
        message = _dim_value_failure_summary({"error_kind": "schema_mismatch"})
        assert "手动删除该集合" in message

    def test_unconfirmed_counts(self):
        message = _dim_value_failure_summary(
            {
                "success": False,
                "error_kind": "embedding",
                "records_loaded": 1,
                "records_failed": 2,
                "records_unconfirmed": 3,
            }
        )
        assert "成功 1 条 / 失败 2 条" in message
        assert "另有 3 条写入结果未确认" in message

    def test_source_failure_names_table(self):
        message = _dim_value_failure_summary(
            {
                "success": False,
                "error_kind": "source",
                "failed_table": "public.t1",
                "error_summary": "relation 不存在",
            }
        )
        assert "public.t1" in message and "relation 不存在" in message

    def test_input_failure(self):
        assert (
            _dim_value_failure_summary({"success": False, "error_kind": "input"})
            == "维度值发布输入不可用，请检查 dim 产物"
        )

    def test_plain_counts(self):
        message = _dim_value_failure_summary(
            {"success": False, "records_loaded": 1, "records_failed": 2}
        )
        assert (
            message == "维度值未全部发布到 Milvus（成功 1 条 / 失败 2 条），请重新发布"
        )
