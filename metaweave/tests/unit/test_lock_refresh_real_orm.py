"""共用锁函数的 populate_existing 刷新语义（真实 SQLAlchemy + 内存 SQLite）。

场景（doc 8.1 §5.1.1 审查收口）：Session 先普通读到 active；另一事务把对象
改为 deleting/deleted；随后本 Session 取锁——with_for_update 负责加锁、
populate_existing=True 负责刷新已加载对象属性，二者不可互替。锁等待语义
只在 PG 演练库验证，此处验证属性刷新。
"""

from sqlalchemy import create_engine, String
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column
from sqlalchemy.pool import StaticPool

from backend.deletion.locks import lock_source_row, lock_workspace_row


class Base(DeclarativeBase):
    pass


class _Source(Base):
    __tablename__ = "db_source_info"
    source_id: Mapped[str] = mapped_column(String, primary_key=True)
    deletion_state: Mapped[str] = mapped_column(String)


class _Workspace(Base):
    __tablename__ = "workspace_info"
    workspace_id: Mapped[str] = mapped_column(String, primary_key=True)
    source_id: Mapped[str] = mapped_column(String)
    deletion_state: Mapped[str] = mapped_column(String)


def _make_session(monkeypatch, model):
    """共享内存库（StaticPool 单连接）+ locks 模块内模型替换。"""
    import backend.deletion.locks as locks

    mapping = {
        lock_source_row: ("DbSourceInfo", _Source),
        lock_workspace_row: ("WorkspaceInfo", _Workspace),
    }
    monkeypatch.setattr(locks, mapping[model][0], mapping[model][1])
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return engine


def test_lock_source_row_refreshes_stale_attributes(monkeypatch):
    engine = _make_session(monkeypatch, lock_source_row)
    with Session(engine) as s:
        s.add(_Source(source_id="db_src_1", deletion_state="active"))
        s.commit()

    reader = Session(engine)
    stale = reader.get(_Source, "db_src_1")  # 普通定位查询 → active 进缓存

    writer = Session(engine)  # 另一事务置 deleting 并提交
    writer.get(_Source, "db_src_1").deletion_state = "deleting"
    writer.commit()
    writer.close()

    locked = lock_source_row(reader, "db_src_1")
    assert locked is stale  # 同一 ORM 实例
    assert locked.deletion_state == "deleting"  # 属性已刷新，不沿用旧 active
    reader.close()
    engine.dispose()


def test_lock_workspace_row_refreshes_stale_attributes(monkeypatch):
    engine = _make_session(monkeypatch, lock_workspace_row)
    with Session(engine) as s:
        s.add(
            _Workspace(
                workspace_id="ws_1", source_id="db_src_1", deletion_state="active"
            )
        )
        s.commit()

    reader = Session(engine)
    reader.get(_Workspace, "ws_1")

    writer = Session(engine)
    writer.get(_Workspace, "ws_1").deletion_state = "deleted"
    writer.commit()
    writer.close()

    locked = lock_workspace_row(reader, "ws_1")
    assert locked.deletion_state == "deleted"
    reader.close()
    engine.dispose()


def test_service_uses_shared_lock_helpers():
    """deletion.service 不再保留本地重复锁实现（审查：消除双轨）。"""
    import backend.deletion.service as service
    import inspect

    source = inspect.getsource(service)
    assert "def _lock_source(" not in source
    assert "def _lock_workspace(" not in source
    assert "lock_source_row(" in source and "lock_workspace_row(" in source
