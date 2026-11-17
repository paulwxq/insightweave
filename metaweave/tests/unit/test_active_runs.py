"""doc 11 §4.2：本进程执行者注册表（加锁集合，注册/注销/幂等/并发安全）。"""

import threading
from uuid import uuid4

from backend.jobs import active_runs


def setup_function(_fn):
    active_runs._ACTIVE.clear()


def test_register_then_active():
    run_id = uuid4()
    assert active_runs.is_active("collection", run_id) is False
    active_runs.register("collection", run_id)
    assert active_runs.is_active("collection", run_id) is True


def test_unregister_stops_activity():
    run_id = uuid4()
    active_runs.register("publish", run_id)
    active_runs.unregister("publish", run_id)
    assert active_runs.is_active("publish", run_id) is False


def test_unregister_is_idempotent():
    run_id = uuid4()
    active_runs.register("collection", run_id)
    active_runs.unregister("collection", run_id)
    active_runs.unregister("collection", run_id)  # 不抛错
    assert active_runs.is_active("collection", run_id) is False


def test_kind_namespaces_are_independent():
    run_id = uuid4()
    active_runs.register("collection", run_id)
    assert active_runs.is_active("publish", run_id) is False


def test_concurrent_register_and_unregister_leave_consistent_state():
    ids = [uuid4() for _ in range(50)]

    def churn(prefix: str) -> None:
        for run_id in ids:
            active_runs.register(prefix, run_id)
            active_runs.unregister(prefix, run_id)

    threads = [
        threading.Thread(target=churn, args=(kind,))
        for kind in ("collection", "publish")
        for _ in range(4)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert all(
        active_runs.is_active(kind, run_id) is False
        for kind in ("collection", "publish")
        for run_id in ids
    )
