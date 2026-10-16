"""meta_load --type db：平台画像入库与清理边界。"""

import json
import logging
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import UUID

import pytest
from click.testing import CliRunner

from backend.persistence.models import DbProfile, DbSourceInfo, JobInfo
from backend.profile_loading import db_loader
from backend.persistence.models import PublishInfo
from backend.profile_loading.db_loader import (
    MetaLoadConfigError,
    MetaLoadSourceDisabledError,
    MetaLoadSourceNotFoundError,
    load_db_profile,
    read_db_profile_input,
    replace_db_profile,
    resolve_db_profile_input_path,
)
from metaweave.cli.main import cli
from metaweave.cli.meta_loader_cli import meta_load_command
from metaweave.core.clean_targets import resolve_db_output_dir
from metaweave.utils.file_utils import clean_top_level_files


SOURCE_ID = UUID("b9a4943d-44d6-4f35-b553-d25207f9d8c1")
OTHER_SOURCE_ID = UUID("adb15693-2dda-4e4d-8ca7-ad5164c5e85d")
JOB_ID = UUID("550e8400-e29b-41d4-a716-446655440000")
# 步骤行运行标识（updated_at）：start_step/finish_step 每次写入都会刷新。
RUN_MARKER = datetime(2026, 9, 27, 10, 0, 0, tzinfo=timezone.utc)


class FakePlatformSession:
    def __init__(self):
        self.source = SimpleNamespace(source_id=SOURCE_ID, is_active=True)
        self.step = SimpleNamespace(
            job_id=JOB_ID,
            source_id=SOURCE_ID,
            step_name="db",
            last_status="success",
            updated_at=RUN_MARKER,
        )
        self.publishes: list[PublishInfo] = []
        self.profiles: list[DbProfile] = []
        self.added = []
        self.committed = []
        self.flush_calls = 0
        self.fail_commit = False

    def get(self, model, key):
        if model is DbSourceInfo:
            return self.source if key == SOURCE_ID else None
        if model is JobInfo:
            return self.step if key == {"job_id": JOB_ID, "step_name": "db"} else None
        raise AssertionError(model)

    def scalars(self, statement):
        entity = statement.column_descriptions[0]["entity"]
        params = statement.compile().params
        if entity is JobInfo:
            return [self.step] if self.step is not None else []
        if entity is DbSourceInfo:
            source_id = params.get("source_id_1")
            if self.source is not None and source_id == self.source.source_id:
                return [self.source]
            return []
        if entity is PublishInfo:
            source_id = params.get("source_id_1")
            step_name = params.get("step_name_1")
            rows = self.publishes
            if source_id is not None:
                rows = [row for row in rows if row.source_id == source_id]
            if step_name is not None:
                rows = [row for row in rows if row.step_name == step_name]
            return rows
        if entity is DbProfile:
            source_id = params.get("source_id_1")
            return [row for row in self.profiles if row.source_id == source_id]
        raise AssertionError(entity)

    def add(self, row):
        assert isinstance(row, (DbProfile, PublishInfo))
        self.added.append(row)

    def flush(self):
        """no-op：真实会话在 replace_db_profile 中间 flush 使 DELETE 先行下发。"""
        self.flush_calls += 1

    def delete(self, row):
        if row in self.profiles:
            self.profiles.remove(row)

    def commit(self):
        if self.fail_commit:
            raise RuntimeError("database password should never be shown")
        for row in self.added:
            if isinstance(row, PublishInfo):
                self.publishes.append(row)
            else:
                self.profiles.append(row)
        self.committed.extend(self.added)
        self.added.clear()


