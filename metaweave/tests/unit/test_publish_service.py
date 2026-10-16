"""发布预检与 db 发布事务。平台库用假会话，产物用临时目录。"""

import json
import logging
import traceback
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import UUID

import pytest
from sqlalchemy.exc import IntegrityError

from backend.jobs.repository import WorkspaceMissingError, WorkspaceSourceMismatchError
from backend.persistence.models import (
    DbProfile,
    DbSourceInfo,
    JobInfo,
    PublishInfo,
    TableDefinition,
)
from backend.persistence.platform_db import PlatformSettings
from backend.profile_loading import db_loader
from backend.profile_loading.db_loader import (
    MetaLoadSourceDisabledError,
    MetaLoadSourceNotFoundError,
    ProfileInputError,
    commit_db_current_state,
    read_db_profile_input,
    resolve_db_profile_input_path,
)
from backend.publishing import publish_service
from backend.publishing.publish_service import (
    INPUT_SUMMARY,
    INTERNAL_MESSAGE,
    NOT_READY_MESSAGE,
    WRITE_SUMMARY,
    PublicationConflictError,
    PublicationStepNotReadyError,
    check_db_output_ready,
    check_publication,
    is_current_index_conflict,
    is_primary_key_conflict,
    publish_steps,
)
from backend.data_sources.registration_service import PlatformUnavailableError

SOURCE_ID = UUID("b9a4943d-44d6-4f35-b553-d25207f9d8c1")
OTHER_SOURCE_ID = UUID("adb15693-2dda-4e4d-8ca7-ad5164c5e85d")
JOB_ID = UUID("550e8400-e29b-41d4-a716-446655440000")
SETTINGS = PlatformSettings(
    host="platform", port=5432, name="metaweave", user="admin", password="secret"
)
LEAK_SQL = "LEAK-SQL-TEXT-7391"
LEAK_PARAM = "LEAK-PARAM-VALUE-8264"
# 步骤行运行标识（updated_at）：start_step/finish_step 每次写入都会刷新。
RUN_MARKER = datetime(2026, 9, 27, 10, 0, 0, tzinfo=timezone.utc)


def profile_document(source_id=SOURCE_ID):
    return {
        "schema_version": 1,
        "source_id": str(source_id),
        "collected_at": "2026-09-25T08:00:00Z",
        "database": {
            "type": "postgresql",
            "host": "example.internal",
            "port": 5432,
            "name": "orders",
            "server_version": "18.0",
            "size_bytes": 1234,
            "size_scope": "whole_database",
            "record_count": None,
        },
        "selected_schemas": ["public"],
        "schemas": [
            {
                "name": "public",
                "ordinary_table_count": 2,
                "partitioned_table_count": 1,
                "partition_child_count": 3,
                "view_count": 1,
                "materialized_view_count": 0,
                "object_count": 4,
            }
        ],
        "totals": {
            "ordinary_table_count": 2,
            "partitioned_table_count": 1,
            "partition_child_count": 3,
            "view_count": 1,
            "materialized_view_count": 0,
            "object_count": 4,
        },
    }


class _Diag:
    def __init__(self, name):
        self.constraint_name = name


class _Orig:
    def __init__(self, sqlstate, name):
        self.sqlstate = sqlstate
        self.diag = _Diag(name) if name is not None else None


def integrity_error(sqlstate, name, statement=LEAK_SQL):
    return IntegrityError(statement, {"password": LEAK_PARAM}, _Orig(sqlstate, name))


def assert_safe_primary_key_exception(exc: BaseException) -> None:
    """服务器会打印这个异常的格式化堆栈，其中不能出现原始 SQL 或参数。"""
    assert type(exc) is RuntimeError
    assert str(exc) == INTERNAL_MESSAGE
    assert exc.__cause__ is None
    rendered = "".join(traceback.format_exception(exc))
    assert LEAK_SQL not in rendered
    assert LEAK_PARAM not in rendered


