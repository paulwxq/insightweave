"""doc 23.2 批次 3：rel 发布服务单元测试（A01–A05、A12、A15–A17、A19–A28）。

覆盖白名单/归组/Literal（A01）、check_publication rel 条目（A02）、
publish_steps 前置控制流（A03/A22 前半）、rel_loader 严格预检与锚点快照
（A03/A05/A20）、_publish_rel 执行链与失败分类（A15–A17、A19、A22–A28）。
"""

import hashlib
import json
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

import backend.publishing.publish_service as publish_service
import backend.profile_loading.rel_loader as rel_loader
from backend.publishing.errors import PublicationStepNotReadyError
from backend.publishing.publish_service import (
    UnsupportedPublicationStepError,
    _publish_rel,
    check_publication,
    normalize_publish_steps,
    publish_steps,
)

SOURCE_ID = uuid4()
JOB_ID = uuid4()
DB_NAME = "orders"
NOW = datetime(2026, 10, 10, 2, 0, 0, tzinfo=timezone.utc)
EARLIER = NOW - timedelta(minutes=10)
LATER = NOW + timedelta(minutes=5)
SETTINGS = SimpleNamespace(database_url="fake")


# ---- 公共假件 ----


class FakeSession:
    def __init__(self, step_rows=None, source_row=None, fail_commit=False):
        self.step_rows = step_rows or {}
        self.source_row = source_row
        self.fail_commit = fail_commit

    def get(self, model, key):
        from backend.persistence.models import DbSourceInfo, JobInfo

        if model is JobInfo:
            step_name = key["step_name"] if isinstance(key, dict) else key
            return self.step_rows.get(step_name)
        if model is DbSourceInfo:
            return self.source_row
        raise AssertionError(f"unexpected model {model}")  # pragma: no cover

    def commit(self):
        if self.fail_commit:
            raise RuntimeError("commit boom")
        return None

    def rollback(self):
        return None


@contextmanager
def _fake_session_ctx(session):
    yield session


def _source_row(updated_at=NOW):
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
        selected_schemas=["public"],
    )


def _step_row(status="success", updated_at=NOW, step="rel"):
    return SimpleNamespace(
        job_id=JOB_ID, step_name=step, last_status=status, updated_at=updated_at
    )


class PublishRecorder:
    def __init__(self):
        self.inserted = []
        self.cleared = []
        self.stored_failures = []
        self.uuid_calls = 0


class FakeGenerator:
    ctor_kwargs: dict = {}
    gen_result: object = None
    gen_raises: Exception | None = None

    def __init__(self, config_path, **kwargs):
        FakeGenerator.ctor_kwargs = dict(kwargs)

    def generate(self):
        if FakeGenerator.gen_raises is not None:
            raise FakeGenerator.gen_raises
        result = FakeGenerator.gen_result
        # 核验需要真实文件：按受管快照的 cql_directory 落盘产物
        cql_dir = FakeGenerator.ctor_kwargs["config"]["output"]["cql_directory"]
        from pathlib import Path

        cql_dir = Path(cql_dir)
        cql_dir.mkdir(parents=True, exist_ok=True)
        written = []
        for name in result.output_files or []:
            path = cql_dir / name
            path.write_text("// fake", encoding="utf-8")
            written.append(str(path))
        result.output_files = written
        return result


class FakeLoader:
    ctor_config: dict = {}
    validate_result: bool = True
    load_result: dict = {}
    load_raises: Exception | None = None
    calls: list = []

    def __init__(self, config):
        FakeLoader.ctor_config = config

    def validate(self):
        return FakeLoader.validate_result

    def load(self, clean=False):
        FakeLoader.calls.append(("load", clean))
        if FakeLoader.load_raises is not None:
            raise FakeLoader.load_raises
        return dict(FakeLoader.load_result)

    def close(self):
        FakeLoader.calls.append("close")


@pytest.fixture(autouse=True)
def _reset_fakes():
    FakeGenerator.ctor_kwargs = {}
    FakeGenerator.gen_raises = None
    FakeLoader.ctor_config = {}
    FakeLoader.validate_result = True
    FakeLoader.load_result = {"success": True}
    FakeLoader.load_raises = None
    FakeLoader.calls = []
    yield


def _workspace(tmp_path):
    root = tmp_path / "output" / str(SOURCE_ID) / str(JOB_ID)
    (root / "json").mkdir(parents=True, exist_ok=True)
    (root / "domain").mkdir(parents=True, exist_ok=True)
    rel_dir = root / "rel"
    rel_dir.mkdir(exist_ok=True)
    return root, rel_dir


