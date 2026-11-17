"""Web 运行时基础设施（doc 2 §4.3/§6.2）：执行器、并发/频率限制、Store 接线。

- query 路由阻塞的装配、Runner 与终态保存放入专用有界 ThreadPoolExecutor，
  执行线程数与 NL2SQL_MAX_CONCURRENT_RUNS 一致（默认 2）；提交前非等待式
  取得同等数量全局名额，满额 429，不向 executor 积压无限队列。
- health/列表/历史/结果/readiness 的阻塞读取放独立受限读取线程，不共用
  长查询 executor；list/detail 触发的元数据校正在该执行器上以 session_id
  合并去重、队列有界，HTTP 请求不等待校正完成。
- Store（PostgresStore/平台池）惰性构建：health 不触发任何数据库连接。
- 会话锁经 acquire_session_lock 获取；注入 fake 可替换（单测不连 PG）。
"""

from __future__ import annotations

import os
import threading
import time
from collections import deque
from collections.abc import Callable
from contextlib import contextmanager
from typing import Any

from src.utils.logger import get_module_logger

logger = get_module_logger("nl2sql.api.v2.runtime")

DEFAULT_CORS_ORIGINS = "http://127.0.0.1:5173"
DEFAULT_ALLOWED_HOSTS = "127.0.0.1,localhost"

# 部署门闩 future 追踪钩子（doc 13 §7.5）：部署开关启用时由 src.api.main
# 注入 gate.submit_tracked（提交前登记、future 完成释放）；默认 None 保持
# 裸 executor.submit，开发/测试零额外依赖。
_future_tracker: Callable[..., Any] | None = None


def set_future_tracker(tracker: Callable[..., Any] | None) -> None:
    """注入或清除 future 追踪器（部署接线与测试共用）。"""
    global _future_tracker
    _future_tracker = tracker


DEFAULT_MAX_CONCURRENT_RUNS = 2
DEFAULT_RATE_LIMIT_PER_MINUTE = 20
_READ_EXECUTOR_THREADS = 4
_RECONCILE_QUEUE_MAX = 32


def _env_int(name: str, default: int, *, minimum: int = 1) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw.strip())
    except ValueError:
        logger.warning("%s=%r 非法，使用默认 %d", name, raw, default)
        return default
    return value if value >= minimum else default


class SessionLockHandle:
    """会话 advisory lock 的进程内包装：区分明确占用与后端异常。"""

    def __init__(self, acquire: Callable[[], bool], release: Callable[[], None]):
        self._acquire = acquire
        self._release = release
        self.held = False

    def try_acquire(self) -> bool:
        self.held = self._acquire()
        return self.held

    def release(self) -> None:
        if self.held:
            self.held = False
            self._release()


class _NullLock(SessionLockHandle):
    def __init__(self):  # 测试/内存模式：无互斥
        super().__init__(lambda: True, lambda: None)


