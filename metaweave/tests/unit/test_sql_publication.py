"""doc 25.2 任务 2/3：sql 发布预检与执行单元测试（A06–A10、A16、A18、A19 发布侧）。"""

from contextlib import contextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

import backend.publishing.publish_service as publish_service
from backend.publishing.errors import PublicationStepNotReadyError
from backend.publishing.publish_service import (
    check_publication,
    normalize_publish_steps,
    publish_steps,
)

SOURCE_ID = uuid4()
JOB_ID = uuid4()
DB_NAME = "orders"
NOW = datetime(2026, 10, 11, 6, 0, 0, tzinfo=timezone.utc)
SETTINGS = SimpleNamespace(database_url="fake")


class FakeSession:
    def __init__(self, step_rows=None, source_row=None):
        self.step_rows = step_rows or {}
        self.source_row = source_row

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


def _step_row(status="success", updated_at=NOW, step="sql"):
    return SimpleNamespace(
        job_id=JOB_ID, step_name=step, last_status=status, updated_at=updated_at
    )


class Recorder:
    def __init__(self):
        self.inserted = []
        self.cleared = []
        self.stored_failures = []


def _make_inspector(rows):
    def inspector(sess, sid, jid):
        row = rows.get("sql")
        if row is None:
            return None, None
        return row.last_status, row.updated_at

    return inspector


# ---------------------------------------------------------------------------
# A18 / A07：白名单与枚举
# ---------------------------------------------------------------------------


class TestWhitelistAndEnums:
    def test_normalize_accepts_sql(self):
        assert normalize_publish_steps(["sql"]) == ["sql"]

    def test_normalize_rejects_unknown_with_sql_in_message(self):
        from backend.publishing.publish_service import (
            UnsupportedPublicationStepError,
        )

        with pytest.raises(UnsupportedPublicationStepError) as ei:
            normalize_publish_steps(["md"])
        assert "sql" in str(ei.value)

    def test_a07_step_name_literals_accept_sql(self):
        from backend.api.schemas import PublicationStepResult, PublishCheckStep

        PublishCheckStep(
            step_name="sql",
            job_status=None,
            output_ready=True,
            publishable=True,
            reason=None,
        )
        PublicationStepResult(
            step="sql", status="success", message="ok", finished_at="x"
        )


# ---------------------------------------------------------------------------
# A06 / A08：check_publication 显式分支与三段 reason 链
# ---------------------------------------------------------------------------