REL_ARTIFACT = {
    "generated_by": "rel",
    "database": DB_NAME,
    "relationships": [{"relationship_id": "rel_x"}],
    "statistics": {"total_relationships_found": 1},
}


def _write_rel_artifact(rel_dir):
    text = json.dumps(REL_ARTIFACT, ensure_ascii=False, indent=2)
    (rel_dir / f"{DB_NAME}.relationships_global.json").write_text(
        text, encoding="utf-8"
    )
    (rel_dir / f"{DB_NAME}.relationships_global.md").write_text("# r", encoding="utf-8")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _snapshot(tmp_path, fingerprint, main_config=None):
    root, _ = _workspace(tmp_path)
    return SimpleNamespace(
        registration=SimpleNamespace(updated_at=NOW, db_name=DB_NAME),
        json_marker=EARLIER,
        domain_marker=EARLIER,
        rel_fingerprint=fingerprint,
        main_config=main_config
        or {
            "loaders": {
                "cql_loader": {
                    "input_file": "output/cql/x.cypher",
                    "neo4j": {"uri": "bolt://x", "user": "u", "password": "p"},
                    "options": {"validate_after_load": False},
                }
            }
        },
    )


def _install_publish(monkeypatch, tmp_path, session, snapshot):
    """patch publish_service 命名空间（_publish_rel 执行链）。"""
    recorder = PublishRecorder()
    monkeypatch.setattr(
        publish_service, "platform_session", lambda s: _fake_session_ctx(session)
    )
    monkeypatch.setattr(
        publish_service, "lock_db_source_row", lambda sess, sid: sess.source_row
    )
    monkeypatch.setattr(publish_service, "assert_workspace_owner", lambda *a, **k: None)

    verified = []

    def fake_verify(sess, jid, step, marker):
        verified.append((step, marker))

    monkeypatch.setattr(publish_service, "_verify_step_current_under_lock", fake_verify)

    def fake_insert(
        sess, pid, sid, jid, step, status, error_summary=None, is_current=False
    ):
        recorder.inserted.append(
            {
                "step": step,
                "status": status,
                "summary": error_summary,
                "current": is_current,
            }
        )

    monkeypatch.setattr(publish_service, "insert_publish_row", fake_insert)
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

    # 受管快照：直接构造指向 tmp 工作区的 output 键（绕过真实 resolver）
    root, rel_dir = _workspace(tmp_path)
    monkeypatch.setattr(publish_service, "resolve_rel_output_dir", lambda s, j: rel_dir)
    monkeypatch.setattr(
        publish_service, "resolve_cql_output_dir", lambda s, j: root / "cql"
    )
    monkeypatch.setattr(
        publish_service, "resolve_domain_output_dir", lambda s, j: root / "domain"
    )
    cleaned = []
    monkeypatch.setattr(
        publish_service,
        "clean_top_level_files",
        lambda path: cleaned.append(str(path)),
    )
    recorder.cleaned = cleaned

    def fake_managed_snapshot(main_config, sid, jid, db_name, schemas):
        return dict(main_config) | {
            "output": {
                "json_directory": str(root / "json"),
                "rel_directory": str(rel_dir),
                "cql_directory": str(root / "cql"),
            }
        }

    monkeypatch.setattr(
        publish_service, "build_managed_output_snapshot", fake_managed_snapshot
    )
    monkeypatch.setattr(publish_service, "DomainResolver", lambda yaml: object())
    monkeypatch.setattr(publish_service, "CQLGenerator", FakeGenerator)
    monkeypatch.setattr(publish_service, "CQLLoader", FakeLoader)
    recorder.verified = verified
    recorder.rel_dir = rel_dir
    recorder.cql_dir = root / "cql"
    return recorder


def _gen_result(success=True, files=("import_all.orders.cypher", "meta.md"), **counts):
    return SimpleNamespace(
        success=success,
        output_files=list(files),
        tables_count=counts.get("tables", 3),
        columns_count=counts.get("columns", 10),
        relationships_count=counts.get("relationships", 2),
        errors=counts.get("errors", []),
    )


# ---- A01 白名单 / 归组 / Literal ----


def test_a01_whitelist_contains_rel():
    assert normalize_publish_steps(["rel"]) == ["rel"]
    assert normalize_publish_steps(["db", "rel", "rel"]) == ["db", "rel"]


