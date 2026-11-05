"""doc 10.1 §4.4：步骤日志上下文从进程级全局改为 ContextVar。

并行的采集步骤任务各自持有独立 step 值，互不覆盖；任务结束时通过
token 恢复原值。线程池任务不会自动继承 ContextVar，须与
``services.logging_manager.submit_with_context`` 的上下文复制配合。
"""

import threading

import pytest

from metaweave.utils import logger as mw_logger


@pytest.fixture(autouse=True)
def _restore_step():
    token = mw_logger.set_current_step("-")
    yield
    mw_logger.reset_current_step(token)


def test_set_current_step_returns_reset_token():
    token = mw_logger.set_current_step("ddl")
    assert token is not None
    assert mw_logger.get_current_step() == "ddl"
    mw_logger.reset_current_step(token)
    assert mw_logger.get_current_step() == "-"


def test_reset_restores_previous_value():
    mw_logger.set_current_step("ddl")
    token = mw_logger.set_current_step("json")
    mw_logger.reset_current_step(token)
    assert mw_logger.get_current_step() == "ddl"


def test_default_step_is_dash():
    assert mw_logger.get_current_step() == "-"


def test_parallel_contexts_do_not_overwrite_each_other():
    """两个并发任务各自设置 step：互不可见，调度线程上下文不受污染。

    这是串行全局实现与 ContextVar 实现的行为分界：全局字符串会被
    后设置者覆盖，两个任务在屏障后都会读到同一个值。
    """
    import contextvars

    ctx_db = contextvars.copy_context()
    ctx_dim = contextvars.copy_context()
    barrier = threading.Barrier(2, timeout=5)
    observed: dict[str, list[str]] = {"db": [], "dim": []}

    def job(step: str) -> None:
        mw_logger.set_current_step(step)
        barrier.wait()  # 双方都完成设置后再读取
        observed[step].append(mw_logger.get_current_step())

    threads = [
        threading.Thread(target=ctx_db.run, args=(job, "db")),
        threading.Thread(target=ctx_dim.run, args=(job, "dim")),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert observed == {"db": ["db"], "dim": ["dim"]}
    # 调度线程（本线程）的上下文未被任务污染
    assert mw_logger.get_current_step() == "-"
