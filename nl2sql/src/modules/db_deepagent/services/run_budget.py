"""RunBudgetLedger：运行级计数器（docs/v4/5 §11.1）

判定与递增在同一把锁内完成（预占），不依赖状态合并；进程内单例挂在
DbAgentServices.budget。锁为 threading.Lock：同步 invoke 下 ToolNode 用线程池
并行执行工具，同一运行的预占操作互斥。预占后即使工具失败也不退还额度，
计数表示“尝试次数”。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Literal

from src.modules.db_deepagent.config import BudgetConfig
from src.utils.logger import get_module_logger

logger = get_module_logger("nl2sql.db_deepagent.run_budget")

ReserveKind = Literal["model_call", "tool_call", "db_execute"]

_STOP_REASONS = {
    "model_call": "model_call_limit",
    "tool_call": "tool_call_limit",
    "db_execute": "execute_limit",
    "deadline": "run_timeout",
}


@dataclass(frozen=True)
class BudgetLimits:
    """三类预占的上限（来自 §4.1 budget 段）"""

    model_call: int
    tool_call: int
    db_execute: int


def limits_from_config(cfg: BudgetConfig) -> BudgetLimits:
    return BudgetLimits(
        model_call=cfg.max_model_calls_per_run,
        tool_call=cfg.max_tool_calls_per_run,
        db_execute=cfg.max_execute_calls_per_run,
    )


@dataclass(frozen=True)
class Reservation:
    """一次预占的结果：ok=False 时 stop_reason 给出原因"""

    ok: bool
    stop_reason: str | None = None
    remaining: int = 0  # ok 时该类剩余额度（含本次）


@dataclass
class BudgetSnapshot:
    """close_run 返回的计数快照（只有三个总数，不按工具名细分）"""

    counters: dict[str, int] = field(default_factory=dict)
    stop_reason: str | None = None


@dataclass
class _RunEntry:
    limits: BudgetLimits
    deadline_monotonic: float
    counters: dict[str, int] = field(
        default_factory=lambda: {
            "model_call": 0,
            "tool_call": 0,
            "db_execute": 0,
        }
    )
    stop_reason: str | None = None


class RunBudgetLedger:
    """运行级计数器：加锁预占，判定不读图状态（§8.5 计数竞争）"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._runs: dict[str, _RunEntry] = {}

    def open_run(
        self, run_id: str, limits: BudgetLimits, deadline_monotonic: float
    ) -> None:
        """注册运行；已存在时是重复 open，保持原条目（幂等）。"""
        with self._lock:
            if run_id in self._runs:
                logger.warning("预算运行 %s 已打开，忽略重复 open", run_id)
                return
            self._runs[run_id] = _RunEntry(
                limits=limits, deadline_monotonic=deadline_monotonic
            )

    def reserve(self, run_id: str, kind: ReserveKind) -> Reservation:
        """预占一次额度；超限、已过时限或运行已记 stop_reason 时失败。"""
        with self._lock:
            entry = self._runs.get(run_id)
            if entry is None:
                # 未注册的运行（Runner 未 open 或测试直接调用）不预占计数，放行
                return Reservation(ok=True)
            if entry.stop_reason is not None:
                return Reservation(ok=False, stop_reason=entry.stop_reason)
            if time.monotonic() >= entry.deadline_monotonic:
                reason = _STOP_REASONS["deadline"]
                entry.stop_reason = reason
                logger.warning("[run_id=%s] 运行时限已到，预占 %s 失败", run_id, kind)
                return Reservation(ok=False, stop_reason=reason)
            used = entry.counters[kind]
            limit = getattr(entry.limits, kind)
            if used >= limit:
                reason = _STOP_REASONS[kind]
                if entry.stop_reason is None:
                    entry.stop_reason = reason
                logger.warning(
                    "[run_id=%s] %s 达上限 %d，预占失败", run_id, kind, limit
                )
                return Reservation(ok=False, stop_reason=reason)
            entry.counters[kind] = used + 1
            return Reservation(ok=True, remaining=limit - used - 1)

    def stop(self, run_id: str, reason: str) -> None:
        """记录 stop_reason（只记第一次）。"""
        with self._lock:
            entry = self._runs.get(run_id)
            if entry is not None and entry.stop_reason is None:
                entry.stop_reason = reason

    def stop_reason(self, run_id: str) -> str | None:
        with self._lock:
            entry = self._runs.get(run_id)
            return entry.stop_reason if entry else None

    def remaining_seconds(self, run_id: str) -> float:
        """距时限的剩余秒数（未注册运行返回正无穷，由调用方自设超时）。"""
        with self._lock:
            entry = self._runs.get(run_id)
            if entry is None:
                return float("inf")
            return entry.deadline_monotonic - time.monotonic()

    def counters(self, run_id: str) -> dict[str, int]:
        """当前计数（只读副本；判定仍以 reserve 为准）。"""
        with self._lock:
            entry = self._runs.get(run_id)
            return dict(entry.counters) if entry else {}

    def close_run(self, run_id: str) -> BudgetSnapshot:
        """返回快照并删除条目（Runner 在 finally 中调用）。"""
        with self._lock:
            entry = self._runs.pop(run_id, None)
            if entry is None:
                return BudgetSnapshot()
            return BudgetSnapshot(
                counters=dict(entry.counters), stop_reason=entry.stop_reason
            )