class FakePublishSession:
    def __init__(self):
        self.source = SimpleNamespace(source_id=SOURCE_ID, is_active=True)
        self.steps = [
            SimpleNamespace(
                job_id=JOB_ID,
                source_id=SOURCE_ID,
                step_name="db",
                last_status="success",
                updated_at=RUN_MARKER,
            )
        ]
        self.publishes: list[PublishInfo] = []
        self.profiles: list[DbProfile] = []
        self.definitions: list[TableDefinition] = []
        self.pending: list = []
        self.snapshot = None
        self._profiles_snapshot: list[DbProfile] | None = None
        self._definitions_snapshot: list[TableDefinition] | None = None
        self.commit_errors: list[BaseException] = []
        self.read_error: BaseException | None = None
        self.flush_calls = 0
        self._clock = datetime(2026, 9, 27, tzinfo=timezone.utc)

    def tick(self) -> datetime:
        """代替 func.now()，每次调用前进一秒。"""
        self._clock += timedelta(seconds=1)
        return self._clock

    def get(self, model, key):
        if self.read_error is not None:
            raise self.read_error
        if model is DbSourceInfo:
            return self.source if key == SOURCE_ID else None
        if model is JobInfo:
            for step in self.steps:
                if {"job_id": step.job_id, "step_name": step.step_name} == key:
                    return step
            return None
        raise AssertionError(model)

    def scalars(self, statement):
        entity = statement.column_descriptions[0]["entity"]
        params = statement.compile().params
        if entity is JobInfo:
            job_id = params.get("job_id_1")
            rows = self.steps
            if job_id is not None:
                rows = [step for step in rows if step.job_id == job_id]
            return rows
        if entity is DbSourceInfo:
            source_id = params.get("source_id_1")
            if self.source is not None and source_id == self.source.source_id:
                return [self.source]
            return []
        if entity is DbProfile:
            source_id = params.get("source_id_1")
            return [row for row in self.profiles if row.source_id == source_id]
        if entity is PublishInfo:
            rows = [row for row in self.publishes if row.is_current_publish]
            source_id = params.get("source_id_1")
            step_name = params.get("step_name_1")
            if source_id is not None:
                rows = [row for row in rows if row.source_id == source_id]
            if step_name is not None:
                rows = [row for row in rows if row.step_name == step_name]
            return rows
        raise AssertionError(entity)

    def add(self, row):
        self.pending.append(row)

    def flush(self):
        """no-op：真实会话在 replace_db_profile 中间 flush 使 DELETE 先行下发。"""
        self.flush_calls += 1

    def add_all(self, rows):
        for row in rows:
            self.add(row)

    def execute(self, statement):
        """受支持的最小 execute：按 source_id/define_type 删 table_definition。"""
        if not getattr(statement, "is_delete", False):
            raise AssertionError(type(statement).__name__)
        params = statement.compile().params
        self.definitions[:] = [
            row
            for row in self.definitions
            if not (
                row.source_id == params.get("source_id_1")
                and row.define_type == params.get("define_type_1")
            )
        ]
        return None

    def delete(self, row):
        if row in self.profiles:
            self.profiles.remove(row)

    def commit(self):
        if self.commit_errors:
            raise self.commit_errors.pop(0)
        for row in self.pending:
            if isinstance(row, PublishInfo):
                if row.created_at is None:
                    row.created_at = self.tick()
                if row.updated_at is None:
                    row.updated_at = row.created_at
                self.publishes.append(row)
            elif isinstance(row, DbProfile):
                self.profiles.append(row)
            elif isinstance(row, TableDefinition):
                if row.loaded_at is None:
                    row.loaded_at = self.tick()
                self.definitions.append(row)
            else:
                raise AssertionError(type(row))
        self.pending.clear()
        self.snapshot = None
        self._profiles_snapshot = None
        self._definitions_snapshot = None

    def rollback(self):
        self.pending.clear()
        if self.snapshot is not None:
            for row, flag, updated in self.snapshot:
                row.is_current_publish = flag
                row.updated_at = updated
            self.snapshot = None
        if self._profiles_snapshot is not None:
            self.profiles[:] = self._profiles_snapshot
            self._profiles_snapshot = None
        if self._definitions_snapshot is not None:
            self.definitions[:] = self._definitions_snapshot
            self._definitions_snapshot = None

    def begin(self):
        self.pending = []
        self.snapshot = [
            (row, row.is_current_publish, getattr(row, "updated_at", None))
            for row in self.publishes
        ]
        self._profiles_snapshot = list(self.profiles)
        self._definitions_snapshot = list(self.definitions)