@pytest.fixture
def loaded_workspace(monkeypatch, tmp_path):
    monkeypatch.setattr("metaweave.utils.file_utils.get_project_root", lambda: tmp_path)
    monkeypatch.setattr(db_loader, "load_project_env", lambda: None)
    monkeypatch.setattr(db_loader, "load_platform_settings_from_env", lambda: object())
    session = FakePlatformSession()

    @contextmanager
    def fake_platform_session(_settings):
        yield session

    monkeypatch.setattr(db_loader, "platform_session", fake_platform_session)
    db_dir = tmp_path / "output" / str(SOURCE_ID) / str(JOB_ID) / "db"
    db_dir.mkdir(parents=True)
    profile = {
        "schema_version": 1,
        "source_id": str(SOURCE_ID),
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
    path = db_dir / "database.json"
    path.write_text(json.dumps(profile), encoding="utf-8")
    return session, db_dir, path, profile


def test_load_maps_database_json_and_keeps_input(loaded_workspace):
    session, _db_dir, path, profile = loaded_workspace
    result = load_db_profile(SOURCE_ID, JOB_ID)
    assert result["success"] is True
    assert result["source_id"] == str(SOURCE_ID)
    assert result["job_id"] == str(JOB_ID)
    assert UUID(result["publish_id"])
    assert len(session.committed) == 2  # 成功 publish 行 + 整行画像（doc 16 §5）
    publish_rows = [row for row in session.committed if isinstance(row, PublishInfo)]
    assert len(publish_rows) == 1
    assert publish_rows[0].publish_id == UUID(result["publish_id"])
    assert publish_rows[0].status == "success"
    assert publish_rows[0].is_current_publish is True
    row = session.profiles[0]
    assert row.publish_id == UUID(result["publish_id"])
    assert row.source_id == SOURCE_ID and row.job_id == JOB_ID
    assert row.schemas == profile["schemas"]
    assert row.selected_schemas == ["public"]
    assert row.ordinary_table_count == 2 and row.partition_child_count == 3
    assert row.record_count is None and row.server_version == "18.0"
    assert row.server_version_full is None and row.os_platform is None
    assert path.exists()
    assert session.step.last_status == "success"


def test_optional_platform_fields_map_or_reject(loaded_workspace):
    session, _db_dir, path, profile = loaded_workspace
    profile["database"][
        "server_version_full"
    ] = "PostgreSQL 18.1 on x86_64-windows, compiled by msvc-19.44.35221, 64-bit"
    profile["database"]["os_platform"] = "Plan9"
    path.write_text(json.dumps(profile), encoding="utf-8")

    result = load_db_profile(SOURCE_ID, JOB_ID)
    assert result["success"] is True
    row = session.profiles[0]
    assert row.server_version_full == profile["database"]["server_version_full"]
    assert row.os_platform == "Plan9"

    profile["database"]["os_platform"] = ""
    path.write_text(json.dumps(profile), encoding="utf-8")
    with pytest.raises(db_loader.ProfileInputError, match="os_platform"):
        read_db_profile_input(SOURCE_ID, JOB_ID)

    profile["database"]["server_version_full"] = 18
    profile["database"]["os_platform"] = "Windows x86_64"
    path.write_text(json.dumps(profile), encoding="utf-8")
    with pytest.raises(db_loader.ProfileInputError, match="server_version_full"):
        read_db_profile_input(SOURCE_ID, JOB_ID)


def test_reloading_same_job_replaces_the_single_profile_row(loaded_workspace):
    """meta_load 重复加载：画像每源一行被替换，publish 各留一行（doc 16 §3.1/§5）。"""
    session, _db_dir, _path, _profile = loaded_workspace
    first = load_db_profile(SOURCE_ID, JOB_ID)
    second = load_db_profile(SOURCE_ID, JOB_ID)
    assert first["success"] and second["success"]
    assert first["publish_id"] != second["publish_id"]
    assert len(session.profiles) == 1
    assert session.profiles[0].publish_id == UUID(second["publish_id"])
    assert len(session.publishes) == 2
    current = [row for row in session.publishes if row.is_current_publish]
    assert len(current) == 1
    assert current[0].publish_id == UUID(second["publish_id"])


def test_load_db_profile_rejects_rerun_after_read(loaded_workspace, monkeypatch):
    """读取输入后步骤被重跑：取消本次加载，不写 failed 历史行（doc 16 §5）。"""
    session, _db_dir, _path, _profile = loaded_workspace
    real_read = db_loader.read_db_profile_input

    def racing_read(source_id, job_id):
        result = real_read(source_id, job_id)
        # 重跑已完整走完 running→success：状态仍 success，但运行标识已变，
        # 读到的输入可能来自重跑中途的部分产物，必须拒绝入库。
        session.step.updated_at = RUN_MARKER + timedelta(seconds=5)
        return result

    monkeypatch.setattr(db_loader, "read_db_profile_input", racing_read)
    result = load_db_profile(SOURCE_ID, JOB_ID)
    assert result["success"] is False
    assert "已被重跑" in result["message"]
    assert session.publishes == [] and session.profiles == []


def test_replacing_legacy_multiple_rows_warns_and_converges(loaded_workspace, caplog):
    """未迁移旧库的同源多行：替换前记一条警告，提交后收敛为一行（doc 16 §6）。"""
    session, _db_dir, _path, _profile = loaded_workspace
    session.profiles.extend(
        [
            DbProfile(publish_id=UUID(int=1), source_id=SOURCE_ID, job_id=JOB_ID),
            DbProfile(publish_id=UUID(int=2), source_id=SOURCE_ID, job_id=JOB_ID),
        ]
    )
    with caplog.at_level(logging.WARNING, logger="backend.profile_loading"):
        result = load_db_profile(SOURCE_ID, JOB_ID)
    assert result["success"] is True
    assert "db_profile 同源存在多行（2 行）" in caplog.text
    assert len(session.profiles) == 1
    assert session.profiles[0].publish_id == UUID(result["publish_id"])


def test_clean_only_after_commit_and_only_top_level(loaded_workspace, monkeypatch):
    session, db_dir, path, _profile = loaded_workspace
    hidden = db_dir / ".debug"
    hidden.write_text("debug", encoding="utf-8")
    child = db_dir / "notes" / "keep.txt"
    child.parent.mkdir()
    child.write_text("keep", encoding="utf-8")
    other = db_dir.parent.parent / "other-job" / "db" / "keep.txt"
    other.parent.mkdir(parents=True)
    other.write_text("other", encoding="utf-8")

    def observed_clean(directory):
        assert any(isinstance(row, DbProfile) for row in session.committed)
        assert any(
            isinstance(row, PublishInfo) and row.status == "success"
            for row in session.committed
        )
        return clean_top_level_files(directory)

    monkeypatch.setattr(db_loader, "clean_top_level_files", observed_clean)
    result = load_db_profile(SOURCE_ID, JOB_ID, clean=True)
    assert result["success"] is True
    assert not path.exists() and not hidden.exists()
    assert child.read_text(encoding="utf-8") == "keep"
    assert other.read_text(encoding="utf-8") == "other"
    retry = load_db_profile(SOURCE_ID, JOB_ID)
    assert retry["success"] is False
    assert len(session.committed) == 2


def test_missing_input_does_not_commit_or_clean(loaded_workspace, monkeypatch):
    session, _db_dir, path, _profile = loaded_workspace
    path.unlink()
    monkeypatch.setattr(
        db_loader, "clean_top_level_files", lambda _: pytest.fail("clean called")
    )
    result = load_db_profile(SOURCE_ID, JOB_ID, clean=True)
    assert result["success"] is False
    assert "database.json" in result["message"]
    assert not session.committed


@pytest.mark.parametrize(
    "mutation",
    [
        lambda p: p.update(schema_version=2),
        lambda p: p.update(source_id=str(OTHER_SOURCE_ID)),
        lambda p: p["database"].update(size_bytes=-1),
        lambda p: p["totals"].update(object_count=999),
    ],
)
def test_invalid_profile_does_not_commit_or_clean(
    loaded_workspace, monkeypatch, mutation
):
    session, _db_dir, path, profile = loaded_workspace
    mutation(profile)
    path.write_text(json.dumps(profile), encoding="utf-8")
    monkeypatch.setattr(
        db_loader, "clean_top_level_files", lambda _: pytest.fail("clean called")
    )
    result = load_db_profile(SOURCE_ID, JOB_ID, clean=True)
    assert result["success"] is False
    assert path.exists() and not session.committed


def test_wrong_workspace_owner_is_config_error(loaded_workspace):
    session, _db_dir, _path, _profile = loaded_workspace
    session.step.source_id = OTHER_SOURCE_ID
    with pytest.raises(MetaLoadConfigError, match="不属于该数据源"):
        load_db_profile(SOURCE_ID, JOB_ID, clean=True)
    assert not session.committed


def test_disabled_source_is_config_error_without_reading_or_cleaning(loaded_workspace):
    """禁用数据源在读取产物与清理之前被拦（doc 11 §4）。"""
    session, db_dir, path, _profile = loaded_workspace
    session.source.is_active = False
    with pytest.raises(MetaLoadConfigError, match="已禁用"):
        load_db_profile(SOURCE_ID, JOB_ID, clean=True)
    assert not session.committed
    assert path.exists()
    assert db_dir.exists()


def test_missing_source_or_workspace_is_config_error(loaded_workspace):
    session, _db_dir, _path, _profile = loaded_workspace
    session.source = None
    with pytest.raises(MetaLoadConfigError, match="source_id"):
        load_db_profile(SOURCE_ID, JOB_ID)
    session.source = SimpleNamespace(source_id=SOURCE_ID, is_active=True)
    session.step = None
    with pytest.raises(MetaLoadConfigError, match="job_id"):
        load_db_profile(SOURCE_ID, JOB_ID)


def test_non_successful_step_does_not_load(loaded_workspace):
    session, _db_dir, path, _profile = loaded_workspace
    session.step.last_status = "failed"
    result = load_db_profile(SOURCE_ID, JOB_ID, clean=True)
    assert result["success"] is False
    assert path.exists() and not session.committed


def test_symlinked_database_json_is_rejected(loaded_workspace):
    session, db_dir, path, _profile = loaded_workspace
    other = db_dir / "other.json"
    path.rename(other)
    path.symlink_to(other)
    result = load_db_profile(SOURCE_ID, JOB_ID, clean=True)
    assert result["success"] is False
    assert other.exists() and not session.committed


def test_platform_commit_failure_preserves_file(loaded_workspace):
    session, _db_dir, path, _profile = loaded_workspace
    session.fail_commit = True
    result = load_db_profile(SOURCE_ID, JOB_ID, clean=True)
    assert result["success"] is False
    assert "password" not in result["message"]
    assert path.exists() and not session.committed


@pytest.mark.parametrize(
    ("vanish", "expected"),
    [
        ("deleted", MetaLoadSourceNotFoundError),
        ("disabled", MetaLoadSourceDisabledError),
    ],
)
def test_failure_record_raises_when_source_gone(loaded_workspace, vanish, expected):
    """提交失败后源被并发删除/禁用：失败记录不写入，并明确报源状态。

    普通提交错误之后、失败记录写入之前源消失时，不能只报通用入库失败
    （doc 16 §5/§7：对外行为与成功提交的锁下重查一致）。
    """
    session, _db_dir, path, _profile = loaded_workspace
    real_commit = session.commit

    def commit_and_vanish():
        # 模拟竞态：画像提交失败的那一刻，登记被并发删除或禁用
        if vanish == "deleted":
            session.source = None
        else:
            session.source.is_active = False
        session.fail_commit = True
        real_commit()

    session.commit = commit_and_vanish
    with pytest.raises(expected):
        load_db_profile(SOURCE_ID, JOB_ID)
    assert session.publishes == []  # 不新增 failed 历史行
    assert path.exists()


def test_clean_failure_reports_persisted_publish_id(loaded_workspace, monkeypatch):
    session, _db_dir, path, _profile = loaded_workspace

    def fail_clean(_directory):
        raise PermissionError("no permission")

    monkeypatch.setattr(db_loader, "clean_top_level_files", fail_clean)
    result = load_db_profile(SOURCE_ID, JOB_ID, clean=True)
    assert result["success"] is False
    assert "画像已入库、清理失败" in result["message"]
    assert result["publish_id"] == str(session.committed[0].publish_id)
    assert path.exists()


@pytest.mark.parametrize("linked_level", ["output", "source", "job", "db"])
def test_resolver_rejects_in_project_symlink(monkeypatch, tmp_path, linked_level):
    monkeypatch.setattr("metaweave.utils.file_utils.get_project_root", lambda: tmp_path)
    expected = tmp_path / "output" / str(SOURCE_ID) / str(JOB_ID) / "db"
    other = tmp_path / "other" / "db"
    other.mkdir(parents=True)
    keep = other / "keep.txt"
    keep.write_text("keep", encoding="utf-8")
    if linked_level == "output":
        (tmp_path / "output").symlink_to(tmp_path / "other", target_is_directory=True)
    elif linked_level == "source":
        expected.parents[2].mkdir()
        expected.parents[1].symlink_to(tmp_path / "other", target_is_directory=True)
    elif linked_level == "job":
        expected.parents[1].mkdir(parents=True)
        expected.parent.symlink_to(tmp_path / "other", target_is_directory=True)
    else:
        expected.parent.mkdir(parents=True)
        expected.symlink_to(other, target_is_directory=True)
    with pytest.raises(ValueError, match="符号链接"):
        resolve_db_output_dir(SOURCE_ID, JOB_ID)
    assert keep.read_text(encoding="utf-8") == "keep"


def test_shared_loader_functions_stage_rows_and_load_commits_both(loaded_workspace):
    """replace_db_profile 只改会话不提交；load 提交成功 publish 行 + 画像行。"""
    session, db_dir, path, _profile = loaded_workspace
    resolved = resolve_db_profile_input_path(SOURCE_ID, JOB_ID)
    fields, parent = read_db_profile_input(SOURCE_ID, JOB_ID)
    assert resolved == path and parent == db_dir
    replace_db_profile(session, UUID(int=1), fields)
    # DELETE 先行下发依赖中间 flush；若被当冗余删除，同主键 delete+add 会
    # 被折叠成漏列的 UPDATE（假会话只锁"flush 被调用"这一事实）。
    assert session.flush_calls >= 1
    assert session.added and not session.committed
    assert all(isinstance(row, DbProfile) for row in session.added)
    session.added.clear()  # 直接调用属于从未提交的事务，不留给后续 load
    result = load_db_profile(SOURCE_ID, JOB_ID)
    assert result["success"] is True
    assert len(session.committed) == 2
    assert any(isinstance(row, PublishInfo) for row in session.committed)
    assert any(isinstance(row, DbProfile) for row in session.committed)


def test_cli_rejects_unsupported_and_missing_options():
    runner = CliRunner()
    for args in (
        ["--type", "ddl"],
        ["--type", "db"],
        ["--type", "db", "--type", "db"],
        ["--type", "db", "--source-id", str(SOURCE_ID)],
        [
            "--type",
            "db",
            "--source-id",
            str(SOURCE_ID),
            "--source-id",
            str(SOURCE_ID),
            "--job-id",
            str(JOB_ID),
        ],
    ):
        result = runner.invoke(meta_load_command, args)
        assert result.exit_code == 2, result.output


def test_root_help_registers_meta_load():
    result = CliRunner().invoke(cli, ["--help"])
    assert result.exit_code == 0
    assert "meta_load" in result.output


def test_cli_reports_disabled_source_as_usage_error(monkeypatch):
    """禁用数据源：meta_load 以 exit 2 结束且消息含"已禁用"（doc 11 §9 第 6 条）。"""
    args = ["--type", "db", "--source-id", str(SOURCE_ID), "--job-id", str(JOB_ID)]

    def disabled(*_args, **_kwargs):
        raise MetaLoadConfigError("指定数据源已禁用，不能加载画像")

    monkeypatch.setattr(db_loader, "load_db_profile", disabled)
    result = CliRunner().invoke(meta_load_command, args)
    assert result.exit_code == 2, result.output
    assert "已禁用" in result.output


def test_cli_success_and_runtime_failure(monkeypatch):
    runner = CliRunner()
    args = ["--type", "db", "--source-id", str(SOURCE_ID), "--job-id", str(JOB_ID)]
    monkeypatch.setattr(
        db_loader,
        "load_db_profile",
        lambda *_args, **_kwargs: {
            "success": True,
            "message": "完成",
            "job_id": str(JOB_ID),
            "publish_id": str(JOB_ID),
        },
    )
    success = runner.invoke(meta_load_command, args)
    assert success.exit_code == 0 and "publish_id" in success.output
    monkeypatch.setattr(
        db_loader,
        "load_db_profile",
        lambda *_args, **_kwargs: {
            "success": False,
            "message": "找不到 database.json",
            "job_id": str(JOB_ID),
        },
    )
    failure = runner.invoke(meta_load_command, args)
    assert failure.exit_code == 1 and "找不到 database.json" in failure.output
