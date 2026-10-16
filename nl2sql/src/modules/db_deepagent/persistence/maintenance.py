"""清理（docs/v4/5 §13.5 + doc 7 §2 折入）

线程级判定：当前线程／待恢复保护，effective_at = max(last_active_at,
retired_at, 最新 checkpoint 时间)；会话归属 parse_thread_id 精确复核；
advisory lock 会话级互斥（拿不到跳过该会话，拿到后重新核对资格）；
独立短连接逐线程显式事务删除（§13.5 清理连接与事务）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from src.modules.db_deepagent.config import DbAgentConfig
from src.modules.db_deepagent.persistence.archive import QaArchive, refs_of_record
from src.modules.db_deepagent.persistence.checkpoint import (
    SessionAdvisoryLock,
    parse_thread_id,
)
from src.modules.db_deepagent.persistence.platform_pool import make_platform_conninfo
from src.utils.logger import get_module_logger

logger = get_module_logger("nl2sql.db_deepagent.persistence.maintenance")


@dataclass
class ThreadCandidate:
    thread_id: str
    reason: str  # expired-retired / expired-inactive / keep-current /
    # keep-pending / skip-state-error / skip-undecidable / keep-recent
    detail: str = ""
    effective_at: datetime | None = None
    action: str = "skip"  # delete / keep / skip


@dataclass
class CleanupReport:
    dry_run: bool
    sessions: list[dict[str, Any]] = field(default_factory=list)
    deleted_threads: list[str] = field(default_factory=list)
    kept_threads: list[str] = field(default_factory=list)
    skipped: list[dict[str, Any]] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)
    result_dirs: list[str] = field(
        default_factory=list
    )  # §13.5 第 4 点（含 dry-run 标注）
    archives: list[tuple[str, str]] = field(
        default_factory=list
    )  # (qa|run_errors, key)

    @property
    def ok(self) -> bool:
        return not self.errors


class MaintenanceService:
    """cleanup [--dry-run|--apply] [--before]（§13.5；agent 提供 get_state）"""

    def __init__(self, cfg: DbAgentConfig, archive: QaArchive, agent):
        self._cfg = cfg
        self._archive = archive
        self._agent = agent

    # ------------------------------------------------------------ 候选发现

    def _checkpoint_thread_ids(self, conn, session_id: str) -> set[str]:
        """模块内前缀枚举：LIKE 转义 `_`/`%`/`\\`（doc 7 §2.1）。"""
        escaped = (
            session_id.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        )
        pattern = f"dbda:{escaped}:%"
        rows = conn.execute(
            "SELECT DISTINCT thread_id FROM db_deepagent.checkpoints "
            "WHERE thread_id LIKE %s ESCAPE '\\'",
            (pattern,),
        ).fetchall()
        out: set[str] = set()
        for row in rows:
            candidate = row["thread_id"]
            parsed = parse_thread_id(candidate)
            if parsed is not None and parsed[0] == session_id:  # 精确归属复核
                out.add(candidate)
        return out

    def _candidates(self, conn, session_id: str) -> list[str]:
        session = self._archive.get_session(session_id)
        candidates: set[str] = set()
        if session is not None:
            max_seq = int(session.get("thread_seq", 1) or 1)
            for seq in range(1, max_seq + 1):
                candidates.add(f"dbda:{session_id}:{seq}")
        candidates |= self._checkpoint_thread_ids(conn, session_id)
        return sorted(candidates)

    def _checkpoint_sessions(self, conn) -> set[str]:
        """从 checkpoint 表发现的会话（归属经 parse_thread_id 复核）。

        兜底“首请求没有查询结果、P3 写入失败只留 checkpoint”的会话——
        sessions／结果目录／归档都发现不了。
        """
        rows = conn.execute(
            "SELECT DISTINCT thread_id FROM db_deepagent.checkpoints"
        ).fetchall()
        out: set[str] = set()
        for row in rows:
            parsed = parse_thread_id(row["thread_id"])
            if parsed is not None:
                out.add(parsed[0])
        return out

    # ------------------------------------------------------------ 判定

    def _thread_state(self, thread_id: str) -> tuple[Any, datetime | None]:
        """(state_snapshot, created_at)；读取异常抛出（调用方跳过）。"""
        snapshot = self._agent.get_state({"configurable": {"thread_id": thread_id}})
        created = getattr(snapshot, "created_at", None)
        return snapshot, _as_utc(created)

    def judge(
        self,
        conn,
        session_id: str,
        thread_id: str,
        *,
        cutoff: datetime,
        current_thread: str | None,
    ) -> ThreadCandidate:
        if thread_id == current_thread:
            return ThreadCandidate(thread_id, "keep-current", action="keep")
        try:
            snapshot, created_at = self._thread_state(thread_id)
        except Exception as exc:  # 状态读取失败→跳过，不删
            return ThreadCandidate(
                thread_id,
                "skip-state-error",
                detail=f"{type(exc).__name__}: {exc}",
                action="skip",
            )
        if getattr(snapshot, "next", None):
            return ThreadCandidate(thread_id, "keep-pending", action="keep")
        track = self._archive.get_thread(session_id, thread_id) or {}
        times = [
            _as_utc(track.get("last_active_at")),
            _as_utc(track.get("retired_at")),
            created_at,
        ]
        if created_at is None:  # created_at 缺失才允许 SQL 兜底（doc 7 §2.2）
            times.append(self._checkpoint_max_ts(conn, thread_id))
        effective = _max_time(times)
        if effective is None:
            return ThreadCandidate(thread_id, "skip-undecidable", action="skip")
        retired = bool(track.get("retired"))
        if effective >= cutoff:
            return ThreadCandidate(
                thread_id,
                "expired-retired" if retired else "keep-recent",
                effective_at=effective,
                action="keep" if not retired else "keep",
            )
        return ThreadCandidate(
            thread_id,
            "expired-retired" if retired else "expired-inactive",
            effective_at=effective,
            action="delete",
        )

    def _checkpoint_max_ts(self, conn, thread_id: str) -> datetime | None:
        row = conn.execute(
            "SELECT max((checkpoint->>'ts')::timestamptz) AS max_ts "
            "FROM db_deepagent.checkpoints WHERE thread_id = %s",
            (thread_id,),
        ).fetchone()
        value = row["max_ts"] if row else None
        return _as_utc(value) if value is not None else None

    # ------------------------------------------------------------ 主流程

    def cleanup(
        self,
        *,
        apply: bool,
        before: datetime | None = None,
    ) -> CleanupReport:
        """§13.5：线程 → 结果目录 → qa/run_errors 三段清理。

        清理会话＝sessions 记录 ∪ 结果目录 ∪ 归档中发现的所有会话，统一走
        同一套 advisory lock 流程（sessions 缺失不代表没有运行：首次请求
        尚未到 P3、或此前 P3 写入失败都可能出现）；取锁失败跳过该会话
        全部清理，避免引用检查与实际删除之间与运行中的会话竞争。
        引用是会话内的（结果读取有会话归属校验），无跨会话保护需求。
        before 规范化为 aware UTC 且只收紧不放宽（晚于当前时刻按当前时刻）。
        """
        report = CleanupReport(dry_run=not apply)
        now = datetime.now(timezone.utc)
        reference_now = _as_utc(before) or now
        if reference_now > now:
            reference_now = now  # --before 只收紧不放宽（§13.5）
        cutoff = reference_now - timedelta(
            days=self._cfg.thread.checkpoint_retention_days
        )
        archive_before = reference_now - timedelta(
            days=self._cfg.thread.archive_retention_days
        )
        result_retention = timedelta(days=self._cfg.thread.result_retention_days)
        conninfo = make_platform_conninfo(self._cfg.platform_db)
        import psycopg
        from psycopg.rows import dict_row

        # §13.5 清理连接规范：autocommit（逐线程 conn.transaction() 为真事务，
        # 先提交后释放锁）＋prepare_threshold=0＋dict_row；search_path 由
        # conninfo 的 options 提供
        with psycopg.connect(
            conninfo,
            autocommit=True,
            prepare_threshold=0,
            row_factory=dict_row,
        ) as conn:
            known_sessions = self._list_sessions()
            result_metas = self._scan_result_metas()
            archive_sessions = self._archive.list_archive_sessions()
            checkpoint_sessions = self._checkpoint_sessions(conn)
            # 全部发现的会话统一取锁清理（无“不取锁直清”分支）；checkpoint
            # 兜底覆盖“首请求无结果 + P3 写入失败只留 checkpoint”的会话
            all_sessions = sorted(
                set(known_sessions)
                | set(result_metas)
                | archive_sessions
                | checkpoint_sessions
            )
            for session_id in all_sessions:
                lock = SessionAdvisoryLock(conninfo, session_id)
                if not lock.try_acquire():
                    report.skipped.append(
                        {
                            "session_id": session_id,
                            "reason": "会话运行中（互斥锁），跳过该会话全部清理",
                        }
                    )
                    continue
                try:
                    # 1) 线程（§13.5 第 1/3 点；无 sessions 记录时序号推导为空，
                    #    仅 checkpoint 枚举兜底）
                    self._cleanup_session(
                        conn, session_id, cutoff=cutoff, apply=apply, report=report
                    )
                    # 2) 结果目录（第 4 点）：同锁内收集保护引用再判删除
                    self._cleanup_session_results(
                        session_id,
                        result_metas.get(session_id, []),
                        retention=result_retention,
                        now=reference_now,
                        apply=apply,
                        report=report,
                        conn=conn,
                    )
                    # 3) 归档（第 5 点）：同锁内按保留期清理
                    report.archives.extend(
                        self._archive.purge_archives(
                            session_id=session_id, before=archive_before, apply=apply
                        )
                    )
                finally:
                    lock.release()
        return report

    def _scan_result_metas(self) -> dict[str, list[tuple[str, dict[str, Any]]]]:
        """按会话归组的结果目录元数据（{session_id: [(result_ref, meta)]}）。"""
        import json as _json

        root = Path(self._cfg.workspace.root) / "results"
        out: dict[str, list[tuple[str, dict[str, Any]]]] = {}
        if not root.is_dir():
            return out
        for path in sorted(root.iterdir()):
            if not path.is_dir() or path.name.startswith("."):
                continue
            try:
                meta = _json.loads((path / "meta.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            out.setdefault(str(meta.get("session_id", "")), []).append(
                (path.name, meta)
            )
        return out

    def _protected_refs(
        self, session_id: str, surviving_threads: set[str]
    ) -> tuple[set[str], bool]:
        """该会话仍被引用的 result_ref 集合（§13.5 第 4 点保护口径）。

        来源：保留线程状态、sessions 携带证据、qa 归档证据；执行／分析
        记录统一经 refs_of_record 提取（分析含 input_result_refs）。
        任何线程状态读取失败即返回 (…, False)——保护信息不完整时跳过
        该会话的结果删除。
        """
        refs: set[str] = set()
        for thread_id in sorted(surviving_threads):
            try:
                values = self._agent.get_state(
                    {"configurable": {"thread_id": thread_id}}
                ).values
            except Exception as exc:  # noqa: BLE001（保护信息不完整→跳过删除）
                logger.warning("保护引用读取失败 %s: %s", thread_id, exc)
                return set(), False
            for record in values.get("db_executions") or []:
                refs |= refs_of_record(dict(record))
            for record in values.get("db_analyses") or []:
                refs |= refs_of_record(dict(record))
        session_rec = self._archive.get_session(session_id) or {}
        for item in session_rec.get("carried_evidence") or []:
            record = item.get("record") or {}
            refs |= refs_of_record(dict(record))
        refs |= self._archive.collect_result_refs(session_id=session_id)
        return refs, True

    def _cleanup_session_results(
        self,
        session_id: str,
        entries: list[tuple[str, dict[str, Any]]],
        *,
        retention: timedelta,
        now: datetime,
        apply: bool,
        report: CleanupReport,
        conn,
    ) -> None:
        """§13.5 第 4 点：结果目录早于 result_retention_days，且不被保留中
        的线程状态（含分析的输入引用）、携带证据、qa 归档引用时才删除。"""
        if not entries:
            return
        surviving = self._checkpoint_thread_ids(conn, session_id)
        protected, readable = self._protected_refs(session_id, surviving)
        if not readable:
            report.skipped.append(
                {
                    "session_id": session_id,
                    "reason": "保护引用读取失败，跳过该会话结果目录清理",
                }
            )
            return
        from src.modules.db_deepagent.errors import ResultStoreError
        from src.modules.db_deepagent.services.result_store import ResultStore

        results = ResultStore(Path(self._cfg.workspace.root))
        for name, meta in entries:
            if meta.get("thread_id") in surviving or name in protected:
                continue
            created = _as_utc(meta.get("created_at"))
            if created is None or (now - created) < retention:
                continue
            if not apply:
                report.result_dirs.append(name + " (dry-run)")
                continue
            try:
                results.delete(name, session_id=meta.get("session_id", ""))
                report.result_dirs.append(name)
            except ResultStoreError as exc:
                report.errors.append(
                    {
                        "result_ref": name,
                        "error": f"{exc.error_type}: {exc.message}",
                    }
                )

    def _list_sessions(self) -> list[str]:
        out: list[str] = []
        offset = 0
        while True:
            page = self._archive._store.search(
                ("db_deepagent", "sessions"), limit=100, offset=offset
            )
            items = getattr(page, "items", page)
            if not items:
                break
            out.extend(item.key for item in items)
            if len(items) < 100:
                break
            offset += 100
        return out

    def _cleanup_session(
        self, conn, session_id: str, *, cutoff, apply: bool, report: CleanupReport
    ) -> None:
        session = self._archive.get_session(session_id)
        current = (session or {}).get("current_thread_id")
        for thread_id in self._candidates(conn, session_id):
            verdict = self.judge(
                conn, session_id, thread_id, cutoff=cutoff, current_thread=current
            )
            entry = {
                "session_id": session_id,
                "thread_id": thread_id,
                "reason": verdict.reason,
                "effective_at": (
                    verdict.effective_at.isoformat() if verdict.effective_at else None
                ),
            }
            if verdict.action == "delete" and apply:
                try:
                    self._delete_thread(conn, thread_id)
                    report.deleted_threads.append(thread_id)
                except Exception as exc:  # 失败回滚该线程，可重试
                    report.errors.append(
                        {**entry, "error": f"{type(exc).__name__}: {exc}"}
                    )
            elif verdict.action == "delete":
                report.deleted_threads.append(thread_id + " (dry-run)")
            elif verdict.action == "skip":
                report.skipped.append({**entry, "detail": verdict.detail})
            else:
                report.kept_threads.append(thread_id)

    def _delete_thread(self, conn, thread_id: str) -> None:
        """逐线程显式事务删除（§13.5：SET LOCAL 超时 + 绑定该连接的 Saver）。"""
        from langgraph.checkpoint.postgres import PostgresSaver

        with conn.transaction():
            conn.execute(
                f"SET LOCAL statement_timeout = {self._cfg.maintenance.statement_timeout_ms}"
            )
            conn.execute(
                f"SET LOCAL lock_timeout = {self._cfg.maintenance.lock_timeout_ms}"
            )
            PostgresSaver(conn).delete_thread(thread_id)


def _as_utc(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _max_time(times: list[datetime | None]) -> datetime | None:
    known = [t for t in times if t is not None]
    return max(known) if known else None