class TestCheckPublicationSql:
    def _install(self, monkeypatch, rows, output_ready=True, job_ok=True):
        session = FakeSession(step_rows=rows, source_row=_source_row())
        monkeypatch.setattr(
            publish_service, "platform_session", lambda s: _fake_session_ctx(session)
        )
        monkeypatch.setattr(
            publish_service, "_job_is_publishable", lambda *a, **k: job_ok
        )

        def mk(step):
            def inspector(sess, sid, jid):
                row = rows.get(step)
                if row is None:
                    return None, None
                return row.last_status, row.updated_at

            return inspector

        # 8 键整字典 patch（doc 25.2 §9 B 组同款形态）：sql 条目由真实
        # check_publication 经显式 elif 分支产出
        monkeypatch.setattr(
            publish_service,
            "_INSPECTORS",
            {
                publish_service.DB_STEP: mk("db"),
                publish_service.DDL_STEP: mk("ddl"),
                publish_service.JSON_STEP: mk("json"),
                publish_service.DOMAIN_STEP: mk("domain"),
                publish_service.REL_STEP: mk("rel"),
                publish_service.TABLE_SCHEMA_STEP: mk("table_schema"),
                publish_service.DIM_VALUE_STEP: mk("dim_config"),
                publish_service.SQL_STEP: mk("sql"),
            },
        )
        monkeypatch.setattr(
            publish_service, "check_sql_output_ready", lambda *a, **k: output_ready
        )
        # 其余步骤的产物检查统一假化，避免触碰真实路径
        monkeypatch.setattr(
            publish_service, "check_db_output_ready", lambda *a, **k: (True, None)
        )
        for name in (
            "check_ddl_output_ready",
            "check_json_output_ready",
            "check_domain_output_ready",
            "check_table_schema_output_ready",
            "check_dim_value_output_ready",
            "check_rel_output_ready",
        ):
            if hasattr(publish_service, name):
                monkeypatch.setattr(publish_service, name, lambda *a, **k: True)
        return session

    def test_a06_publishable_when_row_success_and_file(self, monkeypatch):
        self._install(monkeypatch, {"sql": _step_row()})
        entry = next(
            s
            for s in check_publication(SETTINGS, SOURCE_ID, JOB_ID)["steps"]
            if s["step_name"] == "sql"
        )
        assert entry["publishable"] is True
        assert entry["reason"] is None

    def test_a06_reason_step_not_success(self, monkeypatch):
        self._install(monkeypatch, {"sql": _step_row(status="failed")})
        entry = next(
            s
            for s in check_publication(SETTINGS, SOURCE_ID, JOB_ID)["steps"]
            if s["step_name"] == "sql"
        )
        assert entry["publishable"] is False
        assert entry["reason"] == "最近一次采集失败"

    def test_a06_reason_job_not_publishable(self, monkeypatch):
        self._install(monkeypatch, {"sql": _step_row()}, job_ok=False)
        entry = next(
            s
            for s in check_publication(SETTINGS, SOURCE_ID, JOB_ID)["steps"]
            if s["step_name"] == "sql"
        )
        assert "不能发布" in entry["reason"]

    def test_a06_reason_output_missing(self, monkeypatch):
        self._install(monkeypatch, {"sql": _step_row()}, output_ready=False)
        entry = next(
            s
            for s in check_publication(SETTINGS, SOURCE_ID, JOB_ID)["steps"]
            if s["step_name"] == "sql"
        )
        assert entry["reason"] == "产物不存在或不可用"

    def test_a06_row_missing_returns_entry_not_crash(self, monkeypatch):
        """从未跑过 sql 的作业：条目正常返回（也覆盖 _INSPECTORS 注册完整性）。"""
        self._install(monkeypatch, {})
        entry = next(
            s
            for s in check_publication(SETTINGS, SOURCE_ID, JOB_ID)["steps"]
            if s["step_name"] == "sql"
        )
        assert entry["job_status"] is None
        assert entry["publishable"] is False
        assert entry["reason"] == "没有执行记录"

    def test_a06_sql_is_last_in_step_list(self, monkeypatch):
        """循环顺序：sql 追加在 dim_value 之后（末位）。"""
        self._install(monkeypatch, {"sql": _step_row()})
        names = [
            s["step_name"]
            for s in check_publication(SETTINGS, SOURCE_ID, JOB_ID)["steps"]
        ]
        assert names[-1] == "sql"
        assert names[-2] == "dim_value"


# ---------------------------------------------------------------------------
# A09 / A10 / A16 / A19：_publish_sql 执行链与失败文案分派
# ---------------------------------------------------------------------------


class FakeSqlLoader:
    def __init__(self, validate=True, load_result=None, load_raises=None):
        self.validate_return = validate
        self.load_result = load_result or {"success": True, "replacement_started": True}
        self.load_raises = load_raises
        self.closed = 0
        self.load_calls = 0

    def validate(self):
        return self.validate_return

    def load(self):
        self.load_calls += 1
        if self.load_raises is not None:
            raise self.load_raises
        return dict(self.load_result)

    def close(self):
        self.closed += 1


