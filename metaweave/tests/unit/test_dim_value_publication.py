"""dim_value 发布（doc 22.2、6.2 §8；doc 7.2 §5.2.1/§5.3 改造后）。

准入最小集：dim 目录成功 marker + JSON 画像候选与字段范围 + 本次有效
清单（版本默认值 + 允许覆盖）校验；不再读取 dim_tables.yaml 作为运行
配置。有效清单固化在运行步骤 step_config，执行期从快照构造配置对象
注入加载器，并复查外部条件（画像字段仍在、源启用等）。
"""

import json
from uuid import UUID

import pytest
import yaml

from backend.profile_loading.db_loader import MetaLoadConfigError
from backend.profile_loading.dim_value_config_api import DimValueEmptyTablesError
from backend.publishing import publish_service
from backend.publishing.precheck import PublishPrecheckError, check_step
from backend.publishing.publish_service import (
    PublishRunError,
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


def write_json_profile(tmp_path, object_name, columns, schema="public", database=DB_NAME):
    """写一份受管 JSON 画像产物（object_info + column_profiles 键）。"""
    json_dir = step_dir(tmp_path, "json")
    json_dir.mkdir(parents=True, exist_ok=True)
    document = {
        "object_info": {
            "database": database,
            "schema_name": schema,
            "object_name": object_name,
            "object_type": "table",
        },
        "column_profiles": {
            name: {"ordinal_position": index + 1, "data_type": "text"}
            for index, name in enumerate(columns)
        },
    }
    (json_dir / f"{database}.{schema}.{object_name}.json").write_text(
        json.dumps(document), encoding="utf-8"
    )
    return json_dir


def dim_table_param(object_name, columns, schema="public"):
    return {
        "schema_name": schema,
        "object_name": object_name,
        "embedding_col": list(columns),
    }


def dim_effective(tables):
    return {"dim_value": {"tables": tables}}


def dim_version_config(tables):
    """作业版本 step_config 形态（与 dim_effective 相同的外层结构）。"""
    return {"dim_value": {"tables": tables}}


# ---- check_step：准入最小集与有效清单（doc 7.2 §5.2.1） ----


class TestCheckStepDimValue:
    def test_workspace_check_ready_with_json_candidates(self, monkeypatch, tmp_path):
        """publish-check GET（无有效参数）：marker + JSON 候选可读即就绪。"""
        install(monkeypatch, tmp_path)
        dim_ready(tmp_path)
        write_json_profile(tmp_path, "t1", ["name"])
        result = check_step(None, SOURCE_ID, WORKSPACE_ID, "dim_value", db_name=DB_NAME)
        assert result.ready is True and result.reason is None
        assert result.payload is None  # 只读工作区检查不产出配置载荷

    def test_workspace_check_without_candidates(self, monkeypatch, tmp_path):
        install(monkeypatch, tmp_path)
        dim_ready(tmp_path)  # marker 在，但 json 目录没有画像产物
        result = check_step(None, SOURCE_ID, WORKSPACE_ID, "dim_value", db_name=DB_NAME)
        assert result.ready is False
        assert "没有可配置的维表候选" in result.reason

    def test_workspace_check_ignores_broken_yaml(self, monkeypatch, tmp_path):
        """doc 7.2 §5.2.1：YAML 缺失/损坏不再影响准入（有效配置来自版本/覆盖）。"""
        install(monkeypatch, tmp_path)
        dim_ready(tmp_path, "not: [valid\n")
        write_json_profile(tmp_path, "t1", ["name"])
        result = check_step(None, SOURCE_ID, WORKSPACE_ID, "dim_value", db_name=DB_NAME)
        assert result.ready is True

    def test_marker_missing_beats_file_check(self, monkeypatch, tmp_path):
        install(monkeypatch, tmp_path)
        write_dim_yaml(tmp_path, CONFIGURED_YAML)  # 有文件无 marker
        write_json_profile(tmp_path, "t1", ["name"])
        result = check_step(None, SOURCE_ID, WORKSPACE_ID, "dim_value", db_name=DB_NAME)
        assert result.ready is False
        assert result.reason == "dim/ 目录没有采集 marker，产物不可用"

    def test_effective_ready_payload_uses_field_arrays(self, monkeypatch, tmp_path):
        """有效清单校验通过：载荷为 DimTablesConfig，embedding_col 为数组。"""
        session = install(monkeypatch, tmp_path)
        write_marker(tmp_path, "dim")
        write_json_profile(tmp_path, "t1", ["hall_id", "name", "other"])
        tables = [dim_table_param("t1", ["hall_id", "name"])]
        result = check_step(
            session,
            SOURCE_ID,
            WORKSPACE_ID,
            "dim_value",
            db_name=DB_NAME,
            effective_params=dim_effective(tables),
        )
        assert result.ready is True and result.reason is None
        payload = result.payload
        assert payload.database == DB_NAME
        # 数组形态直接注入，不经字符串逗号拆分（doc 7.2 §3.1.1/验收 25）
        assert payload.tables["public.t1"].embedding_col == ["hall_id", "name"]
        assert payload.tables["public.t1"].embedding_cols_list == ["hall_id", "name"]
        # 输入清单只登记实际读取的 JSON 画像，不登记未读取的 dim YAML
        assert [path for path, _ in result.input_files] == [
            f"json/{DB_NAME}.public.t1.json"
        ]

    def test_effective_missing_tables_key(self, monkeypatch, tmp_path):
        session = install(monkeypatch, tmp_path)
        write_marker(tmp_path, "dim")
        write_json_profile(tmp_path, "t1", ["name"])
        result = check_step(
            session,
            SOURCE_ID,
            WORKSPACE_ID,
            "dim_value",
            db_name=DB_NAME,
            effective_params={"dim_value": {}},
        )
        assert result.ready is False
        assert "缺少必需配置" in result.reason

    def test_effective_empty_tables_keeps_code(self, monkeypatch, tmp_path):
        """显式 tables: [] 沿用 dim_value_empty_tables 及既有文案（验收 17）。"""
        session = install(monkeypatch, tmp_path)
        write_marker(tmp_path, "dim")
        write_json_profile(tmp_path, "t1", ["name"])
        result = check_step(
            session,
            SOURCE_ID,
            WORKSPACE_ID,
            "dim_value",
            db_name=DB_NAME,
            effective_params=dim_effective([]),
        )
        assert result.ready is False
        assert result.code == "dim_value_empty_tables"
        assert result.reason == str(DimValueEmptyTablesError())

    def test_effective_unknown_table_listed(self, monkeypatch, tmp_path):
        session = install(monkeypatch, tmp_path)
        write_marker(tmp_path, "dim")
        write_json_profile(tmp_path, "t1", ["name"])
        result = check_step(
            session,
            SOURCE_ID,
            WORKSPACE_ID,
            "dim_value",
            db_name=DB_NAME,
            effective_params=dim_effective([dim_table_param("ghost", ["name"])]),
        )
        assert result.ready is False
        assert "public.ghost" in result.reason
        assert "编辑作业创建新版本" in result.reason

    def test_effective_unknown_field_listed(self, monkeypatch, tmp_path):
        session = install(monkeypatch, tmp_path)
        write_marker(tmp_path, "dim")
        write_json_profile(tmp_path, "t1", ["name"])
        result = check_step(
            session,
            SOURCE_ID,
            WORKSPACE_ID,
            "dim_value",
            db_name=DB_NAME,
            effective_params=dim_effective([dim_table_param("t1", ["name", "gone"])]),
        )
        assert result.ready is False
        assert "gone" in result.reason

    def test_effective_empty_field_array_rejected(self, monkeypatch, tmp_path):
        """每表字段为空按未配置处理，与 tables: [] 的零表语义分开（§5.2）。"""
        session = install(monkeypatch, tmp_path)
        write_marker(tmp_path, "dim")
        write_json_profile(tmp_path, "t1", ["name"])
        result = check_step(
            session,
            SOURCE_ID,
            WORKSPACE_ID,
            "dim_value",
            db_name=DB_NAME,
            effective_params=dim_effective([dim_table_param("t1", [])]),
        )
        assert result.ready is False
        assert result.code is None
        assert "未指定 embedding_col" in result.reason
        assert "public.t1" in result.reason

    def test_effective_foreign_database_profile_rejected(self, monkeypatch, tmp_path):
        """登记边界（§5.2.1）：画像库名与登记库名不一致时按失效处理，
        不因文件位于本工作区目录而通过（审核问题 2 回归）。"""
        session = install(monkeypatch, tmp_path)
        write_marker(tmp_path, "dim")
        # 残留/错误画像：内容指向 foreign 库的 secret.t
        write_json_profile(tmp_path, "t", ["secret_col"], schema="secret", database="foreign")
        result = check_step(
            session,
            SOURCE_ID,
            WORKSPACE_ID,
            "dim_value",
            db_name=DB_NAME,
            effective_params=dim_effective(
                [dim_table_param("t", ["secret_col"], schema="secret")]
            ),
        )
        assert result.ready is False
        assert "secret.t" in result.reason
        assert "超出登记范围" in result.reason

    def test_effective_schema_outside_registration_rejected(self, monkeypatch, tmp_path):
        """登记边界：库名一致但 schema 不在 selected_schemas 内同样拒绝。"""
        session = install(monkeypatch, tmp_path)
        write_marker(tmp_path, "dim")
        write_json_profile(tmp_path, "t2", ["name"], schema="audit")
        result = check_step(
            session,
            SOURCE_ID,
            WORKSPACE_ID,
            "dim_value",
            db_name=DB_NAME,
            effective_params=dim_effective(
                [dim_table_param("t2", ["name"], schema="audit")]
            ),
        )
        assert result.ready is False
        assert "audit.t2" in result.reason
        assert "超出登记范围" in result.reason


# ---- 集中预检整单拦截（A24/A15；doc 7.2 断代与完整替换） ----


class TestPrecheckInterception:
    def test_legacy_revision_without_tables_rejects(self, monkeypatch, tmp_path):
        """断代（doc 7.2 文首/验收 17）：存量版本缺少 dim_value.tables 时
        运行创建整单拒绝并提示保存新版本，不读 YAML 回退。"""
        make_ready_db(tmp_path)
        dim_ready(tmp_path)  # YAML 内容合法也不构成回退
        write_json_profile(tmp_path, "t1", ["name"])
        session = install(
            monkeypatch, tmp_path, FakeSession(steps=("db", "dim_value"))
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
        assert "缺少必需配置" in failures[0]["reason"]
        assert session.runs == [] and session.run_steps == []

    def test_empty_tables_override_rejects_with_code(self, monkeypatch, tmp_path):
        """custom 显式 tables: [] 覆盖版本默认 → 整单 400 + 既有错误码（§3.2）。"""
        write_marker(tmp_path, "dim")
        write_json_profile(tmp_path, "t1", ["name"])
        version = dim_version_config([dim_table_param("t1", ["name"])])
        session = install(
            monkeypatch,
            tmp_path,
            FakeSession(steps=("dim_value",), step_config=version),
        )
        with pytest.raises(PublishPrecheckError) as excinfo:
            create_publish_run(
                SETTINGS,
                PUBLISH_JOB_ID,
                revision_no=1,
                mode="custom",
                steps=["dim_value"],
                step_config={"dim_value": {"tables": []}},
            )
        failures = excinfo.value.failures
        assert failures[0]["step"] == "dim_value"
        assert failures[0]["code"] == "dim_value_empty_tables"
        assert failures[0]["reason"] == str(DimValueEmptyTablesError())
        assert session.runs == [] and session.run_steps == []

    def test_override_replaces_version_tables_completely(self, monkeypatch, tmp_path):
        """完整替换（§3.2/验收 3）：覆盖清单完整固化，删去的表不补回。"""
        write_marker(tmp_path, "dim")
        write_json_profile(tmp_path, "t1", ["name"])
        write_json_profile(tmp_path, "t2", ["code"])
        version = dim_version_config(
            [dim_table_param("t1", ["name"]), dim_table_param("t2", ["code"])]
        )
        session = install(
            monkeypatch,
            tmp_path,
            FakeSession(steps=("dim_value",), step_config=version),
        )
        result = create_publish_run(
            SETTINGS,
            PUBLISH_JOB_ID,
            revision_no=1,
            mode="custom",
            steps=["dim_value"],
            step_config={
                "dim_value": {"tables": [dim_table_param("t2", ["code"])]}
            },
        )
        run_id = UUID(result["publish_run_id"])
        step = run_step_row(session, run_id, "dim_value")
        # 运行步骤 step_config 为本步骤自己的参数子对象（无外层步骤键）
        assert step.step_config == {"tables": [dim_table_param("t2", ["code"])]}

    def test_step_config_for_unselected_step_rejected(self, monkeypatch, tmp_path):
        """验收 24：custom 对本次未选中的步骤提交 step_config → 400。"""
        make_ready_db(tmp_path)
        session = install(monkeypatch, tmp_path, FakeSession(steps=("db",)))
        with pytest.raises(PublishRunError):
            create_publish_run(
                SETTINGS,
                PUBLISH_JOB_ID,
                revision_no=1,
                mode="custom",
                steps=["db"],
                step_config={
                    "dim_value": {"tables": [dim_table_param("t1", ["name"])]}
                },
            )
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


# ---- 执行链（_publish_dim_value，运行快照注入，doc 7.2 §5.3） ----


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


VERSION_TABLES = [dim_table_param("t1", ["hall_id", "name"])]


def _publish_dim_value(monkeypatch, tmp_path, *, yaml_after_create=None):
    """版本含完整 dim_value.tables；创建运行后可按参数改坏 YAML 再执行。"""
    write_marker(tmp_path, "dim")
    write_dim_yaml(tmp_path, CONFIGURED_YAML)
    write_json_profile(tmp_path, "t1", ["hall_id", "name", "other"])
    session = install(
        monkeypatch,
        tmp_path,
        FakeSession(
            steps=("dim_value",), step_config=dim_version_config(VERSION_TABLES)
        ),
    )
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
    if yaml_after_create == "break":
        write_dim_yaml(tmp_path, "not: [valid\n")  # 创建后改坏 YAML，不影响运行
    elif yaml_after_create == "remove":
        (step_dir(tmp_path, "dim") / "dim_tables.yaml").unlink()
    execute_publish_run(SETTINGS, run_id)
    return session, run_id


class TestPublishDimValueExecution:
    def test_success_uses_run_snapshot_not_file(self, monkeypatch, tmp_path):
        """验收 1/3/7/22：执行用运行快照（多字段数组注入），创建后改坏
        共享 YAML 不影响本次运行，也不经字符串逗号拆分路径。"""
        session, run_id = _publish_dim_value(
            monkeypatch, tmp_path, yaml_after_create="break"
        )
        step = run_step_row(session, run_id, "dim_value")
        assert step.status == "success"
        assert step.message == "维度值已发布到 Milvus（3 条）"
        source, password, workspace_id, publish_run_id, dim_tables = (
            FakeLoader.ctor_args
        )
        assert source.db_name == DB_NAME
        assert password == "plain-pw"
        assert workspace_id == WORKSPACE_ID
        assert publish_run_id == run_id
        assert dim_tables.database == DB_NAME
        assert dim_tables.tables["public.t1"].embedding_col == ["hall_id", "name"]
        assert "close" in FakeLoader.calls
        assert step.target_manifest["targets"][0]["type"] == "milvus"
        # 输入清单登记实际读取的 JSON 画像，不登记未读取的 dim YAML
        assert "dim/dim_tables.yaml" not in json.dumps(
            step.input_manifest or {}, ensure_ascii=False
        )

    def test_snapshot_missing_blocks_at_execution(self, monkeypatch, tmp_path):
        """运行快照缺少 tables 配置（异常存量行）→ 执行期 blocked，不回退 YAML。"""
        write_marker(tmp_path, "dim")
        write_dim_yaml(tmp_path, CONFIGURED_YAML)
        write_json_profile(tmp_path, "t1", ["hall_id", "name"])
        session = install(
            monkeypatch,
            tmp_path,
            FakeSession(
                steps=("dim_value",), step_config=dim_version_config(VERSION_TABLES)
            ),
        )
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
        # 创建后、执行前清掉快照，模拟断代存量运行行
        run_step_row(session, run_id, "dim_value").step_config = None
        execute_publish_run(SETTINGS, run_id)
        step = run_step_row(session, run_id, "dim_value")
        assert step.status == "blocked"
        assert "缺少必需配置" in step.message
        assert FakeLoader.calls == []

    def test_field_removed_blocks_at_execution(self, monkeypatch, tmp_path):
        """执行期复查外部条件：画像字段在创建后消失 → blocked，不静默改配置。"""
        write_marker(tmp_path, "dim")
        write_json_profile(tmp_path, "t1", ["hall_id", "name"])
        session = install(
            monkeypatch,
            tmp_path,
            FakeSession(
                steps=("dim_value",), step_config=dim_version_config(VERSION_TABLES)
            ),
        )
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
        # 创建后画像字段消失（采集重跑等外部变化）
        write_json_profile(tmp_path, "t1", ["name"])
        execute_publish_run(SETTINGS, run_id)
        step = run_step_row(session, run_id, "dim_value")
        assert step.status == "blocked"
        assert "hall_id" in step.message
        assert FakeLoader.calls == []

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
        write_marker(tmp_path, "dim")
        write_json_profile(tmp_path, "t1", ["hall_id", "name"])
        session = install(
            monkeypatch,
            tmp_path,
            FakeSession(
                steps=("dim_value",), step_config=dim_version_config(VERSION_TABLES)
            ),
        )
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
        write_marker(tmp_path, "dim")
        write_json_profile(tmp_path, "t1", ["hall_id", "name"])
        session = install(
            monkeypatch,
            tmp_path,
            FakeSession(
                steps=("dim_value",), step_config=dim_version_config(VERSION_TABLES)
            ),
        )
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


# ---- 作业级预检（doc 7.2 §5.2：与创建运行同一解析规则，不创建运行） ----


class TestJobPrecheck:
    def test_precheck_ready_without_creating_run(self, monkeypatch, tmp_path):
        from backend.publishing.publish_service import precheck_publish_job

        write_marker(tmp_path, "dim")
        write_json_profile(tmp_path, "t1", ["hall_id", "name"])
        session = install(
            monkeypatch,
            tmp_path,
            FakeSession(
                steps=("dim_value",), step_config=dim_version_config(VERSION_TABLES)
            ),
        )
        result = precheck_publish_job(
            SETTINGS,
            PUBLISH_JOB_ID,
            revision_no=1,
            mode="default",
            steps=None,
            step_config=None,
        )
        assert result["steps"] == [
            {"step_name": "dim_value", "ready": True, "reason": None, "code": None}
        ]
        assert session.runs == [] and session.run_steps == []

    def test_precheck_reports_invalid_override(self, monkeypatch, tmp_path):
        """应用草稿后按新有效参数校验：失效字段列出，不创建运行（§4 第 5 项）。"""
        from backend.publishing.publish_service import precheck_publish_job

        write_marker(tmp_path, "dim")
        write_json_profile(tmp_path, "t1", ["name"])
        session = install(
            monkeypatch,
            tmp_path,
            FakeSession(
                steps=("dim_value",), step_config=dim_version_config(VERSION_TABLES)
            ),
        )
        result = precheck_publish_job(
            SETTINGS,
            PUBLISH_JOB_ID,
            revision_no=1,
            mode="custom",
            steps=["dim_value"],
            step_config={
                "dim_value": {"tables": [dim_table_param("t1", ["gone"])]}
            },
        )
        step = result["steps"][0]
        assert step["ready"] is False
        assert "gone" in step["reason"]
        assert session.runs == []

    def test_precheck_legacy_revision_reports_missing_config(
        self, monkeypatch, tmp_path
    ):
        """断代版本（无 dim_value.tables）在作业级预检中明确报告必需配置缺失。"""
        from backend.publishing.publish_service import precheck_publish_job

        write_marker(tmp_path, "dim")
        write_json_profile(tmp_path, "t1", ["name"])
        install(monkeypatch, tmp_path, FakeSession(steps=("dim_value",)))
        result = precheck_publish_job(
            SETTINGS,
            PUBLISH_JOB_ID,
            revision_no=1,
            mode="default",
            steps=None,
            step_config=None,
        )
        step = result["steps"][0]
        assert step["ready"] is False
        assert "缺少必需配置" in step["reason"]
