"""collection_inputs_in_progress 的真实 SQLAlchemy 回归（内存 SQLite）。

锁住反向检查的 SQL 语义：仅同工作区、json/domain/rel、双方均 running
才算输入变更冲突；md/ddl 等其他步骤、终态步骤、兄弟工作区不算。
"""


from sqlalchemy import create_engine, String
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

import pytest

from backend.publishing import repository as repo


class Base(DeclarativeBase):
    pass


class _Job(Base):
    __tablename__ = "collection_job_info"
    collection_job_id: Mapped[str] = mapped_column(String, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String)


class _Run(Base):
    __tablename__ = "collection_run_info"
    collection_run_id: Mapped[str] = mapped_column(String, primary_key=True)
    collection_job_id: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String)


class _Step(Base):
    __tablename__ = "collection_run_step"
    collection_run_id: Mapped[str] = mapped_column(String, primary_key=True)
    step_name: Mapped[str] = mapped_column(String, primary_key=True)
    status: Mapped[str] = mapped_column(String)


@pytest.fixture()
def real_session(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    # helper 在函数体内从 models 模块导入，补丁打在 models 上
    import backend.persistence.models as models

    monkeypatch.setattr(models, "CollectionJobInfo", _Job)
    monkeypatch.setattr(models, "CollectionRunInfo", _Run)
    monkeypatch.setattr(models, "CollectionRunStep", _Step)
    session = Session(engine)
    yield session
    session.close()
    engine.dispose()


def _clear(session):
    session.query(_Step).delete()
    session.query(_Run).delete()
    session.query(_Job).delete()
    session.commit()


def _rows(session, *, step, run_status="running", step_status="running", ws="ws_1"):
    session.add(_Job(collection_job_id="cj_1", workspace_id=ws))
    session.add(
        _Run(
            collection_run_id="r1",
            collection_job_id="cj_1",
            status=run_status,
        )
    )
    session.add(_Step(collection_run_id="r1", step_name=step, status=step_status))
    session.commit()


def test_running_input_step_conflicts(real_session):
    for step in ("json", "domain", "rel"):
        _clear(real_session)
        _rows(real_session, step=step)
        assert repo.collection_inputs_in_progress(real_session, "ws_1") == "r1", step


def test_other_steps_and_terminal_states_do_not_conflict(real_session):
    for step in ("md", "ddl", "dim"):
        _clear(real_session)
        _rows(real_session, step=step)
        assert repo.collection_inputs_in_progress(real_session, "ws_1") is None, step
    _clear(real_session)
    _rows(real_session, step="rel", step_status="success")
    assert repo.collection_inputs_in_progress(real_session, "ws_1") is None
    _clear(real_session)
    _rows(real_session, step="rel", run_status="success")
    assert repo.collection_inputs_in_progress(real_session, "ws_1") is None


def test_sibling_workspace_not_affected(real_session):
    _rows(real_session, step="json", ws="ws_other")
    assert repo.collection_inputs_in_progress(real_session, "ws_1") is None
    assert repo.collection_inputs_in_progress(real_session, "ws_other") == "r1"
