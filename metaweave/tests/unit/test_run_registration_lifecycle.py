"""doc 11 §4.2 规则 1/2：提交前注册、创建/交付失败注销、执行结束注销。

不变式：可能被本进程执行的运行，在其运行行对他人可见（创建提交）前已注册；
交付失败后的注销是设计允许的例外（该运行永不会执行，无需拦截）。
"""

import asyncio
from uuid import uuid4

import pytest

from backend.api.routers import workspaces
from backend.api.schemas import RunCreateRequest
from backend.jobs import active_runs
from backend.persistence.platform_db import PlatformSettings

SETTINGS = PlatformSettings(
    host="platform", port=5432, name="metaweave", user="admin", password="secret"
)


class FakeBackground:
    def __init__(self, *, fail: bool = False):
        self.tasks: list[tuple] = []
        self.fail = fail

    def add_task(self, func, *args):
        if self.fail:
            raise RuntimeError("交付失败")
        self.tasks.append((func, args))


@pytest.fixture(autouse=True)
def _clean_registry():
    active_runs._ACTIVE.clear()
    yield
    active_runs._ACTIVE.clear()


@pytest.fixture
def fixed_run_id(monkeypatch):
    run_id = uuid4()
    monkeypatch.setattr(workspaces, "new_run_id", lambda: run_id)
    return run_id


def test_collection_registers_before_create_and_dispatches(fixed_run_id, monkeypatch):
    seen_active_at_create = []

    def fake_create(settings, job_id, *, run_id, revision_no, mode, steps, step_config):
        seen_active_at_create.append(active_runs.is_active("collection", run_id))
        return {
            "collection_run_id": str(run_id),
            "collection_job_id": job_id,
            "revision_no": revision_no,
            "steps": ["db", "ddl"],
            "status": "pending",
        }

    def fake_execute(settings, run_id, *args):
        return None

    monkeypatch.setattr(workspaces, "create_collection_run", fake_create)
    monkeypatch.setattr(workspaces, "execute_collection_run", fake_execute)
    background = FakeBackground()
    payload = RunCreateRequest(revision_no=1)

    result = asyncio.run(
        workspaces._create_and_dispatch_run(
            "collection",
            workspaces.create_collection_run,
            workspaces.execute_collection_run,
            SETTINGS,
            "cj_1",
            payload,
            background,
        )
    )

    assert result["collection_run_id"] == str(fixed_run_id)
    # 不变式：创建函数执行时（运行行尚未提交）已注册
    assert seen_active_at_create == [True]
    # 交付参数：包装函数 + kind + settings + run_id + steps
    func, args = background.tasks[0]
    assert func is workspaces._run_in_thread
    assert args[0] is fake_execute
    assert args[1] == "collection"
    assert args[3] == fixed_run_id
    assert args[4] == ["db", "ddl"]
    # 交付后仍在册（等待执行器 finally 注销）
    assert active_runs.is_active("collection", fixed_run_id) is True


def test_publish_dispatch_omits_steps_argument(fixed_run_id, monkeypatch):
    def fake_create(settings, job_id, *, run_id, revision_no, mode, steps, step_config):
        return {
            "publish_run_id": str(run_id),
            "publish_job_id": job_id,
            "revision_no": revision_no,
            "steps": ["db"],
            "status": "pending",
        }

    monkeypatch.setattr(workspaces, "create_publish_run", fake_create)
    monkeypatch.setattr(workspaces, "execute_publish_run", lambda *a: None)
    background = FakeBackground()

    asyncio.run(
        workspaces._create_and_dispatch_run(
            "publish",
            workspaces.create_publish_run,
            workspaces.execute_publish_run,
            SETTINGS,
            "pj_1",
            RunCreateRequest(revision_no=2),
            background,
        )
    )

    _func, args = background.tasks[0]
    assert args[1] == "publish"
    assert args[3] == fixed_run_id
    assert len(args) == 4  # 无 steps 尾参


def test_create_failure_unregisters(fixed_run_id, monkeypatch):
    def failing_create(*_a, **_kw):
        raise ValueError("创建失败")

    monkeypatch.setattr(workspaces, "create_collection_run", failing_create)
    monkeypatch.setattr(workspaces, "execute_collection_run", lambda *a: None)

    with pytest.raises(ValueError):
        asyncio.run(
            workspaces._create_and_dispatch_run(
                "collection",
                workspaces.create_collection_run,
                workspaces.execute_collection_run,
                SETTINGS,
                "cj_1",
                RunCreateRequest(revision_no=1),
                FakeBackground(),
            )
        )
    assert active_runs.is_active("collection", fixed_run_id) is False


def test_delivery_failure_unregisters(fixed_run_id, monkeypatch):
    monkeypatch.setattr(
        workspaces,
        "create_collection_run",
        lambda *a, **kw: {
            "collection_run_id": str(kw["run_id"]),
            "steps": ["db"],
            "status": "pending",
        },
    )
    monkeypatch.setattr(workspaces, "execute_collection_run", lambda *a: None)

    with pytest.raises(RuntimeError):
        asyncio.run(
            workspaces._create_and_dispatch_run(
                "collection",
                workspaces.create_collection_run,
                workspaces.execute_collection_run,
                SETTINGS,
                "cj_1",
                RunCreateRequest(revision_no=1),
                FakeBackground(fail=True),
            )
        )
    assert active_runs.is_active("collection", fixed_run_id) is False


def test_run_in_thread_unregisters_on_success(fixed_run_id):
    active_runs.register("collection", fixed_run_id)
    seen = []

    def fake_execute(settings, run_id, *args):
        seen.append(active_runs.is_active("collection", run_id))

    workspaces._run_in_thread(fake_execute, "collection", SETTINGS, fixed_run_id, ["db"])
    assert seen == [True]
    assert active_runs.is_active("collection", fixed_run_id) is False


def test_run_in_thread_unregisters_on_exception(fixed_run_id):
    active_runs.register("publish", fixed_run_id)

    def failing_execute(settings, run_id, *args):
        raise RuntimeError("执行器异常退出")

    with pytest.raises(RuntimeError):
        workspaces._run_in_thread(
            failing_execute, "publish", SETTINGS, fixed_run_id
        )
    assert active_runs.is_active("publish", fixed_run_id) is False
