"""domain 发布步骤（doc 21.2 §4）：预检、锁下复查、整份替换与失败语义。"""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import UUID

import pytest

from backend.persistence.models import PublishInfo, TableDomain
from backend.persistence.platform_db import PlatformSettings
from backend.profile_loading import domain_loader
from backend.publishing import publish_service
from backend.publishing.publish_service import (
    DOMAIN_EMPTY_MESSAGE,
    INPUT_SUMMARY,
    JOB_NOT_PUBLISHABLE_MESSAGE,
    NOT_READY_MESSAGE,
    OUTPUT_REASON,
    REGISTRATION_CHANGED_MESSAGE,
    WRITE_SUMMARY,
    UnsupportedPublicationStepError,
    check_publication,
    normalize_publish_steps,
    publish_steps,
)
from tests.unit.test_domain_publish_loader import (
    DB_NAME,
    JOB_ID,
    RUN_MARKER,
    SOURCE_ID,
    write_md,
    write_yaml,
)
from tests.unit.test_publish_service import OTHER_SOURCE_ID, integrity_error

SETTINGS = PlatformSettings(
    host="platform", port=5432, name="metaweave", user="admin", password="secret"
)
LATER = RUN_MARKER + timedelta(seconds=5)
OLD_PUBLISH_ID = UUID("84c017f4-b438-452b-a888-715b7f0cbea2")
OTHER_PUBLISH_ID = UUID("0f0e0d0c-0b0a-4948-8786-858483828180")


class FakeDomainSession:
    """覆盖 domain 发布链的最小假会话（对照 FakeJsonSession）。"""

    def __init__(
        self,
        *,
        mutate_registration=False,
        mutate_step=False,
        mutate_workspace=False,
        mutate_job_failed=False,
        mutate_source_gone=False,
        mutate_source_inactive=False,
        commit_errors=None,
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
                step_name="domain",
                last_status="success",
                updated_at=RUN_MARKER,
            )
        ]
        self.publishes: list = []
        self.table_domains: list = []
        self.pending: list = []
        self.begins = 0
        self.commit_errors = list(commit_errors or [])
        self.mutate_registration = mutate_registration
        self.mutate_step = mutate_step
        self.mutate_workspace = mutate_workspace
        self.mutate_job_failed = mutate_job_failed
        self.mutate_source_gone = mutate_source_gone
        self.mutate_source_inactive = mutate_source_inactive
        self._publishes_snapshot: list | None = None
        self._domains_snapshot: list | None = None
        self._clock = datetime(2026, 10, 3, tzinfo=timezone.utc)

    def tick(self) -> datetime:
        self._clock += timedelta(seconds=1)
        return self._clock

    def begin(self):
        self.begins += 1
        self.pending = []
        self._publishes_snapshot = [
            (row, row.is_current_publish) for row in self.publishes
        ]
        self._domains_snapshot = list(self.table_domains)
        if self.begins == 2:
            # 预检读取之后状态才变化：复查必须能发现
            if self.mutate_registration:
                self.source.updated_at = LATER
            if self.mutate_step:
                for step in self.steps:
                    step.updated_at = LATER
            if self.mutate_workspace:
                for step in self.steps:
                    step.source_id = OTHER_SOURCE_ID
            if self.mutate_source_gone:
                self.source = None
            if self.mutate_source_inactive:
                self.source.is_active = False
        if self.begins == 3 and self.mutate_job_failed:
            # 准入通过之后、复查会话内整作业变为不可发布
            self.steps.append(
                SimpleNamespace(
                    job_id=JOB_ID,
                    source_id=SOURCE_ID,
                    step_name="md",
                    last_status="failed",
                    updated_at=LATER,
                )
            )

    def get(self, model, key):
        if model.__name__ == "DbSourceInfo":
            if self.source is None:
                return None
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

    def add_all(self, rows):
        for row in rows:
            self.add(row)

    def flush(self):
        """no-op：本链没有跨表外键的落库顺序要求。"""

    def execute(self, statement):
        if not getattr(statement, "is_delete", False):
            raise AssertionError(type(statement).__name__)
        params = statement.compile().params
        source_id = params.get("source_id_1")
        self.table_domains[:] = [
            row for row in self.table_domains if row.source_id != source_id
        ]
        return None

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
            elif isinstance(row, TableDomain):
                if row.loaded_at is None:
                    row.loaded_at = self.tick()
                self.table_domains.append(row)
            else:
                raise AssertionError(type(row))
        self.pending.clear()

    def rollback(self):
        self.pending.clear()
        if self._publishes_snapshot is not None:
            for row, flag in self._publishes_snapshot:
                row.is_current_publish = flag
            self._publishes_snapshot = None
        if self._domains_snapshot is not None:
            self.table_domains[:] = self._domains_snapshot
            self._domains_snapshot = None