class TestPublishSql:
    def _install(
        self,
        monkeypatch,
        loader,
        verify=None,
        rows=None,
    ):
        session = FakeSession(
            step_rows=rows or {"sql": _step_row()}, source_row=_source_row()
        )
        recorder = Recorder()

        def fake_verify(sess, job_id, step, marker):
            if verify is not None:
                verify(sess, job_id, step, marker)

        monkeypatch.setattr(
            publish_service, "platform_session", lambda s: _fake_session_ctx(session)
        )
        monkeypatch.setattr(
            publish_service, "lock_db_source_row", lambda sess, sid: sess.source_row
        )
        monkeypatch.setattr(
            publish_service, "assert_workspace_owner", lambda *a, **k: None
        )
        monkeypatch.setattr(
            publish_service, "_verify_step_current_under_lock", fake_verify
        )
        build_calls = []

        def fake_build(source_id, job_id, publish_id):
            build_calls.append(
                {"source_id": source_id, "job_id": job_id, "publish_id": publish_id}
            )
            return loader

        monkeypatch.setattr(publish_service, "build_sql_example_loader", fake_build)

        def fake_insert(
            sess,
            publish_id,
            source_id,
            job_id,
            step,
            status,
            error_summary=None,
            is_current=False,
        ):
            recorder.inserted.append(
                {
                    "step": step,
                    "status": status,
                    "error_summary": error_summary,
                    "is_current": is_current,
                }
            )

        monkeypatch.setattr(publish_service, "insert_publish_row", fake_insert)

        def fake_clear(sess, source_id, step):
            recorder.cleared.append(step)

        monkeypatch.setattr(publish_service, "clear_current", fake_clear)

        def fake_store_failure(
            settings, source_id, job_id, publish_id, step, summary, revoke_current=False
        ):
            recorder.stored_failures.append(
                {"step": step, "summary": summary, "revoke_current": revoke_current}
            )
            return {
                "step": step,
                "status": "failed",
                "message": summary,
                "finished_at": "x",
            }

        monkeypatch.setattr(publish_service, "_store_failure", fake_store_failure)
        return session, recorder, build_calls

    def _publish(self, steps=("sql",)):
        return publish_steps(SETTINGS, SOURCE_ID, JOB_ID, list(steps))

    def _install_precheck(self, monkeypatch, rows):
        """publish_steps 前置：准入放行 + chosen 循环所需的 inspector。"""
        monkeypatch.setattr(
            publish_service,
            "_INSPECTORS",
            {publish_service.SQL_STEP: _make_inspector(rows)},
        )
        monkeypatch.setattr(
            publish_service, "_assert_job_admission", lambda *a, **k: None
        )

    def test_a09_success_path(self, monkeypatch):
        loader = FakeSqlLoader(
            load_result={
                "success": True,
                "replacement_started": True,
                "records_loaded": 2,
            }
        )
        self._install(monkeypatch, loader)
        self._install_precheck(monkeypatch, {"sql": _step_row()})
        result = self._publish()
        assert result["results"][0]["status"] == "success"
        assert result["results"][0]["message"] == "SQL 样例已发布到 Milvus（共 2 条）"
        assert loader.load_calls == 1 and loader.closed == 1

    def test_a09_uuid_objects_passed_to_builder(self, monkeypatch):
        loader = FakeSqlLoader(
            load_result={
                "success": True,
                "replacement_started": True,
                "records_loaded": 1,
            }
        )
        _, _, build_calls = self._install(monkeypatch, loader)
        self._install_precheck(monkeypatch, {"sql": _step_row()})
        self._publish()
        assert len(build_calls) == 1
        from uuid import UUID

        assert isinstance(build_calls[0]["source_id"], UUID)
        assert isinstance(build_calls[0]["publish_id"], UUID)
        assert build_calls[0]["job_id"] == JOB_ID

    def test_a09_success_row_current(self, monkeypatch):
        loader = FakeSqlLoader(
            load_result={
                "success": True,
                "replacement_started": True,
                "records_loaded": 2,
            }
        )
        _, recorder, _ = self._install(monkeypatch, loader)
        self._install_precheck(monkeypatch, {"sql": _step_row()})
        self._publish()
        assert recorder.cleared == ["sql"]
        assert recorder.inserted == [
            {
                "step": "sql",
                "status": "success",
                "error_summary": None,
                "is_current": True,
            }
        ]
        assert recorder.stored_failures == []

    def test_a10_rerun_changed_marker_rejected(self, monkeypatch):
        def boom(sess, job_id, step, marker):
            raise PublicationStepNotReadyError("步骤尚未采集成功")

        loader = FakeSqlLoader()
        _, recorder, build_calls = self._install(monkeypatch, loader, verify=boom)
        self._install_precheck(monkeypatch, {"sql": _step_row()})
        result = self._publish()
        assert result["results"][0]["status"] == "failed"
        assert recorder.stored_failures[0]["step"] == "sql"
        assert build_calls == []  # 复查失败即终止，未构造 loader

    def test_a10_publish_holding_lock_succeeds(self, monkeypatch):
        """发布先取得锁并通过复查：重跑等待锁释放，本次发布成功。"""
        loader = FakeSqlLoader(
            load_result={
                "success": True,
                "replacement_started": True,
                "records_loaded": 1,
            }
        )
        self._install(monkeypatch, loader)
        self._install_precheck(monkeypatch, {"sql": _step_row()})
        result = self._publish()
        assert result["results"][0]["status"] == "success"

    def test_a09_validate_false_generic_failure(self, monkeypatch):
        loader = FakeSqlLoader(validate=False)
        _, recorder, _ = self._install(monkeypatch, loader)
        self._install_precheck(monkeypatch, {"sql": _step_row()})
        result = self._publish()
        assert result["results"][0]["status"] == "failed"
        entry = recorder.stored_failures[0]
        assert entry["step"] == "sql"
        assert entry["summary"] == (
            "SQL 样例发布失败，请检查 sql-rag 产物及 Milvus、Embedding 服务"
        )
        assert entry["revoke_current"] is False
        assert loader.closed == 1

    def test_a19_cleanup_message(self, monkeypatch):
        # 真实 loader 形态：cleanup 分支恒填 error_summary（删旧异常摘要，修订 11），
        # ⑤ 命中追加规则带原因后缀（审核修订 28）
        loader = FakeSqlLoader(
            load_result={
                "success": False,
                "replacement_started": True,
                "error_kind": "cleanup",
                "error_summary": "delete rpc failed",
                "records_loaded": 5,
                "records_failed": 0,
                "records_unconfirmed": 0,
            }
        )
        _, recorder, _ = self._install(monkeypatch, loader)
        self._install_precheck(monkeypatch, {"sql": _step_row()})
        self._publish()
        row = recorder.inserted[0]
        assert row["status"] == "failed" and row["is_current"] is False
        assert recorder.cleared == ["sql"]  # replacement_started=True 撤销旧 current
        assert row["error_summary"] == (
            "SQL 样例已写入 Milvus（共 5 条），但本源旧记录未清理，请重新发布；原因：delete rpc failed"
        )

    def test_a19_unconfirmed_message(self, monkeypatch):
        loader = FakeSqlLoader(
            load_result={
                "success": False,
                "replacement_started": True,
                "error_kind": None,
                "records_loaded": 5,
                "records_failed": 2,
                "records_unconfirmed": 3,
            }
        )
        _, recorder, _ = self._install(monkeypatch, loader)
        self._install_precheck(monkeypatch, {"sql": _step_row()})
        self._publish()
        assert recorder.inserted[0]["error_summary"] == (
            "SQL 样例未全部发布到 Milvus（成功 5 条 / 失败 2 条，"
            "另有 3 条写入结果未确认），请重新发布"
        )

    def test_a19_failed_only_message(self, monkeypatch):
        loader = FakeSqlLoader(
            load_result={
                "success": False,
                "replacement_started": True,
                "error_kind": None,
                "records_loaded": 5,
                "records_failed": 2,
                "records_unconfirmed": 0,
            }
        )
        _, recorder, _ = self._install(monkeypatch, loader)
        self._install_precheck(monkeypatch, {"sql": _step_row()})
        self._publish()
        assert recorder.inserted[0]["error_summary"] == (
            "SQL 样例未全部发布到 Milvus（成功 5 条 / 失败 2 条），请重新发布"
        )

    def test_a19_mixed_failed_with_reason(self, monkeypatch):
        loader = FakeSqlLoader(
            load_result={
                "success": False,
                "replacement_started": True,
                "error_kind": None,
                "records_loaded": 5,
                "records_failed": 2,
                "records_unconfirmed": 0,
                "error_summary": "unexpected mid-flight",
            }
        )
        _, recorder, _ = self._install(monkeypatch, loader)
        self._install_precheck(monkeypatch, {"sql": _step_row()})
        self._publish()
        assert recorder.inserted[0]["error_summary"] == (
            "SQL 样例未全部发布到 Milvus（成功 5 条 / 失败 2 条），请重新发布"
            "；原因：unexpected mid-flight"
        )

    def test_a19_unclassified_only_message(self, monkeypatch):
        loader = FakeSqlLoader(
            load_result={
                "success": False,
                "replacement_started": False,
                "error_kind": None,
                "records_loaded": 0,
                "records_failed": 0,
                "records_unconfirmed": 0,
                "error_summary": "boom",
            }
        )
        _, recorder, _ = self._install(monkeypatch, loader)
        self._install_precheck(monkeypatch, {"sql": _step_row()})
        self._publish()
        assert recorder.inserted[0]["error_summary"] == (
            "SQL 样例发布失败（boom），请重新发布"
        )
        assert recorder.cleared == []  # replacement_started=False 保留旧 current

    def test_load_raises_treated_as_started(self, monkeypatch):
        loader = FakeSqlLoader(load_raises=RuntimeError("milvus down"))
        _, recorder, _ = self._install(monkeypatch, loader)
        self._install_precheck(monkeypatch, {"sql": _step_row()})
        result = self._publish()
        assert result["results"][0]["status"] == "failed"
        row = recorder.inserted[0]
        assert row["status"] == "failed"
        assert row["error_summary"] == "SQL 样例发布失败（milvus down），请重新发布"
        assert recorder.cleared == ["sql"]  # 保守按已开始替换

    def test_input_without_file_omits_parens(self, monkeypatch):
        """合并零条（error_file=None）不渲染"（None）"（审核修订 27）。"""
        loader = FakeSqlLoader(
            load_result={
                "success": False,
                "replacement_started": False,
                "error_kind": "input",
                "error_file": None,
                "error_summary": "合并后没有任何可加载的 SQL 样例",
            }
        )
        _, recorder, _ = self._install(monkeypatch, loader)
        self._install_precheck(monkeypatch, {"sql": _step_row()})
        self._publish()
        assert recorder.inserted[0]["error_summary"] == (
            "SQL 样例产物不可读或结构非法，请重跑 sql 采集步骤"
        )

    def test_a16_combined_request_independent_rows(self, monkeypatch):
        loader = FakeSqlLoader(
            load_result={
                "success": True,
                "replacement_started": True,
                "records_loaded": 1,
            }
        )
        _, recorder, _ = self._install(monkeypatch, loader)
        self._install_precheck(monkeypatch, {"sql": _step_row()})

        def fake_db(settings, source_id, job_id, publish_id, marker):
            return {
                "step": "db",
                "status": "success",
                "message": "ok",
                "finished_at": "x",
            }

        monkeypatch.setattr(
            publish_service,
            "_INSPECTORS",
            {
                publish_service.DB_STEP: _make_inspector({"sql": _step_row()}),
                publish_service.SQL_STEP: _make_inspector({"sql": _step_row()}),
            },
        )
        monkeypatch.setattr(
            publish_service,
            "_PUBLISHERS",
            {
                publish_service.DB_STEP: fake_db,
                publish_service.SQL_STEP: publish_service._publish_sql,
            },
        )
        result = self._publish(steps=("db", "sql"))
        assert [r["step"] for r in result["results"]] == ["db", "sql"]
        assert all(r["status"] == "success" for r in result["results"])
        assert recorder.inserted[0]["step"] == "sql"
