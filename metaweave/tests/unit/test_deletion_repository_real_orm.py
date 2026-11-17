"""doc 8.1 删除仓储查询的真实 SQLAlchemy 回归（内存 SQLite，不连库）。

背景：仓储查询曾用 ``session.execute(select(...))`` + tuple 解包，真实
Session 返回 Row（非 tuple），任务状态被读成 None。本文件用真实 ORM 会话
锁住这一接口边界：查询必须经 ``session.scalars`` 返回 ORM 对象。

SQLite 不支持平台的 PG 专用列类型，故用同形 stand-in 映射类替换仓储模块
内的 ResourceDeletionTask 引用（仅列名/类型形状一致，查询语义不变）。
"""

from datetime import datetime, timezone

from sqlalchemy import create_engine, String
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

import pytest

from backend.deletion import repository as repo

NOW = datetime(2026, 10, 29, tzinfo=timezone.utc)


class Base(DeclarativeBase):
    pass


class TaskRow(Base):
    __tablename__ = "resource_deletion_task"

    task_id: Mapped[str] = mapped_column(String, primary_key=True)
    action: Mapped[str] = mapped_column(String)
    target_id: Mapped[str] = mapped_column(String)
    source_id: Mapped[str] = mapped_column(String)
    workspace_id: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String)
    error_message: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column()
    queued_at: Mapped[datetime] = mapped_column()


@pytest.fixture()
def real_session(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(repo, "ResourceDeletionTask", TaskRow)
    session = Session(engine)
    yield session
    session.close()
    engine.dispose()


def _task(
    task_id,
    *,
    status,
    source_id="db_src_1",
    action="delete_source",
    created_at=NOW,
    **extra
):
    return TaskRow(
        task_id=task_id,
        action=action,
        target_id=source_id,
        source_id=source_id,
        workspace_id=extra.get("workspace_id"),
        status=status,
        error_message=extra.get("error_message"),
        created_at=created_at,
        queued_at=created_at,
    )


def test_unresolved_task_returns_orm_object_with_real_session(real_session):
    real_session.add(_task("t-running", status="running"))
    real_session.add(_task("t-success", status="success"))
    real_session.commit()

    task = repo.unresolved_task_for_source(real_session, "db_src_1")
    assert task is not None
    assert type(task) is TaskRow
    assert task.status == "running"


def test_unresolved_task_prefers_active_over_latest_failed(real_session):
    real_session.add(
        _task(
            "t-failed",
            status="failed",
            created_at=datetime(2026, 10, 28, tzinfo=timezone.utc),
        )
    )
    real_session.add(
        _task(
            "t-pending",
            status="pending",
            action="clear_source_knowledge",
            created_at=datetime(2026, 10, 27, tzinfo=timezone.utc),
        )
    )
    real_session.commit()

    task = repo.unresolved_task_for_source(real_session, "db_src_1")
    assert task.task_id == "t-pending"

    only = repo.unresolved_task_for_source(
        real_session, "db_src_1", action="delete_source"
    )
    assert only is not None and only.task_id == "t-failed"


def test_summary_reads_fields_from_orm_object(real_session):
    real_session.add(
        _task(
            "t-ws",
            status="failed",
            action="delete_workspace",
            workspace_id="ws_1",
            error_message="boom",
        )
    )
    real_session.commit()

    summary = repo.task_summary(real_session, source_id="db_src_1", workspace_id="ws_1")
    assert summary["task_id"] == "t-ws"
    assert summary["status"] == "failed"
    assert summary["error_message"] == "boom"
    assert (
        repo.task_summary(real_session, source_id="db_src_1", workspace_id="ws_2")
        is None
    )
