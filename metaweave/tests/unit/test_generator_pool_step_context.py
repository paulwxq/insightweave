"""doc 10.1 §4.4：MetadataGenerator 表级线程池须传播步骤上下文。

ContextVar 不会被 ThreadPoolExecutor 自动继承；表级任务必须经
``submit_with_context`` 复制提交者上下文，script 模式的按步骤分流
才不会回归为 '-'。
"""

import threading
from types import SimpleNamespace

from metaweave.core.metadata.generator import MetadataGenerator
from metaweave.utils import logger as mw_logger


def _fake_result() -> SimpleNamespace:
    return SimpleNamespace(
        processed_tables=0, failed_tables=0, success=True, errors=[], add_error=None
    )


def _bare_generator(active_step: str) -> MetadataGenerator:
    """绕过重量级 __init__，仅装配 _process_tables_parallel 用到的属性。"""
    gen = object.__new__(MetadataGenerator)
    gen.active_step = active_step
    return gen


def test_table_pool_tasks_inherit_current_step():
    mw_logger.set_current_step("ddl")
    gen = _bare_generator("ddl")
    observed: list[str] = []
    ready = threading.Barrier(3, timeout=5)

    def fake_process_table(schema, table, object_type, result):
        ready.wait()  # 多个表任务并发在场，读取各自线程的 step
        observed.append(mw_logger.get_current_step())

    gen._process_table = fake_process_table
    result = _fake_result()
    gen._process_tables_parallel(
        [("public", "t1", "table"), ("public", "t2", "table"), ("public", "t3", "table")],
        max_workers=3,
        result=result,
    )
    assert observed == ["ddl", "ddl", "ddl"]
    assert result.failed_tables == 0
    mw_logger.set_current_step("-")


def test_table_pool_tasks_inherit_dim_step_when_changed():
    mw_logger.set_current_step("dim")
    gen = _bare_generator("json")
    observed = []

    def fake_process_table(schema, table, object_type, result):
        observed.append(mw_logger.get_current_step())

    gen._process_table = fake_process_table
    gen._process_tables_parallel(
        [("public", "t9", "table")], max_workers=1, result=_fake_result()
    )
    assert observed == ["dim"]
    mw_logger.set_current_step("-")