def install(monkeypatch, tmp_path, session=None):
    monkeypatch.setattr("metaweave.utils.file_utils.get_project_root", lambda: tmp_path)
    holder = session or FakeDomainSession()

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


def seed_old_state(session):
    """预置旧的 domain current、旧映射行与另一数据源的行。"""
    session.publishes.append(
        PublishInfo(
            publish_id=OLD_PUBLISH_ID,
            step_name="domain",
            source_id=SOURCE_ID,
            job_id=JOB_ID,
            status="success",
            is_current_publish=True,
            created_at=RUN_MARKER,
            updated_at=RUN_MARKER,
        )
    )
    session.table_domains.append(
        SimpleNamespace(
            source_id=SOURCE_ID,
            schema_name="public",
            table_name="legacy",
            domain_name="旧域",
            publish_id=OLD_PUBLISH_ID,
        )
    )
    session.table_domains.append(
        SimpleNamespace(
            source_id=OTHER_SOURCE_ID,
            schema_name="public",
            table_name="other",
            domain_name="别源域",
            publish_id=OTHER_PUBLISH_ID,
        )
    )


def _domain_rows(session):
    return [row for row in session.table_domains if row.source_id == SOURCE_ID]


class TestWhitelistAndPrecheck:
    def test_domain_whitelisted_and_dedup(self):
        assert normalize_publish_steps(["domain", "db", "domain"]) == ["domain", "db"]

    def test_unknown_step_message_mentions_domain(self):
        with pytest.raises(UnsupportedPublicationStepError) as excinfo:
            normalize_publish_steps(["md"])
        assert (
            str(excinfo.value)
            == "当前仅支持 db、ddl、json、domain、table_schema、dim_value、rel、sql 步骤"
        )

    def test_registries_follow_doc(self):
        assert (
            publish_service._INSPECTORS["domain"]
            is domain_loader.inspect_domain_context
        )
        # 与 json 相同：不进 _PUBLISHERS，只走专用分支
        assert "domain" not in publish_service._PUBLISHERS
        assert publish_service.DOMAIN_STEP == domain_loader.DOMAIN_STEP

    def test_precheck_includes_domain_row(self, monkeypatch, tmp_path):
        install(monkeypatch, tmp_path)
        write_yaml(tmp_path)
        write_md(tmp_path)
        result = check_publication(SETTINGS, SOURCE_ID, JOB_ID)
        domain_row = next(s for s in result["steps"] if s["step_name"] == "domain")
        assert domain_row["output_ready"] is True
        assert domain_row["publishable"] is True
        # 产物检查走 domain 分支而非 json 的 else：json 目录为空也不影响
        assert [step["step_name"] for step in result["steps"]] == [
            "db",
            "ddl",
            "json",
            "domain",
            "rel",
            "table_schema",
            "dim_value",
            "sql",
        ]

    def test_precheck_domain_not_ready_without_output(self, monkeypatch, tmp_path):
        install(monkeypatch, tmp_path)
        domain_dir = tmp_path / "output" / str(SOURCE_ID) / str(JOB_ID) / "domain"
        domain_dir.mkdir(parents=True)
        result = check_publication(SETTINGS, SOURCE_ID, JOB_ID)
        domain_row = next(s for s in result["steps"] if s["step_name"] == "domain")
        assert domain_row["output_ready"] is False
        assert domain_row["publishable"] is False
        assert domain_row["reason"] == OUTPUT_REASON