class WebRuntime:
    """v2 Web 运行时单例（lifespan 管理；测试可注入组件）。"""

    def __init__(
        self,
        *,
        store: Any | None = None,
        readiness_service: Any | None = None,
        lock_factory: Callable[[str], SessionLockHandle] | None = None,
        query_runner: Callable[..., Any] | None = None,
        now: Callable[..., Any] | None = None,
        snapshot_store: Any | None = None,
    ):
        from src.api.v2.webstore import WebStore

        self._store_obj = store
        self._store_injected = store is not None
        self._store_lock = threading.Lock()
        self._platform_pool = None
        self._snapshot_store = snapshot_store
        self._snapshot_table_ready: bool | None = None
        self.webstore = WebStore(store, now=now) if store is not None else None
        self.readiness = readiness_service
        self._lock_factory = lock_factory
        self._query_runner = query_runner

        self.max_concurrent_runs = _env_int(
            "NL2SQL_MAX_CONCURRENT_RUNS", DEFAULT_MAX_CONCURRENT_RUNS
        )
        self.rate_limit_per_minute = _env_int(
            "NL2SQL_QUERY_RATE_LIMIT_PER_MINUTE", DEFAULT_RATE_LIMIT_PER_MINUTE
        )
        origins = os.getenv("NL2SQL_CORS_ORIGINS") or DEFAULT_CORS_ORIGINS
        self.cors_origins = [o.strip() for o in origins.split(",") if o.strip()]
        hosts = os.getenv("NL2SQL_ALLOWED_HOSTS") or DEFAULT_ALLOWED_HOSTS
        self.allowed_hosts = {h.strip().lower() for h in hosts.split(",") if h.strip()}

        from concurrent.futures import ThreadPoolExecutor

        self._run_executor = ThreadPoolExecutor(
            max_workers=self.max_concurrent_runs,
            thread_name_prefix="nl2sql-v2-run",
        )
        self._read_executor = ThreadPoolExecutor(
            max_workers=_READ_EXECUTOR_THREADS,
            thread_name_prefix="nl2sql-v2-read",
        )
        self._capacity = threading.BoundedSemaphore(self.max_concurrent_runs)
        self._rate_lock = threading.Lock()
        self._rate_window: deque[float] = deque()
        # 按需校正队列（doc 2 §4.4）：session_id 去重、有界
        self._reconcile_lock = threading.Lock()
        self._reconcile_queued: set[str] = set()
        self._reconcile_tasks: dict[str, Callable[[], None]] = {}
        self._closed = False

    # ------------------------------------------------------------ 组件接线

    def get_store(self) -> Any:
        """惰性 Store：postgres 平台池；health 不触发（doc 2 §4.2）。"""
        if self._store_obj is not None:
            return self._store_obj
        with self._store_lock:
            if self._store_obj is None:
                self._store_obj = self._build_store()
            return self._store_obj

    def _build_store(self) -> Any:
        from langgraph.store.postgres import PostgresStore

        from src.modules.db_deepagent.config import load_base_db_agent_config
        from src.modules.db_deepagent.persistence.platform_pool import (
            build_platform_pool,
            check_platform_tables,
        )

        cfg = load_base_db_agent_config()
        pool = build_platform_pool(cfg.platform_db)
        try:
            pool.open(wait=False)
            pool.wait()
            missing = check_platform_tables(pool, cfg.platform_db.schema_name)
            if missing:
                raise RuntimeError(
                    f"平台库缺表 {', '.join(missing)}；请先运行 maintenance init-platform"
                )
        except BaseException:
            pool.close()
            raise
        self._platform_pool = pool
        return PostgresStore(pool)

    def get_webstore(self) -> Any:
        if self.webstore is None:
            from src.api.v2.webstore import WebStore

            self.webstore = WebStore(self.get_store())
        return self.webstore

    def get_readiness(self) -> Any:
        if self.readiness is None:
            from src.api.v2.backend import DefaultReadinessBackend
            from src.api.v2.readiness import ReadinessService

            self.readiness = ReadinessService(DefaultReadinessBackend())
        return self.readiness

    def get_snapshot_store(self) -> Any:
        """结果快照表读取门面（惰性；复用运行时平台池）。"""
        if self._snapshot_store is None:
            from src.api.v2.snapshots import SnapshotStore

            self.get_store()  # 确保平台池已建立
            self._snapshot_store = SnapshotStore(self._platform_pool)
        return self._snapshot_store

    def snapshot_table_ready(self) -> bool:
        """查询入口门禁：快照表缺失时拒绝新查询（docs/3.1 §7.2）。

        生产路径首次调用时核对并缓存；测试注入 fake Store 时视为就绪。
        """
        if self._store_injected:
            return True
        if self._snapshot_table_ready is not None:
            return self._snapshot_table_ready
        from src.modules.db_deepagent.persistence.platform_pool import (
            snapshot_table_exists,
        )

        self.get_store()
        self._snapshot_table_ready = snapshot_table_exists(self._platform_pool)
        if not self._snapshot_table_ready:
            logger.warning(
                "快照表 %s 不存在，query 入口拒绝新查询；"
                "请先运行 maintenance init-platform",
                "db_deepagent.result_snapshots",
            )
        return self._snapshot_table_ready

    def acquire_session_lock(self, session_id: str) -> SessionLockHandle:
        if self._lock_factory is not None:
            return self._lock_factory(session_id)
        from src.modules.db_deepagent.config import load_base_db_agent_config
        from src.modules.db_deepagent.persistence.checkpoint import (
            LockBackendError,
            SessionAdvisoryLock,
        )
        from src.modules.db_deepagent.persistence.platform_pool import (
            make_platform_conninfo,
        )

        cfg = load_base_db_agent_config()
        lock = SessionAdvisoryLock(make_platform_conninfo(cfg.platform_db), session_id)

        def _acquire() -> bool:
            try:
                return lock.try_acquire()
            except LockBackendError:
                raise

        return SessionLockHandle(_acquire, lock.release)

    # ------------------------------------------------------------ 并发与限流

    def try_acquire_capacity(self) -> bool:
        """非等待式全局名额（doc 2 §4.3）：满额立即 False（429）。"""
        return self._capacity.acquire(blocking=False)

    def release_capacity(self) -> None:
        try:
            self._capacity.release()
        except ValueError:  # pragma: no cover（防御：不重复释放）
            logger.warning("全局执行名额重复释放")

    def check_rate_limit(self) -> tuple[bool, int]:
        """共享 guest 的提交频率限制（含被拒绝的重复提交）。

        返回 (allowed, retry_after_seconds)。
        """
        now = time.monotonic()
        with self._rate_lock:
            while self._rate_window and now - self._rate_window[0] > 60.0:
                self._rate_window.popleft()
            if len(self._rate_window) >= self.rate_limit_per_minute:
                retry_after = int(60.0 - (now - self._rate_window[0])) + 1
                return False, max(retry_after, 1)
            self._rate_window.append(now)
            return True, 0

    # ------------------------------------------------------------ 执行器

    @property
    def closed(self) -> bool:
        return self._closed

    def submit_run(self, fn: Callable[[], Any]) -> Any:
        """提交阻塞运行到专用 executor（调用方已持名额）。

        部署门闩启用时经 tracker 走 submit_tracked（提交前登记、
        future 完成释放；doc 13 §7.5）；未启用保持裸提交。
        """
        if self._closed:
            raise RuntimeError("服务正在关闭，不再接受新查询")
        if _future_tracker is not None:
            return _future_tracker(self._run_executor.submit, fn)
        return self._run_executor.submit(fn)

    def submit_read(self, fn: Callable[[], Any]) -> Any:
        """阻塞读取（Store/文件/知识 SDK）放独立受限读取线程。"""
        if self._closed:
            raise RuntimeError("服务正在关闭")
        if _future_tracker is not None:
            return _future_tracker(self._read_executor.submit, fn)
        return self._read_executor.submit(fn)

    # ------------------------------------------------------------ 按需校正

    def schedule_reconcile(self, session_id: str, task: Callable[[], None]) -> bool:
        """安排按会话锁的受限校正（doc 2 §4.4）：同会话合并、队列有界；
        队列满返回 False（调用方走进程内抑制）。"""
        with self._reconcile_lock:
            if session_id in self._reconcile_queued:
                return True
            if len(self._reconcile_queued) >= _RECONCILE_QUEUE_MAX:
                return False
            self._reconcile_queued.add(session_id)
            self._reconcile_tasks[session_id] = task

        def _wrapped() -> None:
            try:
                task()
            finally:
                with self._reconcile_lock:
                    self._reconcile_queued.discard(session_id)
                    self._reconcile_tasks.pop(session_id, None)

        try:
            self.submit_read(_wrapped)
        except RuntimeError:
            with self._reconcile_lock:
                self._reconcile_queued.discard(session_id)
                self._reconcile_tasks.pop(session_id, None)
            return False
        return True

    # ------------------------------------------------------------ 查询执行

    def run_query(self, *args: Any, **kwargs: Any) -> Any:
        """执行一轮问答（默认为真实装配；测试注入 fake）。"""
        if self._query_runner is not None:
            return self._query_runner(*args, **kwargs)
        from src.api.v2.runs import run_agent_query

        return run_agent_query(*args, **kwargs)

    # ------------------------------------------------------------ 关闭

    def shutdown(self) -> None:
        """排空关闭（doc 2 §5.5.1）：等待在途 Runner/收尾结束后再关资源。"""
        if self._closed:
            return
        self._closed = True
        self._run_executor.shutdown(wait=True)
        self._read_executor.shutdown(wait=True)
        if self._platform_pool is not None:
            try:
                self._platform_pool.close()
            except Exception as exc:  # noqa: BLE001
                logger.warning("平台池关闭失败: %s", exc)
        logger.info("Web 运行时已关闭（执行器排空、平台池释放）")


