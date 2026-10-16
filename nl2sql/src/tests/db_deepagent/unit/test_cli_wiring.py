"""CLI 装配接线单元测试（会话锁／cleanup 平台池／清理连接 autocommit）

不连真实后端：monkeypatch 各构造入口，核对 Runner 收到 persistence、
cleanup 接通平台池、清理连接以 autocommit 打开（doc 7 §2.3、§13.5）。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.tests.db_deepagent.conftest import FakeStore

pytestmark = pytest.mark.unit


# ============================================================================
# _Session → DbAgentRunner 的 persistence 接线（会话级互斥）


class _RecordingRunner:
    instances: list["_RecordingRunner"] = []

    def __init__(self, *, services, handle, archive, persistence=None, **kwargs):
        self.services = services
        self.handle = handle
        self.archive = archive
        self.persistence = persistence
        _RecordingRunner.instances.append(self)


def _install_session_stubs(monkeypatch, persistence_obj):
    import src.modules.db_deepagent.agent as agent_mod
    import src.modules.db_deepagent.cli as cli_mod
    import src.modules.db_deepagent.persistence.archive as archive_mod
    import src.modules.db_deepagent.persistence.checkpoint as checkpoint_mod
    import src.modules.db_deepagent.runner as runner_mod
    import src.modules.db_deepagent.services as services_mod

    fake_services = SimpleNamespace(config=None, close=lambda: None)
    monkeypatch.setattr(services_mod, "build_services", lambda cfg: fake_services)
    monkeypatch.setattr(
        checkpoint_mod,
        "build_persistence",
        lambda cfg, platform_pool=None: persistence_obj,
    )
    fake_handle = SimpleNamespace(agent=object())
    monkeypatch.setattr(agent_mod, "build_db_agent", lambda *a, **k: fake_handle)
    monkeypatch.setattr(archive_mod, "QaArchive", lambda store: SimpleNamespace())
    monkeypatch.setattr(runner_mod, "DbAgentRunner", _RecordingRunner)
    monkeypatch.setattr(
        cli_mod._Session, "_open_platform_pool", staticmethod(lambda cfg: object())
    )
    _RecordingRunner.instances.clear()
    return fake_services


class TestSessionRunnerWiring:
    def test_postgres_mode_passes_persistence(self, monkeypatch, db_agent_config):
        """postgres 模式：Runner 必须收到 persistence，否则会话互斥锁不生效。"""
        from src.modules.db_deepagent.cli import _Session

        persistence = SimpleNamespace(
            checkpointer=object(), store=FakeStore(), close=lambda: None
        )
        _install_session_stubs(monkeypatch, persistence)
        session = _Session(db_agent_config, session_id="s-wire")
        assert session.runner.persistence is persistence
        session.close()

    def test_memory_mode_skips_lock(self, monkeypatch, db_agent_config):
        """memory 模式无平台库：persistence 传 None，跳过互斥锁。"""
        from src.modules.db_deepagent.cli import _Session

        persistence = SimpleNamespace(
            checkpointer=object(), store=FakeStore(), close=lambda: None
        )
        _install_session_stubs(monkeypatch, persistence)
        session = _Session(
            db_agent_config, session_id="s-wire-mem", memory_persistence=True
        )
        assert session.runner.persistence is None
        session.close()

    def test_memory_cfg_mode_skips_platform_pool(self, monkeypatch, db_agent_config):
        """配置文件 persistence.mode=memory（未加命令行标志）也不连平台库。"""
        import src.modules.db_deepagent.cli as cli_mod

        from src.modules.db_deepagent.cli import _Session

        cfg = db_agent_config.model_copy(deep=True)
        cfg.persistence.mode = "memory"
        persistence = SimpleNamespace(
            checkpointer=object(), store=FakeStore(), close=lambda: None
        )
        pool_calls: list = []

        def _no_pool(c):
            pool_calls.append(c)
            raise AssertionError("memory 配置模式不得打开平台池")

        _install_session_stubs(monkeypatch, persistence)
        monkeypatch.setattr(cli_mod._Session, "_open_platform_pool", _no_pool)
        session = _Session(cfg, session_id="s-wire-cfg-mem")
        assert session.runner.persistence is None
        assert pool_calls == []
        session.close()


# ============================================================================
# maintenance cleanup 入口的平台池接通


class TestCleanupWiring:
    def _install(self, monkeypatch, cfg, *, pool, persistence_calls, cleanup_calls):
        import src.modules.db_deepagent.agent as agent_mod
        import src.modules.db_deepagent.cli as cli_mod
        import src.modules.db_deepagent.persistence.archive as archive_mod
        import src.modules.db_deepagent.persistence.checkpoint as checkpoint_mod
        import src.modules.db_deepagent.persistence.maintenance as maintenance_mod
        import src.modules.db_deepagent.services as services_mod

        fake_services = SimpleNamespace(config=cfg, close=lambda: None)
        monkeypatch.setattr(services_mod, "build_services", lambda c: fake_services)
        monkeypatch.setattr(
            cli_mod._Session, "_open_platform_pool", staticmethod(lambda c: pool)
        )

        def fake_build_persistence(c, platform_pool=None):
            persistence_calls.append(platform_pool)
            return SimpleNamespace(
                checkpointer=object(), store=FakeStore(), close=lambda: None
            )

        monkeypatch.setattr(checkpoint_mod, "build_persistence", fake_build_persistence)
        monkeypatch.setattr(
            agent_mod, "build_db_agent", lambda *a, **k: SimpleNamespace(agent=object())
        )
        monkeypatch.setattr(archive_mod, "QaArchive", lambda store: SimpleNamespace())

        class FakeMaintenance:
            def __init__(self, c, archive, agent):
                pass

            def cleanup(self, *, apply, before=None):
                cleanup_calls.append(apply)
                return SimpleNamespace(
                    dry_run=not apply,
                    deleted_threads=[],
                    kept_threads=[],
                    skipped=[],
                    errors=[],
                    result_dirs=[],
                    archives=[],
                )

        monkeypatch.setattr(maintenance_mod, "MaintenanceService", FakeMaintenance)

    def test_cleanup_uses_platform_pool(self, monkeypatch, db_agent_config):
        """postgres 模式：cleanup 经已核对表的平台池构建持久化，不再缺池报错。"""
        from src.modules.db_deepagent.cli import _run_maintenance_cleanup

        pool = SimpleNamespace(close=lambda: None)
        persistence_calls: list = []
        cleanup_calls: list = []
        self._install(
            monkeypatch,
            db_agent_config,
            pool=pool,
            persistence_calls=persistence_calls,
            cleanup_calls=cleanup_calls,
        )
        args = SimpleNamespace(dry_run=False, apply=True, before=None)
        rc = _run_maintenance_cleanup(args, db_agent_config)
        assert rc == 0
        assert persistence_calls == [pool]
        assert cleanup_calls == [True]

    def test_cleanup_memory_mode_skipped(self, monkeypatch, db_agent_config, capsys):
        """memory 模式没有平台库可清理：跳过且不触碰持久化构造。"""
        from src.modules.db_deepagent.cli import _run_maintenance_cleanup

        cfg = db_agent_config.model_copy(deep=True)
        cfg.persistence.mode = "memory"
        pool = SimpleNamespace(close=lambda: None)
        persistence_calls: list = []
        cleanup_calls: list = []
        self._install(
            monkeypatch,
            cfg,
            pool=pool,
            persistence_calls=persistence_calls,
            cleanup_calls=cleanup_calls,
        )
        args = SimpleNamespace(dry_run=False, apply=True, before=None)
        rc = _run_maintenance_cleanup(args, cfg)
        assert rc == 0
        assert persistence_calls == [] and cleanup_calls == []
        assert "跳过" in capsys.readouterr().out


# ============================================================================
# 清理连接 autocommit（逐线程事务独立提交，先于锁释放）


class TestCleanupConnectionAutocommit:
    def test_connect_uses_autocommit(self, monkeypatch, db_agent_config):
        import psycopg

        from src.modules.db_deepagent.persistence import maintenance as maintenance_mod
        from src.modules.db_deepagent.persistence.archive import QaArchive
        from src.tests.db_deepagent.conftest import FakeAgent

        captured: dict = {}

        class FakeConn:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def execute(self, sql, params=None):
                return SimpleNamespace(fetchall=lambda: [])

            def close(self):
                pass

        def fake_connect(conninfo, **kwargs):
            captured.update(kwargs)
            return FakeConn()

        monkeypatch.setattr(psycopg, "connect", fake_connect)

        class FakeLock:
            def __init__(self, conninfo, session_id):
                pass

            def try_acquire(self):
                return True

            def release(self):
                pass

        monkeypatch.setattr(maintenance_mod, "SessionAdvisoryLock", FakeLock)
        service = maintenance_mod.MaintenanceService(
            db_agent_config, QaArchive(FakeStore()), FakeAgent({"messages": []})
        )
        monkeypatch.setattr(service, "_list_sessions", lambda: [])
        report = service.cleanup(apply=False)
        assert report.ok
        assert captured.get("autocommit") is True