def test_a01_unknown_step_message_lists_rel():
    with pytest.raises(UnsupportedPublicationStepError, match="rel"):
        normalize_publish_steps(["nope"])


def test_a01_rag_steps_swap_cql_for_rel():
    from backend.publishing.listing import RAG_STEPS

    assert "rel" in RAG_STEPS
    assert "cql" not in RAG_STEPS


def test_a01_response_literals_accept_rel():
    from backend.api.schemas import PublicationStepResult, PublishCheckStep

    PublishCheckStep(
        step_name="rel",
        job_status="success",
        output_ready=True,
        publishable=True,
        reason=None,
    )
    row = PublicationStepResult(
        step="rel", status="success", message="m", finished_at="2026-10-10T00:00:00Z"
    )
    assert row.step == "rel"


# ---- A02 check_publication rel 条目 ----


class TestCheckPublicationRel:
    def _install(self, monkeypatch, rel_row, output_ready=True):
        full_rows = {
            step: _step_row(step=step)
            for step in ("db", "ddl", "json", "domain", "table_schema", "dim_value")
        }
        full_rows["md"] = _step_row(step="md")
        if rel_row is not None:
            full_rows["rel"] = rel_row
        session = FakeSession(step_rows=full_rows, source_row=_source_row())
        monkeypatch.setattr(
            publish_service, "platform_session", lambda s: _fake_session_ctx(session)
        )
        monkeypatch.setattr(
            publish_service, "_job_is_publishable", lambda *a, **k: True
        )
        monkeypatch.setattr(
            publish_service, "check_rel_output_ready", lambda sid, jid: output_ready
        )

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
            lambda sess, sid: SimpleNamespace(updated_at=NOW, db_name=DB_NAME),
        )
        monkeypatch.setattr(
            publish_service, "check_db_output_ready", lambda *a, **k: (True, None)
        )
        for name in (
            "check_ddl_output_ready",
            "check_json_output_ready",
            "check_domain_output_ready",
            "check_table_schema_output_ready",
            "check_dim_value_output_ready",
        ):
            if hasattr(publish_service, name):
                monkeypatch.setattr(publish_service, name, lambda *a, **k: True)
        return session

    def test_a02_publishable_when_row_success_and_file(self, monkeypatch):
        self._install(monkeypatch, _step_row())
        entry = next(
            s
            for s in check_publication(SETTINGS, SOURCE_ID, JOB_ID)["steps"]
            if s["step_name"] == "rel"
        )
        assert entry["publishable"] is True
        assert entry["reason"] is None

    def test_a02_row_failed_reason_step_not_success(self, monkeypatch):
        self._install(monkeypatch, _step_row(status="failed"))
        entry = next(
            s
            for s in check_publication(SETTINGS, SOURCE_ID, JOB_ID)["steps"]
            if s["step_name"] == "rel"
        )
        assert entry["publishable"] is False
        assert entry["reason"] == "最近一次采集失败"

    def test_a02_no_artifact_reason(self, monkeypatch):
        self._install(monkeypatch, _step_row(), output_ready=False)
        entry = next(
            s
            for s in check_publication(SETTINGS, SOURCE_ID, JOB_ID)["steps"]
            if s["step_name"] == "rel"
        )
        assert entry["publishable"] is False
        assert entry["reason"] == "该作业没有 Rel 产物"

    def test_a02_step_order_between_domain_and_table_schema(self, monkeypatch):
        self._install(monkeypatch, _step_row())
        names = [
            s["step_name"]
            for s in check_publication(SETTINGS, SOURCE_ID, JOB_ID)["steps"]
        ]
        assert names.index("domain") < names.index("rel") < names.index("table_schema")

    def test_a02_freshness_not_in_ui(self, monkeypatch):
        """json 重跑后 rel 未重跑：页面仍可选（新鲜度只在执行预检）。"""
        rows = {
            "json": _step_row(step="json", updated_at=LATER),
            "domain": _step_row(step="domain", updated_at=EARLIER),
            "rel": _step_row(step="rel", updated_at=NOW),
        }
        for step in ("db", "ddl", "table_schema", "dim_value"):
            rows[step] = _step_row(step=step)
        rows["md"] = _step_row(step="md")
        self._install(monkeypatch, rows["rel"])
        # 重新注入带时间戳的行
        entry = next(
            s
            for s in check_publication(SETTINGS, SOURCE_ID, JOB_ID)["steps"]
            if s["step_name"] == "rel"
        )
        assert entry["publishable"] is True


# ---- publish_steps 前置控制流（A03/A22 前半） ----


