"""doc 22.2 任务 3+4：dim_value 预检、三态拦截与发布执行的单元测试。

覆盖 §11 分配到 publish 层的验收：A09（预检）、A10/A11/A17（拦截三态与放行）、
A12（执行成功与 close）、A13（run_marker 复查）、A14（替换中途/未开始失败）、
A20（同一份配置内容传递）、A21（枚举与显式分支）、A22（错误出口经 TestClient）、
A23（登记库名与版本）、A25（拦截顺序）、A28（文案分派六行表）。
"""

from contextlib import contextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
import yaml
from fastapi.testclient import TestClient

import backend.publishing.publish_service as publish_service
from backend.profile_loading.dim_value_config_api import (
    DimValueConfigInvalidError,
    DimValueEmptyTablesError,
    DimValueNotConfiguredError,
)
from backend.publishing.errors import (
    JobNotPublishableError,
    PublicationStepNotReadyError,
)
from backend.publishing.publish_service import (
    RegistrationChangedError,
    _publish_dim_value,
    check_publication,
    normalize_publish_steps,
    publish_steps,
)

SOURCE_ID = uuid4()
JOB_ID = uuid4()
DB_NAME = "orders"
STEP_AT = datetime(2026, 10, 9, 1, 0, 0, tzinfo=timezone.utc)
SETTINGS = SimpleNamespace(database_url="fake")

YAML_DIR = SimpleNamespace(path=None)  # install 时指向 tmp 产物目录


# ---- 公共假件 ----


class FakeSession:
    def __init__(self, step_rows=None, source_row=None):
        self.step_rows = step_rows or {}
        self.source_row = source_row
        self.added = []
        self.executed = []

    def get(self, model, key):
        from backend.persistence.models import DbSourceInfo, JobInfo

        if model is JobInfo:
            step_name = key["step_name"] if isinstance(key, dict) else key
            return self.step_rows.get(step_name)
        if model is DbSourceInfo:
            return self.source_row
        raise AssertionError(f"unexpected model {model}")  # pragma: no cover

    def commit(self):
        return None

    def rollback(self):
        return None


def _source_row(updated_at=STEP_AT):
    return SimpleNamespace(
        source_id=SOURCE_ID,
        db_name=DB_NAME,
        is_active=True,
        updated_at=updated_at,
        host="h",
        port=5432,
        username="u",
        password_ciphertext="cipher",
        sslmode=None,
    )


def _step_row(status="success", updated_at=STEP_AT, step="dim_config"):
    return SimpleNamespace(
        job_id=JOB_ID, step_name=step, last_status=status, updated_at=updated_at
    )


@contextmanager
def _fake_session_ctx(session):
    yield session


class FakeLoader:
    calls: list = []
    ctor_kwargs: dict = {}
    next_result: dict = {}
    validate_result: bool = True

    def __init__(self, **kwargs):
        FakeLoader.ctor_kwargs = kwargs

    def validate(self):
        return FakeLoader.validate_result

    def load(self, clean=False):
        FakeLoader.calls.append("load")
        return dict(FakeLoader.next_result)

    def close(self):
        FakeLoader.calls.append("close")


@pytest.fixture(autouse=True)
def _reset_fake():
    FakeLoader.calls = []
    FakeLoader.ctor_kwargs = {}
    FakeLoader.next_result = {
        "success": True,
        "replacement_started": True,
        "records_loaded": 3,
    }
    FakeLoader.validate_result = True
    yield


class PublishRecorder:
    def __init__(self):
        self.inserted = []
        self.cleared = []
        self.stored_failures = []


