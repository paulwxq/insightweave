"""v2 单测公共 fixture：注入 fake 组件的 WebRuntime。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.api.v2.readiness import ReadinessService
from src.api.v2.runtime import WebRuntime, set_web_runtime, shutdown_web_runtime
from src.tests.unit.api.v2.fakes import (
    FakeBackend,
    FakeLockPool,
    FakeSnapshotStore,
    FakeStore,
)


@pytest.fixture()
def web_env():
    """fake Store + fake readiness + fake 锁池 + 可注入 query_runner 的运行时。"""
    store = FakeStore()
    backend = FakeBackend()
    readiness = ReadinessService(backend)
    locks = FakeLockPool()
    snapshots = FakeSnapshotStore()
    runner_calls: list[dict] = []

    def _default_runner(**kwargs):
        raise AssertionError("测试未注入 query_runner 却触发了真实装配")

    runtime = WebRuntime(
        store=store,
        readiness_service=readiness,
        lock_factory=locks.factory,
        query_runner=_default_runner,
        snapshot_store=snapshots,
    )
    set_web_runtime(runtime)
    env = SimpleNamespace(
        store=store,
        backend=backend,
        readiness=readiness,
        locks=locks,
        snapshots=snapshots,
        runtime=runtime,
        runner_calls=runner_calls,
    )
    yield env
    shutdown_web_runtime()


@pytest.fixture()
def web_session(web_env):
    """预置一个 active 的 Web 会话登记（含通用绑定）。"""
    from src.api.v2.runs import create_web_session

    return create_web_session(web_env.runtime, web_env.backend.source_id)


@pytest.fixture()
def client(web_env):
    """挂载真实 app 的 TestClient（组件已由 web_env 注入为 fake）。

    不进入 lifespan：startup 的 init_api_logging 会 dictConfig 全局日志
    （disable_existing_loggers 污染同进程后续用例的 caplog），且真实运行时
    组装属联调范畴。路由行为经注入的 fake runtime 覆盖。
    """
    from fastapi.testclient import TestClient

    from src.api.main import app

    return TestClient(app)
