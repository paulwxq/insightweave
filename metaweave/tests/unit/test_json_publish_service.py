"""json 发布步骤：预检、整批事务、登记复查与失败语义（doc 19.2 §7.4）。"""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import UUID

import pytest

from backend.persistence.models import PublishInfo, TableColumnProfile, TableProfile
from backend.persistence.platform_db import PlatformSettings
from backend.publishing import publish_service
from backend.publishing.publish_service import (
    INPUT_SUMMARY,
    NOT_READY_MESSAGE,
    REGISTRATION_CHANGED_MESSAGE,
    check_publication,
    is_primary_key_conflict,
    normalize_publish_steps,
    publish_steps,
)
from tests.unit.test_json_publish_loader import (
    DB_NAME,
    JOB_ID,
    SOURCE_ID,
    sample_document,
    write_json_dir,
)
from tests.unit.test_publish_service import OTHER_SOURCE_ID, integrity_error

SETTINGS = PlatformSettings(
    host="platform", port=5432, name="metaweave", user="admin", password="secret"
)
RUN_MARKER = datetime(2026, 10, 3, 10, 0, 0, tzinfo=timezone.utc)
LATER = RUN_MARKER + timedelta(seconds=5)
OLD_PUBLISH_ID = UUID("84c017f4-b438-452b-a888-715b7f0cbea2")


class FakeJsonSession:
    """覆盖 json 发布链的最小假会话（对照 FakePublishSession）。"""

    def __init__(
        self, *, mutate_registration=False, mutate_step=False, mutate_workspace=False
    ):
        self.source = SimpleNamespace(
            source_id=SOURCE_ID,
            is_active=True,
            updated_at=RUN_MARKER,
            db_name=DB_NAME,
            selected_schemas=["public"],
        )
        self.steps = [
            SimpleNamespace(
                job_id=JOB_ID,
                source_id=SOURCE_ID,
                step_name="json",
                last_status="success",
                updated_at=RUN_MARKER,
            )
        ]
        self.publishes: list = []
        self.table_profiles: list = []
        self.table_column_profiles: list = []
        self.pending: list = []
        self.begins = 0
        self.mutate_registration = mutate_registration
        self.mutate_step = mutate_step
        self.mutate_workspace = mutate_workspace
        self._clock = datetime(2026, 10, 3, tzinfo=timezone.utc)

    def tick(self) -> datetime:
        self._clock += timedelta(seconds=1)
        return self._clock

    def begin(self):
        self.begins += 1
        self.pending = []
        if self.begins == 2 and self.mutate_registration:
            self.source.updated_at = LATER
        if self.begins == 2 and self.mutate_step:
            for step in self.steps:
                step.updated_at = LATER
        if self.begins == 2 and self.mutate_workspace:
            for step in self.steps:
                step.source_id = OTHER_SOURCE_ID

    def get(self, model, key):
        if model.__name__ == "DbSourceInfo":
            return self.source if key == SOURCE_ID else None
        if model.__name__ == "JobInfo":
            for step in self.steps:
                if {"job_id": step.job_id, "step_name": step.step_name} == key:
                    return step
            return None
        raise AssertionError(model)

    def scalars(self, statement):
        entity = statement.column_descriptions[0]["entity"]
        params = statement.compile().params
        if entity.__name__ == "JobInfo":
            job_id = params.get("job_id_1")
            rows = self.steps
            if job_id is not None:
                rows = [step for step in rows if step.job_id == job_id]
            return rows
        if entity.__name__ == "DbSourceInfo":
            source_id = params.get("source_id_1")
            if self.source is not None and source_id == self.source.source_id:
                return [self.source]
            return []
        if entity.__name__ == "PublishInfo":
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
        """对象行先落库的分隔点（真实会话语义）。"""

    def add_all(self, rows):
        for row in rows:
            self.add(row)

    def execute(self, statement):
        if not getattr(statement, "is_delete", False):
            raise AssertionError(type(statement).__name__)
        params = statement.compile().params
        source_id = params.get("source_id_1")
        removed = [row for row in self.table_profiles if row.source_id == source_id]
        self.table_profiles[:] = [
            row for row in self.table_profiles if row.source_id != source_id
        ]
        # 模拟 ON DELETE CASCADE：字段行随对象行级联删除
        removed_keys = {
            (row.source_id, row.schema_name, row.object_name) for row in removed
        }
        self.table_column_profiles[:] = [
            row
            for row in self.table_column_profiles
            if (row.source_id, row.schema_name, row.object_name) not in removed_keys
        ]
        return None

    def commit(self):
        for row in self.pending:
            if isinstance(row, PublishInfo):
                if row.created_at is None:
                    row.created_at = self.tick()
                if row.updated_at is None:
                    row.updated_at = row.created_at
                self.publishes.append(row)
            elif isinstance(row, TableProfile):
                self.table_profiles.append(row)
            elif isinstance(row, TableColumnProfile):
                self.table_column_profiles.append(row)
            else:
                raise AssertionError(type(row))
        self.pending.clear()

    def rollback(self):
        self.pending.clear()