class TestPublishStepsPrecheck:
    def _install(self, monkeypatch, session):
        monkeypatch.setattr(
            publish_service, "platform_session", lambda s: _fake_session_ctx(session)
        )
        monkeypatch.setattr(
            publish_service, "_assert_job_admission", lambda *a, **k: None
        )
        monkeypatch.setattr(
            publish_service, "_job_is_publishable", lambda *a, **k: True
        )
        monkeypatch.setattr(
            publish_service,
            "_INSPECTORS",
            {publish_service.REL_STEP: lambda sess, sid, jid: ("success", NOW)},
        )
        monkeypatch.setattr(
            publish_service,
            "read_registration_snapshot",
            lambda sess, sid: SimpleNamespace(updated_at=NOW, db_name=DB_NAME),
        )
        calls = {"strict": 0, "capture": 0}

        def fake_strict(settings, sid, jid):
            calls["strict"] += 1

        snapshot = SimpleNamespace(rel_fingerprint="f")

        def fake_capture(settings, sid, jid):
            calls["capture"] += 1
            return snapshot

        monkeypatch.setattr(publish_service, "assert_rel_publish_strict", fake_strict)
        monkeypatch.setattr(
            publish_service, "capture_rel_precheck_snapshot", fake_capture
        )
        return calls, snapshot

    def test_anchor_branch_runs_and_dispatches_six_args(self, monkeypatch, tmp_path):
        session = FakeSession(source_row=_source_row())
        calls, snapshot = self._install(monkeypatch, session)
        dispatched = []

        def fake_publish_rel(settings, sid, jid, pid, marker, snap):
            dispatched.append((sid, jid, pid, marker, snap))
            return SimpleNamespace(
                step="rel", status="success", message="m", finished_at="t"
            )

        monkeypatch.setattr(publish_service, "_publish_rel", fake_publish_rel)
        publish_steps(SETTINGS, SOURCE_ID, JOB_ID, ["rel"])

        assert calls == {"strict": 1, "capture": 1}
        assert len(dispatched) == 1
        sid, jid, pid, marker, snap = dispatched[0]
        assert (sid, jid) == (SOURCE_ID, JOB_ID)
        assert marker == NOW
        assert snap is snapshot

    def test_strict_failure_no_publish_id(self, monkeypatch):
        session = FakeSession(source_row=_source_row())
        self._install(monkeypatch, session)

        def boom(settings, sid, jid):
            raise PublicationStepNotReadyError("过期")

        monkeypatch.setattr(publish_service, "assert_rel_publish_strict", boom)
        uuid_calls = []

        def fake_uuid4():
            uuid_calls.append(1)
            return uuid4()

        monkeypatch.setattr(publish_service, "uuid4", fake_uuid4)
        with pytest.raises(PublicationStepNotReadyError, match="过期"):
            publish_steps(SETTINGS, SOURCE_ID, JOB_ID, ["rel"])
        assert uuid_calls == []

    def test_non_rel_choice_skips_branch(self, monkeypatch, tmp_path):
        """仅发布 db：不跑 rel 严格预检/锚点（控制流块只在 chosen 含 rel 时）。"""
        session = FakeSession(source_row=_source_row())
        calls, _ = self._install(monkeypatch, session)
        monkeypatch.setattr(
            publish_service,
            "_INSPECTORS",
            {publish_service.DB_STEP: lambda sess, sid, jid: ("success", NOW)},
        )
        monkeypatch.setitem(
            publish_service._PUBLISHERS,
            publish_service.DB_STEP,
            lambda *a: SimpleNamespace(
                step="db", status="success", message="m", finished_at="t"
            ),
        )
        publish_steps(SETTINGS, SOURCE_ID, JOB_ID, ["db"])
        assert calls == {"strict": 0, "capture": 0}


# ---- rel_loader 严格预检与锚点快照（A03/A05/A20） ----