_RUNTIME: WebRuntime | None = None
_RUNTIME_LOCK = threading.Lock()


def get_web_runtime() -> WebRuntime:
    global _RUNTIME
    if _RUNTIME is None:
        with _RUNTIME_LOCK:
            if _RUNTIME is None:
                _RUNTIME = WebRuntime()
    return _RUNTIME


def set_web_runtime(runtime: WebRuntime | None) -> None:
    """测试注入/清理用（生产路径不调用）。"""
    global _RUNTIME
    with _RUNTIME_LOCK:
        _RUNTIME = runtime


def shutdown_web_runtime() -> None:
    global _RUNTIME
    with _RUNTIME_LOCK:
        if _RUNTIME is not None:
            _RUNTIME.shutdown()
            _RUNTIME = None


@contextmanager
def capacity_slot():
    """全局名额上下文（doc 2 §4.3）：涵盖装配至清理全程。"""
    acquired = False
    try:
        acquired = get_web_runtime().try_acquire_capacity()
        if not acquired:
            from src.api.v2 import codes

            raise codes.WebApiError(
                codes.QUERY_CAPACITY_EXCEEDED,
                "全局执行名额已满，请稍后重试",
                headers={"Retry-After": "5"},
            )
        yield
    finally:
        if acquired:
            get_web_runtime().release_capacity()