def install(monkeypatch, tmp_path, session=None):
    monkeypatch.setattr("metaweave.utils.file_utils.get_project_root", lambda: tmp_path)
    holder = session or FakeJsonSession()

    @contextmanager
    def fake_platform_session(_settings):
        holder.begin()
        try:
            yield holder
        except Exception:
            holder.rollback()
            raise

    monkeypatch.setattr(publish_service, "platform_session", fake_platform_session)
    import backend.publishing.repository as repository

    monkeypatch.setattr(repository, "func", SimpleNamespace(now=lambda: holder.tick()))
    return holder


def seed_old_current(session):
    """预置一份旧的 json current 与画像行。"""
    session.publishes.append(
        PublishInfo(
            publish_id=OLD_PUBLISH_ID,
            step_name="json",
            source_id=SOURCE_ID,
            job_id=JOB_ID,
            status="success",
            is_current_publish=True,
            created_at=RUN_MARKER,
            updated_at=RUN_MARKER,
        )
    )
    session.table_profiles.append(
        SimpleNamespace(
            source_id=SOURCE_ID,
            schema_name="public",
            object_name="legacy",
            publish_id=OLD_PUBLISH_ID,
        )
    )
    session.table_column_profiles.append(
        SimpleNamespace(
            source_id=SOURCE_ID,
            schema_name="public",
            object_name="legacy",
            column_name="id",
            publish_id=OLD_PUBLISH_ID,
        )
    )


class TestWhitelistAndPrecheck:
    def test_json_whitelisted(self):
        assert normalize_publish_steps(["db", "ddl", "json", "json"]) == [
            "db",
            "ddl",
            "json",
        ]

    def test_md_still_rejected(self):
        from backend.publishing.publish_service import (
            UnsupportedPublicationStepError,
        )

        with pytest.raises(UnsupportedPublicationStepError):
            normalize_publish_steps(["md"])

    def test_precheck_includes_json_row(self, monkeypatch, tmp_path):
        install(monkeypatch, tmp_path)
        write_json_dir(tmp_path, [sample_document()])
        result = check_publication(SETTINGS, SOURCE_ID, JOB_ID)
        json_row = next(s for s in result["steps"] if s["step_name"] == "json")
        assert json_row["output_ready"] is True
        assert json_row["publishable"] is True

    def test_precheck_json_not_ready_without_output(self, monkeypatch, tmp_path):
        install(monkeypatch, tmp_path)
        result = check_publication(SETTINGS, SOURCE_ID, JOB_ID)
        json_row = next(s for s in result["steps"] if s["step_name"] == "json")
        assert json_row["output_ready"] is False
        assert json_row["publishable"] is False