class TestRelLoaderStrict:
    def _install(self, monkeypatch, tmp_path, rows, source=None):
        session = FakeSession(step_rows=rows, source_row=source or _source_row())
        monkeypatch.setattr(
            rel_loader, "platform_session", lambda s: _fake_session_ctx(session)
        )
        monkeypatch.setattr(rel_loader, "assert_workspace_owner", lambda *a, **k: None)
        root, rel_dir = _workspace(tmp_path)
        monkeypatch.setattr(rel_loader, "resolve_rel_output_dir", lambda s, j: rel_dir)
        monkeypatch.setattr(
            rel_loader, "resolve_domain_output_dir", lambda s, j: root / "domain"
        )
        (root / "domain" / "db_domains.yaml").write_text("d: 1", encoding="utf-8")
        monkeypatch.setattr(
            rel_loader,
            "read_managed_json_input",
            lambda s, j, d: [{"schema": "public", "object": "t"}],
        )
        monkeypatch.setattr(
            rel_loader,
            "verify_domain_artifact",
            lambda path: (True, ""),
        )
        monkeypatch.setattr(
            rel_loader,
            "load_main_config_snapshot",
            lambda p: {"decision": {}},
        )
        monkeypatch.setattr(
            "metaweave.core.cql_generator.generator.CQLGenerator",
            lambda *a, **k: object(),
        )
        return rel_dir

    def _rows(
        self,
        json_at=EARLIER,
        domain_at=EARLIER,
        rel_at=NOW,
        rel_status="success",
        json_status="success",
    ):
        return {
            "json": _step_row(step="json", updated_at=json_at, status=json_status),
            "domain": _step_row(step="domain", updated_at=domain_at),
            "rel": _step_row(step="rel", updated_at=rel_at, status=rel_status),
        }

    def test_check_output_ready_ignore_rules(self, monkeypatch, tmp_path):
        _, rel_dir = _workspace(tmp_path)
        monkeypatch.setattr(rel_loader, "resolve_rel_output_dir", lambda s, j: rel_dir)
        assert rel_loader.check_rel_output_ready(SOURCE_ID, JOB_ID) is False
        (rel_dir / ".tmp-abc").write_text("x", encoding="utf-8")
        backup = rel_dir / ".backup"
        backup.mkdir()
        (backup / f"{DB_NAME}.relationships_global.json").write_text(
            "{}", encoding="utf-8"
        )
        assert rel_loader.check_rel_output_ready(SOURCE_ID, JOB_ID) is False
        (rel_dir / f"{DB_NAME}.relationships_global.json").write_text(
            "{}", encoding="utf-8"
        )
        assert rel_loader.check_rel_output_ready(SOURCE_ID, JOB_ID) is True

    def test_check_output_ready_swallows_directory_errors(self, monkeypatch, tmp_path):
        """目录不可用（符号链接 ValueError / 不可读 OSError）兜底降级为未就绪
        ——不让整个 check_publication 500（与 domain/dim_value 兄弟同构）。"""

        def boom(_s, _j, _e=None):
            raise _e

        _, rel_dir = _workspace(tmp_path)
        monkeypatch.setattr(rel_loader, "resolve_rel_output_dir", lambda s, j: rel_dir)
        for exc in (ValueError("路径包含符号链接"), OSError("permission denied")):
            monkeypatch.setattr(
                rel_loader,
                "resolve_rel_output_dir",
                lambda s, j, _e=exc: boom(s, j, _e),
            )
            assert rel_loader.check_rel_output_ready(SOURCE_ID, JOB_ID) is False

    def test_missing_row_rejected(self, monkeypatch, tmp_path):
        rows = self._rows()
        rows.pop("json")
        self._install(monkeypatch, tmp_path, rows)
        with pytest.raises(PublicationStepNotReadyError, match="未成功"):
            rel_loader.assert_rel_publish_strict(SETTINGS, SOURCE_ID, JOB_ID)

    def test_failed_row_rejected(self, monkeypatch, tmp_path):
        self._install(monkeypatch, tmp_path, self._rows(json_status="failed"))
        with pytest.raises(PublicationStepNotReadyError, match="未成功"):
            rel_loader.assert_rel_publish_strict(SETTINGS, SOURCE_ID, JOB_ID)

    def test_a20_stale_json_rejected(self, monkeypatch, tmp_path):
        self._install(monkeypatch, tmp_path, self._rows(json_at=LATER))
        with pytest.raises(
            PublicationStepNotReadyError, match="已重跑，Rel 产物已过期"
        ):
            rel_loader.assert_rel_publish_strict(SETTINGS, SOURCE_ID, JOB_ID)

    def test_a20_equal_timestamps_treated_stale(self, monkeypatch, tmp_path):
        self._install(monkeypatch, tmp_path, self._rows(json_at=NOW, rel_at=NOW))
        with pytest.raises(PublicationStepNotReadyError, match="过期"):
            rel_loader.assert_rel_publish_strict(SETTINGS, SOURCE_ID, JOB_ID)

    def test_strict_passes_when_fresh(self, monkeypatch, tmp_path):
        self._install(monkeypatch, tmp_path, self._rows())
        rel_loader.assert_rel_publish_strict(SETTINGS, SOURCE_ID, JOB_ID)  # 不抛即通过

    def test_rerun_between_assert_and_capture_rejected(self, monkeypatch, tmp_path):
        """A20 强化（并发窗口）：json 在 assert 与 capture 两个无锁会话之间
        完成重跑——捕获期以锚点 marker 复查排序，旧 rel 不再配合新 json 发布。"""
        rows = self._rows()  # json=EARLIER / rel=NOW → assert 以旧时间戳通过
        rel_dir = self._install(monkeypatch, tmp_path, rows)
        rel_loader.assert_rel_publish_strict(SETTINGS, SOURCE_ID, JOB_ID)
        _write_rel_artifact(rel_dir)
        rows["json"] = _step_row(step="json", updated_at=LATER)  # 重跑恰在其间提交
        with pytest.raises(PublicationStepNotReadyError, match="过期"):
            rel_loader.capture_rel_precheck_snapshot(SETTINGS, SOURCE_ID, JOB_ID)

    def test_capture_snapshot_happy_path(self, monkeypatch, tmp_path):
        rel_dir = self._install(monkeypatch, tmp_path, self._rows())
        fingerprint = _write_rel_artifact(rel_dir)
        snap = rel_loader.capture_rel_precheck_snapshot(SETTINGS, SOURCE_ID, JOB_ID)
        assert snap.rel_fingerprint == fingerprint
        assert snap.json_marker == EARLIER
        assert snap.domain_marker == EARLIER
        assert snap.registration.db_name == DB_NAME

    def test_capture_extra_file_rejected(self, monkeypatch, tmp_path):
        rel_dir = self._install(monkeypatch, tmp_path, self._rows())
        _write_rel_artifact(rel_dir)
        (rel_dir / "extra.txt").write_text("x", encoding="utf-8")
        with pytest.raises(PublicationStepNotReadyError, match="两份文件"):
            rel_loader.capture_rel_precheck_snapshot(SETTINGS, SOURCE_ID, JOB_ID)

    def test_capture_tmp_ignored(self, monkeypatch, tmp_path):
        rel_dir = self._install(monkeypatch, tmp_path, self._rows())
        _write_rel_artifact(rel_dir)
        (rel_dir / ".tmp-x").write_text("x", encoding="utf-8")
        rel_loader.capture_rel_precheck_snapshot(SETTINGS, SOURCE_ID, JOB_ID)  # 通过

    def test_capture_database_mismatch_rejected(self, monkeypatch, tmp_path):
        rel_dir = self._install(monkeypatch, tmp_path, self._rows())
        (rel_dir / f"{DB_NAME}.relationships_global.json").write_text(
            json.dumps({"database": "other", "relationships": [], "statistics": {}}),
            encoding="utf-8",
        )
        (rel_dir / f"{DB_NAME}.relationships_global.md").write_text(
            "# r", encoding="utf-8"
        )
        with pytest.raises(PublicationStepNotReadyError, match="结构不合法"):
            rel_loader.capture_rel_precheck_snapshot(SETTINGS, SOURCE_ID, JOB_ID)


