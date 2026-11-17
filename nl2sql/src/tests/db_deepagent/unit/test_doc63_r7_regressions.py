"""doc 6.3 R7 / M2 增量防御回归（codex_doc63_impl_review_r6.md §5 M2）

R6 PASS 后的局部防御性改进：孤儿快照删除排序无法覆盖整个 deletion_set 时
明确失败，而不是返回按名称拼接的部分顺序；调用方 _cleanup_session_resources
窄捕获该专用异常，受控记录原因并跳过本会话本轮全部孤儿运行快照实删。

测试全部复用 test_doc63_r5_regressions.py 的真实维护夹具（QaArchive +
InMemoryStore + 禁实删钩子 + memory lease），不连真实 PG / 模型。

说明：成环 / 排序失败的图是**人工构造**的防御性输入，用于验证排序函数与
调用方边界；当前正常工具链在 _protected_closure 阶段 1 已对成环保守停止，
不会把这种图送到排序函数。不把它表述为正常业务可产生的真实场景。

既有 R5-01 正式回归（两种名称排序、消费者删除失败、共享来源、多跳、
恢复后回收、锁内复核、dry-run）保留在 test_doc63_r5_regressions.py，本文件
不重复，仅在维护专项与后端全量中复跑。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

from src.modules.db_deepagent.persistence.maintenance import (
    MaintenanceService,
    OrphanDeletionOrderError,
)
from src.tests.db_deepagent.unit.test_doc63_r5_regressions import (
    THREAD,
    _build_chain,
    _cleanup,
    _FaultStore,
    _has,
    _remaining,
)

pytestmark = pytest.mark.unit


def _snap_key(run: str) -> str:
    return json.dumps([THREAD, run], separators=(",", ":"))


class TestM2OrderCoverage:
    """M2：排序必须覆盖整个删除集，否则明确失败。"""

    def test_normal_chain_and_shared_source_full_order(self):
        """正常链 + 共享来源仍产出消费者先于来源的完整顺序（纯排序函数）。

        集成层的正常链顺序 / 共享来源行为已由 R5-01 回归覆盖，本例只补充
        排序函数对「共享来源须等全部 consumer」的精确顺序断言。
        """
        a = _snap_key("a-ancestor")
        b = _snap_key("b-middle")
        shared = _snap_key("c-shared")
        c1 = _snap_key("z-consumer1")
        c2 = _snap_key("z-consumer2")
        # 链 c1 -> b -> a；共享来源 shared 同时被 c1、c2 引用；无环。
        deps = {
            b: {a},
            c1: {b, shared},
            c2: {shared},
        }
        deletion_set = {a, b, shared, c1, c2}
        order = MaintenanceService._order_orphan_deletions(deletion_set, deps)
        assert sorted(order) == sorted(deletion_set), order
        pos = {key: i for i, key in enumerate(order)}
        assert pos[c1] < pos[b] < pos[a], order
        # 共享来源必须排在所有引用它的 consumer 之后
        assert pos[shared] > max(pos[c1], pos[c2]), order

    def test_cyclic_deps_raise_instead_of_partial_order(self):
        """无法完整排序时抛 OrphanDeletionOrderError，不返回按名称拼接的剩余节点。

        人工构造的环仅用于验证排序函数的明确失败语义；正常工具链在阶段 1
        已对成环保守停止，不会把环送到这里。
        """
        x, y = _snap_key("x"), _snap_key("y")
        deps = {x: {y}, y: {x}}  # 环：没有任何节点可进入 ready
        with pytest.raises(OrphanDeletionOrderError) as exc:
            MaintenanceService._order_orphan_deletions({x, y}, deps)
        message = str(exc.value)
        assert "未满足来源依赖" in message
        # 明确说明覆盖不全并把未解决节点列入原因，而非静默拼接返回
        assert "0/2" in message
        assert x in message and y in message

    def test_partial_dependency_gap_also_refused(self):
        """删除集内存在无法解开的依赖缺口时同样明确失败（非拼接兜底）。"""
        free = _snap_key("free")
        x, y = _snap_key("x"), _snap_key("y")
        deps = {x: {y}, y: {x}}  # x/y 互相依赖，free 独立
        # 旧实现会把 x/y 按名称追加到 [free] 之后返回；新实现必须拒绝整批。
        with pytest.raises(OrphanDeletionOrderError):
            MaintenanceService._order_orphan_deletions({free, x, y}, deps)


class TestM2CleanupBoundary:
    """M2：实际 _cleanup_session_resources 对排序失败的受控处理。"""

    def test_order_failure_reports_and_skips_all_deletes(self):
        """局部异常注入：排序失败 → 受控报告、零孤儿快照 delete、不穿透。

        用 patch 令排序函数抛 OrphanDeletionOrderError，验证调用方边界；
        这是防御性注入，不代表正常工具链能生成该异常。
        """
        now = datetime.now(UTC)
        store = _FaultStore()
        archive = _build_chain(
            store, [("z-source", 10, None), ("a-child", 8, "z-source")], now
        )
        before = sorted(_remaining(archive))

        with patch.object(
            MaintenanceService,
            "_order_orphan_deletions",
            side_effect=OrphanDeletionOrderError("injected sort failure"),
        ):
            # 不抛异常即证明维护流程受控返回，而非任异常穿透
            report = _cleanup(archive, now, apply=True)

        assert store.attempts == [], "排序失败不得执行任何孤儿运行快照 delete"
        assert sorted(_remaining(archive)) == before, "本轮快照应全部保留"
        assert _has(report, "孤儿快照删除排序失败"), report.skipped
        assert _has(report, "不使用部分顺序"), report.skipped
        assert not any("孤儿快照已清理" in x["reason"] for x in report.skipped)

    def test_order_failure_dry_run_also_no_delete(self):
        """dry-run 下排序失败同样零 delete 且受控报告。"""
        now = datetime.now(UTC)
        store = _FaultStore()
        archive = _build_chain(
            store, [("z-source", 10, None), ("a-child", 8, "z-source")], now
        )
        with patch.object(
            MaintenanceService,
            "_order_orphan_deletions",
            side_effect=OrphanDeletionOrderError("injected sort failure"),
        ):
            report = _cleanup(archive, now, apply=False)
        assert store.attempts == []
        assert _has(report, "孤儿快照删除排序失败")

    def test_recovers_next_cycle_after_transient_order_failure(self):
        """排序失败只在注入那一轮跳过；后端/顺序恢复后下一轮正常回收。"""
        now = datetime.now(UTC)
        store = _FaultStore()
        archive = _build_chain(
            store, [("z-source", 10, None), ("a-child", 8, "z-source")], now
        )
        with patch.object(
            MaintenanceService,
            "_order_orphan_deletions",
            side_effect=OrphanDeletionOrderError("injected sort failure"),
        ):
            first = _cleanup(archive, now, apply=True)
        assert store.attempts == []
        assert _has(first, "孤儿快照删除排序失败")

        # 不注入失败，下一轮按正常依赖顺序回收
        second = _cleanup(archive, now + timedelta(days=1))
        assert _remaining(archive) == [], _remaining(archive)
        assert store.attempts == ["a-child", "z-source"], store.attempts
        assert not _has(second, "缺源")