class TestPublishJson:
    def test_success_replaces_current(self, monkeypatch, tmp_path):
        session = install(monkeypatch, tmp_path)
        seed_old_current(session)
        write_json_dir(tmp_path, [sample_document()])

        result = publish_steps(SETTINGS, SOURCE_ID, JOB_ID, ["json"])

        assert result["results"][0]["status"] == "success"
        publish_id = UUID(result["publish_id"])
        current = [p for p in session.publishes if p.step_name == "json"]
        assert len(current) == 2
        new_row = next(p for p in current if p.publish_id == publish_id)
        assert new_row.is_current_publish is True and new_row.status == "success"
        old_row = next(p for p in current if p.publish_id == OLD_PUBLISH_ID)
        assert old_row.is_current_publish is False
        # 旧对象行删除、新对象与字段行写入且版本一致
        assert [r.object_name for r in session.table_profiles] == ["orders"]
        assert session.table_profiles[0].publish_id == publish_id
        assert {r.column_name for r in session.table_column_profiles} == {
            "order_id",
            "amount",
        }
        assert all(r.publish_id == publish_id for r in session.table_column_profiles)

    def test_bad_input_fails_and_keeps_old_state(self, monkeypatch, tmp_path):
        session = install(monkeypatch, tmp_path)
        seed_old_current(session)
        document = sample_document()
        document["column_profiles"]["amount"]["ordinal_position"] = 0
        write_json_dir(tmp_path, [document])

        result = publish_steps(SETTINGS, SOURCE_ID, JOB_ID, ["json"])

        step = result["results"][0]
        assert step["status"] == "failed"
        assert step["message"] == INPUT_SUMMARY
        assert [r.object_name for r in session.table_profiles] == ["legacy"]
        old_row = next(p for p in session.publishes if p.publish_id == OLD_PUBLISH_ID)
        assert old_row.is_current_publish is True
        failed = [
            p
            for p in session.publishes
            if p.status == "failed" and not p.is_current_publish
        ]
        assert len(failed) == 1

    def test_workspace_owner_rechecked_under_lock(self, monkeypatch, tmp_path):
        """§7.4：锁下复查工作区仍属该源；归属在预检后被改写 → 受控失败。"""
        session = install(monkeypatch, tmp_path, FakeJsonSession(mutate_workspace=True))
        write_json_dir(tmp_path, [sample_document()])

        result = publish_steps(SETTINGS, SOURCE_ID, JOB_ID, ["json"])

        step = result["results"][0]
        assert step["status"] == "failed"
        assert step["message"] == INPUT_SUMMARY
        assert session.table_profiles == []

    def test_registration_changed_fails_controlled(self, monkeypatch, tmp_path):
        session = install(
            monkeypatch, tmp_path, FakeJsonSession(mutate_registration=True)
        )
        write_json_dir(tmp_path, [sample_document()])

        result = publish_steps(SETTINGS, SOURCE_ID, JOB_ID, ["json"])

        step = result["results"][0]
        assert step["status"] == "failed"
        assert step["message"] == REGISTRATION_CHANGED_MESSAGE
        assert session.table_profiles == []

    def test_step_rerun_during_read_reports_not_ready(self, monkeypatch, tmp_path):
        session = install(monkeypatch, tmp_path, FakeJsonSession(mutate_step=True))
        write_json_dir(tmp_path, [sample_document()])

        result = publish_steps(SETTINGS, SOURCE_ID, JOB_ID, ["json"])

        step = result["results"][0]
        assert step["status"] == "failed"
        assert step["message"] == NOT_READY_MESSAGE
        assert session.table_profiles == []

    def test_combined_partial_success(self, monkeypatch, tmp_path):
        session = install(monkeypatch, tmp_path)
        document = sample_document()
        document["object_info"]["total_rows"] = "not-int"
        write_json_dir(tmp_path, [document])
        # db 步骤用替身返回成功：本用例测 json 失败不回滚已成功步骤
        monkeypatch.setitem(
            publish_service._PUBLISHERS,
            "db",
            lambda *args, **kwargs: publish_service._result(
                "db", "success", "数据库画像发布完成"
            ),
        )

        session.steps.append(
            SimpleNamespace(
                job_id=JOB_ID,
                source_id=SOURCE_ID,
                step_name="db",
                last_status="success",
                updated_at=RUN_MARKER,
            )
        )

        result = publish_steps(SETTINGS, SOURCE_ID, JOB_ID, ["db", "json"])

        statuses = {r["step"]: r["status"] for r in result["results"]}
        assert statuses == {"db": "success", "json": "failed"}

    def test_conflict_predicates_include_new_tables(self):
        assert is_primary_key_conflict(integrity_error("23505", "table_profile_pkey"))
        assert is_primary_key_conflict(
            integrity_error("23505", "table_column_profile_pkey")
        )
        assert is_primary_key_conflict(
            integrity_error("23505", "table_column_profile_object_order_idx")
        )
        assert not is_primary_key_conflict(integrity_error("23505", "some_other_idx"))


class TestDeleteSourceCleanup:
    def test_purge_removes_table_profile_rows(self):
        from backend.data_sources import repository as ds_repository

        session = FakeJsonSession()
        seed_old_current(session)
        session.publishes.append(
            SimpleNamespace(
                source_id=SOURCE_ID,
                step_name="json",
                is_current_publish=True,
                updated_at=RUN_MARKER,
            )
        )
        ds_repository.purge_source_profiles(session, SOURCE_ID)
        assert session.table_profiles == []
        assert session.table_column_profiles == []
        assert all(not p.is_current_publish for p in session.publishes)
