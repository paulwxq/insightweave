"""清理（docs/v4/5 §13.5 + doc 7 §2 折入 + docs/3.1 R2/R3）

线程级判定：当前线程／待恢复保护，effective_at = max(last_active_at,
retired_at, 最新 checkpoint 时间)；会话归属 parse_thread_id 精确复核；
advisory lock 会话级互斥（拿不到跳过该会话，拿到后重新核对资格）；
独立短连接逐线程显式事务删除（§13.5 清理连接与事务）。

结果目录（R2）与会话循环解耦：全局按目录年龄删除，不读会话归属、
不看引用、不取会话锁；快照表不存在时跳过实删。快照行（R3）按
last_referenced_at + archive_retention_days 全局删除。
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from src.modules.db_deepagent.config import DbAgentConfig
from src.modules.db_deepagent.persistence.archive import QaArchive
from src.modules.db_deepagent.persistence.checkpoint import (
    SessionAdvisoryLock,
    parse_thread_id,
)
from src.modules.db_deepagent.persistence.platform_pool import make_platform_conninfo
from src.utils.logger import get_module_logger

logger = get_module_logger("nl2sql.db_deepagent.persistence.maintenance")

_RESULT_REF_RE = re.compile(r"^res_[0-9a-f]{32}$")


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
    snapshot_deleted: int = 0  # result_snapshots 删除（或 dry-run 计数）
    archives: list[tuple[str, str]] = field(
        default_factory=list
    )  # (qa|run_errors, key)
    # Web 命名空间（doc 2 §4.4 P1 扩展）
    web_turns_deleted: list[str] = field(default_factory=list)  # 含 dry-run 标注
    web_reconciled: list[str] = field(default_factory=list)  # session_id
    web_reports: list[dict[str, Any]] = field(default_factory=list)
    creation_pending: list[str] = field(default_factory=list)  # 创建 pending 登记
    suspected_create_residuals: list[dict[str, Any]] = field(default_factory=list)

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
        """§13.5 + docs/3.1：会话内线程/归档/Web 清理，循环外 R2/R3 全局清理。

        清理会话＝sessions 记录 ∪ 结果目录 ∪ 归档中发现的所有会话，统一走
        同一套 advisory lock 流程（sessions 缺失不代表没有运行：首次请求
        尚未到 P3、或此前 P3 写入失败都可能出现）；取锁失败跳过该会话的
        线程/归档/Web 清理。结果目录由循环外的 R2 全局步骤处理，不取会话锁。
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
            # Web 命名空间发现（doc 2 §4.4 P1）：web_sessions 键 + web_turns
            # 中出现的会话 ID 一并纳入；读取失败记录报告、不中断其余清理
            webstore = self._build_webstore()
            web_session_keys: set[str] = set()
            web_turn_sessions: set[str] = set()
            if webstore is not None:
                try:
                    web_session_keys = set(webstore.list_web_session_keys())
                except Exception as exc:  # noqa: BLE001
                    report.web_reports.append(
                        {
                            "reason": "web_sessions 枚举失败",
                            "detail": type(exc).__name__,
                        }
                    )
                try:
                    web_turn_sessions = webstore.list_turn_session_ids()
                except Exception as exc:  # noqa: BLE001
                    report.web_reports.append(
                        {"reason": "web_turns 枚举失败", "detail": type(exc).__name__}
                    )
            # 全部发现的会话统一取锁清理（无“不取锁直清”分支）；checkpoint
            # 兜底覆盖“首请求无结果 + P3 写入失败只留 checkpoint”的会话
            all_sessions = sorted(
                set(known_sessions)
                | set(result_metas)
                | archive_sessions
                | checkpoint_sessions
                | web_session_keys
                | web_turn_sessions
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
                    # 2) 归档（第 5 点）：同锁内按保留期清理
                    report.archives.extend(
                        self._archive.purge_archives(
                            session_id=session_id, before=archive_before, apply=apply
                        )
                    )
                    # 4) Web 命名空间（doc 2 §4.4）：web_turns 保留清理、元数据
                    #    校正与疑似创建残留报告，同一会话锁内执行
                    if webstore is not None:
                        self._web_cleanup(
                            webstore,
                            session_id,
                            conn=conn,
                            result_refs=result_metas.get(session_id, []),
                            archive_before=archive_before,
                            apply=apply,
                            report=report,
                        )
                finally:
                    lock.release()
            # R2/R3：会话循环之外的全局步骤（不取会话锁）
            self._cleanup_result_dirs_global(
                retention=result_retention,
                now=reference_now,
                apply=apply,
                report=report,
                conn=conn,
            )
            self._cleanup_snapshots(
                before=archive_before,
                apply=apply,
                report=report,
                conn=conn,
            )
        return report

    # ------------------------------------------------------------ Web 命名空间

    def _build_webstore(self):
        """从归档 Store 构造 WebStore 门面（无 Web 需求时也可安全构造）。"""
        try:
            from src.api.v2.webstore import WebStore

            return WebStore(self._archive._store)  # noqa: SLF001（同一持久化层）
        except Exception as exc:  # noqa: BLE001
            logger.warning("WebStore 构造失败，跳过 Web 命名空间维护: %s", exc)
            return None

    def _web_cleanup(
        self,
        webstore,
        session_id: str,
        *,
        conn,
        result_refs,
        archive_before: datetime,
        apply: bool,
        report: CleanupReport,
    ) -> None:
        """web_turns 保留清理 + web_sessions 元数据校正 + 残留报告（doc 2 §4.4）。

        坏记录/超限/读取失败均按校正失败处理：不删除文本、不校正缓存，
        可写时设 retry_after=失败时刻+300 秒；疑似创建残留先于历史删除检查。
        dry-run 严格只读：只出报告，不删除、不校正、不写退避；apply 时
        删除失败的轮次仍按在库计入校正（不按“已删除”更新缓存）。
        """
        from src.api.v2.webstore import (
            InvalidTurnRecord,
            ScanLimitExceeded,
            StoreUnavailable,
            core_ownership_ok,
            parse_tz_datetime,
        )

        try:
            record = webstore.get_registration(session_id)
        except StoreUnavailable:
            record = None
            report.web_reports.append(
                {"session_id": session_id, "reason": "web 登记读取失败（核验未完成）"}
            )
        # 退避（合法未来 ≤1h）：报告并跳过本轮清理/校正
        if record is not None:
            retry_after = parse_tz_datetime(
                record.get("metadata_reconcile_retry_after")
            )
            if retry_after is not None and (
                0 < (retry_after - datetime.now().astimezone()).total_seconds() <= 3600
            ):
                report.web_reports.append(
                    {"session_id": session_id, "reason": "校正退避中（retry_after）"}
                )
                return
            pending = record.get("pending_run_id")
            if isinstance(pending, str) and pending.startswith("create_"):
                report.creation_pending.append(session_id)
        # 疑似创建残留（report-only；先于历史删除检查，doc 2 §4.4）
        if record is None:
            suspected = self._suspected_create_residual(
                session_id, conn=conn, result_refs=result_refs, webstore=webstore
            )
            if suspected is not None:
                report.suspected_create_residuals.append(suspected)
        # web_turns 全量校验（共用谓词 + 预算）
        try:
            turn_records = webstore.iter_turn_records(session_id)
            turn_records.sort(
                key=lambda item: (
                    parse_tz_datetime(item[1].get("saved_at"))
                    or datetime(1, 1, 1, tzinfo=timezone.utc),
                    item[0],
                )
            )
        except InvalidTurnRecord as exc:
            report.web_reports.append(
                {
                    "session_id": session_id,
                    "reason": "需人工核验（web_turn 记录损坏）",
                    "key": exc.key,
                }
            )
            if apply:  # 退避是写操作，dry-run 不写
                self._mark_reconcile_failure(webstore, session_id, record, report)
            return
        except ScanLimitExceeded:
            report.web_reports.append(
                {"session_id": session_id, "reason": "需人工核验（web_turns 扫描超限）"}
            )
            if apply:
                self._mark_reconcile_failure(webstore, session_id, record, report)
            return
        except StoreUnavailable:
            report.web_reports.append(
                {
                    "session_id": session_id,
                    "reason": "web_turns 读取失败（保留，不猜测删除）",
                }
            )
            return
        # 保留期清理（archive_retention_days；损坏的 saved_at 不会到达这里）
        expired = [
            (key, rec)
            for key, rec in turn_records
            if (parse_tz_datetime(rec.get("saved_at")) or datetime.now(timezone.utc))
            < archive_before
        ]
        if not apply:
            # dry-run 严格只读：只报告将删除项，不做任何写（含校正/退避）
            report.web_turns_deleted.extend(f"{key} (dry-run)" for key, _rec in expired)
            return
        deleted: set[str] = set()
        for key, _rec in expired:
            try:
                webstore.delete_turn(session_id, key)
                report.web_turns_deleted.append(key)
                deleted.add(key)
            except StoreUnavailable as exc:
                report.errors.append(
                    {
                        "session_id": session_id,
                        "key": key,
                        "error": f"web_turn 删除失败: {type(exc).__name__}",
                    }
                )
        # 删除失败的轮次仍在库中：按实际存活集校正，不按“已删除”更新缓存
        kept = [item for item in turn_records if item[0] not in deleted]
        # 元数据校正（登记存在且归属可核对时；单次扫描内完成）
        if record is not None and core_ownership_ok(record, session_id):
            try:
                self._reconcile_registration_locked(webstore, session_id, record, kept)
                report.web_reconciled.append(session_id)
            except Exception as exc:  # noqa: BLE001
                report.web_reports.append(
                    {
                        "session_id": session_id,
                        "reason": "元数据校正失败",
                        "detail": type(exc).__name__,
                    }
                )
                self._mark_reconcile_failure(webstore, session_id, record, report)

    def _reconcile_registration_locked(
        self, webstore, session_id: str, record: dict, kept: list
    ) -> None:
        """锁内全量校正（与 WebStore.reconcile_metadata 同规则，记录由调用方
        扫描提供以满足单次扫描预算）。"""
        from src.api.v2.webstore import (
            SessionMetadataInvalid,
            derive_title,
            parse_tz_datetime,
        )

        created = parse_tz_datetime(record.get("created_at"))
        if created is None:
            raise SessionMetadataInvalid(session_id, ["created_at"])
        updated = dict(record)
        updated["turn_count"] = len(kept)
        title = record.get("title")
        # §4.4：已有合法标题保留（含仍为默认“新对话”），缺失/非法按首问派生
        if not isinstance(title, str) or not title.strip():
            first_question = kept[0][1]["question"] if kept else ""
            updated["title"] = derive_title(first_question)
        times = [created]
        existing_last = parse_tz_datetime(record.get("last_active_at"))
        if existing_last is not None and existing_last >= created:
            times.append(existing_last)
        for _key, rec in kept:
            saved = parse_tz_datetime(rec.get("saved_at"))
            if saved is not None:
                times.append(saved)
        updated["last_active_at"] = max(times).isoformat()
        updated["metadata_reconciled_at"] = webstore.now_iso()
        updated["metadata_reconcile_retry_after"] = None
        webstore.put_registration(session_id, updated)

    def _mark_reconcile_failure(
        self, webstore, session_id: str, record, report: CleanupReport
    ) -> None:
        """校正失败：登记可写时设 retry_after=当前+300 秒（doc 2 §4.4）。"""
        from src.api.v2.webstore import core_ownership_ok

        if record is None or not core_ownership_ok(record, session_id):
            return
        try:
            webstore.mark_reconcile_failure(session_id, record)
        except Exception as exc:  # noqa: BLE001
            report.web_reports.append(
                {
                    "session_id": session_id,
                    "reason": "退避标记写入失败（本次仅尝试一次）",
                    "detail": type(exc).__name__,
                }
            )

    def _suspected_create_residual(
        self, session_id: str, *, conn, result_refs, webstore
    ):
        """疑似 Web 创建残留（doc 2 §4.4 report-only）：长 ID + 有通用绑定 +
        无 Web 登记 + 无任何执行/历史痕迹。None=核验未完成（读取失败）。"""
        import re

        from src.api.v2.webstore import StoreUnavailable

        if not re.fullmatch(r"s_[0-9a-f]{32}", session_id or ""):
            return None
        generic = self._archive.get_session(session_id)
        if generic is None:
            return None
        try:
            checkpoint_threads = self._checkpoint_thread_ids(conn, session_id)
        except Exception as exc:  # noqa: BLE001
            logger.info(
                "创建残留核验未完成（checkpoint 读取失败）session=%s: %s",
                session_id,
                type(exc).__name__,
            )
            return None
        try:

            def _has_any(namespace) -> bool | None:
                try:
                    page = self._archive._store.search(  # noqa: SLF001（同一持久化层）
                        namespace, limit=1, offset=0
                    )
                    return bool(list(getattr(page, "items", page)))
                except Exception:  # noqa: BLE001
                    return None

            has_threads = _has_any(("db_deepagent", "threads", session_id))
            has_qa = _has_any(("db_deepagent", "qa", session_id))
            has_run_errors = _has_any(("db_deepagent", "run_errors", session_id))
            if None in (has_threads, has_qa, has_run_errors):
                logger.info(
                    "创建残留核验未完成（Store 读取失败）session=%s", session_id
                )
                return None
            try:
                turn_keys = webstore.iter_turn_records(session_id)
                has_turns = bool(turn_keys)
                turns_verified = True
            except Exception:  # noqa: BLE001（坏记录/超限也说明“有数据”）
                has_turns = True
                turns_verified = False
        except StoreUnavailable:
            logger.info("创建残留核验未完成（Store 读取失败）session=%s", session_id)
            return None
        has_carried = bool(generic.get("carried_evidence"))
        has_results = bool(result_refs)
        has_local_runs = (
            Path(self._cfg.workspace.root) / "sessions" / session_id
        ).exists()
        if any(
            (
                checkpoint_threads,
                has_threads,
                has_qa,
                has_run_errors,
                has_turns,
                has_carried,
                has_results,
                has_local_runs,
            )
        ):
            return None
        return {
            "session_id": session_id,
            "reason": "疑似 Web 创建残留（仅报告，不自动删除/解禁）",
            "note": "长 ID 仅缩小候选，不证明 Web 来源；人工处理须先核验无执行/历史数据",
            "turns_verified": turns_verified,
        }

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
            if not isinstance(meta, dict):
                continue
            out.setdefault(str(meta.get("session_id", "")), []).append(
                (path.name, meta)
            )
        return out

    def _snapshot_table_ready(self, conn) -> bool | None:
        """R2/R3 统一门禁：只核对一次快照表是否存在；None=无法确认。"""
        try:
            row = conn.execute(
                "SELECT to_regclass('result_snapshots') AS reg"
            ).fetchone()
        except Exception as exc:  # noqa: BLE001
            logger.warning("快照表存在性核对失败: %s: %s", type(exc).__name__, exc)
            return None
        return bool(row and row.get("reg"))

    def _result_dir_age(self, path: Path, now: datetime) -> datetime:
        """目录年龄基准：meta.created_at 可解析且不晚于基准时间时优先，
        否则回退目录 mtime；mtime 在未来时夹紧到基准时间。"""
        try:
            meta = json.loads((path / "meta.json").read_text(encoding="utf-8"))
            if isinstance(meta, dict):
                created = _as_utc(meta.get("created_at"))
                if created is not None and created <= now:
                    return created
        except (OSError, ValueError, TypeError, AttributeError):
            pass
        try:
            mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        except OSError:
            return now
        return min(mtime, now)

    def _cleanup_result_dirs_global(
        self,
        *,
        retention: timedelta,
        now: datetime,
        apply: bool,
        report: CleanupReport,
        conn,
    ) -> None:
        """R2：全局按目录时间删除，不读会话归属、不看引用、不取会话锁。"""
        root = Path(self._cfg.workspace.root) / "results"
        if not root.is_dir():
            return
        if self._snapshot_table_ready(conn) is not True:
            report.skipped.append(
                {
                    "result_ref": "*",
                    "reason": "快照表不存在或无法确认，跳过 R2 实删",
                }
            )
            return
        for path in sorted(root.iterdir()):
            if not path.is_dir() or not _RESULT_REF_RE.match(path.name):
                continue
            if (now - self._result_dir_age(path, now)) < retention:
                continue
            if not apply:
                report.result_dirs.append(path.name + " (dry-run)")
                continue
            try:
                shutil.rmtree(path)
                report.result_dirs.append(path.name)
            except OSError as exc:
                report.errors.append(
                    {"result_ref": path.name, "error": f"{type(exc).__name__}: {exc}"}
                )

    def _cleanup_snapshots(
        self,
        *,
        before: datetime,
        apply: bool,
        report: CleanupReport,
        conn,
    ) -> None:
        """R3：按 last_referenced_at 清理快照行。"""
        if self._snapshot_table_ready(conn) is not True:
            report.skipped.append(
                {
                    "snapshot": "*",
                    "reason": "快照表不存在或无法确认，跳过 R3 清理",
                }
            )
            return
        if apply:
            cur = conn.execute(
                "DELETE FROM result_snapshots WHERE last_referenced_at < %s",
                (before,),
            )
            report.snapshot_deleted = int(cur.rowcount or 0)
        else:
            row = conn.execute(
                "SELECT count(*) AS n FROM result_snapshots "
                "WHERE last_referenced_at < %s",
                (before,),
            ).fetchone()
            report.snapshot_deleted = int(row["n"] if row else 0)

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