def install_session(monkeypatch, tmp_path, session: FakePublishSession | None = None):
    monkeypatch.setattr("metaweave.utils.file_utils.get_project_root", lambda: tmp_path)
    holder = session or FakePublishSession()

    @contextmanager
    def fake_platform_session(_settings):
        holder.begin()
        try:
            yield holder
        except Exception:
            holder.rollback()
            raise

    monkeypatch.setattr(publish_service, "platform_session", fake_platform_session)
    monkeypatch.setattr(db_loader, "platform_session", fake_platform_session)
    monkeypatch.setattr("backend.publishing.repository.func.now", holder.tick)
    return holder


def write_profile(tmp_path, document=None, source_id=SOURCE_ID, job_id=JOB_ID):
    db_dir = tmp_path / "output" / str(source_id) / str(job_id) / "db"
    db_dir.mkdir(parents=True)
    path = db_dir / "database.json"
    path.write_text(json.dumps(document or profile_document()), encoding="utf-8")
    return db_dir, path


def test_conflict_predicates_use_sqlstate_and_constraint_name():
    current = integrity_error("23505", "publish_info_current_idx")
    publish_pk = integrity_error("23505", "publish_info_pkey")
    profile_pk = integrity_error("23505", "db_profile_pkey")
    check = integrity_error("23514", "publish_info_status_check")
    assert is_current_index_conflict(current) is True
    assert is_current_index_conflict(publish_pk) is False
    assert is_current_index_conflict(check) is False
    assert is_current_index_conflict(RuntimeError("x")) is False
    assert is_primary_key_conflict(publish_pk) is True
    assert is_primary_key_conflict(profile_pk) is True
    assert is_primary_key_conflict(current) is False
    assert is_primary_key_conflict(check) is False
    assert is_primary_key_conflict(RuntimeError("x")) is False
    missing = IntegrityError(LEAK_SQL, {}, SimpleNamespace(sqlstate="23505"))
    assert is_current_index_conflict(missing) is False
    assert is_primary_key_conflict(missing) is False


def test_ready_profile_is_publishable(monkeypatch, tmp_path):
    install_session(monkeypatch, tmp_path)
    db_dir, path = write_profile(tmp_path)
    before = path.read_text(encoding="utf-8")
    result = check_publication(SETTINGS, SOURCE_ID, JOB_ID)
    step = result["steps"][0]
    assert result["source_id"] == str(SOURCE_ID)
    assert step["job_status"] == "success"
    assert step["output_ready"] is True
    assert step["publishable"] is True
    assert step["reason"] is None
    assert path.read_text(encoding="utf-8") == before
    assert db_dir.is_dir()


@pytest.mark.parametrize(
    ("status", "reason"),
    [("failed", "最近一次采集失败"), ("running", "正在执行")],
)
def test_unsuccessful_job_is_not_publishable(monkeypatch, tmp_path, status, reason):
    session = install_session(monkeypatch, tmp_path)
    session.steps[0].last_status = status
    write_profile(tmp_path)
    step = check_publication(SETTINGS, SOURCE_ID, JOB_ID)["steps"][0]
    assert step["publishable"] is False
    assert step["job_status"] == status
    assert step["reason"] == reason
    assert step["output_ready"] is True


def test_job_without_db_row_stays_a_check_result(monkeypatch, tmp_path):
    session = install_session(monkeypatch, tmp_path)
    session.steps = [
        SimpleNamespace(
            job_id=JOB_ID,
            source_id=SOURCE_ID,
            step_name="ddl",
            last_status="success",
            updated_at=RUN_MARKER,
        )
    ]
    write_profile(tmp_path)
    step = check_publication(SETTINGS, SOURCE_ID, JOB_ID)["steps"][0]
    assert step["job_status"] is None
    assert step["publishable"] is False
    assert step["reason"] == "没有执行记录"


