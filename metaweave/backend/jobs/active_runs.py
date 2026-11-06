"""本进程执行中的运行注册表（doc 11 §4.2）。

生命周期三条规则：
1. 提交前注册：创建路由先生成 run_id 并注册，再创建运行行、交付后台任务；
   不变式"可能被本进程执行的运行，在运行行对他人可见（提交）前已注册"。
2. 执行结束注销：``_run_in_thread`` 包装函数的 finally 中注销（幂等）。
3. 收尾端点入口一次活性检查：命中即 409，不加任何数据库锁。

部署前提（doc 11 §4.6）：单 worker 进程，注册表只反映本进程执行状态。
"""

from __future__ import annotations

import threading
from uuid import UUID

_LOCK = threading.Lock()
_ACTIVE: set[tuple[str, UUID]] = set()


def register(kind: str, run_id: UUID) -> None:
    """登记一条本进程正在（或将要）执行的运行。"""
    with _LOCK:
        _ACTIVE.add((kind, run_id))


def unregister(kind: str, run_id: UUID) -> None:
    """注销（幂等）：执行结束或创建/交付失败时调用。"""
    with _LOCK:
        _ACTIVE.discard((kind, run_id))


def is_active(kind: str, run_id: UUID) -> bool:
    """该运行是否正由本进程执行（含已交付未开始的排队窗口）。"""
    with _LOCK:
        return (kind, run_id) in _ACTIVE
