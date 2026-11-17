"""WebRuntime 的部署 future 追踪接线（doc 13 §7.5、任务 2A 切片 2）。

追踪器经 set_future_tracker 注入（部署开关启用时由 src.api.main 接线为
gate.submit_tracked）；未注入时保持原有裸 executor.submit 行为。
"""

from __future__ import annotations

import concurrent.futures
import threading

import pytest

from src.api.v2.runtime import WebRuntime, set_future_tracker


@pytest.fixture()
def runtime():
    rt = WebRuntime()
    yield rt
    rt.shutdown()


class TestSubmitTracking:
    def test_submit_run_routes_through_tracker(self, runtime):
        calls: list[tuple[str, object]] = []
        started = threading.Event()
        release = threading.Event()

        def tracker(submit, fn):
            calls.append(("track", fn))
            return submit(fn)

        def job():
            started.set()
            release.wait(5)
            return "done"

        set_future_tracker(tracker)
        try:
            future = runtime.submit_run(job)
            assert started.wait(5)
            assert len(calls) == 1
            release.set()
            assert future.result(timeout=5) == "done"
        finally:
            set_future_tracker(None)

    def test_submit_read_routes_through_tracker(self, runtime):
        seen: list[object] = []
        set_future_tracker(lambda submit, fn: (seen.append(fn), submit(fn))[1])
        try:
            future = runtime.submit_read(lambda: "read-done")
            assert future.result(timeout=5) == "read-done"
            assert len(seen) == 1
        finally:
            set_future_tracker(None)

    def test_without_tracker_plain_submit(self, runtime):
        set_future_tracker(None)
        future = runtime.submit_run(lambda: 7)
        assert future.result(timeout=5) == 7

    def test_closed_runtime_still_rejects_before_tracker(self, runtime):
        def tracker(submit, fn):  # 不应被调用
            raise AssertionError("关闭后不应进入追踪器")

        set_future_tracker(tracker)
        try:
            runtime.shutdown()
            with pytest.raises(RuntimeError, match="关闭"):
                runtime.submit_run(lambda: None)
        finally:
            set_future_tracker(None)

    def test_tracker_receives_real_executor_future(self, runtime):
        holder: list[concurrent.futures.Future] = []
        release = threading.Event()

        def tracker(submit, fn):
            future = submit(fn)
            holder.append(future)
            return future

        set_future_tracker(tracker)
        try:
            future = runtime.submit_read(lambda: (release.set(), "x")[1])
            assert future.result(timeout=5) == "x"
            assert holder[0] is future
        finally:
            set_future_tracker(None)