@pytest.mark.parametrize(
    "prepare",
    ["missing", "empty", "subdir", "hidden", "directory", "symlink", "linked_dir"],
)
def test_output_not_ready_uses_fixed_reason(monkeypatch, tmp_path, prepare):
    install_session(monkeypatch, tmp_path)
    db_dir, path = write_profile(tmp_path)
    if prepare == "missing":
        path.unlink()
        db_dir.rmdir()
    elif prepare == "empty":
        path.unlink()
        assert db_dir.is_dir() and not any(db_dir.iterdir())
    elif prepare == "subdir":
        path.unlink()
        (db_dir / "notes").mkdir()
    elif prepare == "hidden":
        path.unlink()
        (db_dir / ".keep").write_text("x", encoding="utf-8")
    elif prepare == "directory":
        path.unlink()
        path.mkdir()
    elif prepare == "symlink":
        other = db_dir / "other.json"
        path.rename(other)
        path.symlink_to(other)
    else:
        path.unlink()
        outside = tmp_path / "outside"
        outside.mkdir()
        db_dir.rmdir()
        db_dir.symlink_to(outside, target_is_directory=True)
    step = check_publication(SETTINGS, SOURCE_ID, JOB_ID)["steps"][0]
    assert step["output_ready"] is False
    assert step["publishable"] is False
    assert step["reason"] == "产物不存在或不可用"
    assert not (tmp_path / "output" / str(SOURCE_ID) / str(JOB_ID) / "created-by-check").exists()
    if prepare == "missing":
        assert not db_dir.exists()
    elif prepare == "empty":
        assert db_dir.is_dir() and not any(db_dir.iterdir())


def test_failed_status_reason_wins_over_missing_output(monkeypatch, tmp_path):
    session = install_session(monkeypatch, tmp_path)
    session.steps[0].last_status = "failed"
    step = check_publication(SETTINGS, SOURCE_ID, JOB_ID)["steps"][0]
    assert step["output_ready"] is False
    assert step["reason"] == "最近一次采集失败"


def test_precheck_logs_specific_input_reason(monkeypatch, tmp_path, caplog):
    install_session(monkeypatch, tmp_path)
    caplog.set_level(logging.WARNING, logger="backend.publishing")
    check_db_output_ready(SOURCE_ID, JOB_ID)
    missing = caplog.records[-1]
    assert missing.levelno == logging.WARNING
    assert str(SOURCE_ID) in missing.message and str(JOB_ID) in missing.message
    assert "未找到可加载的普通文件" in missing.message
    assert LEAK_PARAM not in missing.message

    outside = tmp_path / "outside"
    outside.mkdir()
    linked = tmp_path / "output" / str(SOURCE_ID)
    linked.parent.mkdir(parents=True)
    linked.symlink_to(outside, target_is_directory=True)
    check_db_output_ready(SOURCE_ID, JOB_ID)
    linked_log = caplog.records[-1].message
    assert "符号链接" in linked_log
    assert "产物不存在或不可用" not in linked_log


def test_shared_file_rule_rejects_the_same_inputs(monkeypatch, tmp_path):
    monkeypatch.setattr("metaweave.utils.file_utils.get_project_root", lambda: tmp_path)
    db_dir, path = write_profile(tmp_path)
    assert resolve_db_profile_input_path(SOURCE_ID, JOB_ID) == path
    assert check_db_output_ready(SOURCE_ID, JOB_ID) == (True, None)
    fields, parent = read_db_profile_input(SOURCE_ID, JOB_ID)
    assert parent == db_dir and fields["source_id"] == SOURCE_ID

    path.unlink()
    with pytest.raises(ProfileInputError):
        resolve_db_profile_input_path(SOURCE_ID, JOB_ID)
    assert check_db_output_ready(SOURCE_ID, JOB_ID)[0] is False
    with pytest.raises(ProfileInputError):
        read_db_profile_input(SOURCE_ID, JOB_ID)


