"""count_workspace_jobs 的真实 SQLAlchemy 回归（内存 SQLite，doc 8.1 §3.3）。

口径与作业列表一致：已逻辑删除的作业不计数；父级删除中但作业自身
未删除的仍计数。采集/发布两类共用同一实现，一并覆盖。
"""

from datetime import datetime, timezone

from sqlalchemy import create_engine, String
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

import pytest

from backend.workspaces import repository as repo

NOW = datetime(2026, 10, 29, tzinfo=timezone.utc)


class Base(DeclarativeBase):
    pass


class _CollectionJob(Base):
    __tablename__ = "collection_job_info"
    collection_job_id: Mapped[str] = mapped_column(String, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String)
    deleted_at: Mapped[datetime | None] = mapped_column(nullable=True)


class _PublishJob(Base):
    __tablename__ = "publish_job_info"
    publish_job_id: Mapped[str] = mapped_column(String, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String)
    deleted_at: Mapped[datetime | None] = mapped_column(nullable=True)


@pytest.fixture()
def real_session(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(repo, "CollectionJobInfo", _CollectionJob)
    monkeypatch.setattr(repo, "PublishJobInfo", _PublishJob)
    session = Session(engine)
    yield session
    session.close()
    engine.dispose()


def test_deleted_jobs_excluded_from_counts(real_session):
    """一个有效 + 一个已删除 → 计 1；采集与发布两类都覆盖。"""
    session = real_session
    session.add(
        _CollectionJob(collection_job_id="c1", workspace_id="ws_1", deleted_at=None)
    )
    session.add(
        _CollectionJob(collection_job_id="c2", workspace_id="ws_1", deleted_at=NOW)
    )
    session.add(_PublishJob(publish_job_id="p1", workspace_id="ws_1", deleted_at=None))
    session.add(_PublishJob(publish_job_id="p2", workspace_id="ws_1", deleted_at=NOW))
    session.commit()

    collection_counts, publish_counts = repo.count_workspace_jobs(session, ["ws_1"])
    assert collection_counts == {"ws_1": 1}
    assert publish_counts == {"ws_1": 1}


def test_workspace_without_active_jobs_absent_and_others_counted(real_session):
    """无有效作业的工作区不出现在计数（调用方补零）；多工作区各自计数。"""
    session = real_session
    session.add(
        _CollectionJob(collection_job_id="c3", workspace_id="ws_2", deleted_at=NOW)
    )
    session.add(
        _CollectionJob(collection_job_id="c4", workspace_id="ws_3", deleted_at=None)
    )
    session.commit()

    collection_counts, publish_counts = repo.count_workspace_jobs(
        session, ["ws_1", "ws_2", "ws_3"]
    )
    assert "ws_1" not in collection_counts
    assert "ws_2" not in collection_counts
    assert collection_counts.get("ws_3") == 1
    assert publish_counts == {}