def install_publish(monkeypatch, tmp_path, session, recorder=None, dim_yaml=None):
    """patch publish_service 命名空间：会话、锁、复查、登记行写、loader 构造。"""

    recorder = recorder or PublishRecorder()
    monkeypatch.setattr(
        publish_service, "platform_session", lambda s: _fake_session_ctx(session)
    )
    monkeypatch.setattr(
        publish_service, "lock_db_source_row", lambda sess, sid: sess.source_row
    )
    monkeypatch.setattr(publish_service, "assert_workspace_owner", lambda *a, **k: None)
    monkeypatch.setattr(
        publish_service, "_verify_step_current_under_lock", lambda *a, **k: None
    )
    monkeypatch.setattr(
        publish_service,
        "insert_publish_row",
        lambda sess, pid, sid, jid, step, status, error_summary=None, is_current=False: (
            recorder.inserted.append(
                {
                    "step": step,
                    "status": status,
                    "summary": error_summary,
                    "current": is_current,
                }
            )
        ),
    )
    monkeypatch.setattr(
        publish_service,
        "clear_current",
        lambda sess, sid, step: recorder.cleared.append(step),
    )
    monkeypatch.setattr(
        publish_service,
        "_store_failure",
        lambda settings, sid, jid, pid, step, summary, revoke_current=False: (
            recorder.stored_failures.append(
                {"step": step, "summary": summary, "revoke": revoke_current}
            )
            or SimpleNamespace(
                step=step, status="failed", message=summary, finished_at="t"
            )
        ),
    )
    monkeypatch.setattr(
        "backend.profile_loading.dim_value_loader.decrypt_password",
        lambda _c: "plain-pw",
    )
    # 编辑 API 服务函数在 config_api 模块命名空间持有自己的 session/resolve 绑定
    import backend.profile_loading.dim_value_config_api as config_api

    monkeypatch.setattr(
        config_api, "platform_session", lambda s: _fake_session_ctx(session)
    )
    root = tmp_path / str(SOURCE_ID) / str(JOB_ID) / "dim_config"
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(config_api, "resolve_dim_config_output_dir", lambda s, j: root)
    monkeypatch.setattr(
        config_api,
        "resolve_json_output_dir",
        lambda s, j: tmp_path / str(SOURCE_ID) / str(JOB_ID) / "json",
    )
    monkeypatch.setattr(
        "backend.profile_loading.dim_value_loader.build_dim_value_loader",
        lambda source, password, job_id, publish_id, dim_tables: FakeLoader(
            source=source,
            password=password,
            job_id=job_id,
            publish_id=publish_id,
            dim_tables=dim_tables,
        ),
    )
    if dim_yaml is not None:
        (root / "dim_tables.yaml").write_text(dim_yaml, encoding="utf-8")
        monkeypatch.setattr(
            publish_service,
            "resolve_dim_config_output_dir",
            lambda s, j: root,
        )
    return recorder


def _full_config_yaml(db=DB_NAME, col="name"):
    return yaml.safe_dump(
        {"databases": {db: {"tables": {"public.t1": {"embedding_col": col}}}}}
    )


def _install_steps(monkeypatch, dim_status="success", marker=STEP_AT, admission=None):
    """patch 预检/拦截前置：_INSPECTORS、登记快照、准入。"""

    def inspector(sess, sid, jid):
        if dim_status == "missing":
            return None, None
        return dim_status, marker

    monkeypatch.setattr(
        publish_service, "_INSPECTORS", {publish_service.DIM_VALUE_STEP: inspector}
    )
    monkeypatch.setattr(
        publish_service,
        "read_registration_snapshot",
        lambda sess, sid: SimpleNamespace(updated_at=STEP_AT, db_name=DB_NAME),
    )
    if admission is None:
        monkeypatch.setattr(
            publish_service, "_assert_job_admission", lambda *a, **k: None
        )
    else:
        monkeypatch.setattr(publish_service, "_assert_job_admission", admission)
    monkeypatch.setattr(publish_service, "_job_is_publishable", lambda *a, **k: True)


# ---- A09/A21 预检 ----


