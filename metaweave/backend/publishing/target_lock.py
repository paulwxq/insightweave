"""Neo4j 目标互斥锁与持久目标保留记录（doc 10.2 §5.1）。

部署级、跨进程互斥：发布 rel 与删除清理共用同一锁键与保留协议。

- advisory lock：独立 psycopg 连接上的 session 级锁，锁键由目标实例
  URI + 数据库经 SHA-256 派生（跨进程稳定，不用 Python hash()）；
  竞争时带超时等待，超时抛 ``TargetLockTimeoutError``。
- 锁存活探测：每次目标 schema/数据操作前后在原专用连接上 SELECT 1；
  失锁抛 ``TargetLockLostError``，停止后续写入、不宣告成功。探测不能
  撤回已发出的请求，也不能消除探测后立即断线的窗口（§5.1）。
- 持久保留 ``publish_target_guard``：取得 advisory lock 后在短事务中
  检查/建立 owner。已有任何记录（含相同 owner_id——删除 ``retry_task()``
  以同一 task_id 重入同理）一律拒绝进入，只能人工核验后清除；正常
  完成全部在途操作且终态可靠提交后按 owner 匹配清除。
"""

from __future__ import annotations

from hashlib import sha256
from typing import Any

from sqlalchemy import delete as sa_delete
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.persistence.models import PublishTargetGuard
from backend.persistence.platform_db import PlatformSettings
from metaweave.core.loaders.cql_loader import LockLostError as _CoreLockLostError

_LOCK_NAMESPACE = "metaweave:neo4j_target:v2:"

# bolt/neo4j（含 +s/+ssc）默认端口；显式端口缺失时按此补全。
_DEFAULT_NEO4J_PORT = 7687

# 回环/通配写法归一（2026-11-06 用户裁定堵别名坑）：同一端口在本机回环
# 上只可能有一个监听服务，归一只会"过度合并"（多挡一次，安全方向），
# 不会漏判。0.0.0.0 作为连接目标在 Linux 上落到回环，同样归一。
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0"})


def canonical_neo4j_target(uri: str, database: str) -> tuple[str, str]:
    """目标实例身份规范化：``normalize_neo4j_uri`` 之上补默认端口、归一
    回环别名与主机尾点，并**剥离协议头**。

    同一主机的同一端口只可能有一个监听进程，bolt/neo4j（含 TLS 变体）
    只是同一目的地的不同传输方式，不进身份。由此 ``localhost`` /
    ``127.0.0.1`` / ``[::1]`` / 省略端口 / 大小写 / 尾点等同一实例的
    不同写法得到同一键。URI 为空时原样透传（与既有缺省口径一致）。

    刻意不做 DNS 解析：解析结果随时间漂移（轮询/故障切换），可能让
    同一实例先后得到不同键，比别名问题更危险；宁可保守合并。
    """
    from urllib.parse import urlsplit

    from metaweave.core.clean_targets import normalize_neo4j_uri

    db = (database or "").strip() or "neo4j"
    if not (uri or "").strip():
        return "", db
    parts = urlsplit(normalize_neo4j_uri(uri))
    host = (parts.hostname or "").lower().rstrip(".")
    if host in _LOOPBACK_HOSTS:
        host = "127.0.0.1"
    port = parts.port or _DEFAULT_NEO4J_PORT
    return f"{host}:{port}", db


class TargetLockLostError(_CoreLockLostError):
    """目标锁专用连接失效：停止后续目标操作，不宣告成功（§5.1）。

    继承 core 的 ``LockLostError``：loader/cleaner 的探测钩子抛出时能被
    中性基类识别并原样传播，不被当作业务失败回滚处理。
    """


class TargetLockTimeoutError(RuntimeError):
    """目标锁等待超时：受控失败，未取得锁、未产生任何目标写入。"""


def neo4j_target_lock_key(uri: str, database: str) -> int:
    """目标实例 → 稳定有符号 64 位锁键（§5.1）。

    经 ``canonical_neo4j_target`` 规范化后派生：主机+端口+database 为
    身份，协议头与别名写法不区分实例。
    """
    identity, db = canonical_neo4j_target(uri, database)
    payload = (_LOCK_NAMESPACE + f"{identity}|{db}").encode("utf-8")
    return int.from_bytes(sha256(payload).digest()[:8], "big", signed=True)


def neo4j_target_guard_key(uri: str, database: str) -> str:
    """保留记录可读键；与锁键同一规范化口径（发布/删除两侧共用）。"""
    identity, db = canonical_neo4j_target(uri, database)
    return f"neo4j|{identity}|{db}"


def connect_target_lock(settings: PlatformSettings) -> Any:
    """打开目标锁专用物理连接（psycopg3、autocommit、独立于业务池）。"""
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


