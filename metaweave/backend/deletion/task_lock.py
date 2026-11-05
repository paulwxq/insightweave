"""任务执行锁：task_id 派生的 session advisory 锁（doc 8.1 §7.1）。

执行者、详情探测、启动扫描、重试全部共用 ``task_lock_key`` 与
专用连接约定，不自行散写 SQL。专用物理连接 autocommit、只持
session 锁，不承载业务事务，用完即关，不回普通业务池。
"""

from __future__ import annotations

from hashlib import sha256
from typing import Any
from uuid import UUID

from backend.persistence.platform_db import PlatformSettings

_LOCK_NAMESPACE = "metaweave:resource_deletion_task:v1:"


def task_lock_key(task_id: UUID | str) -> int:
    """UUID 规范化后取命名空间 SHA-256 前 8 字节的有符号 64 位键。"""
    canonical_id = str(UUID(str(task_id)))
    payload = (_LOCK_NAMESPACE + canonical_id).encode("ascii")
    return int.from_bytes(sha256(payload).digest()[:8], "big", signed=True)


def connect_task_lock(settings: PlatformSettings) -> Any:
    """打开执行锁专用物理连接（psycopg3、autocommit）。

    连到与平台库相同的库；不经过 SQLAlchemy 业务池。调用方负责
    try/finally 关闭；连接故障视为锁丢失，按既有失败/中断规则处理。
    """
    import psycopg

    kwargs: dict[str, Any] = {
        "host": settings.host,
        "port": settings.port,
        "dbname": settings.name,
        "user": settings.user,
        "password": settings.password,
        "connect_timeout": 10,
    }
    if settings.sslmode is not None:
        kwargs["sslmode"] = settings.sslmode
    conn = psycopg.connect(**kwargs)
    conn.autocommit = True
    return conn


def try_acquire(conn: Any, key: int) -> bool:
    """非阻塞取得 session 级 advisory 锁；失败返回 False，不等待。"""
    with conn.cursor() as cur:
        cur.execute("SELECT pg_try_advisory_lock(%s)", (key,))
        row = cur.fetchone()
    return bool(row and row[0])


def release(conn: Any, key: int) -> None:
    """解锁并关闭连接；解锁失败也必须关闭（doc 8.1 §7.1）。"""
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_unlock(%s)", (key,))
    finally:
        conn.close()


def close(conn: Any) -> None:
    """获取失败时直接关闭连接（§7.1：获取失败也关闭）。"""
    conn.close()


class TaskLockLostError(RuntimeError):
    """专用锁连接失效：停止后续清理，不得改写任务状态（§7.1）。

    其他执行者可能已取得锁并接管任务，旧执行者不能再把它标成失败或
    成功；任务由既有启动扫描/重试流程收尾。
    """


def ensure_lock_connection_alive(conn: Any) -> None:
    """在同一专用连接上探测（SELECT 1）；断开/失效抛 TaskLockLostError。

    不重连、不再次加锁；检查不能撤回已发出的外部删除请求，也不消除
    检查后立即断线的窗口——各清理步骤仍须幂等（§7.1）。
    """
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
    except Exception as exc:  # noqa: BLE001 —— 任何探测失败都视为失锁
        raise TaskLockLostError(
            f"任务执行锁连接已失效（{type(exc).__name__}），停止后续清理"
        ) from exc