# ---- _publish_rel 执行链（A15–A17、A19、A22–A28） ----


class TestPublishRelExecution:
    def _run(self, monkeypatch, tmp_path, snapshot=None):
        root, rel_dir = _workspace(tmp_path)
        fingerprint = _write_rel_artifact(rel_dir)
        snapshot = snapshot or _snapshot(tmp_path, fingerprint)
        session = FakeSession(source_row=_source_row())
        recorder = _install_publish(monkeypatch, tmp_path, session, snapshot)
        result = _publish_rel(SETTINGS, SOURCE_ID, JOB_ID, uuid4(), NOW, snapshot)
        return recorder, result

    def test_success_message_and_counts(self, monkeypatch, tmp_path):
        FakeGenerator.gen_result = _gen_result(tables=4, columns=30, relationships=3)
        recorder, result = self._run(monkeypatch, tmp_path)
        assert result["status"] == "success"
        assert "4 张表 / 30 列 / 3 条关系" in result["message"]
        assert recorder.inserted == [
            {"step": "rel", "status": "success", "summary": None, "current": True}
        ]
        assert recorder.cleared == ["rel"]
        assert FakeLoader.calls[-1] == "close"  # A26
        assert ("load", True) in FakeLoader.calls  # A17：全清重载（clean=True）
        loader_section = FakeLoader.ctor_config["cql_loader"]
        assert loader_section["input_file"].endswith("import_all.orders.cypher")
        # A28：主配置 false 也强制 True
        assert loader_section["options"]["validate_after_load"] is True
        # provenance 传递（A13 链路）
        assert FakeGenerator.ctor_kwargs["provenance"] is not None

    def test_a19_zero_relationship_warning(self, monkeypatch, tmp_path):
        FakeGenerator.gen_result = _gen_result(relationships=0)
        recorder, result = self._run(monkeypatch, tmp_path)
        assert result["status"] == "success"
        assert "关系为空" in result["message"]

    def test_a16_validate_failure_no_revoke(self, monkeypatch, tmp_path):
        FakeGenerator.gen_result = _gen_result()
        FakeLoader.validate_result = False
        recorder, result = self._run(monkeypatch, tmp_path)
        assert result["status"] == "failed"
        assert recorder.inserted[0]["summary"] == "Rel 发布失败：Neo4j 服务不可用"
        assert recorder.cleared == []
        assert recorder.stored_failures == []
        assert "close" in FakeLoader.calls  # A26：validate 拒绝路径也收尾

    def test_a16_load_returns_failure_with_mismatch(self, monkeypatch, tmp_path):
        FakeGenerator.gen_result = _gen_result()
        FakeLoader.load_result = {
            "success": False,
            "stats_mismatch": [{"item": "table_count", "expected": 4, "actual": 3}],
        }
        recorder, result = self._run(monkeypatch, tmp_path)
        assert result["status"] == "failed"
        assert "图内数量与脚本统计不符" in recorder.inserted[0]["summary"]
        assert "table_count 期望 4/实际 3" in recorder.inserted[0]["summary"]
        assert recorder.cleared == ["rel"]
        assert recorder.inserted[0]["current"] is False
        assert "close" in FakeLoader.calls  # A26：load 返回失败路径也收尾

    def test_a16_load_returns_failure_with_section_error(self, monkeypatch, tmp_path):
        FakeGenerator.gen_result = _gen_result()
        FakeLoader.load_result = {
            "success": False,
            "errors": [{"section_name": "S", "error_message": "boom"}],
        }
        recorder, result = self._run(monkeypatch, tmp_path)
        assert recorder.inserted[0]["summary"] == "Rel 发布失败：boom"

    def test_a16_load_raises_generic_revokes_current(self, monkeypatch, tmp_path):
        FakeGenerator.gen_result = _gen_result()
        FakeLoader.load_raises = RuntimeError("net")
        recorder, result = self._run(monkeypatch, tmp_path)
        assert "加载中断" in recorder.inserted[0]["summary"]
        assert recorder.cleared == ["rel"]
        assert "close" in FakeLoader.calls  # A26：load 异常路径也收尾

    def test_a23_script_validation_rejected_before_clean(self, monkeypatch, tmp_path):
        from metaweave.core.loaders.cql_loader import CQLScriptValidationError

        FakeGenerator.gen_result = _gen_result()
        FakeLoader.load_raises = CQLScriptValidationError("bad script")
        recorder, result = self._run(monkeypatch, tmp_path)
        assert recorder.inserted[0]["summary"] == (
            "Rel 发布失败：CQL 脚本缺少必需章节或统计头"
        )
        assert recorder.cleared == []
        assert recorder.stored_failures == []
        assert "close" in FakeLoader.calls  # A26：脚本核验拒绝路径也收尾

    def test_a15_generation_failure_row(self, monkeypatch, tmp_path):
        FakeGenerator.gen_result = _gen_result(
            success=False, files=(), errors=["生成炸了"]
        )
        recorder, result = self._run(monkeypatch, tmp_path)
        assert "CQL 生成或产物核验未通过（生成炸了）" in recorder.inserted[0]["summary"]
        assert recorder.cleared == []
        assert recorder.stored_failures == []

    def test_a22_fingerprint_mismatch_raises_no_row(self, monkeypatch, tmp_path):
        root, rel_dir = _workspace(tmp_path)
        _write_rel_artifact(rel_dir)
        snapshot = _snapshot(tmp_path, "ffffffffffffffff")
        session = FakeSession(source_row=_source_row())
        recorder = _install_publish(monkeypatch, tmp_path, session, snapshot)
        with pytest.raises(PublicationStepNotReadyError, match="内容已变更"):
            _publish_rel(SETTINGS, SOURCE_ID, JOB_ID, uuid4(), NOW, snapshot)
        assert recorder.inserted == []
        assert recorder.stored_failures == []

    def test_a04_three_verifies_with_captured_markers(self, monkeypatch, tmp_path):
        FakeGenerator.gen_result = _gen_result()
        recorder, _ = self._run(monkeypatch, tmp_path)
        assert set(recorder.verified) == {
            ("rel", NOW),
            ("json", EARLIER),
            ("domain", EARLIER),
        }

    def test_wide_exception_uses_store_failure_unlocked(self, monkeypatch, tmp_path):
        FakeGenerator.gen_raises = RuntimeError("gen segfault")
        recorder, result = self._run(monkeypatch, tmp_path)
        assert recorder.stored_failures == [
            {
                "step": "rel",
                "summary": "Rel 发布失败，请检查配置",
                "revoke": False,
            }
        ]
        assert recorder.inserted == []


