"""doc 6.3 R5-01 审核反例回归（codex_doc63_impl_review_r5.md）

R5-01：孤儿快照删除集合已考虑 TTL 与来源依赖，但执行阶段按键名逐个删除，
consumer 删除失败或锁内复核需保留时仍继续删除其来源，留下缺源 consumer，
后续清理因清理自身制造的缺源而永久保守停止。

本文件以 R5-01 为唯一验收清单，全部使用真实
MaintenanceService._cleanup_session_resources() 控制流 + 真实内存
InMemoryStore.delete（一次性失败注入），文件 rmtree 由 patch 禁止，会话用
memory lease，不连真实 PG、不调用真实模型、不删除真实资源。

覆盖：
- 两种名称字典序下 consumer 一次性删除失败；
- 首轮不留缺源 consumer、来源祖先保住；
- 后端恢复后下一轮正常回收，不持续停止；
- 多跳依赖 / 共享来源 / consumer 成功后来源删除失败 / 锁内复核需保留；
- 既有 dry-run 报告行为不变；TTL 多周期、有根保护、移除最后根、坏图停止
  由本仓库既有 R3/R4 维护回归在全量后端中继续覆盖。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from langgraph.store.memory import InMemoryStore

from src.modules.db_deepagent.persistence.archive import QaArchive
from src.modules.db_deepagent.persistence.maintenance import (
    CleanupReport,
    MaintenanceService,
)
from src.modules.db_deepagent.persistence.session_guard import (
    acquire_memory_lease,
    release_memory_lease,
)

pytestmark = pytest.mark.unit

SESSION = "s1"
THREAD = "dbda:s1:1"
REF = "res_" + "a" * 32
_NS = ("db_deepagent", "run_snapshots", SESSION)


class _FaultStore(InMemoryStore):
    """内存 Store：对指定 run 快照的 delete 注入一次性失败并记录 delete 尝试。

    fail_once 的 key 命中一次后即移除，模拟「后端随后恢复」；attempts 按
    delete 实际进入顺序记录 run_id，用于断言本轮没有误删来源。
    """

    def __init__(self):
        super().__init__()
        self.fail_keys: set[str] = set()
        self.attempts: list[str] = []

    def fail_once(self, key: str) -> None:
        self.fail_keys.add(key)

    def delete(self, namespace, key):
        if namespace == _NS:
            try:
                run_id = json.loads(key)[1]
            except (ValueError, IndexError, TypeError):
                run_id = str(key)
            self.attempts.append(run_id)
            if key in self.fail_keys:
                self.fail_keys.discard(key)
                raise RuntimeError("injected one-shot snapshot DELETE failure")
        return super().delete(namespace, key)


def _source_record(run: str) -> dict:
    return {
        "session_id": SESSION,
        "thread_id": THREAD,
        "run_id": run,
        "evidence_id": "E-" + run,
        "result_ref": REF,
        "sub_task_id": "T1",
        "sub_task_revision": 1,
        "input_result_refs": [],
    }


def _build_chain(store, chain, now):
    """chain: [(run_id, age_days, source_run_or_None)]，均用 direct_reuse_from 建血缘。

    所有记录共享同一 result_ref；证据身份按 (session, thread, run, evidence_id)
    区分，来源引用可被 evidence_index 解析。无 QA/携带根 → 全部为孤儿。
    """
    archive = QaArchive(store)
    for run, age, source_run in chain:
        record = _source_record(run)
        if source_run is not None:
            record["direct_reuse_from"] = {
                "evidence_ref": {
                    "session_id": SESSION,
                    "thread_id": THREAD,
                    "run_id": source_run,
                    "evidence_id": "E-" + source_run,
                },
                "source_task_ref": {
                    "session_id": SESSION,
                    "thread_id": THREAD,
                    "run_id": source_run,
                    "sub_task_id": "T1",
                    "revision": 1,
                },
            }
        archive.write_run_snapshot(
            SESSION,
            THREAD,
            run,
            {
                "run_snapshot_schema_version": 1,
                "ledger_protocol_version": 1,
                "session_id": SESSION,
                "thread_id": THREAD,
                "run_id": run,
                "sealed_at": (now - timedelta(days=age)).isoformat(),
                "tasks": [],
                "executions": [record],
                "analyses": [],
            },
        )
    return archive


def _cleanup(archive, moment, *, apply=True):
    """真实 _cleanup_session_resources；apply 用真实内存 Store 快照删除，
    文件 rmtree 由 patch 禁止，避免删除真实资源。"""
    with TemporaryDirectory(prefix="doc63-r5-") as root:
        svc = MaintenanceService(
            SimpleNamespace(workspace=SimpleNamespace(root=str(root))), archive, None
        )
        report = CleanupReport(dry_run=not apply)
        lease = acquire_memory_lease(SESSION)
        try:
            with patch(
                "src.modules.db_deepagent.persistence.maintenance.shutil.rmtree",
                side_effect=AssertionError("real file deletion forbidden"),
            ):
                svc._cleanup_session_resources(
                    SESSION,
                    result_metas={},
                    result_retention=timedelta(days=30),
                    archive_before=moment - timedelta(days=7),
                    now=moment,
                    apply=apply,
                    report=report,
                    conn=None,
                )
        finally:
            release_memory_lease(lease)
        return report


def _remaining(archive):
    return sorted(s["run_id"] for s in archive.list_run_snapshots(SESSION))


def _has(report, needle):
    return any(needle in x["reason"] for x in report.skipped)


class TestR501PartialDeleteFailure:
    """R5-01：消费者先于来源的依赖顺序 + 第一次失败/保留即停止本轮剩余删除。"""

    @pytest.mark.parametrize(
        "source,child",
        [
            ("a-source", "z-child"),  # source 名称在前
            ("z-source", "a-child"),  # consumer 名称在前
        ],
        ids=["source_sorts_first", "consumer_sorts_first"],
    )
    def test_consumer_delete_failure_keeps_source_and_recovers(self, source, child):
        """验收 1–4：两种字典序下 consumer 一次性删除失败。

        首轮：consumer 删除失败 → 停止本轮剩余删除，来源（无论名称排序）不得
        被删，不留缺源 consumer。后端恢复后第二轮：依赖顺序下一起回收，无
        「缺源」/「跳过该会话全部」持续停止。
        """
        now = datetime.now(UTC)
        store = _FaultStore()
        archive = _build_chain(store, [(source, 10, None), (child, 8, source)], now)
        store.fail_once(archive._run_snapshot_key(THREAD, child))

        first = _cleanup(archive, now)
        first_attempts = list(store.attempts)
        assert child in _remaining(archive), "consumer 删除失败后仍存在"
        assert source in _remaining(archive), "来源必须保住，不能留缺源 consumer"
        assert source not in first_attempts, "首轮不得尝试删除来源"
        assert _has(first, "孤儿快照清理失败")
        assert _has(first, "停止该会话本轮剩余孤儿快照删除")
        assert not _has(first, "缺源")
        assert not _has(first, "跳过该会话全部")

        second = _cleanup(archive, now + timedelta(days=1))
        assert _remaining(archive) == [], _remaining(archive)
        assert not _has(second, "缺源")
        assert not _has(second, "跳过该会话全部")

    def test_multihop_retained_consumer_keeps_all_source_ancestors(self):
        """验收 5：多跳依赖，保留 consumer 时来源祖先仍保留，恢复后一起回收。"""
        now = datetime.now(UTC)
        store = _FaultStore()
        archive = _build_chain(
            store,
            [
                ("z-source", 10, None),
                ("middle", 9, "z-source"),
                ("z-child", 8, "middle"),
            ],
            now,
        )
        store.fail_once(archive._run_snapshot_key(THREAD, "z-child"))

        first = _cleanup(archive, now)
        first_attempts = list(store.attempts)
        assert _remaining(archive) == ["middle", "z-child", "z-source"], _remaining(
            archive
        )
        assert "middle" not in first_attempts, "中间来源祖先不得删除"
        assert "z-source" not in first_attempts, "最上游来源祖先不得删除"
        assert _has(first, "停止该会话本轮剩余孤儿快照删除")
        assert not _has(first, "缺源")

        second = _cleanup(archive, now + timedelta(days=1))
        assert _remaining(archive) == [], _remaining(archive)
        assert not _has(second, "缺源")

    def test_shared_source_kept_while_consumer_undeleted(self):
        """验收 6：共享来源，仍有未删除 consumer 时来源不能被删。"""
        now = datetime.now(UTC)
        store = _FaultStore()
        archive = _build_chain(
            store,
            [
                ("a-source", 10, None),
                ("b-child1", 8, "a-source"),
                ("c-child2", 8, "a-source"),
            ],
            now,
        )
        store.fail_once(archive._run_snapshot_key(THREAD, "b-child1"))

        first = _cleanup(archive, now)
        first_attempts = list(store.attempts)
        assert "a-source" in _remaining(archive), "共享来源必须保住"
        assert "a-source" not in first_attempts, "来源在 consumer 未全删前不得删除"
        assert _has(first, "停止该会话本轮剩余孤儿快照删除")
        assert not _has(first, "缺源")

        second = _cleanup(archive, now + timedelta(days=1))
        assert _remaining(archive) == [], _remaining(archive)
        assert not _has(second, "缺源")

    def test_source_delete_failure_after_consumer_success_recovers(self):
        """验收 7：consumer 先删成功、随后来源删除失败，下一轮仍可回收。

        consumer 已不存在，来源残留不会形成缺源引用；下一轮来源作为无引用
        孤儿正常回收，不持续停止。
        """
        now = datetime.now(UTC)
        store = _FaultStore()
        archive = _build_chain(
            store, [("z-source", 10, None), ("a-child", 8, "z-source")], now
        )
        store.fail_once(archive._run_snapshot_key(THREAD, "z-source"))

        first = _cleanup(archive, now)
        assert _remaining(archive) == ["z-source"], _remaining(archive)
        assert _has(first, "孤儿快照清理失败")
        assert _has(first, "停止该会话本轮剩余孤儿快照删除")
        assert not _has(first, "缺源")

        second = _cleanup(archive, now + timedelta(days=1))
        assert _remaining(archive) == [], _remaining(archive)
        assert not _has(second, "缺源")
        assert not _has(second, "跳过该会话全部")

    def test_lock_recheck_retention_does_not_delete_source(self):
        """验收 8：锁内复核发现 consumer 仍需保留时，不继续删它的来源。

        第 1 次 list_run_snapshots（闭包/规划）看到 consumer 已到期；第 2 次
        （锁内重读）consumer sealed_at 变为未到期，复核判定需保留 → 停止，
        来源一次 delete 都不执行。后端恢复（无补丁）后下一轮正常回收。
        """
        now = datetime.now(UTC)
        store = _FaultStore()
        archive = _build_chain(
            store, [("z-source", 10, None), ("a-child", 8, "z-source")], now
        )
        real_list = archive.list_run_snapshots
        calls = {"n": 0}

        def flaky(sid):
            calls["n"] += 1
            snaps = real_list(sid)
            if calls["n"] >= 2:
                return [
                    (
                        {**s, "sealed_at": now.isoformat()}
                        if s.get("run_id") == "a-child"
                        else s
                    )
                    for s in snaps
                ]
            return snaps

        with patch.object(archive, "list_run_snapshots", side_effect=flaky):
            first = _cleanup(archive, now)
        assert calls["n"] >= 2
        assert store.attempts == [], "复核需保留时本轮一个 delete 都不应执行"
        assert _remaining(archive) == ["a-child", "z-source"], _remaining(archive)
        assert _has(first, "锁内复核发现孤儿快照")
        assert _has(first, "停止该会话本轮剩余孤儿快照删除")
        assert not _has(first, "缺源")

        second = _cleanup(archive, now + timedelta(days=1))
        assert _remaining(archive) == [], _remaining(archive)
        assert not _has(second, "缺源")

    def test_dry_run_reports_all_pending_without_deleting(self):
        """验收 9：dry-run 行为不变——按依赖顺序报告全部待清理、不实删。"""
        now = datetime.now(UTC)
        store = _FaultStore()
        archive = _build_chain(
            store, [("z-source", 10, None), ("a-child", 8, "z-source")], now
        )
        report = _cleanup(archive, now, apply=False)
        assert _remaining(archive) == ["a-child", "z-source"], _remaining(archive)
        pending = [x for x in report.skipped if "待清理 (dry-run)" in x["reason"]]
        assert sorted(x["run_snapshot"] for x in pending) == ["a-child", "z-source"]
        assert store.attempts == [], "dry-run 不执行 delete"
        assert not _has(report, "停止该会话本轮剩余孤儿快照删除")

    def test_clean_expired_chain_reclaimed_in_one_cycle(self):
        """合法成功路径：无故障时依赖顺序下 consumer 与来源同轮一起回收。"""
        now = datetime.now(UTC)
        store = _FaultStore()
        archive = _build_chain(
            store, [("z-source", 10, None), ("a-child", 8, "z-source")], now
        )
        report = _cleanup(archive, now)
        assert _remaining(archive) == [], _remaining(archive)
        # 消费者先于来源：attempts 第一个是 consumer
        assert store.attempts == ["a-child", "z-source"], store.attempts
        assert not _has(report, "缺源")
        assert not _has(report, "停止该会话本轮剩余孤儿快照删除")