def acquire_target_lock(
    settings: PlatformSettings, key: int, *, wait_timeout_s: float
) -> Any:
    """带超时等待取得 session advisory lock；成功返回专用连接。

    超时抛 ``TargetLockTimeoutError``（连接已关闭，无副作用）。
    statement_timeout 经 ``set_config`` 设置——SET 语法不接受绑定参数。
    """
    conn = connect_target_lock(settings)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT set_config('statement_timeout', %s, false)",
                (f"{max(1, int(wait_timeout_s * 1000))}ms",),
            )
            cur.execute("SELECT pg_advisory_lock(%s)", (key,))
            cur.fetchone()
    except TargetLockTimeoutError:
        raise
    except Exception as exc:
        conn.close()
        if _is_query_canceled(exc):
            raise TargetLockTimeoutError(
                f"等待 Neo4j 目标锁超时（{wait_timeout_s}s），本次未执行任何目标操作"
            ) from exc
        raise
    return conn


def _is_query_canceled(exc: BaseException) -> bool:
    orig = getattr(exc, "orig", exc)
    return getattr(orig, "sqlstate", None) in ("57014", "55P03") or (
        getattr(exc, "sqlstate", None) == "57014"
    )


def ensure_target_lock_alive(conn: Any) -> None:
    """原专用连接探测；断开抛 ``TargetLockLostError``（§5.1）。

    不重连、不再次加锁；不能撤回已发出的外部请求。
    """
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
    except Exception as exc:  # noqa: BLE001 —— 任何探测失败都视为失锁
        raise TargetLockLostError(
            f"Neo4j 目标锁连接已失效（{type(exc).__name__}），停止后续目标操作"
        ) from exc


def release_target_lock(conn: Any, key: int) -> None:
    """解锁并关闭专用连接；任何失败只记录，不向外传播。

    解锁异常不得遮蔽调用方正在传播的主异常（如 ``_StateWriteFailed``，
    Codex R2 意见 4）：session advisory lock 随连接关闭由服务端释放，
    关闭失败也只记录（连接可能已断）。
    """
    import logging

    logger = logging.getLogger("backend.publishing")
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_unlock(%s)", (key,))
    except Exception as exc:  # noqa: BLE001 —— 连接失效：锁随连接关闭释放
        logger.warning("目标锁解锁失败（连接可能已失效）：%s", exc)
    try:
        conn.close()
    except Exception as exc:  # noqa: BLE001
        logger.warning("目标锁连接关闭失败：%s", exc)


# ---- 持久目标保留记录（publish_target_guard，§5.1） ----


def reserve_target_guard(
    session: Session, target_key: str, owner_kind: str, owner_id: str
) -> dict | None:
    """短事务检查/建立保留记录；已有记录（任何 owner）返回其内容。

    返回 ``None`` 表示成功建立；调用方随后的外部写入在锁与记录双重
    保护下进行。事务边界由调用方决定（应独立短事务提交）。
    """
    existing = session.get(PublishTargetGuard, target_key)
    if existing is not None:
        return {
            "target_key": existing.target_key,
            "owner_kind": existing.owner_kind,
            "owner_id": existing.owner_id,
            "reserved_at": (
                existing.reserved_at.isoformat()
                if existing.reserved_at is not None
                else None
            ),
        }
    session.add(
        PublishTargetGuard(
            target_key=target_key, owner_kind=owner_kind, owner_id=owner_id
        )
    )
    session.flush()
    return None


def release_target_guard(
    session: Session, target_key: str, owner_kind: str, owner_id: str
) -> bool:
    """按 owner 匹配清除保留记录；不匹配（含已被人工清除/他者占用）不动。

    只在"全部在途操作结束且终态可靠提交"的正常完成路径调用；失锁或
    状态未知的执行者不得清除。返回是否实际删除。
    """
    result = session.execute(
        sa_delete(PublishTargetGuard).where(
            PublishTargetGuard.target_key == target_key,
            PublishTargetGuard.owner_kind == owner_kind,
            PublishTargetGuard.owner_id == owner_id,
        )
    )
    return bool(result.rowcount)


def read_target_guard(session: Session, target_key: str) -> dict | None:
    """只读查询保留记录（运维预览用）。"""
    row = session.execute(
        select(PublishTargetGuard).where(PublishTargetGuard.target_key == target_key)
    ).scalar_one_or_none()
    if row is None:
        return None
    return {
        "target_key": row.target_key,
        "owner_kind": row.owner_kind,
        "owner_id": row.owner_id,
        "reserved_at": (
            row.reserved_at.isoformat() if row.reserved_at is not None else None
        ),
    }