class TestCheckPublication:
    def _install(
        self, monkeypatch, tmp_path, step_rows, output_ready=True, job_ok=True
    ):
        # 预填全套步骤行（table_schema/json 分支会读其他行的 updated_at）
        full_rows = {
            step: _step_row(step=step)
            for step in ("db", "ddl", "json", "md", "domain", "dim_config")
        }
        full_rows.update(step_rows or {})
        session = FakeSession(step_rows=full_rows, source_row=_source_row())
        monkeypatch.setattr(
            publish_service, "platform_session", lambda s: _fake_session_ctx(session)
        )
        monkeypatch.setattr(
            publish_service, "_job_is_publishable", lambda *a, **k: job_ok
        )
        monkeypatch.setattr(
            publish_service,
            "check_dim_value_output_ready",
            lambda sid, jid: output_ready,
        )

        # 其余五步的 inspector 与登记快照全部替换为最小假件（本测试只断言 dim_value 条目）
        def make_inspector(step_col):
            def inspector(sess, _sid, _jid):
                row = sess.step_rows.get(step_col)
                if row is None:
                    return None, None
                return row.last_status, row.updated_at

            return inspector

        monkeypatch.setattr(
            publish_service,
            "_INSPECTORS",
            {
                publish_service.DB_STEP: make_inspector("db"),
                publish_service.DDL_STEP: make_inspector("ddl"),
                publish_service.JSON_STEP: make_inspector("json"),
                publish_service.DOMAIN_STEP: make_inspector("domain"),
                publish_service.REL_STEP: make_inspector("rel"),
                publish_service.TABLE_SCHEMA_STEP: make_inspector("md"),
                publish_service.DIM_VALUE_STEP: make_inspector("dim_config"),
                publish_service.SQL_STEP: make_inspector("sql"),
            },
        )
        monkeypatch.setattr(
            publish_service,
            "read_registration_snapshot",
            lambda sess, sid: SimpleNamespace(updated_at=STEP_AT, db_name=DB_NAME),
        )
        # db/ddl/json/domain/table_schema 的产物就绪检查：统一返回就绪，避免触碰真实路径
        monkeypatch.setattr(
            publish_service, "check_db_output_ready", lambda *a, **k: (True, None)
        )
        for name in (
            "check_ddl_output_ready",
            "check_json_output_ready",
            "check_domain_output_ready",
            "check_table_schema_output_ready",
        ):
            if hasattr(publish_service, name):
                monkeypatch.setattr(publish_service, name, lambda *a, **k: True)
        return session

    def test_a09_publishable_when_success_and_file(self, monkeypatch, tmp_path):
        self._install(monkeypatch, tmp_path, {"dim_config": _step_row()})
        result = check_publication(SETTINGS, SOURCE_ID, JOB_ID)
        entry = next(s for s in result["steps"] if s["step_name"] == "dim_value")
        assert entry["publishable"] is True
        assert entry["reason"] is None

    def test_a09_reasons_three_tiers(self, monkeypatch, tmp_path):
        def dim_entry():
            return next(
                s
                for s in check_publication(SETTINGS, SOURCE_ID, JOB_ID)["steps"]
                if s["step_name"] == "dim_value"
            )

        # 行 failed → _JOB_REASONS
        self._install(monkeypatch, tmp_path, {"dim_config": _step_row(status="failed")})
        entry = dim_entry()
        assert entry["publishable"] is False
        assert entry["reason"] and entry["reason"] != "产物缺失或不可读"
        # 整作业不可发布
        self._install(monkeypatch, tmp_path, {"dim_config": _step_row()}, job_ok=False)
        entry = dim_entry()
        assert "不能发布" in entry["reason"]
        # 产物缺失
        self._install(
            monkeypatch, tmp_path, {"dim_config": _step_row()}, output_ready=False
        )
        entry = dim_entry()
        assert entry["reason"] == "产物不存在或不可用"

    def test_a21_step_enum_in_schemas(self):
        import typing

        from backend.api import schemas

        for model, field in (
            (schemas.PublishCheckStep, "step_name"),
            (schemas.PublicationStepResult, "step"),
        ):
            literal = model.model_fields[field].annotation
            assert "dim_value" in typing.get_args(literal)

    def test_normalize_whitelist_includes_dim_value(self):
        assert normalize_publish_steps(["dim_value"]) == ["dim_value"]
        with pytest.raises(Exception):
            normalize_publish_steps(["dim_value", "nope"])


# ---- A17/A25 三态拦截与顺序（publish_steps 层）----