def test_invalid_json_is_publishable_then_fails_on_post(monkeypatch, tmp_path):
    session = install_session(monkeypatch, tmp_path)
    _db_dir, path = write_profile(tmp_path)
    path.write_text("{", encoding="utf-8")
    assert check_publication(SETTINGS, SOURCE_ID, JOB_ID)["steps"][0]["publishable"] is True
    result = publish_steps(SETTINGS, SOURCE_ID, JOB_ID, ["db"])
    assert result["results"][0]["status"] == "failed"
    assert result["results"][0]["message"] == INPUT_SUMMARY
    assert session.profiles == []
    assert session.publishes[0].status == "failed"
    assert session.publishes[0].error_summary == INPUT_SUMMARY
    assert session.publishes[0].is_current_publish is False


def test_mismatched_source_in_json_records_failure(monkeypatch, tmp_path):
    session = install_session(monkeypatch, tmp_path)
    write_profile(tmp_path, profile_document(OTHER_SOURCE_ID))
    assert check_publication(SETTINGS, SOURCE_ID, JOB_ID)["steps"][0]["publishable"] is True
    result = publish_steps(SETTINGS, SOURCE_ID, JOB_ID, ["db"])
    assert result["results"][0]["status"] == "failed"
    assert result["results"][0]["message"] == INPUT_SUMMARY
    assert session.profiles == []
    assert len(session.publishes) == 1
    assert session.publishes[0].source_id == SOURCE_ID
    assert session.publishes[0].error_summary == INPUT_SUMMARY


@pytest.mark.parametrize("setup", ["missing", "unicode"])
def test_input_failures_share_one_summary(monkeypatch, tmp_path, setup):
    session = install_session(monkeypatch, tmp_path)
    _db_dir, path = write_profile(tmp_path)
    if setup == "missing":
        path.unlink()
    else:
        path.write_bytes(b"\xff")
    result = publish_steps(SETTINGS, SOURCE_ID, JOB_ID, ["db"])
    assert result["results"][0]["message"] == INPUT_SUMMARY
    assert session.publishes[0].error_summary == INPUT_SUMMARY


def test_missing_source_job_or_disabled_does_not_publish(monkeypatch, tmp_path):
    session = install_session(monkeypatch, tmp_path)
    write_profile(tmp_path)
    session.source = None
    with pytest.raises(MetaLoadSourceNotFoundError):
        publish_steps(SETTINGS, SOURCE_ID, JOB_ID, ["db"])
    session.source = SimpleNamespace(source_id=SOURCE_ID, is_active=False)
    with pytest.raises(MetaLoadSourceDisabledError):
        check_publication(SETTINGS, SOURCE_ID, JOB_ID)
    session.source.is_active = True
    session.steps = []
    with pytest.raises(WorkspaceMissingError):
        publish_steps(SETTINGS, SOURCE_ID, JOB_ID, ["db"])
    session.steps = [
        SimpleNamespace(
            job_id=JOB_ID,
            source_id=OTHER_SOURCE_ID,
            step_name="db",
            last_status="success",
        )
    ]
    with pytest.raises(WorkspaceSourceMismatchError):
        publish_steps(SETTINGS, SOURCE_ID, JOB_ID, ["db"])
    assert session.publishes == [] and session.profiles == []


def test_duplicate_db_runs_once_and_unknown_steps_are_rejected(monkeypatch, tmp_path):
    session = install_session(monkeypatch, tmp_path)
    write_profile(tmp_path)
    result = publish_steps(SETTINGS, SOURCE_ID, JOB_ID, ["db", "db"])
    assert len(result["results"]) == 1
    assert len(session.profiles) == 1
    with pytest.raises(publish_service.UnsupportedPublicationStepError):
        publish_steps(SETTINGS, SOURCE_ID, JOB_ID, [])
    with pytest.raises(publish_service.UnsupportedPublicationStepError):
        publish_steps(SETTINGS, SOURCE_ID, JOB_ID, ["DB"])
    assert len(session.profiles) == 1


def test_status_change_after_check_rejects_without_id(monkeypatch, tmp_path):
    session = install_session(monkeypatch, tmp_path)
    write_profile(tmp_path)
    assert check_publication(SETTINGS, SOURCE_ID, JOB_ID)["steps"][0]["publishable"]
    session.steps[0].last_status = "running"
    with pytest.raises(PublicationStepNotReadyError):
        publish_steps(SETTINGS, SOURCE_ID, JOB_ID, ["db"])
    assert session.publishes == [] and session.profiles == []


