"""发布执行计划与三通道调度单测（doc 10.2 §4）。

调度测试用 ``threading.Event`` 屏障控制 worker 推进，不依赖 sleep；
目标是验证通道容量、顺序约束、失败传播与控制面故障停发语义，
不触达数据库（步骤执行器用替身）。
"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from backend.publishing.scheduling import (
    MAX_ACTIVE_PUBLISH_RUNS,
    PUBLISH_LOCK_TIMEOUT_MS,
    RUN_CONCURRENCY,
    STATE_UNKNOWN,
    build_execution_plan,
    execute_parallel,
    max_active_publish_runs,
    publish_lock_timeout_ms,
)
from backend.publishing.target_lock import neo4j_target_lock_key


# ---- 计划构造（§4.2） ----


def test_plan_full_steps_three_channels():
    plan = build_execution_plan(
        ["db", "ddl", "json", "domain", "table_schema", "dim_value", "rel", "sql"],
    )
    assert plan["channel_limits"] == {"pg": 1, "milvus": 1, "neo4j": 1}
    assert plan["run_concurrency"] == RUN_CONCURRENCY
    # 通道内相邻顺序边，跨通道不连接
    assert plan["order_predecessors"] == {
        "db": [],
        "ddl": ["db"],
        "json": ["ddl"],
        "domain": ["json"],
        "table_schema": [],
        "dim_value": ["table_schema"],
        "sql": ["dim_value"],
        "rel": [],
    }
    assert plan["data_dependencies"] == {}


def test_plan_subset_does_not_fill_missing_steps():
    """只选 ddl/domain 与 dim_value/sql/rel：不补选 db/json/table_schema。"""
    plan = build_execution_plan(["ddl", "domain", "dim_value", "sql", "rel"])
    assert plan["selected_steps"] == ["ddl", "domain", "dim_value", "rel", "sql"]
    assert plan["order_predecessors"] == {
        "ddl": [],
        "domain": ["ddl"],
        "dim_value": [],
        "sql": ["dim_value"],
        "rel": [],
    }


def test_plan_rejects_unknown_step():
    with pytest.raises(ValueError):
        build_execution_plan(["db", "unknown_step"])


def test_publish_capacity_constants():
    """2026-11-06 裁定：发布并行零配置——名额与锁超时是代码常量，
    不读任何环境变量/YAML。"""
    assert max_active_publish_runs() == MAX_ACTIVE_PUBLISH_RUNS == 1
    assert publish_lock_timeout_ms() == PUBLISH_LOCK_TIMEOUT_MS == 30000


def test_neo4j_target_lock_key_stable_and_cross_side_consistent():
    """键=主机+端口+database：别名/协议/省略端口/大小写/尾点等同一实例
    的不同写法同键；协议头与回环写法不区分实例（2026-11-06 堵别名坑）。
    """
    from metaweave.core.clean_targets import normalize_neo4j_uri
    from backend.publishing.target_lock import (
        canonical_neo4j_target,
        neo4j_target_guard_key,
    )

    key1 = neo4j_target_lock_key("neo4j://localhost:7687", "neo4j")
    for alias in (
        "neo4j://localhost:7687",
        "neo4j://127.0.0.1:7687",
        "bolt://LOCALHOST:7687",       # 协议头不进身份（同一监听进程）
        "neo4j+s://localhost:7687",
        "neo4j://[::1]:7687",          # 回环 IPv6 写法
        "neo4j://0.0.0.0:7687",
        "neo4j://localhost",           # 省略默认端口 7687
        "NEO4J://localhost.",          # 大小写 + 主机尾点
    ):
        assert neo4j_target_lock_key(alias, "neo4j") == key1, alias
    # 可读保留键同一规范化口径，发布/删除两侧共用
    assert neo4j_target_guard_key("bolt://LOCALHOST", "neo4j") == (
        "neo4j|127.0.0.1:7687|neo4j"
    )
    # 不同实例/端口/库仍区分
    assert neo4j_target_lock_key("neo4j://other:7687", "neo4j") != key1
    assert neo4j_target_lock_key("neo4j://localhost:7688", "neo4j") != key1
    assert neo4j_target_lock_key("neo4j://localhost:7687", "other") != key1
    # URI/database 为空时：URI 原样透传、database 缺省 neo4j（Codex 审核
    # 2026-11-06：断具体返回值，不用自比恒真）
    assert canonical_neo4j_target("", "") == ("", "neo4j")
    assert neo4j_target_guard_key("", "") == "neo4j||neo4j"
    assert -(2**63) <= key1 < 2**63
    # 两侧契约：传入 normalize_neo4j_uri 输出与原始写法同键
    assert neo4j_target_lock_key(
        normalize_neo4j_uri("NEO4J://LocalHost:7687"), "neo4j"
    ) == key1


# ---- 协调器（§4.1） ----


class _Recorder:
    def __init__(self):
        self.lock = threading.Lock()
        self.started: list[str] = []
        self.finished: list[str] = []
        self.concurrent = 0
        self.max_concurrent = 0
        self.channel_concurrent: dict[str, int] = {}


def _make_step_fn(recorder, *, blockers=None, results=None, unknown_steps=()):
    """构造受控步骤执行器：blockers[step] 完成前该步骤不放行。"""
    blockers = blockers or {}
    results = results or {}

    def _step(step_name: str) -> dict:
        with recorder.lock:
            recorder.started.append(step_name)
            recorder.concurrent += 1
            recorder.max_concurrent = max(
                recorder.max_concurrent, recorder.concurrent
            )
        try:
            blocker = blockers.get(step_name)
            if blocker is not None:
                blocker.wait(timeout=10)
            if step_name in unknown_steps:
                return {"step": step_name, "status": STATE_UNKNOWN}
            return {"step": step_name, "status": results.get(step_name, "success")}
        finally:
            with recorder.lock:
                recorder.finished.append(step_name)
                recorder.concurrent -= 1

    return _step


def test_channels_overlap_but_serialize_within_channel():
    """db（PG）与 table_schema/rel 可重叠；PG 通道内 db→ddl 串行（§9.1）。"""
    recorder = _Recorder()
    db_done = threading.Event()
    ddl_started = threading.Event()

    # 三个通道首步骤都停在屏障上：db 未完成前 ddl 不启动，且三通道
    # 确实同时运行（并发峰值 3）。
    blockers = {"db": db_done, "table_schema": db_done, "rel": db_done}
    original = _make_step_fn(recorder, blockers=blockers)

    def _step(step_name):
        if step_name == "ddl":
            ddl_started.set()
        return original(step_name)

    steps = ["db", "ddl", "table_schema", "rel"]
    plan = build_execution_plan(steps)
    pool = ThreadPoolExecutor(max_workers=3)
    try:
        result_thread = threading.Thread(
            target=lambda: execute_parallel(steps, plan, _step, pool=pool)
        )
        result_thread.start()
        # db 未完成时：ddl 不启动（通道串行），table_schema/rel 可启动
        import time

        deadline = time.monotonic() + 5
        while recorder.lock:
            with recorder.lock:
                started = list(recorder.started)
            if {"table_schema", "rel"} <= set(started) and "db" in started:
                break
            assert time.monotonic() < deadline, f"启动超时: {started}"
        with recorder.lock:
            started = list(recorder.started)
        assert "ddl" not in started, "PG 通道容量 1：db 运行中 ddl 不得启动"
        assert set(started) == {"db", "table_schema", "rel"}
        assert recorder.max_concurrent == 3
        db_done.set()
        result_thread.join(timeout=10)
    finally:
        pool.shutdown(wait=True)
    assert not result_thread.is_alive()


def test_predecessor_failure_does_not_block_successor():
    """顺序约束不是数据依赖：db 失败仍执行 ddl（§9.3）。"""
    recorder = _Recorder()
    step_fn = _make_step_fn(recorder, results={"db": "failed"})
    steps = ["db", "ddl"]
    plan = build_execution_plan(steps)
    terminal, fault, _skipped = execute_parallel(steps, plan, step_fn)
    assert fault is False
    assert terminal == {"db": "failed", "ddl": "success"}


def test_worker_exception_collected_as_failed():
    """未捕获异常按 failed 收集，独立节点继续（§6.1）。"""

    def _step(step_name):
        if step_name == "db":
            raise RuntimeError("boom")
        return {"step": step_name, "status": "success"}

    steps = ["db", "ddl", "rel"]
    plan = build_execution_plan(steps)
    terminal, fault, _skipped = execute_parallel(steps, plan, _step)
    assert fault is False
    assert terminal == {"db": "failed", "ddl": "success", "rel": "success"}


def test_state_unknown_stops_dispatch_and_joins_in_flight():
    """状态写回未知 → 停发新任务、等待在途结束（§9.8）。"""
    recorder = _Recorder()
    rel_done = threading.Event()
    blockers = {"rel": rel_done}

    original = _make_step_fn(
        recorder, blockers=blockers, unknown_steps=("db",)
    )

    def _step(step_name):
        return original(step_name)

    # db 立即返回 unknown；rel 停在屏障上；ddl 因 unknown 不再派发
    steps = ["db", "ddl", "rel"]
    plan = build_execution_plan(steps)
    import time

    result: dict = {}

    def _run():
        result["value"] = execute_parallel(steps, plan, _step)

    thread = threading.Thread(target=_run)
    thread.start()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        with recorder.lock:
            if "rel" in recorder.started:
                break
        time.sleep(0.01)
    with recorder.lock:
        started = list(recorder.started)
    assert "ddl" not in started, "控制面故障后不得再派发 ddl"
    rel_done.set()
    thread.join(timeout=10)
    terminal, fault, skipped = result["value"]
    assert fault is True
    assert terminal["db"] == STATE_UNKNOWN
    assert terminal["rel"] == "success"
    assert "ddl" not in terminal
    assert skipped == set()


def test_run_concurrency_cap_limits_parallel_steps():
    """run_concurrency 调低为 1 时按全局固定顺序逐个执行（§4.1）。"""
    recorder = _Recorder()
    step_fn = _make_step_fn(recorder)
    steps = ["db", "table_schema", "rel"]
    plan = build_execution_plan(steps)
    plan = {**plan, "run_concurrency": 1}
    terminal, fault, _skipped = execute_parallel(steps, plan, step_fn)
    assert fault is False
    assert recorder.max_concurrent == 1
    with recorder.lock:
        started = list(recorder.started)
    assert started == ["db", "table_schema", "rel"], "按全局固定顺序选择就绪节点"


# ---- 日志 StepFilter 改用 record.step（§7.5） ----


def test_step_filter_uses_record_step_not_live_context():
    from metaweave.utils.logger import StepFilter, reset_current_step, set_current_step

    filt = StepFilter(["db"])
    token = set_current_step("ddl")
    try:
        # 记录创建于 ddl 上下文；即使过滤时上下文已切换，也按 record.step 路由
        record = logging.LogRecord(
            "backend.publishing", logging.INFO, __file__, 1, "msg", None, None
        )
        record.step = "db"
        assert filt.filter(record) is True
        record2 = logging.LogRecord(
            "backend.publishing", logging.INFO, __file__, 1, "msg", None, None
        )
        record2.step = "ddl"
        assert filt.filter(record2) is False
        # 工厂未注入 step 字段时回退实时读取（API 配置兼容）
        record3 = logging.LogRecord(
            "backend.publishing", logging.INFO, __file__, 1, "msg", None, None
        )
        assert filt.filter(record3) is False  # 当前上下文是 ddl
    finally:
        reset_current_step(token)




def test_channel_limits_respected_without_order_edges():
    """DeepSeek R1 #3 回归：移除顺序边的未来计划下，同通道多 ready 单轮
    派发也不得突破 channel_limits——db 阻塞期间（PG 通道被占），
    rel 可完成，而 ddl/json 必须仍未启动；db 放行后才逐一执行。"""
    import threading
    import time

    recorder = _Recorder()
    db_release = threading.Event()

    original = _make_step_fn(recorder, blockers={"db": db_release})

    def _step(step_name):
        return original(step_name)

    steps = ["db", "ddl", "json", "rel"]
    plan = build_execution_plan(steps)
    plan = {**plan, "order_predecessors": {step: [] for step in steps}}

    result = {}

    def _run():
        result["value"] = execute_parallel(steps, plan, _step)

    thread = threading.Thread(target=_run)
    thread.start()
    # db 阻塞在屏障上（PG 通道占用中）；等待 rel（Neo4j 通道）启动并完成
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        with recorder.lock:
            rel_done = "rel" in recorder.finished
            early_pg = [x for x in ("ddl", "json") if x in recorder.started]
        if rel_done or early_pg:
            break
        time.sleep(0.02)
    with recorder.lock:
        rel_done = "rel" in recorder.finished
        early_pg = [x for x in ("ddl", "json") if x in recorder.started]
    assert not early_pg, f"db 运行中 PG 后继被启动（通道容量突破）: {early_pg}"
    assert rel_done, "rel（跨通道）应能与 db 并行完成"
    db_release.set()
    thread.join(timeout=10)
    terminal, fault, _skipped = result["value"]
    assert fault is False
    assert terminal == {
        "db": "success", "ddl": "success", "json": "success", "rel": "success",
    }
