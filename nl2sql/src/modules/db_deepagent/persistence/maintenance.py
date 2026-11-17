"""清理（docs/v4/5 §13.5 + doc 7 §2 折入 + docs/3.1 R2/R3；doc 6.3 §6.5/§8.2）

线程级判定：当前线程／待恢复保护，effective_at = max(last_active_at,
retired_at, 最新 checkpoint 时间)；会话归属 parse_thread_id 精确复核；
advisory lock 会话级互斥（拿不到跳过该会话，拿到后重新核对资格）；
独立短连接逐线程显式事务删除（§13.5 清理连接与事务）。

doc 6.3 §6.5：全局 R2/R3 已收口——结果目录与 PG result_snapshots 按资源
所属 session 分组，与该会话的线程/归档/运行快照清理共用同一把会话锁；
仅候选枚举允许在锁外，取得锁后重新读取归属、年龄和保留闭包，直至实际
删除结束才释放；取锁失败或锁后端异常跳过并报告，不再有循环外的无锁全
局兜底删除。保留闭包以仍在保留期的 QA/run_errors、轮换携带引用为根，
递归保护直接沿用来源、分析/占位符输入涉及的运行快照与结果文件；保护
不刷新 created_at、不延长沿用有效期；孤儿快照可清理。

memory 持久化模式：清理须与 Runner 同进程共享 guard；跨进程共享结果根
的并发清理在本层拒绝，不宣称进程内锁提供跨进程保护。
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from src.modules.db_deepagent.config import DbAgentConfig
from src.modules.db_deepagent.errors import DbAgentConfigError
from src.modules.db_deepagent.persistence.archive import (
    QaArchive,
    RUN_SNAPSHOT_SCHEMA_VERSION,
    refs_of_record,
)
from src.modules.db_deepagent.persistence.checkpoint import (
    SessionAdvisoryLock,
    parse_thread_id,
)
from src.modules.db_deepagent.persistence.platform_pool import make_platform_conninfo
from src.utils.logger import get_module_logger

logger = get_module_logger("nl2sql.db_deepagent.persistence.maintenance")

_RESULT_REF_RE = re.compile(r"^res_[0-9a-f]{32}$")


class OrphanDeletionOrderError(Exception):
    """孤儿快照删除排序无法覆盖整个删除集（R6/M2 局部防御）。

    仅由 ``_order_orphan_deletions`` 在检测到删除集内存在未满足的来源依赖
    （例如环）时抛出，代表「无法给出消费者先于来源的完整顺序」。调用方
    ``_cleanup_session_resources`` 以窄捕获受控处理：记录原因、跳过本会话
    本轮全部孤儿运行快照实删，不继续使用部分排序结果，也不让异常穿透维护
    流程。这是局部错误类型，不改动共享 errors.py 的异常分层。
    """


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
        memory_in_process: bool = False,
    ) -> CleanupReport:
        """§13.5 + docs/3.1 + doc 6.3 §6.5：会话内线程/归档/Web/R2/R3 清理。

        清理会话＝sessions 记录 ∪ 结果目录 ∪ 归档 ∪ 运行快照 ∪ PG 快照
        中发现的所有会话，统一走同一套会话锁流程（sessions 缺失不代表没有
        运行）；取锁失败跳过该会话的全部清理。R2/R3 按资源所属 session
        分组在同锁内执行（锁内重读归属/年龄/闭包，至删除结束才释放），
        没有循环外的无锁全局删除。before 规范化为 aware UTC 且只收紧不
        放宽（晚于当前时刻按当前时刻）。

        memory 持久化模式不支持跨进程共享结果根的并发清理：默认拒绝；
        与 Runner 同进程的调用方显式传 memory_in_process=True 并共享
        进程内 guard 后方可执行（本方法内结果清理按会话取进程内锁）。
        """
        report = CleanupReport(dry_run=not apply)
        if self._cfg.persistence.mode == "memory" and not memory_in_process:
            raise DbAgentConfigError(
                "memory 持久化模式的结果清理必须与 Runner 同进程执行"
                "（memory_in_process=True 并共享进程内 session guard）；"
                "多进程共享结果根的并发写/清理不受支持"
            )
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
        if self._cfg.persistence.mode == "memory":
            return self._cleanup_memory_mode(
                apply=apply,
                reference_now=reference_now,
                archive_before=archive_before,
                result_retention=result_retention,
                report=report,
            )
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
            # 兜底覆盖“首请求无结果 + P3 写入失败只留 checkpoint”的会话。
            # doc 6.3 §6.5：R2/R3 的资源（结果目录/PG 快照/运行快照）同样按
            # 会话分组，进入同一把会话锁。
            snapshot_sessions = self._archive.list_run_snapshot_sessions()
            pg_snapshot_sessions = self._pg_snapshot_sessions(conn)
            all_sessions = sorted(
                set(known_sessions)
                | set(result_metas)
                | archive_sessions
                | checkpoint_sessions
                | snapshot_sessions
                | pg_snapshot_sessions
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
                    # 2) 归档（第 5 点）：同锁内按保留期清理——先删过期归档，
                    #    保留闭包以仍在保留期的条目为根（§8.2）
                    report.archives.extend(
                        self._archive.purge_archives(
                            session_id=session_id, before=archive_before, apply=apply
                        )
                    )
                    # 3) R2/R3/运行快照（doc 6.3 §6.5）：同锁内重读归属、年龄
                    #    与保留闭包后受控删除
                    self._cleanup_session_resources(
                        session_id,
                        result_metas=result_metas,
                        result_retention=result_retention,
                        archive_before=archive_before,
                        now=reference_now,
                        apply=apply,
                        report=report,
                        conn=conn,
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
        return report

    # ------------------------------------------------------------ memory 模式（R1-04）

    def _cleanup_memory_mode(
        self,
        *,
        apply: bool,
        reference_now: datetime,
        archive_before: datetime,
        result_retention: timedelta,
        report: CleanupReport,
    ) -> CleanupReport:
        """memory 持久化清理（R1-04）：与 Runner 共用进程内会话 guard，
        不连接 PG、不执行 checkpoint/PG 快照清理。

        会话发现＝sessions/归档/运行快照/结果目录；每会话经
        acquire_memory_lease 非阻塞取锁（取不到→跳过该会话全部清理，
        即运行/沿用窗口内的资源不被并发删除）；锁内重读归属与年龄后
        执行归档保留清理与 R2 结果目录清理。
        """
        from src.modules.db_deepagent.persistence.session_guard import (
            acquire_memory_lease,
            release_memory_lease,
        )

        result_metas = self._scan_result_metas()
        sessions = sorted(
            set(self._list_sessions())
            | set(result_metas)
            | self._archive.list_archive_sessions()
            | self._archive.list_run_snapshot_sessions()
        )
        for session_id in sessions:
            if not session_id:
                # 坏元数据目录归属未知：只报告，不按 mtime 强删（§6.5）
                report.skipped.append(
                    {"session_id": "", "reason": "结果目录元数据不可读（跳过）"}
                )
                continue
            lease = acquire_memory_lease(session_id)
            if lease is None:
                report.skipped.append(
                    {
                        "session_id": session_id,
                        "reason": "会话运行中（memory guard），跳过该会话全部清理",
                    }
                )
                continue
            try:
                report.archives.extend(
                    self._archive.purge_archives(
                        session_id=session_id, before=archive_before, apply=apply
                    )
                )
                # 锁内重读归属/年龄（不用锁外枚举快照代替）
                fresh_metas = self._scan_result_metas()
                self._cleanup_session_resources(
                    session_id,
                    result_metas=fresh_metas,
                    result_retention=result_retention,
                    archive_before=archive_before,
                    now=reference_now,
                    apply=apply,
                    report=report,
                    conn=None,
                )
            finally:
                release_memory_lease(lease)
        return report

    # ------------------------------------------------------------ R2/R3（doc 6.3 §6.5）

    def _pg_snapshot_sessions(self, conn) -> set[str]:
        """PG result_snapshots 中出现的会话（会话发现补全）。表不可用返回空。"""
        if self._snapshot_table_ready(conn) is not True:
            return set()
        try:
            rows = conn.execute(
                "SELECT DISTINCT session_id FROM result_snapshots"
            ).fetchall()
        except Exception as exc:  # noqa: BLE001
            logger.warning("result_snapshots 会话枚举失败: %s", exc)
            return set()
        return {row["session_id"] for row in rows if row.get("session_id")}

    def _carried_result_refs(self, session_id: str) -> set[str]:
        """轮换携带证据引用的结果文件（§8.2 保留根之一）。"""
        session = self._archive.get_session(session_id) or {}
        refs: set[str] = set()
        for item in session.get("carried_evidence") or []:
            record = (item or {}).get("record") or {}
            ref = record.get("result_ref") or record.get("output_result_ref")
            if isinstance(ref, str) and ref:
                refs.add(ref)
        return refs

    def _retained_snapshot_roots(
        self, session_id: str, archive_before: datetime
    ) -> tuple[set[str], set[str], list[dict[str, Any]]]:
        """仍在保留期的 QA/run_errors 提供的保护根（R1-05）。

        返回 (root_refs, root_snapshot_keys, problems)：根同时包含公开
        evidence 的 result_ref 与记录保存的 snapshot_ref（完整运行身份键）。
        过期归档不是保护根；读取异常记录为 problems（保守保护）。
        """
        import json as _json

        refs: set[str] = set()
        snapshot_keys: set[str] = set()
        problems: list[dict[str, Any]] = []
        for item in self._archive._iter_archive_items(session_id):  # noqa: SLF001
            value = getattr(item, "value", None) or {}
            created = _as_utc(getattr(item, "created_at", None))
            if created is None or created < archive_before:
                continue  # 已过期归档不是保护根；时间不可判的保守跳过
            try:
                for evidence in value.get("evidence") or []:
                    refs |= refs_of_record(dict(evidence))
                snap_ref = value.get("snapshot_ref")
                if isinstance(snap_ref, Mapping):
                    snapshot_keys.add(
                        _json.dumps(
                            [
                                snap_ref.get("thread_id") or "",
                                snap_ref.get("run_id") or "",
                            ],
                            separators=(",", ":"),
                        )
                    )
            except Exception as exc:  # noqa: BLE001（保护根读取失败报告）
                problems.append(
                    {
                        "session_id": session_id,
                        "reason": f"保护根读取失败: {type(exc).__name__}",
                    }
                )
        return refs, snapshot_keys, problems

    def _protected_closure(
        self,
        session_id: str,
        root_refs: set[str],
        root_snapshot_keys: set[str],
        *,
        edge_out: dict[str, Any] | None = None,
    ) -> tuple[set[str], set[str], list[dict[str, Any]]]:
        """按根集合计算递归保留闭包（§8.2；R1-05 / R2-03 完整血缘核验）。

        图节点＝result_ref（结果文件）与快照键；边＝快照→其全部记录引用
        （refs_of_record，含结构化输入与沿用来源自身的 result_ref），以及
        result_ref→引用它的快照，另加全部 direct_reuse_from / input_result_refs
        的完整来源身份边。根＝仍在保留期的 QA/run_errors 展开的引用与快照 +
        轮换携带引用。BFS 至不动点；不可达快照为孤儿。

        R2-03：协议/作用域/保护根存在性、全部 direct/input 来源存在性、重复
        冲突与环均须核验；任何问题都作为 problems 返回，调用方据此跳过该会话
        的**全部**实删，不降级为不完整闭包继续删除。

        R3-05：**图合法性检查与保护标记分离**。阶段 1 只校验/建图，不向
        protected 写入；阶段 2 的保护集合只从最终 QA/run_errors/携带根出发，
        沿必要的来源关系扩展。全图扫描或资源相同本身不创造新的保留根，
        因此无保留根的过期证据链可回收。

        R4-03：传入 edge_out 时额外暴露阶段 1 已核验的来源图（按快照键），
        供 _cleanup_session_resources 规划依赖一致的孤儿快照删除集合。
        返回 (protected_refs, orphan_snapshot_keys, problems)。
        """
        import json as _json

        from src.modules.db_deepagent.services.reuse_qualification import (
            TASK_LEDGER_PROTOCOL_VERSION,
            SourceUnqualified,
            evidence_edges,
        )

        def _problem(reason: str, **extra: Any) -> None:
            problems.append({"session_id": session_id, "reason": reason, **extra})

        snapshots = self._archive.list_run_snapshots(session_id)
        snapshot_by_key: dict[str, dict[str, Any]] = {}
        snapshot_refs: dict[str, set[str]] = {}
        ref_to_snapshots: dict[str, set[str]] = {}
        ref_to_producers: dict[str, list[tuple[str, str, str, str]]] = {}
        problems: list[dict[str, Any]] = []
        evidence_index: dict[tuple[str, str, str, str], dict[str, Any]] = {}
        record_refs: dict[tuple[str, str, str, str], set[str]] = {}
        source_edges: dict[
            tuple[str, str, str, str], list[tuple[str, str, str, str]]
        ] = {}

        # ---- 阶段 1：合法性检查（只读，不产生任何保留根） ----
        for snapshot in snapshots:
            thread = snapshot.get("thread_id") or ""
            run = snapshot.get("run_id") or ""
            key = _json.dumps([thread, run], separators=(",", ":"))
            snapshot_by_key[key] = snapshot
            version = snapshot.get("run_snapshot_schema_version")
            if type(version) is not int or version != RUN_SNAPSHOT_SCHEMA_VERSION:
                _problem(f"未知运行快照协议版本: {version!r}", run_snapshot=run)
            protocol = snapshot.get("ledger_protocol_version")
            if type(protocol) is not int or protocol != TASK_LEDGER_PROTOCOL_VERSION:
                _problem(f"未知账本协议版本: {protocol!r}", run_snapshot=run)
            if (
                snapshot.get("session_id") != session_id
                or (snapshot.get("thread_id") or "") != thread
                or (snapshot.get("run_id") or "") != run
            ):
                _problem("快照内部作用域与寻址不符", run_snapshot=run)
            refs: set[str] = set()
            for record in [
                *(snapshot.get("executions") or []),
                *(snapshot.get("analyses") or []),
            ]:
                record = dict(record)
                own_refs = refs_of_record(record)
                refs |= own_refs
                ident = (
                    session_id,
                    thread,
                    run,
                    record.get("evidence_id") or "",
                )
                if ident in evidence_index and evidence_index[ident] != record:
                    _problem(
                        f"快照证据 {ident[3]} 存在不同事实的重复记录",
                        run_snapshot=run,
                    )
                evidence_index[ident] = record
                record_refs[ident] = own_refs
                produced = record.get("result_ref") or record.get("output_result_ref")
                if isinstance(produced, str) and produced:
                    ref_to_producers.setdefault(produced, []).append(ident)
            snapshot_refs[key] = refs
            for ref in refs:
                ref_to_snapshots.setdefault(ref, set()).add(key)

        for key in sorted(root_snapshot_keys):
            if key not in snapshot_by_key:
                _problem("保护根快照缺失（缺源）", run_snapshot=key)

        for ident, record in evidence_index.items():
            try:
                edges = evidence_edges(record, session_id=session_id)
            except SourceUnqualified as exc:
                _problem(
                    f"证据 {ident[3]} 血缘解析失败：{exc.reason}",
                    run_snapshot=ident[2],
                )
                source_edges[ident] = []
                continue
            resolved: list[tuple[str, str, str, str]] = []
            for edge in edges:
                ref = edge["evidence_ref"]
                r_session = ref.get("session_id")
                r_thread = ref.get("thread_id")
                r_run = ref.get("run_id")
                r_eid = ref.get("evidence_id")
                if r_session != session_id or not all(
                    isinstance(x, str) and x for x in (r_thread, r_run, r_eid)
                ):
                    _problem("来源引用身份不完整或跨会话", run_snapshot=ident[2])
                    continue
                source_ident = (r_session, r_thread, r_run, r_eid)
                if source_ident not in evidence_index:
                    _problem(
                        f"来源证据 {r_run}:{r_eid} 缺失（缺源）",
                        run_snapshot=ident[2],
                    )
                    continue
                resolved.append(source_ident)
            source_edges[ident] = resolved

        # 环检测（只报告，不标记保护）
        visiting: set[tuple[str, str, str, str]] = set()
        visited: set[tuple[str, str, str, str]] = set()

        def _cycle_walk(ident: tuple[str, str, str, str]) -> None:
            if ident in visiting:
                _problem("证据来源链成环", run_snapshot=ident[2])
                return
            if ident in visited:
                return
            visiting.add(ident)
            for nxt in source_edges.get(ident, ()):
                _cycle_walk(nxt)
            visiting.discard(ident)
            visited.add(ident)

        for ident in evidence_index:
            _cycle_walk(ident)

        # ---- 阶段 2：保护标记只从最终保留根出发扩展 ----
        protected_refs: set[str] = set(root_refs)
        protected_snapshots: set[str] = set(root_snapshot_keys)
        worklist_refs = list(protected_refs)
        worklist_snaps = list(protected_snapshots)
        while worklist_refs or worklist_snaps:
            while worklist_refs:
                ref = worklist_refs.pop()
                for snap_key in ref_to_snapshots.get(ref, ()):
                    if snap_key not in protected_snapshots:
                        protected_snapshots.add(snap_key)
                        worklist_snaps.append(snap_key)
                # 产出该受保护结果的记录，其来源记录的结果文件也需保留
                for ident in ref_to_producers.get(ref, ()):
                    for src in source_edges.get(ident, ()):
                        for src_ref in record_refs.get(src, ()):
                            if src_ref not in protected_refs:
                                protected_refs.add(src_ref)
                                worklist_refs.append(src_ref)
            while worklist_snaps:
                snap_key = worklist_snaps.pop()
                for ref in snapshot_refs.get(snap_key, ()):
                    if ref not in protected_refs:
                        protected_refs.add(ref)
                        worklist_refs.append(ref)
        orphans = set(snapshot_by_key) - protected_snapshots
        # R4-03：把阶段 1 已核验的来源身份边折叠成「快照 → 来源快照」图，
        # 供删除规划复用同一份图（不另写一套血缘解析）。仅暴露跨快照边；
        # 同一快照内记录互引随整份快照一起删，不构成删除依赖。
        if edge_out is not None:

            def _snap_key(ident: tuple[str, str, str, str]) -> str:
                return _json.dumps([ident[1], ident[2]], separators=(",", ":"))

            snapshot_deps: dict[str, set[str]] = {}
            for ident, sources in source_edges.items():
                from_key = _snap_key(ident)
                bucket = snapshot_deps.setdefault(from_key, set())
                for source in sources:
                    source_key = _snap_key(source)
                    if source_key != from_key:
                        bucket.add(source_key)
            edge_out["snapshot_deps"] = snapshot_deps
            edge_out["snapshot_by_key"] = snapshot_by_key
        return protected_refs, orphans, problems

    @staticmethod
    def _plan_orphan_snapshot_deletions(
        orphan_keys: set[str],
        archive_before: datetime,
        snapshot_deps: dict[str, set[str]],
        snapshot_by_key: dict[str, dict[str, Any]],
    ) -> tuple[set[str], set[str]]:
        """R4-03：在现有来源图上规划依赖一致的孤儿运行快照删除集合。

        - 仅「到期」（sealed_at < archive_before）的孤儿进入候选；不刷新
          created_at、不延长业务沿用有效期。
        - 若本轮保留的快照仍引用某个到期来源，则延迟删除该来源；被延迟的
          来源成为新的保留节点，其上游来源同样延迟，递归收敛到不动点。
        - 保证「本轮保留的快照不会指向本轮刚删除的来源」；相关孤儿全部
          到期时一起回收。
        返回 (deletion_set, deferred)。
        """
        expired: set[str] = set()
        for key in orphan_keys:
            snap = snapshot_by_key.get(key)
            sealed = (
                _as_utc(snap.get("sealed_at")) if isinstance(snap, Mapping) else None
            )
            if sealed is not None and sealed < archive_before:
                expired.add(key)
        deletion_set = set(expired)
        # retained 包含所有本轮保留的快照（受保护 + 未到期/被延迟的孤儿）。
        retained = set(snapshot_by_key) - deletion_set
        changed = True
        while changed:
            changed = False
            for kept_key in list(retained):
                for source_key in snapshot_deps.get(kept_key, ()):
                    if source_key in deletion_set:
                        deletion_set.discard(source_key)
                        retained.add(source_key)
                        changed = True
        deferred = expired - deletion_set
        return deletion_set, deferred

    @staticmethod
    def _order_orphan_deletions(
        deletion_set: set[str],
        snapshot_deps: dict[str, set[str]],
    ) -> list[str]:
        """R5-01：返回「消费者先于来源」的孤儿快照删除顺序（稳定拓扑排序）。

        snapshot_deps[consumer] 是 consumer 引用的来源快照键集合。对每条
        consumer→source 边，consumer 必须排在 source 之前：这样某个 consumer
        删除失败/需保留而中止时，它的来源祖先在顺序上尚未被处理，不会被误删
        而制造缺源。共享来源只有在其全部 consumer 都处理完后才进入顺序。
        图已在 _protected_closure 阶段 1 校验为无环（有环会保守停止、不进入
        删除）。R6/M2：正常支持路径下排序应能覆盖整个删除集；若因未来调用
        路径变化出现无法覆盖的情况，明确抛出 OrphanDeletionOrderError，不
        返回按名称拼接、含未满足依赖节点的部分顺序（避免把确定性顺序误当
        成满足依赖的顺序）。
        """
        within = set(deletion_set)
        # 作为来源时，仍需先删除的 consumer 集合；为空才轮到该来源。
        pending: dict[str, set[str]] = {key: set() for key in within}
        for consumer in within:
            for source in snapshot_deps.get(consumer, ()):
                if source in within and source != consumer:
                    pending[source].add(consumer)
        ready = sorted(key for key in within if not pending[key])
        ordered: list[str] = []
        done: set[str] = set()
        queued: set[str] = set(ready)
        while ready:
            key = ready.pop(0)
            queued.discard(key)
            ordered.append(key)
            done.add(key)
            for source in sorted(snapshot_deps.get(key, ())):
                if source in within and source != key:
                    pending[source].discard(key)
                    if (
                        not pending[source]
                        and source not in done
                        and source not in queued
                    ):
                        ready.append(source)
                        ready.sort()
                        queued.add(source)
        # R6/M2：无法覆盖整个删除集时明确失败，不返回带未满足依赖节点的
        # 部分顺序。阶段 1 已对成环保守停止，正常支持路径不应到达此处；
        # 这是针对未来调用路径变化的局部防御，由调用方受控处理。
        if len(ordered) != len(within):
            unresolved = sorted(within - done)
            raise OrphanDeletionOrderError(
                f"删除顺序仅覆盖 {len(ordered)}/{len(within)} 个快照，"
                f"未满足来源依赖: {unresolved}"
            )
        return ordered

    def _cleanup_session_resources(
        self,
        session_id: str,
        *,
        result_metas: dict[str, list[tuple[str, dict[str, Any]]]],
        result_retention: timedelta,
        archive_before: datetime,
        now: datetime,
        apply: bool,
        report: CleanupReport,
        conn,
    ) -> None:
        """同锁内的 R2（结果目录）/R3（PG 快照）/孤儿运行快照清理（§6.5/§8.2）。

        R1-05：锁内重新扫描该会话结果目录（不信任锁外枚举的 meta）；
        保护不刷新 TTL；坏元数据目录（归属不可确定/created_at 不可解析）
        只报告，不按 mtime 强删。
        """
        fresh_metas = self._scan_result_metas()
        session_metas = fresh_metas.get(session_id, [])
        bad_metas = [
            path_name
            for path_name, meta in session_metas
            if _as_utc(meta.get("created_at")) is None
        ]
        for path_name in bad_metas:
            report.skipped.append(
                {
                    "session_id": session_id,
                    "result_ref": path_name,
                    "reason": "结果元数据时间不可解析（跳过，不按 mtime 强删）",
                }
            )
        # R1-05：保护根＝仍在保留期 QA/run_errors 的公开引用 + snapshot_ref
        # + 轮换携带引用；图遍历闭包（ref↔快照双向边）
        root_refs, root_snapshots, closure_problems = self._retained_snapshot_roots(
            session_id, archive_before
        )
        root_refs |= self._carried_result_refs(session_id)
        graph_out: dict[str, Any] = {}
        protected, orphan_snapshot_keys, graph_problems = self._protected_closure(
            session_id, root_refs, root_snapshots, edge_out=graph_out
        )
        all_problems = [*closure_problems, *graph_problems]
        for problem in all_problems:
            report.skipped.append(problem)
        if all_problems:
            # R2-03：保护根或闭包核验出现任何问题，立即保守停止——跳过该
            # 会话后续结果目录、PG 结果快照与孤儿运行快照的全部实删，
            # 不用不完整闭包降级删除。
            report.skipped.append(
                {
                    "session_id": session_id,
                    "reason": (
                        "保护根/闭包核验失败：跳过该会话全部结果目录、PG "
                        "结果快照与孤儿运行快照删除"
                    ),
                }
            )
            return
        # R2：本会话的结果目录（锁内重读的归属与年龄）
        for path_name, meta in session_metas:
            path = Path(self._cfg.workspace.root) / "results" / path_name
            if not path.is_dir():
                continue
            created = _as_utc(meta.get("created_at"))
            if created is None or (now - created) < result_retention:
                continue
            if path_name in protected:
                continue  # 保留闭包内（QA/携带/快照引用）；不刷新 TTL
            if not apply:
                report.result_dirs.append(path_name + " (dry-run)")
                continue
            try:
                shutil.rmtree(path)
                report.result_dirs.append(path_name)
            except OSError as exc:
                report.errors.append(
                    {
                        "session_id": session_id,
                        "result_ref": path_name,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
        # R3：本会话的 PG result_snapshots（排除受保护 refs）；memory
        # 分支 conn=None 跳过（无 PG 资源）。R1-16：保护集为空时省略
        # NOT IN 子句——NOT IN (NULL) 三值逻辑会永假，孤儿快照无法回收。
        if conn is not None and self._snapshot_table_ready(conn) is True:
            exclusion = (
                " AND result_ref NOT IN (" + ", ".join(["%s"] * len(protected)) + ")"
                if protected
                else ""
            )
            params: tuple = (session_id, archive_before, *protected)
            if apply:
                cur = conn.execute(
                    "DELETE FROM result_snapshots WHERE session_id = %s "
                    f"AND last_referenced_at < %s{exclusion}",
                    params,
                )
                report.snapshot_deleted += int(cur.rowcount or 0)
            else:
                row = conn.execute(
                    "SELECT count(*) AS n FROM result_snapshots "
                    f"WHERE session_id = %s AND last_referenced_at < %s{exclusion}",
                    params,
                ).fetchone()
                report.snapshot_deleted += int(row["n"] if row else 0)
        # 孤儿运行快照（R4-03 依赖一致删除集合）：不再逐快照按各自年龄
        # 删除而在跨周期中制造悬空引用。先按现有来源图规划删除集，保证本轮
        # 保留的快照不会指向本轮刚删除的来源；被保留快照引用的到期来源延迟
        # 删除，相关孤儿全部到期后一起回收。
        deletion_set, deferred = self._plan_orphan_snapshot_deletions(
            orphan_snapshot_keys,
            archive_before,
            graph_out.get("snapshot_deps", {}),
            graph_out.get("snapshot_by_key", {}),
        )
        import json as _json

        live = self._archive.list_run_snapshots(session_id)  # 锁内重读
        live_by_key = {
            _json.dumps(
                [s.get("thread_id") or "", s.get("run_id") or ""],
                separators=(",", ":"),
            ): s
            for s in live
        }
        # R5-01：删除集合按「消费者先于来源」的依赖顺序执行，不再按键名
        # sorted(deletion_set)。任何一次删除失败，或锁内复核发现消费者仍需
        # 保留/状态未知时，立即停止该会话本轮剩余孤儿快照删除并记录原因：
        # 其来源祖先在依赖顺序上尚未处理，不会被误删而制造缺源；后端恢复
        # 后下一轮可继续回收。三种结果严格区分：已确认节点不存在（等同已删、
        # 安全继续）、节点仍存在但不应删除（停止）、删除失败/状态未知（停止，
        # 绝不当作删除成功）。这允许暂时延迟独立分支回收，不新增事务框架。
        # R6/M2：排序失败是局部、确定的拒绝信号——窄捕获该专用异常，记录
        # 清晰原因并跳过本会话本轮全部孤儿运行快照实删；不使用部分排序结果，
        # 不让异常穿透维护流程，不撤销本方法此前已完成的结果目录 / PG 快照
        # 清理，也不为整个维护过程新增事务。正常支持路径下不会触发。
        try:
            deletion_order = self._order_orphan_deletions(
                deletion_set, graph_out.get("snapshot_deps", {})
            )
        except OrphanDeletionOrderError as exc:
            report.skipped.append(
                {
                    "session_id": session_id,
                    "reason": (
                        f"孤儿快照删除排序失败：跳过该会话本轮全部孤儿运行"
                        f"快照删除（不使用部分顺序，等待下次重试）: {exc}"
                    ),
                }
            )
            deletion_order = []
        abort_reason: str | None = None
        for key in deletion_order:
            thread_id, run_id = _json.loads(key)
            snapshot = live_by_key.get(key)
            if snapshot is None:
                # 已确认节点不存在（锁内整份重读列表无此键）——等同已删除，
                # 不会产生悬空引用，继续处理其来源。
                continue
            sealed = (
                _as_utc(snapshot.get("sealed_at"))
                if isinstance(snapshot, Mapping)
                else None
            )
            if sealed is None or sealed >= archive_before:
                # 节点仍存在但不应删除，或 sealed_at 未知/坏元数据：保守停止，
                # 不删其来源祖先（消费者先于来源的顺序保证祖先尚未处理）。
                abort_reason = f"锁内复核发现孤儿快照 {run_id} 仍需保留或状态未知"
                break
            if not apply:
                report.skipped.append(
                    {
                        "session_id": session_id,
                        "run_snapshot": run_id,
                        "reason": "孤儿快照待清理 (dry-run)",
                    }
                )
                continue
            # 删除失败/状态未知：绝不当作删除成功，停止本轮剩余删除。
            try:
                self._archive.delete_run_snapshot(session_id, thread_id, run_id)
                report.skipped.append(
                    {
                        "session_id": session_id,
                        "run_snapshot": run_id,
                        "reason": "孤儿快照已清理",
                    }
                )
            except Exception as exc:  # noqa: BLE001
                report.skipped.append(
                    {
                        "session_id": session_id,
                        "run_snapshot": run_id,
                        "reason": f"孤儿快照清理失败: {type(exc).__name__}",
                    }
                )
                abort_reason = f"孤儿快照 {run_id} 删除失败（{type(exc).__name__}）"
                break
        if abort_reason is not None:
            report.skipped.append(
                {
                    "session_id": session_id,
                    "reason": (
                        f"{abort_reason}：停止该会话本轮剩余孤儿快照删除，"
                        "等待下次重试（保证本轮保留快照不指向本轮刚删来源）"
                    ),
                }
            )
        for key in sorted(deferred):
            _, run_id = _json.loads(key)
            report.skipped.append(
                {
                    "session_id": session_id,
                    "run_snapshot": run_id,
                    "reason": (
                        "孤儿快照延迟清理（本轮保留的快照仍引用该来源，"
                        "避免制造悬空引用）"
                    ),
                }
            )

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