def test_deleted_file_after_check_records_failure(monkeypatch, tmp_path):
    session = install_session(monkeypatch, tmp_path)
    _db_dir, path = write_profile(tmp_path)
    assert check_publication(SETTINGS, SOURCE_ID, JOB_ID)["steps"][0]["publishable"]
    path.unlink()
    result = publish_steps(SETTINGS, SOURCE_ID, JOB_ID, ["db"])
    assert result["publish_id"]
    assert result["results"][0]["status"] == "failed"
    assert session.profiles == []
    assert session.publishes[0].publish_id == UUID(result["publish_id"])


def test_successful_publish_links_profile_and_keeps_job(monkeypatch, tmp_path):
    session = install_session(monkeypatch, tmp_path)
    _db_dir, path = write_profile(tmp_path)
    result = publish_steps(SETTINGS, SOURCE_ID, JOB_ID, ["db"])
    assert result["results"][0]["status"] == "success"
    assert result["results"][0]["finished_at"].endswith("Z")
    publish_id = UUID(result["publish_id"])
    assert session.profiles[0].publish_id == publish_id
    assert session.publishes[0].publish_id == publish_id
    assert session.publishes[0].is_current_publish is True
    assert session.publishes[0].created_by_user_id is None
    assert session.publishes[0].error_summary is None
    assert session.steps[0].last_status == "success"
    assert path.exists()


def test_repeat_publish_replaces_the_single_profile_row(monkeypatch, tmp_path):
    """db_profile 每源一行：重复成功发布整行替换（doc 16 §3.1）。"""
    session = install_session(monkeypatch, tmp_path)
    write_profile(tmp_path)
    first = publish_steps(SETTINGS, SOURCE_ID, JOB_ID, ["db"])
    second = publish_steps(SETTINGS, SOURCE_ID, JOB_ID, ["db"])
    assert first["publish_id"] != second["publish_id"]
    # 每次整行替换都经过中间 flush（DELETE 先行下发），不被折叠成 UPDATE。
    assert session.flush_calls == 2
    assert len(session.profiles) == 1
    assert session.profiles[0].publish_id == UUID(second["publish_id"])
    current = [row for row in session.publishes if row.is_current_publish]
    assert len(current) == 1
    assert current[0].publish_id == UUID(second["publish_id"])
    previous = session.publishes[0]
    assert previous.is_current_publish is False
    assert previous.updated_at > previous.created_at


def test_later_failure_keeps_previous_current_and_profile(monkeypatch, tmp_path):
    """失败尝试不清 current、不动画像（doc 16 §1 失败语义）。"""
    session = install_session(monkeypatch, tmp_path)
    _db_dir, path = write_profile(tmp_path)
    first = publish_steps(SETTINGS, SOURCE_ID, JOB_ID, ["db"])
    path.unlink()
    second = publish_steps(SETTINGS, SOURCE_ID, JOB_ID, ["db"])
    assert second["results"][0]["status"] == "failed"
    current = [row for row in session.publishes if row.is_current_publish]
    assert len(current) == 1
    assert current[0].publish_id == UUID(first["publish_id"])
    assert current[0].status == "success"
    assert session.profiles[0].publish_id == UUID(first["publish_id"])


def test_commit_rechecks_source_under_lock_and_skips_failure_row(
    monkeypatch, tmp_path
):
    """锁下重查发现源已删除或禁用：直接抛错，不写失败记录（doc 16 §5）。"""
    session = install_session(monkeypatch, tmp_path)
    write_profile(tmp_path)
    fields, _directory = read_db_profile_input(SOURCE_ID, JOB_ID)
    session.source = None
    with pytest.raises(MetaLoadSourceNotFoundError):
        commit_db_current_state(
            SETTINGS, SOURCE_ID, JOB_ID, UUID(int=1), fields, RUN_MARKER
        )
    session.source = SimpleNamespace(source_id=SOURCE_ID, is_active=False)
    with pytest.raises(MetaLoadSourceDisabledError):
        commit_db_current_state(
            SETTINGS, SOURCE_ID, JOB_ID, UUID(int=1), fields, RUN_MARKER
        )
    assert session.publishes == [] and session.profiles == []