class TestInterception:
    def _run(self, monkeypatch, tmp_path, dim_yaml, **kw):
        session = FakeSession(
            step_rows={"dim_config": _step_row()}, source_row=_source_row()
        )
        recorder = install_publish(monkeypatch, tmp_path, session, dim_yaml=dim_yaml)
        _install_steps(monkeypatch, **kw)
        executed = []
        monkeypatch.setattr(
            publish_service,
            "_publish_dim_value",
            lambda *a, **k: executed.append(a)
            or {
                "step": "dim_value",
                "status": "success",
                "message": "ok",
                "finished_at": "t",
            },
        )
        return recorder, executed

    def test_a17_zero_tables_intercepts_whole_request(self, monkeypatch, tmp_path):
        for content in (
            "databases: {}\n",
            yaml.safe_dump({"databases": {DB_NAME: {"tables": {}}}}),
        ):
            recorder, executed = self._run(monkeypatch, tmp_path, content)
            with pytest.raises(DimValueEmptyTablesError):
                publish_steps(SETTINGS, SOURCE_ID, JOB_ID, ["dim_value"])
            assert executed == []
            assert recorder.inserted == []  # 不生成发布记录

    def test_a17_invalid_file_intercepts(self, monkeypatch, tmp_path):
        self._run(monkeypatch, tmp_path, "not: [valid\n")
        with pytest.raises(DimValueConfigInvalidError) as excinfo:
            publish_steps(SETTINGS, SOURCE_ID, JOB_ID, ["dim_value"])
        assert excinfo.value.details["reason"]

    def test_a17_unconfigured_tables_intercepts(self, monkeypatch, tmp_path):
        content = yaml.safe_dump(
            {"databases": {DB_NAME: {"tables": {"public.t1": {"embedding_col": None}}}}}
        )
        self._run(monkeypatch, tmp_path, content)
        with pytest.raises(DimValueNotConfiguredError) as excinfo:
            publish_steps(SETTINGS, SOURCE_ID, JOB_ID, ["dim_value"])
        assert excinfo.value.details["tables"] == ["public.t1"]

    def test_a11_passes_injected_config_to_publisher(self, monkeypatch, tmp_path):
        _, executed = self._run(monkeypatch, tmp_path, _full_config_yaml())
        result = publish_steps(SETTINGS, SOURCE_ID, JOB_ID, ["dim_value"])
        assert result["results"][0]["status"] == "success"
        injected = executed[0][6]  # 第 7 参 dim_tables
        assert injected.tables["public.t1"].embedding_col == "name"
        assert injected.database == DB_NAME

    def test_a20_registration_snapshot_passed(self, monkeypatch, tmp_path):
        _, executed = self._run(monkeypatch, tmp_path, _full_config_yaml())
        publish_steps(SETTINGS, SOURCE_ID, JOB_ID, ["dim_value"])
        registration = executed[0][5]
        assert registration.db_name == DB_NAME

    def test_a25_job_not_publishable_wins(self, monkeypatch, tmp_path):
        def deny(*a, **k):
            raise JobNotPublishableError()

        self._run(monkeypatch, tmp_path, "databases: {}\n", admission=deny)
        with pytest.raises(JobNotPublishableError):
            publish_steps(SETTINGS, SOURCE_ID, JOB_ID, ["dim_value"])

    def test_a25_missing_step_row_step_not_ready(self, monkeypatch, tmp_path):
        self._run(monkeypatch, tmp_path, "databases: {}\n", dim_status="missing")
        with pytest.raises(PublicationStepNotReadyError):
            publish_steps(SETTINGS, SOURCE_ID, JOB_ID, ["dim_value"])


# ---- A12/A13/A14/A20/A23/A28 发布执行（_publish_dim_value 层）----


def _dim_tables_cfg():
    from metaweave.core.dim_value.models import DimTableConfig, DimTablesConfig

    return DimTablesConfig(
        database=DB_NAME,
        tables={
            "public.t1": DimTableConfig(
                schema="public", table="t1", embedding_col="name"
            )
        },
    )


def _registration(updated_at=STEP_AT):
    return SimpleNamespace(updated_at=updated_at, db_name=DB_NAME)


def _run_publish_dim_value(
    monkeypatch,
    tmp_path,
    *,
    load_result=None,
    validate=True,
    registration=None,
    verify=None,
    source_updated_at=STEP_AT,
):
    session = FakeSession(
        step_rows={"dim_config": _step_row()},
        source_row=_source_row(updated_at=source_updated_at),
    )
    recorder = install_publish(monkeypatch, tmp_path, session)
    if verify is not None:
        monkeypatch.setattr(publish_service, "_verify_step_current_under_lock", verify)
    FakeLoader.validate_result = validate
    if load_result is not None:
        FakeLoader.next_result = load_result
    dim_tables = _dim_tables_cfg()
    result = _publish_dim_value(
        SETTINGS,
        SOURCE_ID,
        JOB_ID,
        uuid4(),
        STEP_AT,
        registration or _registration(),
        dim_tables,
    )
    return result, recorder, dim_tables


