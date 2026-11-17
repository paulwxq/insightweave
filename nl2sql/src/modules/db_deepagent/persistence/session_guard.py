"""运行与清理共用的会话互斥（doc 6.3 §6.5）

- PostgreSQL 模式复用 Runner 已持有的 SessionAdvisoryLock（同键同后端），
  不新开连接重复取自己的锁；lease 校验作用域与存续状态。
- memory 模式提供进程内会话 guard（非阻塞排他）：Runner 与清理共用同一
  实例；同一会话内工具并行共享持有凭据，不各自抢锁。进程内锁不提供跨
  进程保护，memory 模式不支持多进程共享结果根并发写/清理。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

from src.modules.db_deepagent.errors import DbAgentError


class SessionGuardRequired(DbAgentError):
    """缺少有效运行持锁凭据（doc 6.3 §7.3 session_guard_required）"""

    def __init__(self, message: str = ""):
        super().__init__(
            message
            or "当前入口要求有效运行持锁凭据（SessionGuardLease）；"
            "请经由 Runner 会话锁内调用"
        )


@dataclass(frozen=True)
class SessionGuardLease:
    """运行持锁凭据（doc 6.3 §6.5：私有，不写模型参数/持久化记录）"""

    session_id: str
    backend: str  # "postgres" | "memory"
    token: object  # 后端特定的存续校验令牌（锁对象或 holder 令牌）


def _lock_is_held(lock) -> bool:
    """锁交接持有判定（R1-01）：接受 `SessionAdvisoryLock.locked` 与
    Web `SessionLockHandle.held` 两种现行持有标志。

    锁的所有权、作用域与释放职责仍归外层（Web 执行链/Runner）；本函数
    只读持有状态，不把任意对象当作可信锁——无任一已知标志即视为未持有。
    """
    for attr in ("locked", "held"):
        if getattr(lock, attr, None) is True:
            return True
    return False


def validate_lease(lease: object | None, session_id: str) -> SessionGuardLease:
    """校验 lease 作用域与存续状态；无效抛 SessionGuardRequired。"""
    if not isinstance(lease, SessionGuardLease):
        raise SessionGuardRequired()
    if lease.session_id != session_id:
        raise SessionGuardRequired(
            f"持锁凭据属于会话 {lease.session_id!r}，与当前会话 {session_id!r} 不符"
        )
    if lease.backend == "postgres":
        if not _lock_is_held(lease.token):
            raise SessionGuardRequired("会话锁已释放，凭据失效")
        return lease
    if lease.backend == "memory":
        # token 为 (签发 guard, 令牌)：校验必须在签发实例上进行（任意
        # InProcessSessionGuard 实例的凭据不被单例误判）
        if not isinstance(lease.token, tuple) or len(lease.token) != 2:
            raise SessionGuardRequired("进程内凭据格式非法")
        guard, token = lease.token
        if not isinstance(guard, InProcessSessionGuard) or not guard._holds(
            lease.session_id, token
        ):
            raise SessionGuardRequired("进程内会话 guard 已释放，凭据失效")
        return lease
    raise SessionGuardRequired(f"未知 guard 后端: {lease.backend!r}")


def require_lease(lease: object | None, session_id: str) -> SessionGuardLease:
    """validate_lease 的别名（服务端入口显式调用）。"""
    return validate_lease(lease, session_id)


def lease_from_advisory_lock(lock, session_id: str) -> SessionGuardLease:
    """由 Runner 已持有的会话锁构造 PG 凭据（§6.5 第 1 点）。

    接受两类现行持有者：模块内 `SessionAdvisoryLock`（`.locked`）与 Web
    执行链交接的 `SessionLockHandle`（`.held`）。工具消费同一持锁凭据，
    不用新连接重复取自己的锁；释放仍由原持有方负责。
    """
    if not _lock_is_held(lock):
        raise SessionGuardRequired("会话锁未持有，不能构造运行凭据")
    return SessionGuardLease(session_id=session_id, backend="postgres", token=lock)


# ------------------------------------------------------------ memory guard


class InProcessSessionGuard:
    """进程内会话 guard：同会话排他、不同会话独立；不阻塞已持有者的校验。

    acquire 为非阻塞：被他人持有时返回 None。release 按 lease.token 归还。
    不提供跨进程保护（memory 模式单进程使用；多进程共享结果根的并发
    清理由配置层/调用方拒绝）。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._held: dict[str, tuple[int, SessionGuardLease]] = {}
        self._next_token = 0

    def try_acquire(self, session_id: str) -> SessionGuardLease | None:
        with self._lock:
            if session_id in self._held:
                return None
            self._next_token += 1
            token = self._next_token
            lease = SessionGuardLease(
                session_id=session_id,
                backend="memory",
                token=(self, token),  # 签发实例随凭据携带（校验须在原实例上）
            )
            self._held[session_id] = (token, lease)
            return lease

    def _holds(self, session_id: str, token: int) -> bool:
        with self._lock:
            entry = self._held.get(session_id)
            return entry is not None and entry[0] == token

    def is_held(self, lease: SessionGuardLease) -> bool:
        if not isinstance(lease.token, tuple) or len(lease.token) != 2:
            return False
        guard, token = lease.token
        return guard is self and self._holds(lease.session_id, token)

    def release(self, lease: SessionGuardLease) -> None:
        if not isinstance(lease.token, tuple) or len(lease.token) != 2:
            return
        _guard, token = lease.token
        with self._lock:
            entry = self._held.get(lease.session_id)
            if entry is not None and entry[0] == token:
                del self._held[lease.session_id]


_GUARD_LOCK = threading.Lock()
_GUARD_SINGLETON: InProcessSessionGuard | None = None


def _default_memory_guard() -> InProcessSessionGuard:
    """进程内共享默认实例：Runner 与清理必须同一实例才互斥（§6.5 第 5 点）。"""
    global _GUARD_SINGLETON
    with _GUARD_LOCK:
        if _GUARD_SINGLETON is None:
            _GUARD_SINGLETON = InProcessSessionGuard()
        return _GUARD_SINGLETON


def acquire_memory_lease(session_id: str) -> SessionGuardLease | None:
    """Runner/清理共用的 memory 模式取锁入口（非阻塞）。"""
    return _default_memory_guard().try_acquire(session_id)


def release_memory_lease(lease: SessionGuardLease) -> None:
    _default_memory_guard().release(lease)
