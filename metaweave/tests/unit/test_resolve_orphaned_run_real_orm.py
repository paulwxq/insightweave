"""仅汇总修复的真实 ORM 回归：内存 SQLite，不连接平台库。"""

from datetime import datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy import JSON, String, Uuid, create_engine, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from backend.jobs.run_repository import RunSpec
from scripts import resolve_orphaned_run as script


class Base(DeclarativeBase):
    pass


class Source(Base):
    __tablename__ = "source"
    source_id: Mapped[str] = mapped_column(String, primary_key=True)


class Workspace(Base):
    __tablename__ = "workspace"
    workspace_id: Mapped[str] = mapped_column(String, primary_key=True)
    source_id: Mapped[str] = mapped_column(String)


class Job(Base):
    __tablename__ = "job"
    publish_job_id: Mapped[str] = mapped_column(String, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String)


class Run(Base):
    __tablename__ = "run"
    publish_run_id: Mapped[object] = mapped_column(Uuid, primary_key=True)
    publish_job_id: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String)
    message: Mapped[str | None] = mapped_column(String)
    started_at: Mapped[datetime | None] = mapped_column()
    finished_at: Mapped[datetime | None] = mapped_column()


class Step(Base):
    __tablename__ = "step"
    publish_run_id: Mapped[object] = mapped_column(Uuid, primary_key=True)
    step_name: Mapped[str] = mapped_column(String, primary_key=True)
    status: Mapped[str] = mapped_column(String)
    message: Mapped[str] = mapped_column(String)
    input_manifest: Mapped[dict] = mapped_column(JSON)
    started_at: Mapped[datetime | None] = mapped_column()
    finished_at: Mapped[datetime | None] = mapped_column()


@pytest.fixture
def orm(monkeypatch):
    from backend.persistence import models

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = Session(engine)
    run_id = uuid4()
    spec = RunSpec(Run, Step, "publish_run_id", "publish_job_id", Job)
    monkeypatch.setattr(script, "_spec_for", lambda kind: spec)
    monkeypatch.setattr(models, "DbSourceInfo", Source)
    monkeypatch.setattr(models, "WorkspaceInfo", Workspace)
    session.add_all(
        [
            Source(source_id="db_src_1"),
            Workspace(workspace_id="ws_1", source_id="db_src_1"),
            Job(publish_job_id="pub_job_1", workspace_id="ws_1"),
            Run(
                publish_run_id=run_id,
                publish_job_id="pub_job_1",
                status="pending",
                message="原记录",
            ),
            Step(
                publish_run_id=run_id,
                step_name="db",
                status="success",
                message="已发布",
                input_manifest={"files": ["original.json"]},
                finished_at=datetime(2026, 10, 29, tzinfo=timezone.utc),
            ),
        ]
    )
    session.commit()
    try:
        yield session, engine, run_id
    finally:
        session.close()
        engine.dispose()


def test_finalize_only_commits_parent_preserves_steps_and_is_idempotent(orm):
    session, engine, run_id = orm
    before = script.preview(session, "publish", run_id, finalize_only=True)
    assert before["planned_final_status"] == "success"
    result = script.apply_resolution(
        session, "publish", run_id, "维护修复", finalize_only=True
    )
    assert result["final_status"] == "success" and result["steps"] == []
    session.expire_all()
    run = session.get(Run, run_id)
    step = session.get(Step, (run_id, "db"))
    assert run.status == "success" and run.finished_at is not None
    assert run.started_at is None
    assert run.message.startswith("原记录\n仅汇总修复")
    assert step.status == "success" and step.message == "已发布"
    assert step.input_manifest == {"files": ["original.json"]}
    assert step.finished_at == datetime(2026, 10, 29)
    assert not script.apply_resolution(
        session, "publish", run_id, "重复修复", finalize_only=True
    )["changed"]


def test_stale_step_cache_is_refreshed_before_applying(orm):
    session, engine, run_id = orm
    cached_step = session.get(Step, (run_id, "db"))
    assert script.preview(session, "publish", run_id, finalize_only=True)["actionable"]
    with engine.begin() as connection:
        connection.execute(update(Step).values(status="running"))
    assert cached_step.status == "success"
    result = script.apply_resolution(
        session, "publish", run_id, "状态已变", finalize_only=True
    )
    assert result["changed"] is False and "未结束" in result["error"]
    assert cached_step.status == "running"
    assert session.get(Run, run_id).status == "pending"


def test_stale_run_cache_cannot_overwrite_new_terminal_status(orm):
    session, engine, run_id = orm
    cached_run = session.get(Run, run_id)
    with engine.begin() as connection:
        connection.execute(
            update(Run).values(status="failed", message="其他执行者已收尾")
        )
    assert cached_run.status == "pending"
    result = script.apply_resolution(
        session, "publish", run_id, "陈旧预览", finalize_only=True
    )
    assert result["changed"] is False
    assert cached_run.status == "failed" and cached_run.message == "其他执行者已收尾"


def test_failed_commit_rolls_back_parent_and_keeps_steps(orm, monkeypatch):
    session, engine, run_id = orm

    def fail():
        raise SQLAlchemyError("模拟提交故障")

    monkeypatch.setattr(session, "commit", fail)
    with pytest.raises(SQLAlchemyError):
        script.apply_resolution(
            session, "publish", run_id, "提交失败", finalize_only=True
        )
    session.rollback()
    assert session.get(Run, run_id).status == "pending"
    assert session.get(Run, run_id).message == "原记录"
    assert session.get(Step, (run_id, "db")).status == "success"


def test_repair_does_not_touch_another_run(orm):
    session, engine, run_id = orm
    other = uuid4()
    session.add_all(
        [
            Run(
                publish_run_id=other,
                publish_job_id="pub_job_1",
                status="pending",
                message="其他运行",
            ),
            Step(
                publish_run_id=other,
                step_name="db",
                status="pending",
                message="未执行",
                input_manifest={},
            ),
        ]
    )
    session.commit()
    script.apply_resolution(
        session, "publish", run_id, "仅修复指定记录", finalize_only=True
    )
    session.expire_all()
    assert session.get(Run, other).status == "pending"
    assert session.get(Run, other).message == "其他运行"
    assert session.get(Step, (other, "db")).status == "pending"