class TestPublishDimValue:
    def test_a12_success_writes_current_and_closes(self, monkeypatch, tmp_path):
        result, recorder, dim_tables = _run_publish_dim_value(monkeypatch, tmp_path)
        assert result["status"] == "success"
        assert recorder.cleared == ["dim_value"]  # clear 先行
        assert recorder.inserted == [
            {"step": "dim_value", "status": "success", "summary": None, "current": True}
        ]
        assert "close" in FakeLoader.calls  # A12：alias 不泄漏
        # 构造参数：登记连接、注入配置
        assert FakeLoader.ctor_kwargs["password"] == "plain-pw"
        assert FakeLoader.ctor_kwargs["dim_tables"] is dim_tables
        assert FakeLoader.ctor_kwargs["source"].db_name == DB_NAME

    def test_a13_run_marker_changed_rejected(self, monkeypatch, tmp_path):
        def verify(*a, **k):
            raise PublicationStepNotReadyError()

        with pytest.raises(PublicationStepNotReadyError):
            _run_publish_dim_value(monkeypatch, tmp_path, verify=verify)

    def test_a23_registration_changed_rejected_before_any_query(
        self, monkeypatch, tmp_path
    ):
        with pytest.raises(RegistrationChangedError):
            _run_publish_dim_value(
                monkeypatch,
                tmp_path,
                registration=_registration(
                    updated_at=datetime(2020, 1, 1, tzinfo=timezone.utc)
                ),
                source_updated_at=STEP_AT,
            )
        assert FakeLoader.calls == []  # 未构造 loader、零源库查询

    def test_a14_midway_failure_revokes_current(self, monkeypatch, tmp_path):
        result, recorder, _ = _run_publish_dim_value(
            monkeypatch,
            tmp_path,
            load_result={
                "success": False,
                "replacement_started": True,
                "error_kind": "embedding",
                "records_loaded": 1,
                "records_failed": 2,
                "records_unconfirmed": 0,
            },
        )
        assert result["status"] == "failed"
        assert recorder.cleared == ["dim_value"]  # 撤销旧 current
        assert recorder.inserted[0]["status"] == "failed"
        assert recorder.inserted[0]["current"] is False

    def test_a14_not_started_failure_keeps_current(self, monkeypatch, tmp_path):
        result, recorder, _ = _run_publish_dim_value(
            monkeypatch,
            tmp_path,
            load_result={
                "success": False,
                "replacement_started": False,
                "error_kind": "schema_mismatch",
            },
        )
        assert result["status"] == "failed"
        assert recorder.cleared == []  # 旧 current 保留（不 clear）
        # 仅失败行：写入失败行但不置 current
        assert recorder.inserted == [
            {
                "step": "dim_value",
                "status": "failed",
                "summary": "dim_value_embeddings 集合结构为旧版本，请手动删除该集合后重新发布",
                "current": False,
            }
        ]

    def test_a28_message_dispatch(self, monkeypatch, tmp_path):
        cases = [
            # (load_result, expected_summary_part)
            (
                {
                    "success": False,
                    "replacement_started": True,
                    "error_kind": "embedding",
                    "records_loaded": 1,
                    "records_failed": 2,
                    "records_unconfirmed": 0,
                },
                "成功 1 条 / 失败 2 条",
            ),
            (
                {
                    "success": False,
                    "replacement_started": True,
                    "error_kind": "embedding",
                    "records_loaded": 1,
                    "records_failed": 2,
                    "records_unconfirmed": 3,
                },
                "另有 3 条写入结果未确认",
            ),
            (
                {
                    "success": False,
                    "replacement_started": False,
                    "error_kind": "schema_mismatch",
                },
                "手动删除该集合",
            ),
            (
                {
                    "success": False,
                    "replacement_started": False,
                    "error_kind": "source",
                    "failed_table": "public.t1",
                    "error_summary": "relation 不存在",
                },
                "public.t1：relation 不存在",
            ),
            (
                {"success": False, "replacement_started": False, "error_kind": "input"},
                "dim_config 产物",
            ),
        ]
        for load_result, part in cases:
            _, recorder, _ = _run_publish_dim_value(
                monkeypatch, tmp_path, load_result=load_result
            )
            summary = (recorder.inserted or recorder.stored_failures)[0]["summary"]
            assert part in summary, f"文案应含 {part!r}，实际 {summary!r}"

    def test_a28_validate_failure_uses_service_message(self, monkeypatch, tmp_path):
        _, recorder, _ = _run_publish_dim_value(monkeypatch, tmp_path, validate=False)
        summary = (recorder.inserted or recorder.stored_failures)[0]["summary"]
        assert "Milvus、源库或 Embedding" in summary

    def test_a20_injected_config_used_not_file(self, monkeypatch, tmp_path):
        # 注入对象恒为发布输入；文件内容与本用例无关（PUT 改写由注入兜住）
        result, _, dim_tables = _run_publish_dim_value(monkeypatch, tmp_path)
        assert result["status"] == "success"
        assert FakeLoader.ctor_kwargs["dim_tables"] is dim_tables