@pytest.mark.parametrize(
    "mutate",
    ["rerun-running", "rerun-finished"],
)
def test_publish_db_step_rerun_during_execution_is_reported_per_step(
    monkeypatch, tmp_path, mutate
):
    """读取输入与提交之间该作业步骤被重跑：按未就绪计入结果并写 failed 历史行。

    执行期间被重跑不再整体抛 400（doc 16 §5 部分成功语义）：多步发布中
    前面的步骤可能已提交，调用方需要 publish_id 追认已提交的当前态。
    rerun-running：重跑进行中（状态翻回 running，目录正被清空重写）；
    rerun-finished：重跑已完整走完 running→success，状态仍 success 但
    运行标识已变——两次读取可能读到新旧混合内容，同样必须拒绝。
    """
    session = install_session(monkeypatch, tmp_path)
    write_profile(tmp_path)
    real_read = publish_service.read_db_profile_input

    def racing_read(source_id, job_id):
        result = real_read(source_id, job_id)
        if mutate == "rerun-running":
            session.steps[0].last_status = "running"
        else:
            session.steps[0].updated_at = RUN_MARKER + timedelta(seconds=5)
        return result

    monkeypatch.setattr(publish_service, "read_db_profile_input", racing_read)
    result = publish_steps(SETTINGS, SOURCE_ID, JOB_ID, ["db"])
    assert result["results"][0]["status"] == "failed"
    # 审核修订后出口保留异常文本：db 发布器锁下本就传具体文案
    assert result["results"][0]["message"] == "db 采集步骤已被重跑"
    # 对外报告了失败尝试就留一条可凭 publish_id 查到的历史行；
    # 不动画像，也不翻 current。
    assert len(session.publishes) == 1
    assert session.publishes[0].status == "failed"
    assert session.publishes[0].publish_id == UUID(result["publish_id"])
    assert session.publishes[0].is_current_publish is False
    assert session.profiles == []


def test_commit_db_rejects_changed_run_marker(monkeypatch, tmp_path):
    """直调提交：运行标识与快照不一致时拒绝，不产生任何写入。"""
    session = install_session(monkeypatch, tmp_path)
    write_profile(tmp_path)
    fields, _directory = read_db_profile_input(SOURCE_ID, JOB_ID)
    with pytest.raises(PublicationStepNotReadyError):
        commit_db_current_state(
            SETTINGS,
            SOURCE_ID,
            JOB_ID,
            UUID(int=1),
            fields,
            RUN_MARKER + timedelta(seconds=5),
        )
    assert session.publishes == [] and session.profiles == []


def test_commit_failure_rolls_back_then_records_failure(monkeypatch, tmp_path):
    session = install_session(monkeypatch, tmp_path)
    write_profile(tmp_path)
    session.commit_errors.append(RuntimeError("database password should never be shown"))
    result = publish_steps(SETTINGS, SOURCE_ID, JOB_ID, ["db"])
    assert result["results"][0]["status"] == "failed"
    assert result["results"][0]["message"] == WRITE_SUMMARY
    assert session.profiles == []
    assert len(session.publishes) == 1
    assert session.publishes[0].status == "failed"
    assert "password" not in (session.publishes[0].error_summary or "")


def test_current_conflict_on_success_does_not_record_failure(monkeypatch, tmp_path):
    session = install_session(monkeypatch, tmp_path)
    write_profile(tmp_path)
    session.commit_errors.append(integrity_error("23505", "publish_info_current_idx"))
    with pytest.raises(PublicationConflictError):
        publish_steps(SETTINGS, SOURCE_ID, JOB_ID, ["db"])
    assert session.profiles == [] and session.publishes == []


def test_current_conflict_on_failure_record_does_not_retry(monkeypatch, tmp_path):
    session = install_session(monkeypatch, tmp_path)
    write_profile(tmp_path)
    (tmp_path / "output" / str(SOURCE_ID) / str(JOB_ID) / "db" / "database.json").unlink()
    session.commit_errors.append(integrity_error("23505", "publish_info_current_idx"))
    with pytest.raises(PublicationConflictError):
        publish_steps(SETTINGS, SOURCE_ID, JOB_ID, ["db"])
    assert session.publishes == [] and session.profiles == []


