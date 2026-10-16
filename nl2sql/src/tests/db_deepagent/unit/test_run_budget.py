"""RunBudgetLedger 单元测试（docs/v4/5 §11.1）"""

from __future__ import annotations

import time

import pytest

from src.modules.db_deepagent.services.run_budget import BudgetLimits, RunBudgetLedger

pytestmark = pytest.mark.unit

LIMITS = BudgetLimits(model_call=2, tool_call=3, db_execute=2)


@pytest.fixture()
def ledger() -> RunBudgetLedger:
    led = RunBudgetLedger()
    led.open_run("run-1", LIMITS, deadline_monotonic=time.monotonic() + 600)
    return led


class TestReserve:
    def test_within_limit(self, ledger):
        r = ledger.reserve("run-1", "db_execute")
        assert r.ok and r.remaining == 1

    def test_exhausted_sets_stop_reason(self, ledger):
        assert ledger.reserve("run-1", "db_execute").ok
        assert ledger.reserve("run-1", "db_execute").ok
        r = ledger.reserve("run-1", "db_execute")
        assert not r.ok
        assert r.stop_reason == "execute_limit"

    def test_kinds_independent(self, ledger):
        """各类型计数独立：model_call 消耗不影响 db_execute 剩余额度。"""
        assert ledger.reserve("run-1", "model_call").ok
        r = ledger.reserve("run-1", "db_execute")
        assert r.ok and r.remaining == 1  # 仍按 db_execute 自身额度计

    def test_stop_reason_blocks_all(self, ledger):
        """首次失败预占记 stop_reason 后，其他类型也失败。"""
        for _ in range(2):
            ledger.reserve("run-1", "db_execute")  # 用满额度，尚未记 stop
        failed = ledger.reserve("run-1", "db_execute")
        assert failed.stop_reason == "execute_limit"
        r = ledger.reserve("run-1", "model_call")
        assert not r.ok and r.stop_reason == "execute_limit"

    def test_unregistered_run_passes(self):
        """未注册运行不计数（测试直调场景）。"""
        led = RunBudgetLedger()
        assert led.reserve("ghost", "model_call").ok


class TestDeadline:
    def test_deadline_records_run_timeout(self):
        led = RunBudgetLedger()
        led.open_run(
            "run-d", BudgetLimits(1, 1, 1), deadline_monotonic=time.monotonic() - 1
        )
        r = led.reserve("run-d", "model_call")
        assert not r.ok and r.stop_reason == "run_timeout"

    def test_remaining_seconds_positive(self, ledger):
        assert 0 < ledger.remaining_seconds("run-1") <= 600


class TestLifecycle:
    def test_open_run_idempotent(self, ledger):
        """重复 open 保持原条目（不重置计数）。"""
        ledger.reserve("run-1", "model_call")
        ledger.open_run(
            "run-1", BudgetLimits(99, 99, 99), deadline_monotonic=time.monotonic() + 600
        )
        assert ledger.counters("run-1")["model_call"] == 1

    def test_close_run_returns_snapshot(self, ledger):
        ledger.reserve("run-1", "model_call")
        ledger.reserve("run-1", "tool_call")
        snap = ledger.close_run("run-1")
        assert snap.counters == {"model_call": 1, "tool_call": 1, "db_execute": 0}
        assert ledger.counters("run-1") == {}  # 条目已删除

    def test_stop_records_first_reason_only(self, ledger):
        ledger.stop("run-1", "model_call_limit")
        ledger.stop("run-1", "run_timeout")
        assert ledger.stop_reason("run-1") == "model_call_limit"
