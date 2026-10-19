"""Checkpoint 与 Store 构建（docs/v4/5 §13.2）

postgres 模式共用平台连接池；读写异常原样抛出，Runner 转换为
persistence_error，不 fail-open。memory 模式用于无平台库环境，显式选择，
不在 PG 失败时自动切换。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.store.base import BaseStore

from src.modules.db_deepagent.config import DbAgentConfig
from src.utils.logger import get_module_logger

logger = get_module_logger("nl2sql.db_deepagent.persistence.checkpoint")


@dataclass
class PersistenceHandle:
    """一次 CLI 会话的 checkpointer 与 store"""

    checkpointer: BaseCheckpointSaver
    store: BaseStore
    mode: str
    pool: Any | None = None  # postgres 模式下的平台池（close 用）

    def close(self) -> None:
        if self.pool is not None:
            try:
                self.pool.close()
            except Exception as exc:  # noqa: BLE001（关闭失败不阻断退出）
                logger.warning("平台连接池关闭失败: %s", exc)


def build_persistence(cfg: DbAgentConfig, platform_pool=None) -> PersistenceHandle:
    """按 persistence.mode 构建；postgres 需要已核对表的平台池。"""
    if cfg.persistence.mode == "memory":
        from langgraph.checkpoint.memory import InMemorySaver
        from langgraph.store.memory import InMemoryStore

        logger.warning("当前为内存模式，会话不会保存")
        return PersistenceHandle(
            checkpointer=InMemorySaver(), store=InMemoryStore(), mode="memory"
        )
    if platform_pool is None:
        raise ValueError("postgres 模式需要平台连接池")
    from langgraph.checkpoint.postgres import PostgresSaver
    from langgraph.store.postgres import PostgresStore

    return PersistenceHandle(
        checkpointer=PostgresSaver(platform_pool),
        store=PostgresStore(platform_pool),
        mode="postgres",
        pool=platform_pool,
    )


def make_thread_id(session_id: str, seq: int) -> str:
    """线程标识：dbda:<session_id>:<序号>（§13.2）。"""
    return f"dbda:{session_id}:{seq}"


_SESSION_ID_RE = None  # 延迟编译，避免模块导入期 re 依赖


def parse_thread_id(thread_id: str) -> tuple[str, int] | None:
    """解析线程标识 dbda:<session_id>:<seq> → (session_id, seq)；格式不符返回 None。

    只负责格式解析；调用方比较返回的 session_id 与预期会话（doc 7 §2.1）。
    """
    import re

    global _SESSION_ID_RE
    if _SESSION_ID_RE is None:
        _SESSION_ID_RE = re.compile(r"^dbda:([^:]+):([1-9][0-9]*)$")
    match = _SESSION_ID_RE.match(thread_id or "")
    if match is None:
        return None
    return match.group(1), int(match.group(2))


def valid_session_id(session_id: str) -> bool:
    """会话 ID 字符集限制 ^[A-Za-z0-9_-]{1,64}$（禁冒号，doc 7 §2.1；长度
    上限为 doc 2 §4.2 的统一 ID 契约，修复此前无长度上限的正则）。"""
    import re

    return bool(re.fullmatch(r"[A-Za-z0-9_-]{1,64}", session_id or ""))


def valid_web_id(value: object) -> bool:
    """Web 层 session_id/request_id 的同一 ID 契约（doc 2 §4.2）：
    完整匹配 ``[A-Za-z0-9_-]{1,64}``；Runner/build_db_agent/build_session_dir
    入口与 API 共用本校验，先于任何目录/锁/记录副作用。"""
    import re

    return isinstance(value, str) and bool(re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value))


class LockBackendError(RuntimeError):
    """锁后端（平台库连接/查询）异常，与“明确未取得锁”区分（doc 2 §4.2）。

    只有 PostgreSQL 明确返回未取得锁才归为忙碌；连接/查询异常抛本异常，
    由调用方映射为持久化或服务不可用错误，不误报 session_busy。
    """


class SessionAdvisoryLock:
    """会话级 advisory lock：专用连接持有，with 语句释放（doc 7 §2.3）。

    postgres 模式专用；memory 模式由调用方跳过。
    """

    def __init__(self, conninfo: str, session_id: str):
        self._conninfo = conninfo
        self._key = f"dbda_session:{session_id}"
        self._conn = None

    @property
    def locked(self) -> bool:
        return self._conn is not None

    def try_acquire(self) -> bool:
        """尝试取锁；明确未取得返回 False，后端异常抛 LockBackendError。

        连接建立后、赋给 self._conn 前若 execute 抛错，局部连接在此处关闭，
        不依赖只检查 self._conn 的旧异常路径（doc 2 §4.2 连接回收修复）。
        """
        import psycopg

        conn = None
        try:
            conn = psycopg.connect(self._conninfo, connect_timeout=5)
            row = conn.execute(
                "SELECT pg_try_advisory_lock(hashtext(%s)) AS ok", (self._key,)
            ).fetchone()
            if row is None or not row[0]:
                conn.close()
                return False
            self._conn = conn  # 取得锁后才移交生命周期管理
            return True
        except psycopg.Error as exc:
            if conn is not None:
                try:
                    conn.close()
                except Exception:  # noqa: BLE001（回收失败不覆盖原始错误）
                    pass
            if self._conn is not None:
                try:
                    self._conn.close()
                except Exception:  # noqa: BLE001
                    pass
                self._conn = None
            raise LockBackendError(f"会话锁后端异常: {type(exc).__name__}") from exc

    def release(self) -> None:
        if self._conn is None:
            return
        try:
            self._conn.execute("SELECT pg_advisory_unlock(hashtext(%s))", (self._key,))
            self._conn.commit()
        except Exception:  # noqa: BLE001（释放失败随连接关闭失效）
            pass
        finally:
            try:
                self._conn.close()
            except Exception:  # noqa: BLE001
                pass
            self._conn = None