class TestPostLoadFailureRevocation:
    """§6.3 提交失败撤销（审核修订）：进入过 load() 后的任何失败都不得保留
    指向已消失数据的旧 current——宽兜底按 replacement_started 传 revoke。"""

    def _setup(self, monkeypatch, tmp_path):
        root, rel_dir = _workspace(tmp_path)
        fingerprint = _write_rel_artifact(rel_dir)
        snapshot = _snapshot(tmp_path, fingerprint)
        session = FakeSession(source_row=_source_row())
        recorder = _install_publish(monkeypatch, tmp_path, session, snapshot)
        return snapshot, session, recorder

    def test_post_load_commit_failure_revokes_current(self, monkeypatch, tmp_path):
        """load 成功、锁内 commit 失败 → 图已替换，锁外补记撤销旧 current。"""
        FakeGenerator.gen_result = _gen_result()
        snapshot, session, recorder = self._setup(monkeypatch, tmp_path)
        session.fail_commit = True

        result = _publish_rel(SETTINGS, SOURCE_ID, JOB_ID, uuid4(), NOW, snapshot)

        assert result.status == "failed"
        assert recorder.stored_failures == [
            {
                "step": "rel",
                "summary": "Rel 发布失败，请检查配置",
                "revoke": True,
            }
        ]

    def test_post_load_value_error_revokes_current(self, monkeypatch, tmp_path):
        """加载后的意外 ValueError 不映射 400（会保留旧 current），按已替换撤销。"""
        FakeGenerator.gen_result = _gen_result()
        FakeLoader.load_result = {"success": False}
        snapshot, _session, recorder = self._setup(monkeypatch, tmp_path)

        def boom(_result):
            raise ValueError("summary crashed")

        monkeypatch.setattr(publish_service, "_rel_failure_summary", boom)

        result = _publish_rel(SETTINGS, SOURCE_ID, JOB_ID, uuid4(), NOW, snapshot)

        assert result.status == "failed"
        assert recorder.stored_failures[0]["revoke"] is True

    def test_pre_load_wide_failure_keeps_current(self, monkeypatch, tmp_path):
        """对照组：加载前（生成段）失败图未动 → 不撤销（既有语义回归锁）。"""
        FakeGenerator.gen_raises = RuntimeError("gen segfault")
        snapshot, _session, recorder = self._setup(monkeypatch, tmp_path)

        _publish_rel(SETTINGS, SOURCE_ID, JOB_ID, uuid4(), NOW, snapshot)

        assert recorder.stored_failures == [
            {
                "step": "rel",
                "summary": "Rel 发布失败，请检查配置",
                "revoke": False,
            }
        ]


def test_generation_check_requires_exact_cypher_name(monkeypatch, tmp_path):
    """审核修订 5：核验段精确比对 import_all.{db}.cypher——仅元数据 md
    （import_all.orders.md）不能满足，防 startswith 前缀被 md 单独满足。"""
    FakeGenerator.gen_result = _gen_result(files=("import_all.orders.md", "meta.md"))
    root, rel_dir = _workspace(tmp_path)
    fingerprint = _write_rel_artifact(rel_dir)
    snapshot = _snapshot(tmp_path, fingerprint)
    session = FakeSession(source_row=_source_row())
    recorder = _install_publish(monkeypatch, tmp_path, session, snapshot)

    result = _publish_rel(SETTINGS, SOURCE_ID, JOB_ID, uuid4(), NOW, snapshot)

    assert result["status"] == "failed"
    assert "CQL 生成或产物核验未通过" in recorder.inserted[0]["summary"]
    assert recorder.cleared == []
