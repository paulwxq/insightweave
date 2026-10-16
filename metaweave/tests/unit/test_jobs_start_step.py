"""start_step 的源行锁与登记确认（doc 16 §6：与删源、发布共用同一把锁）。"""

from types import SimpleNamespace
from uuid import UUID

import pytest

from backend.jobs.repository import (
    SourceRegistrationMissingError,
    StepAlreadyRunningError,
    STATUS_RUNNING,
    start_step,
)
from backend.persistence.models import DbSourceInfo, JobInfo

SOURCE_ID = UUID("b9a4943d-44d6-4f35-b553-d25207f9d8c1")
JOB_ID = UUID("550e8400-e29b-41d4-a716-446655440000")


class FakeSession:
    def __init__(self, source):
        self.source = source
        self.rows: dict = {}
        self.added: list[JobInfo] = []
        self.commits = 0
        self.lock_sql: str | None = None
        self.commit_error: Exception | None = None

    def scalars(self, statement):
        entity = statement.column_descriptions[0]["entity"]
        assert entity is DbSourceInfo, entity
        self.lock_sql = str(statement)
        return [self.source] if self.source is not None else []

    def get(self, model, key):
        assert model is JobInfo, model
        return self.rows.get((key["job_id"], key["step_name"]))

    def add(self, row):
        self.added.append(row)

    def commit(self):
        if self.commit_error is not None:
            raise self.commit_error
        self.commits += 1


def _source():
    return SimpleNamespace(source_id=SOURCE_ID)


def test_start_step_locks_source_row_and_requires_registration():
    """锁下查不到登记：不写 running、不新增行（验收 doc 16 §7 第 9 条）。"""
    session = FakeSession(source=None)
    with pytest.raises(SourceRegistrationMissingError, match="数据源登记已不存在"):
        start_step(session, JOB_ID, SOURCE_ID, "ddl")
    assert session.lock_sql is not None and "FOR UPDATE" in session.lock_sql
    assert session.added == [] and session.commits == 0


def test_start_step_creates_running_row_under_lock():
    session = FakeSession(source=_source())
    start_step(session, JOB_ID, SOURCE_ID, "ddl")
    assert session.commits == 1
    (row,) = session.added
    assert isinstance(row, JobInfo)
    assert (row.job_id, row.source_id, row.step_name) == (JOB_ID, SOURCE_ID, "ddl")
    assert row.last_status == STATUS_RUNNING


def test_start_step_rejects_when_still_running():
    session = FakeSession(source=_source())
    session.rows[(JOB_ID, "ddl")] = SimpleNamespace(
        job_id=JOB_ID, source_id=SOURCE_ID, step_name="ddl",
        last_status="running", updated_at=None,
    )
    with pytest.raises(StepAlreadyRunningError):
        start_step(session, JOB_ID, SOURCE_ID, "ddl")
    assert session.commits == 0 and session.added == []


def test_start_step_flips_terminal_row_back_to_running():
    session = FakeSession(source=_source())
    row = SimpleNamespace(
        job_id=JOB_ID, source_id=SOURCE_ID, step_name="ddl",
        last_status="failed", updated_at=None,
    )
    session.rows[(JOB_ID, "ddl")] = row
    start_step(session, JOB_ID, SOURCE_ID, "ddl")
    assert row.last_status == STATUS_RUNNING
    assert session.commits == 1 and session.added == []


# ---- doc 21.1 批次 1：db_desc 哨兵语义（不传不触碰；传值或显式 NULL 写入） ----


def _domain_row(**overrides):
    row = SimpleNamespace(
        job_id=JOB_ID, source_id=SOURCE_ID, step_name="domain",
        last_status="failed", updated_at=None, db_desc="旧描述",
    )
    for key, value in overrides.items():
        setattr(row, key, value)
    return row


def test_start_step_without_db_desc_keeps_existing_value():
    """不传 db_desc（既有全部调用方）：UPDATE 分支不触碰该列。"""
    session = FakeSession(source=_source())
    row = _domain_row()
    session.rows[(JOB_ID, "domain")] = row

    start_step(session, JOB_ID, SOURCE_ID, "domain")

    assert row.db_desc == "旧描述"
    assert row.last_status == STATUS_RUNNING
    assert session.commits == 1


def test_start_step_writes_db_desc_on_new_row():
    session = FakeSession(source=_source())
    start_step(session, JOB_ID, SOURCE_ID, "domain", db_desc="订单库描述")
    (row,) = session.added
    assert row.db_desc == "订单库描述"


def test_start_step_writes_explicit_null_db_desc_on_new_row():
    session = FakeSession(source=_source())
    start_step(session, JOB_ID, SOURCE_ID, "domain", db_desc=None)
    (row,) = session.added
    assert row.db_desc is None


def test_start_step_overwrites_db_desc_on_existing_row():
    session = FakeSession(source=_source())
    row = _domain_row()
    session.rows[(JOB_ID, "domain")] = row

    start_step(session, JOB_ID, SOURCE_ID, "domain", db_desc="新描述")

    assert row.db_desc == "新描述"
    assert session.commits == 1


def test_start_step_overwrites_db_desc_with_explicit_null():
    """None 与"不传"必须区分：显式 None 把既有值清成 NULL。"""
    session = FakeSession(source=_source())
    row = _domain_row()
    session.rows[(JOB_ID, "domain")] = row

    start_step(session, JOB_ID, SOURCE_ID, "domain", db_desc=None)

    assert row.db_desc is None


def test_domain_pattern_commits_step_and_source_description_with_single_commit():
    """会话 2 固定顺序：先改锁下源行描述（挂起），start_step 唯一一次 commit 提交两处。"""
    source = SimpleNamespace(source_id=SOURCE_ID, description="旧源描述")
    session = FakeSession(source=source)
    row = _domain_row(db_desc=None)
    session.rows[(JOB_ID, "domain")] = row

    source.description = "新描述"
    start_step(session, JOB_ID, SOURCE_ID, "domain", db_desc="新描述")

    assert session.commits == 1
    assert source.description == "新描述"
    assert row.db_desc == "新描述"
    assert row.last_status == STATUS_RUNNING


def test_start_step_commit_failure_propagates():
    """commit 失败不吞异常：两处挂起修改由调用方会话关闭一起回滚。"""
    session = FakeSession(source=_source())
    session.commit_error = RuntimeError("平台库写入失败")

    with pytest.raises(RuntimeError, match="平台库写入失败"):
        start_step(session, JOB_ID, SOURCE_ID, "domain", db_desc="新描述")

    assert session.commits == 0