# ---- A22 错误出口（TestClient）----


class TestErrorOutlet:
    def test_a22_three_codes_via_testclient(self, monkeypatch, tmp_path):
        import backend.api.main as api_main

        app = api_main.create_app()
        client = TestClient(app, raise_server_exceptions=False)

        session = FakeSession(
            step_rows={"dim_config": _step_row()}, source_row=_source_row()
        )
        install_publish(monkeypatch, tmp_path, session)
        _install_steps(monkeypatch)

        base = "/api/data-sources/{}/collection-jobs/{}".format(SOURCE_ID, JOB_ID)

        # 零表
        root = tmp_path / str(SOURCE_ID) / str(JOB_ID) / "dim_config"
        root.mkdir(parents=True, exist_ok=True)
        (root / "dim_tables.yaml").write_text("databases: {}\n", encoding="utf-8")
        monkeypatch.setattr(
            publish_service, "resolve_dim_config_output_dir", lambda s, j: root
        )
        resp = client.post(base + "/publications", json={"steps": ["dim_value"]})
        assert resp.status_code == 400
        body = resp.json()
        assert body["code"] == "dim_value_empty_tables"
        assert body["details"] == {}

        # 无效文件
        (root / "dim_tables.yaml").write_text("not: [valid\n", encoding="utf-8")
        resp = client.post(base + "/publications", json={"steps": ["dim_value"]})
        assert resp.status_code == 400
        assert resp.json()["code"] == "dim_value_config_invalid"
        assert resp.json()["details"]["reason"]

        # 未配置
        (root / "dim_tables.yaml").write_text(
            yaml.safe_dump(
                {
                    "databases": {
                        DB_NAME: {"tables": {"public.t1": {"embedding_col": None}}}
                    }
                }
            ),
            encoding="utf-8",
        )
        resp = client.post(base + "/publications", json={"steps": ["dim_value"]})
        assert resp.status_code == 400
        body = resp.json()
        assert body["code"] == "dim_value_not_configured"
        assert body["details"]["tables"] == ["public.t1"]
        assert body["message"]

    def test_put_dim_config_response_has_no_version(self, monkeypatch, tmp_path):
        """PUT 成功响应只含 tables（§5.2 第 5 步），response_model 不得要求 version。"""

        import backend.api.main as api_main

        app = api_main.create_app()
        client = TestClient(app, raise_server_exceptions=False)

        session = FakeSession(
            step_rows={"dim_config": _step_row()}, source_row=_source_row()
        )
        install_publish(monkeypatch, tmp_path, session)
        base = "/api/data-sources/{}/collection-jobs/{}".format(SOURCE_ID, JOB_ID)

        resp = client.put(
            base + "/dim-config",
            json={
                "tables": [
                    {
                        "schema_name": "public",
                        "object_name": "t1",
                        "embedding_col": "name",
                    }
                ],
                "version": STEP_AT.isoformat(),
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["tables"] == [
            {"schema_name": "public", "object_name": "t1", "embedding_col": "name"}
        ]
        assert "version" not in body