class TestPublishDomain:
    def test_success_replaces_rows_and_current(self, monkeypatch, tmp_path):
        session = install(monkeypatch, tmp_path)
        seed_old_state(session)
        write_yaml(tmp_path)
        write_md(tmp_path)

        result = publish_steps(SETTINGS, SOURCE_ID, JOB_ID, ["domain"])

        assert result["results"][0]["status"] == "success"
        publish_id = UUID(result["publish_id"])
        rows = _domain_rows(session)
        assert {(row.table_name, row.domain_name) for row in rows} == {
            ("users", "用户管理"),
            ("products", "用户管理"),
            ("users", "订单交易"),
        }
        assert all(row.publish_id == publish_id for row in rows)
        assert all(row.job_id == JOB_ID and row.db_name == DB_NAME for row in rows)
        assert all(row.loaded_at is not None for row in rows)
        # 旧 current 翻牌、新 current 成功
        old = next(p for p in session.publishes if p.publish_id == OLD_PUBLISH_ID)
        assert old.is_current_publish is False
        new = next(p for p in session.publishes if p.publish_id == publish_id)
        assert new.is_current_publish is True and new.status == "success"
        # 其他数据源的行不动
        assert [
            row for row in session.table_domains if row.source_id == OTHER_SOURCE_ID
        ] == [row for row in session.table_domains if row.source_id == OTHER_SOURCE_ID]

    def test_zero_rows_fails_with_mapping_message(self, monkeypatch, tmp_path):
        session = install(monkeypatch, tmp_path)
        seed_old_state(session)
        payload = {
            "database": {"name": DB_NAME},
            "domains": [
                {
                    "name": "_未分类_",
                    "description": "无法归入其他业务主题的表",
                    "tables": [],
                }
            ],
        }
        write_yaml(tmp_path, payload)
        write_md(tmp_path)

        result = publish_steps(SETTINGS, SOURCE_ID, JOB_ID, ["domain"])

        step = result["results"][0]
        assert step["status"] == "failed"
        assert step["message"] == DOMAIN_EMPTY_MESSAGE
        assert [row.table_name for row in _domain_rows(session)] == ["legacy"]
        current = [p for p in session.publishes if p.step_name == "domain"]
        assert [p.publish_id for p in current if p.is_current_publish] == [
            OLD_PUBLISH_ID
        ]
        failed = [p for p in current if p.status == "failed"]
        assert len(failed) == 1 and failed[0].is_current_publish is False

    def test_parse_failure_uses_input_summary(self, monkeypatch, tmp_path):
        session = install(monkeypatch, tmp_path)
        seed_old_state(session)
        write_yaml(tmp_path, text="domains: [\n")
        write_md(tmp_path)

        result = publish_steps(SETTINGS, SOURCE_ID, JOB_ID, ["domain"])

        step = result["results"][0]
        assert step["status"] == "failed"
        assert step["message"] == INPUT_SUMMARY
        assert [row.table_name for row in _domain_rows(session)] == ["legacy"]

    def test_marker_changed_reports_not_ready(self, monkeypatch, tmp_path):
        session = install(monkeypatch, tmp_path, FakeDomainSession(mutate_step=True))
        seed_old_state(session)
        write_yaml(tmp_path)
        write_md(tmp_path)

        result = publish_steps(SETTINGS, SOURCE_ID, JOB_ID, ["domain"])

        step = result["results"][0]
        assert step["status"] == "failed"
        assert step["message"] == NOT_READY_MESSAGE
        assert [row.table_name for row in _domain_rows(session)] == ["legacy"]
        assert not any(
            p.publish_id != OLD_PUBLISH_ID and p.is_current_publish
            for p in session.publishes
        )

    def test_registration_changed_reports_controlled_failure(
        self, monkeypatch, tmp_path
    ):
        session = install(
            monkeypatch, tmp_path, FakeDomainSession(mutate_registration=True)
        )
        seed_old_state(session)
        write_yaml(tmp_path)
        write_md(tmp_path)

        result = publish_steps(SETTINGS, SOURCE_ID, JOB_ID, ["domain"])

        step = result["results"][0]
        assert step["status"] == "failed"
        assert step["message"] == REGISTRATION_CHANGED_MESSAGE
        assert [row.table_name for row in _domain_rows(session)] == ["legacy"]

    def test_job_not_publishable_under_lock(self, monkeypatch, tmp_path):
        session = install(
            monkeypatch, tmp_path, FakeDomainSession(mutate_job_failed=True)
        )
        seed_old_state(session)
        write_yaml(tmp_path)
        write_md(tmp_path)

        result = publish_steps(SETTINGS, SOURCE_ID, JOB_ID, ["domain"])

        step = result["results"][0]
        assert step["status"] == "failed"
        assert step["message"] == JOB_NOT_PUBLISHABLE_MESSAGE
        assert [row.table_name for row in _domain_rows(session)] == ["legacy"]

    def test_workspace_mismatch_under_lock_is_step_failure(self, monkeypatch, tmp_path):
        session = install(
            monkeypatch, tmp_path, FakeDomainSession(mutate_workspace=True)
        )
        seed_old_state(session)
        write_yaml(tmp_path)
        write_md(tmp_path)

        result = publish_steps(SETTINGS, SOURCE_ID, JOB_ID, ["domain"])

        step = result["results"][0]
        assert step["status"] == "failed"
        assert step["message"] == INPUT_SUMMARY
        assert [row.table_name for row in _domain_rows(session)] == ["legacy"]

    def test_source_gone_under_lock_propagates(self, monkeypatch, tmp_path):
        from backend.profile_loading.db_loader import MetaLoadSourceNotFoundError

        session = install(
            monkeypatch, tmp_path, FakeDomainSession(mutate_source_gone=True)
        )
        seed_old_state(session)
        write_yaml(tmp_path)
        write_md(tmp_path)

        with pytest.raises(MetaLoadSourceNotFoundError):
            publish_steps(SETTINGS, SOURCE_ID, JOB_ID, ["domain"])
        assert [row.table_name for row in _domain_rows(session)] == ["legacy"]
        assert len(session.publishes) == 1  # 不写失败记录

    def test_source_inactive_under_lock_propagates(self, monkeypatch, tmp_path):
        from backend.profile_loading.db_loader import MetaLoadSourceDisabledError

        session = install(
            monkeypatch, tmp_path, FakeDomainSession(mutate_source_inactive=True)
        )
        seed_old_state(session)
        write_yaml(tmp_path)
        write_md(tmp_path)

        with pytest.raises(MetaLoadSourceDisabledError):
            publish_steps(SETTINGS, SOURCE_ID, JOB_ID, ["domain"])
        assert [row.table_name for row in _domain_rows(session)] == ["legacy"]

    def test_write_failure_reports_write_summary_and_keeps_old(
        self, monkeypatch, tmp_path
    ):
        session = install(
            monkeypatch,
            tmp_path,
            FakeDomainSession(
                commit_errors=[integrity_error("23505", "table_domain_pkey")]
            ),
        )
        seed_old_state(session)
        write_yaml(tmp_path)
        write_md(tmp_path)

        result = publish_steps(SETTINGS, SOURCE_ID, JOB_ID, ["domain"])

        step = result["results"][0]
        assert step["status"] == "failed"
        assert step["message"] == WRITE_SUMMARY
        assert [row.table_name for row in _domain_rows(session)] == ["legacy"]
        old = next(p for p in session.publishes if p.publish_id == OLD_PUBLISH_ID)
        assert old.is_current_publish is True

    def test_directory_resolution_oserror_reports_input_summary(
        self, monkeypatch, tmp_path
    ):
        """目录访问抛 OSError：按解析失败记 INPUT_SUMMARY 失败行，不成为 500（doc 21.2 §4）。"""
        session = install(monkeypatch, tmp_path)
        seed_old_state(session)
        write_yaml(tmp_path)
        write_md(tmp_path)

        def boom(source_id, job_id):
            raise PermissionError("denied")

        monkeypatch.setattr(domain_loader, "resolve_domain_output_dir", boom)

        result = publish_steps(SETTINGS, SOURCE_ID, JOB_ID, ["domain"])

        step = result["results"][0]
        assert step["status"] == "failed"
        assert step["message"] == INPUT_SUMMARY
        assert [row.table_name for row in _domain_rows(session)] == ["legacy"]
        failed = [p for p in session.publishes if p.status == "failed"]
        assert len(failed) == 1 and failed[0].is_current_publish is False