@pytest.mark.parametrize("constraint", ["publish_info_pkey", "db_profile_pkey"])
def test_primary_key_conflict_on_success_propagates(monkeypatch, tmp_path, constraint):
    session = install_session(monkeypatch, tmp_path)
    write_profile(tmp_path)
    session.commit_errors.append(integrity_error("23505", constraint))
    with pytest.raises(RuntimeError) as caught:
        publish_steps(SETTINGS, SOURCE_ID, JOB_ID, ["db"])
    assert_safe_primary_key_exception(caught.value)
    assert session.profiles == [] and session.publishes == []


@pytest.mark.parametrize("constraint", ["publish_info_pkey", "db_profile_pkey"])
def test_primary_key_conflict_on_failure_record_propagates(
    monkeypatch, tmp_path, constraint
):
    session = install_session(monkeypatch, tmp_path)
    _db_dir, path = write_profile(tmp_path)
    path.unlink()
    session.commit_errors.append(integrity_error("23505", constraint))
    with pytest.raises(RuntimeError) as caught:
        publish_steps(SETTINGS, SOURCE_ID, JOB_ID, ["db"])
    assert_safe_primary_key_exception(caught.value)
    assert session.profiles == [] and session.publishes == []


def test_other_constraint_on_success_records_write_failure(monkeypatch, tmp_path):
    session = install_session(monkeypatch, tmp_path)
    write_profile(tmp_path)
    session.commit_errors.append(integrity_error("23514", "publish_info_status_check"))
    result = publish_steps(SETTINGS, SOURCE_ID, JOB_ID, ["db"])
    assert result["results"][0]["status"] == "failed"
    assert result["results"][0]["message"] == WRITE_SUMMARY
    assert session.profiles == []
    assert session.publishes[0].error_summary == WRITE_SUMMARY


def test_other_constraint_on_failure_record_is_unavailable(monkeypatch, tmp_path):
    session = install_session(monkeypatch, tmp_path)
    write_profile(tmp_path)
    path = tmp_path / "output" / str(SOURCE_ID) / str(JOB_ID) / "db" / "database.json"
    path.unlink()
    session.commit_errors.append(integrity_error("23514", "publish_info_status_check"))
    with pytest.raises(PlatformUnavailableError):
        publish_steps(SETTINGS, SOURCE_ID, JOB_ID, ["db"])
    assert session.publishes == [] and session.profiles == []


def test_constraint_logs_omit_driver_text(monkeypatch, tmp_path, caplog):
    session = install_session(monkeypatch, tmp_path)
    write_profile(tmp_path)
    caplog.set_level(logging.ERROR, logger="backend.publishing")
    session.commit_errors.append(integrity_error("23505", "db_profile_pkey"))
    with pytest.raises(RuntimeError) as caught:
        publish_steps(SETTINGS, SOURCE_ID, JOB_ID, ["db"])
    assert_safe_primary_key_exception(caught.value)
    message = caplog.records[-1].message
    assert str(SOURCE_ID) in message and str(JOB_ID) in message
    assert "成功事务" in message
    assert "IntegrityError" in message
    assert "23505" in message and "db_profile_pkey" in message
    assert LEAK_SQL not in message and LEAK_PARAM not in message


def test_platform_read_failure_is_unavailable(monkeypatch, tmp_path):
    session = install_session(monkeypatch, tmp_path)
    write_profile(tmp_path)
    session.read_error = RuntimeError("DRIVER-SECRET-XYZ")
    with pytest.raises(PlatformUnavailableError):
        publish_steps(SETTINGS, SOURCE_ID, JOB_ID, ["db"])
    assert session.publishes == [] and session.profiles == []


def test_new_publish_ids_differ_from_each_other(monkeypatch, tmp_path):
    install_session(monkeypatch, tmp_path)
    write_profile(tmp_path)
    first = UUID(publish_steps(SETTINGS, SOURCE_ID, JOB_ID, ["db"])["publish_id"])
    second = UUID(publish_steps(SETTINGS, SOURCE_ID, JOB_ID, ["db"])["publish_id"])
    assert first != second
