"""Web 会话登记与文本轮次日志（doc 2 §4.4）。

两个独立 Store 命名空间，不在 Runner 整体覆盖的通用 sessions 记录里夹带
Web 元数据，也不新建 SQL 会话表：

- Web 登记：namespace ``("db_deepagent", "web_sessions", "guest")``，key 为
  服务端 session_id。value 保存 owner/origin、session_id/source_id、原始
  created_at、title、登记字段指纹、knowledge_snapshot，以及持久化
  state/invalid_reason/invalidated_at/pending_run_id 与可修复的
  turn_count/last_active_at、metadata_reconciled_at、
  metadata_reconcile_retry_after 缓存。
- 文本日志：namespace ``("db_deepagent", "web_turns", session_id)``，key 为
  权威 request_id。value 保存原文 question、非空白 answer、与 key 一致的
  request_id、run_id、终态、带时区 saved_at，以及可选的 display 回放投影
  （elapsed_ms 与 sql/result_ref）。旧记录没有 display 时历史只回问答文本。

展示字段完整性先于 SessionInfo 构造；状态字段不完整走固定安全投影。
GET 不为补字段写 Store。所有时间均为带时区 ISO 8601。
"""

from __future__ import annotations

import threading
import time as time_mod
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

from langgraph.store.base import BaseStore

from src.utils.logger import get_module_logger

logger = get_module_logger("nl2sql.api.v2.webstore")

WEB_SESSIONS_NS = ("db_deepagent", "web_sessions", "guest")
# Runner 整体覆盖的通用 sessions 命名空间（doc 2 §4.4：Web 登记不夹带其中）
GENERIC_SESSIONS_NS = ("db_deepagent", "sessions")
DEFAULT_TITLE = "新对话"
TITLE_MAX_CODEPOINTS = 40

# 列表/轮次扫描上限（doc 2 §4.2）：最多 10000 条原始记录、最长 5 秒
SCAN_MAX_RECORDS = 10000
SCAN_MAX_SECONDS = 5.0
_PAGE = 100

# 元数据缓存新鲜度与失败退避（doc 2 §4.4）
RECONCILE_FRESH_SECONDS = 300.0
RECONCILE_FUTURE_TOLERANCE_SECONDS = 60.0
RETRY_AFTER_SECONDS = 300.0
RETRY_AFTER_MAX_FUTURE_SECONDS = 3600.0


def web_turns_ns(session_id: str) -> tuple[str, ...]:
    return ("db_deepagent", "web_turns", session_id)


def parse_tz_datetime(value: Any) -> Optional[datetime]:
    """带时区时间解析；缺失/非字符串/无时区/非法返回 None。"""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def valid_source_uuid(value: Any) -> bool:
    """数据源短 ID 校验（doc 6 §1：db_src_N；函数名保留历史命名）。"""
    from src.modules.db_deepagent.source_registry import SOURCE_ID_RE

    return isinstance(value, str) and bool(SOURCE_ID_RE.match(value))


def derive_title(question: str) -> str:
    """首问 strip() 后取前 40 个 Unicode 码点；仍为空用“新对话”。"""
    stripped = question.strip()
    if not stripped:
        return DEFAULT_TITLE
    return stripped[:TITLE_MAX_CODEPOINTS] or DEFAULT_TITLE


def web_turn_valid(key: str, value: Any) -> bool:
    """web_turn 共用的历史有效性谓词（doc 2 §4.4）：question/answer 均为
    strip() 后非空字符串；request_id 非空、满足 ID 契约且与 Store key 一致；
    saved_at 为合法带时区时间。run_id/终态是审计字段，不参与判定。"""
    from src.modules.db_deepagent.persistence.checkpoint import valid_web_id

    if not isinstance(value, dict):
        return False
    question, answer = value.get("question"), value.get("answer")
    if not (
        isinstance(question, str)
        and question.strip()
        and isinstance(answer, str)
        and answer.strip()
    ):
        return False
    request_id = value.get("request_id")
    if not isinstance(request_id, str) or not request_id:
        return False
    if not valid_web_id(request_id) or request_id != key:
        return False
    return parse_tz_datetime(value.get("saved_at")) is not None


def normalize_turn_display(display: Any) -> dict:
    """把查询结果收成可回放投影。多余字段（operation、告警原文等）不入库。"""
    elapsed = None
    evidence: list[dict] = []
    if isinstance(display, dict):
        raw_elapsed = display.get("elapsed_ms")
        if (
            isinstance(raw_elapsed, int)
            and not isinstance(raw_elapsed, bool)
            and raw_elapsed >= 0
        ):
            elapsed = raw_elapsed
        raw_items = display.get("evidence")
        if isinstance(raw_items, list):
            for item in raw_items:
                if not isinstance(item, dict):
                    continue
                sql = item.get("sql")
                ref = item.get("result_ref")
                row_count = item.get("row_count")
                evidence.append(
                    {
                        "sql": sql if isinstance(sql, str) and sql.strip() else None,
                        "result_ref": ref if isinstance(ref, str) and ref else None,
                        "row_count": (
                            row_count
                            if isinstance(row_count, int)
                            and not isinstance(row_count, bool)
                            and row_count >= 0
                            else 0
                        ),
                        "source_truncated": item.get("source_truncated") is True,
                    }
                )
    return {"elapsed_ms": elapsed, "evidence": evidence}


def public_turn(record: dict) -> dict:
    """历史接口条目。display 缺失或损坏时退回空投影，不让整页历史失败。"""
    display = normalize_turn_display(record.get("display"))
    saved_at = record.get("saved_at")
    return {
        "question": record["question"],
        "answer": record["answer"],
        "saved_at": saved_at if isinstance(saved_at, str) else None,
        "elapsed_ms": display["elapsed_ms"],
        "evidence": display["evidence"],
    }


def display_from_result(result: Any) -> dict:
    """从本轮查询结果提取回放投影。一致性未确认的空证据会得到空列表。"""
    stats = getattr(result, "stats", None)
    elapsed = getattr(stats, "elapsed_ms", None)
    items = []
    for item in getattr(result, "evidence", None) or []:
        items.append(
            {
                "sql": getattr(item, "sql", None),
                "result_ref": getattr(item, "result_ref", None),
                "row_count": getattr(item, "row_count", 0),
                "source_truncated": getattr(item, "source_truncated", False),
            }
        )
    return normalize_turn_display({"elapsed_ms": elapsed, "evidence": items})


@dataclass
class ScanBudget:
    """扫描预算：条数与剩余时间双上限（doc 2 §4.2）。"""

    max_records: int = SCAN_MAX_RECORDS
    deadline: float = 0.0

    @classmethod
    def create(
        cls, *, max_records: int = SCAN_MAX_RECORDS, seconds: float = SCAN_MAX_SECONDS
    ) -> "ScanBudget":
        return cls(max_records=max_records, deadline=time_mod.monotonic() + seconds)

    def consume(self, count: int = 1) -> None:
        if count > self.max_records:
            raise _ScanLimitExceeded()
        self.max_records -= count
        if time_mod.monotonic() > self.deadline:
            raise _ScanLimitExceeded()


class _ScanLimitExceeded(Exception):
    """达到扫描上限（条数/时间）：整次读取按 503/history_unavailable。"""


# 排序兜底时间（display_field_problems 通过后 last_active 必非 None，
# 类型上仍为 Optional，排序键不允许 None）
_MIN_TIME = datetime(1, 1, 1, tzinfo=timezone.utc)


class StoreUnavailable(Exception):
    """Store 读取/写入故障：503，不伪装成空历史或忙碌。"""


class SessionMetadataInvalid(Exception):
    """Web 登记展示字段缺失/非法（doc 2 §4.2）：详情与新查询均拒绝。"""

    def __init__(self, session_id: str, problem_fields: list[str]):
        self.session_id = session_id
        self.problem_fields = problem_fields
        super().__init__(
            f"会话展示信息不完整: {session_id}（问题字段: {', '.join(problem_fields)}）"
        )


def _search_page(store: BaseStore, namespace: tuple[str, ...], offset: int) -> list:
    try:
        page = store.search(namespace, limit=_PAGE, offset=offset)
    except Exception as exc:  # noqa: BLE001（Store 故障统一 503）
        raise StoreUnavailable(f"Store 读取失败: {type(exc).__name__}") from exc
    items = getattr(page, "items", None)
    return list(items) if items is not None else list(page)


def _store_put(
    store: BaseStore, namespace: tuple[str, ...], key: str, value: dict
) -> None:
    try:
        store.put(namespace, key, value)
    except Exception as exc:  # noqa: BLE001
        raise StoreUnavailable(f"Store 写入失败: {type(exc).__name__}") from exc


def _store_delete(store: BaseStore, namespace: tuple[str, ...], key: str) -> None:
    try:
        store.delete(namespace, key)
    except Exception as exc:  # noqa: BLE001
        raise StoreUnavailable(f"Store 删除失败: {type(exc).__name__}") from exc


# ----------------------------------------------------------------------------
# 登记记录的展示/状态字段校验与安全投影
# ----------------------------------------------------------------------------


def display_field_problems(record: dict) -> list[str]:
    """展示字段完整性检查（doc 2 §4.2）：created_at/last_active_at 合法带时区
    且 last ≥ created；title 非空白；turn_count 非负整数（不接受 bool）。"""
    problems: list[str] = []
    created = parse_tz_datetime(record.get("created_at"))
    last_active = parse_tz_datetime(record.get("last_active_at"))
    if created is None:
        problems.append("created_at")
    if last_active is None:
        problems.append("last_active_at")
    if created is not None and last_active is not None and last_active < created:
        problems.append("last_active_at<created_at")
    title = record.get("title")
    if not isinstance(title, str) or not title.strip():
        problems.append("title")
    turn_count = record.get("turn_count")
    if (
        isinstance(turn_count, bool)
        or not isinstance(turn_count, int)
        or turn_count < 0
    ):
        problems.append("turn_count")
    return problems


def state_projection(record: dict) -> dict:
    """状态字段读取投影（doc 2 §4.2 不变量）：字段齐全且组合一致时返回
    原值；缺失/非法/矛盾时固定投影 invalidated/session_binding_missing/
    continuable=false/invalidated_at=null（该 null 仅表达读取投影，不是新写
    入失效登记可省略时间的规则）。GET 调用本函数不写 Store。"""
    state = record.get("state")
    reason = record.get("invalid_reason")
    invalidated_at = parse_tz_datetime(record.get("invalidated_at"))
    has_pending_key = "pending_run_id" in record
    pending = record.get("pending_run_id")
    pending_ok = has_pending_key and (
        pending is None
        or (
            isinstance(pending, str)
            and (pending.startswith("req_") or pending.startswith("create_"))
            and len(pending) <= 64
            and all(c.isalnum() or c in "_-" for c in pending)
        )
    )
    valid_reasons = {
        "knowledge_changed",
        "dependency_unavailable",
        "source_changed",
        "session_binding_missing",
    }
    structurally_ok = pending_ok and (
        (state == "active" and reason is None and invalidated_at is None)
        or (
            state == "invalidated"
            and isinstance(reason, str)
            and reason in valid_reasons
            and invalidated_at is not None
        )
    )
    if not structurally_ok:
        return {
            "state": "invalidated",
            "invalid_reason": "session_binding_missing",
            "invalidated_at": None,
            "pending_run_id": None,
            "continuable": False,
            "state_valid": False,
        }
    continuable = state == "active" and pending is None
    return {
        "state": state,
        "invalid_reason": reason,
        "invalidated_at": (
            invalidated_at.isoformat() if invalidated_at is not None else None
        ),
        "pending_run_id": pending,
        "continuable": continuable,
        "state_valid": True,
    }


def core_ownership_ok(record: Any, session_id: str) -> bool:
    """核心 Web 归属核对（doc 2 §4.2 P1 读取约束）：owner/origin、登记
    session_id 与路径一致、source_id 是合法 UUID。不要求展示/状态字段完整。"""
    if not isinstance(record, dict):
        return False
    return (
        record.get("owner") == "guest"
        and record.get("origin") == "web"
        and record.get("session_id") == session_id
        and valid_source_uuid(record.get("source_id"))
    )


# ----------------------------------------------------------------------------
# WebStore：登记与文本日志的读写门面
# ----------------------------------------------------------------------------


class WebStore:
    """guest 的 Web 会话登记 + 文本轮次日志（doc 2 §4.4）。

    只做投影与校验，不擅自修复；所有写入口由调用方在会话锁内使用。
    """

    def __init__(
        self, store: BaseStore, *, now: Optional[Callable[[], datetime]] = None
    ):
        self._store = store
        self._now = now or (lambda: datetime.now().astimezone())
        # 进程内校正抑制（doc 2 §4.4）：session_id → (单调到期, 持久化时间快照)
        self._suppress: dict[str, tuple[float, tuple[Any, Any]]] = {}
        self._suppress_lock = threading.Lock()

    # ------------------------------------------------------------ 通用绑定

    def now_iso(self) -> str:
        """当前带时区时间 ISO 字符串（与登记写入同一时钟）。"""
        return self._now().isoformat()

    def generic_session_exists(self, session_id: str) -> bool:
        """通用 sessions 记录是否存在（创建碰撞检查用，只读）。"""
        try:
            item = self._store.get(GENERIC_SESSIONS_NS, session_id)
        except Exception as exc:  # noqa: BLE001
            raise StoreUnavailable(f"Store 读取失败: {type(exc).__name__}") from exc
        return item is not None

    def put_generic_session(self, session_id: str, record: dict) -> None:
        """写入通用 sessions 绑定（doc 2 §4.4 创建写序第一步；Runner 后续
        整体覆盖时经 archive.upsert_session 保留 source_id）。"""
        _store_put(self._store, GENERIC_SESSIONS_NS, session_id, record)

    # ------------------------------------------------------------ 登记

    def get_registration(self, session_id: str) -> Optional[dict]:
        try:
            item = self._store.get(WEB_SESSIONS_NS, session_id)
        except Exception as exc:  # noqa: BLE001
            raise StoreUnavailable(f"Store 读取失败: {type(exc).__name__}") from exc
        return dict(item.value) if item is not None else None

    def put_registration(self, session_id: str, record: dict) -> None:
        _store_put(self._store, WEB_SESSIONS_NS, session_id, record)

    def new_registration(
        self,
        *,
        session_id: str,
        source_id: str,
        source_fingerprint: str,
        knowledge_snapshot: dict,
        creation_id: str,
    ) -> dict:
        """创建时的 Web 登记记录：state=active/null/null，pending=creation_id。"""
        now_iso = self._now().isoformat()
        return {
            "owner": "guest",
            "origin": "web",
            "session_id": session_id,
            "source_id": source_id,
            "created_at": now_iso,
            "title": DEFAULT_TITLE,
            "last_active_at": now_iso,
            "turn_count": 0,
            "source_fingerprint": source_fingerprint,
            "knowledge_snapshot": knowledge_snapshot,
            "state": "active",
            "invalid_reason": None,
            "invalidated_at": None,
            "pending_run_id": creation_id,
            "metadata_reconciled_at": None,
            "metadata_reconcile_retry_after": None,
        }

    def invalidate(
        self,
        session_id: str,
        record: dict,
        reason: str,
        *,
        keep_pending: bool = True,
    ) -> None:
        """锁内失效写入（doc 2 §4.4）：同一次写入保存 state/invalid_reason/
        invalidated_at，保留 pending 等其余字段（含未修改的元数据缓存字段）；
        首次 invalidated_at 不被后续请求改写。"""
        if record.get("state") == "invalidated":
            return  # 已失效：不覆盖首次原因与时间
        now_iso = self._now().isoformat()
        record = dict(record)
        record["state"] = "invalidated"
        record["invalid_reason"] = reason
        record["invalidated_at"] = now_iso
        if not keep_pending and "pending_run_id" in record:
            record["pending_run_id"] = None
        self.put_registration(session_id, record)

    def set_pending(
        self, session_id: str, record: dict, pending: Optional[str]
    ) -> None:
        """确认写入 pending（doc 2 §4.4）：不清除/改写元数据缓存字段。"""
        record = dict(record)
        record["pending_run_id"] = pending
        self.put_registration(session_id, record)

    def clear_pending(self, session_id: str, record: dict) -> None:
        self.set_pending(session_id, record, None)

    # ------------------------------------------------------------ 会话投影

    def session_info(self, session_id: str, record: dict) -> dict:
        """构造 SessionInfo（doc 2 §4.2）：展示字段完整性先于构造，缺失/非法
        抛 SessionMetadataInvalid；状态字段走安全投影。"""
        problems = display_field_problems(record)
        if problems:
            raise SessionMetadataInvalid(session_id, problems)
        projection = state_projection(record)
        return {
            "session_id": session_id,
            "source_id": record.get("source_id"),
            "title": record["title"],
            "created_at": record["created_at"],
            "last_active_at": record["last_active_at"],
            "turn_count": record["turn_count"],
            "state": projection["state"],
            "continuable": projection["continuable"],
            "invalid_reason": projection["invalid_reason"],
            "invalidated_at": projection["invalidated_at"],
        }

    def list_session_infos(
        self, *, source_id: Optional[str] = None, limit: int = 30, offset: int = 0
    ) -> tuple[list[dict], bool, Optional[int], list[dict]]:
        """枚举 guest Web 会话并分页（doc 2 §4.2）。

        返回 (items, has_more, next_offset, skipped_invalid)；offset 是过滤
        排序后的有效条目偏移，不是原始 Store 键偏移；损坏条目跳过并返回
        问题字段；达到扫描上限抛 _ScanLimitExceeded（调用方 503）。空会话
        也显示（含完整失效会话）。
        """
        budget = ScanBudget.create()
        collected: list[tuple[Optional[datetime], str, dict]] = []
        skipped: list[dict] = []
        scan_offset = 0
        while True:
            items = _search_page(self._store, WEB_SESSIONS_NS, scan_offset)
            if not items:
                break
            budget.consume(len(items))
            for item in items:
                record = dict(item.value) if hasattr(item, "value") else dict(item)
                if not core_ownership_ok(record, item.key):
                    continue
                if source_id is not None and record.get("source_id") != source_id:
                    continue
                if display_field_problems(record):
                    skipped.append(
                        {
                            "session_id": item.key,
                            "reason": "session_metadata_invalid",
                            "problem_fields": display_field_problems(record),
                        }
                    )
                    continue
                last_active = parse_tz_datetime(record.get("last_active_at"))
                collected.append((last_active, item.key, record))
            if len(items) < _PAGE:
                break
            scan_offset += _PAGE
        # last_active_at 降序、session_id 升序打破同刻并列：先按 session_id
        # 升序排，再稳定排序按时间倒序（并列保持 session_id 升序）
        collected.sort(key=lambda entry: entry[1])
        collected.sort(key=lambda entry: entry[0] or _MIN_TIME, reverse=True)
        page = collected[offset : offset + limit + 1]
        has_more = len(page) > limit
        page = page[:limit]
        infos = [self.session_info(key, rec) for _t, key, rec in page]
        next_offset = offset + len(page) if has_more else None
        return infos, has_more, next_offset, skipped

    # ------------------------------------------------------------ 文本轮次

    def write_turn(
        self,
        session_id: str,
        request_id: str,
        *,
        question: str,
        answer: str,
        run_id: str,
        status: str,
        display: Optional[dict] = None,
    ) -> None:
        saved_at = self._now().isoformat()
        _store_put(
            self._store,
            web_turns_ns(session_id),
            request_id,
            {
                "question": question,
                "answer": answer,
                "request_id": request_id,
                "run_id": run_id,
                "status": status,
                "saved_at": saved_at,
                "display": normalize_turn_display(display),
            },
        )

    def read_turn(self, session_id: str, request_id: str) -> Optional[dict]:
        try:
            item = self._store.get(web_turns_ns(session_id), request_id)
        except Exception as exc:  # noqa: BLE001
            raise StoreUnavailable(f"Store 读取失败: {type(exc).__name__}") from exc
        return dict(item.value) if item is not None else None

    def list_turns(
        self, session_id: str, *, limit: int = 50, offset: int = 0
    ) -> tuple[list[dict], bool, Optional[int]]:
        """全量遍历该会话保留日志 → 校验/排序/分页（doc 2 §4.2）。

        发现最小有效记录判定失败立即抛 SessionMetadataInvalid 语义的
        坏记录错误（调用方 500/history_record_invalid）；仅有资源上限未完成
        时抛 _ScanLimitExceeded（503）。轮次按 saved_at 升序、request_id 升序。
        """
        budget = ScanBudget.create()
        collected: list[tuple[Optional[datetime], str, dict]] = []
        scan_offset = 0
        while True:
            items = _search_page(self._store, web_turns_ns(session_id), scan_offset)
            if not items:
                break
            budget.consume(len(items))
            for item in items:
                record = dict(item.value) if hasattr(item, "value") else dict(item)
                if not web_turn_valid(item.key, record):
                    raise _InvalidTurnRecord(session_id, item.key)
                saved_at = parse_tz_datetime(record["saved_at"])
                collected.append((saved_at, item.key, record))
            if len(items) < _PAGE:
                break
            scan_offset += _PAGE
        collected.sort(key=lambda entry: (entry[0], entry[1]))
        page = collected[offset : offset + limit + 1]
        has_more = len(page) > limit
        page = page[:limit]
        turns = [public_turn(rec) for _t, _k, rec in page]
        next_offset = offset + len(page) if has_more else None
        return turns, has_more, next_offset

    def iter_turn_records(self, session_id: str) -> list[tuple[str, dict]]:
        """全量读取（maintenance/校正共用）：坏记录抛 _InvalidTurnRecord，
        超限抛 _ScanLimitExceeded；不排序，调用方自行处理。"""
        budget = ScanBudget.create()
        out: list[tuple[str, dict]] = []
        scan_offset = 0
        while True:
            items = _search_page(self._store, web_turns_ns(session_id), scan_offset)
            if not items:
                break
            budget.consume(len(items))
            for item in items:
                record = dict(item.value) if hasattr(item, "value") else dict(item)
                if not web_turn_valid(item.key, record):
                    raise _InvalidTurnRecord(session_id, item.key)
                out.append((item.key, record))
            if len(items) < _PAGE:
                break
            scan_offset += _PAGE
        return out

    def delete_turn(self, session_id: str, request_id: str) -> None:
        _store_delete(self._store, web_turns_ns(session_id), request_id)

    def list_turn_session_ids(self) -> set[str]:
        """web_turns 命名空间中出现的会话 ID（maintenance 发现用）。"""
        # 枚举父命名空间 ("db_deepagent","web_turns")：Store.search 按前缀匹配
        out: set[str] = set()
        offset = 0
        while True:
            items = _search_page(self._store, ("db_deepagent", "web_turns"), offset)
            if not items:
                break
            for item in items:
                ns = tuple(getattr(item, "namespace", ()) or ())
                if len(ns) >= 3:
                    out.add(ns[2])
            if len(items) < _PAGE:
                break
            offset += _PAGE
        return out

    def list_web_session_keys(self) -> list[str]:
        out: list[str] = []
        offset = 0
        while True:
            items = _search_page(self._store, WEB_SESSIONS_NS, offset)
            if not items:
                break
            out.extend(item.key for item in items)
            if len(items) < _PAGE:
                break
            offset += _PAGE
        return out

    # ------------------------------------------------------------ 元数据缓存

    def reconcile_metadata(self, session_id: str, record: dict) -> dict:
        """锁内全量校正（doc 2 §4.4 回退规则，简化实现）。

        遍历并校验全部保留日志后一次更新：turn_count=有效条数（无文本为 0）；
        已有合法标题保留，缺失/非法按首问派生；last_active_at 取合法既有
        活动时间、保留日志活动时间与 created_at 的最大值。成功写入
        metadata_reconciled_at=当前时间并清除 retry_after；坏记录/超限抛错，
        不覆盖缓存。返回更新后的记录。
        """
        records = self.iter_turn_records(session_id)  # 坏记录/超限在此抛出
        # 首问派生需按保存时间正序（§4.4：第一条已保存问答）
        records.sort(
            key=lambda item: (
                parse_tz_datetime(item[1].get("saved_at")) or _MIN_TIME,
                item[0],
            )
        )
        created = parse_tz_datetime(record.get("created_at"))
        if created is None:
            raise SessionMetadataInvalid(session_id, ["created_at"])
        updated = dict(record)
        updated["turn_count"] = len(records)
        title = record.get("title")
        if not isinstance(title, str) or not title.strip():
            first_question = records[0][1]["question"] if records else ""
            updated["title"] = derive_title(first_question)
        times = [created]
        existing_last = parse_tz_datetime(record.get("last_active_at"))
        if existing_last is not None and existing_last >= created:
            times.append(existing_last)
        for _key, rec in records:
            saved = parse_tz_datetime(rec.get("saved_at"))
            if saved is not None:
                times.append(saved)
        updated["last_active_at"] = max(times).isoformat()
        updated["metadata_reconciled_at"] = self._now().isoformat()
        updated["metadata_reconcile_retry_after"] = None
        self.put_registration(session_id, updated)
        return updated

    def mark_reconcile_failure(self, session_id: str, record: dict) -> None:
        """校正失败：持有登记可写时设 retry_after=当前+300 秒（doc 2 §4.4）。"""
        updated = dict(record)
        updated["metadata_reconcile_retry_after"] = (
            self._now() + timedelta(seconds=RETRY_AFTER_SECONDS)
        ).isoformat()
        self.put_registration(session_id, updated)

    def reconcile_needed(self, record: dict, *, now: Optional[datetime] = None) -> bool:
        """读取时判定是否安排校正（doc 2 §4.4）。

        - metadata_reconciled_at 缺失 → 需要（首次列表/详情的全量校正）；
        - 解析失败/无时区/未来偏差严格大于 60 秒 → 视为未命中；
        - 超过 300 秒 → 过期；
        - retry_after 合法且未到期（未来不超过 1 小时）→ 暂不重试；
        - 其余非法 retry_after 按无退避处理。
        """
        now = now or self._now()
        reconciled = parse_tz_datetime(record.get("metadata_reconciled_at"))
        if reconciled is None:
            stale = True
        else:
            if (reconciled - now).total_seconds() > RECONCILE_FUTURE_TOLERANCE_SECONDS:
                stale = True
            else:
                stale = (now - reconciled).total_seconds() > RECONCILE_FRESH_SECONDS
        retry_after = parse_tz_datetime(record.get("metadata_reconcile_retry_after"))
        if retry_after is not None:
            ahead = (retry_after - now).total_seconds()
            if 0 < ahead <= RETRY_AFTER_MAX_FUTURE_SECONDS:
                return False  # 退避中
        return stale or display_field_problems(record) != []

    # ------------------------------------------------------------ 进程内抑制

    def suppressed(self, session_id: str, record: dict) -> bool:
        """按需校正的进程内抑制（doc 2 §4.4 简化实现）：持久化时间值变化
        即解除；否则最多持续 300 秒（单调时钟，不受墙钟回拨影响）。"""
        with self._suppress_lock:
            entry = self._suppress.get(session_id)
        if entry is None:
            return False
        deadline, snapshot = entry
        current = (
            record.get("metadata_reconciled_at"),
            record.get("metadata_reconcile_retry_after"),
        )
        if current != snapshot:
            with self._suppress_lock:
                self._suppress.pop(session_id, None)
            return False
        if time_mod.monotonic() > deadline:
            with self._suppress_lock:
                self._suppress.pop(session_id, None)
            return False
        return True

    def mark_suppressed(self, session_id: str, record: dict) -> None:
        with self._suppress_lock:
            self._suppress[session_id] = (
                time_mod.monotonic() + RECONCILE_FRESH_SECONDS,
                (
                    record.get("metadata_reconciled_at"),
                    record.get("metadata_reconcile_retry_after"),
                ),
            )


class _InvalidTurnRecord(Exception):
    """Web 文本轮次记录损坏（doc 2 §4.4）：整次 turns 请求失败并报告。"""

    def __init__(self, session_id: str, key: str):
        self.session_id = session_id
        self.key = key
        super().__init__(f"Web 文本轮次记录损坏: {session_id}/{key}")


# 包内路由/维护直接引用的公开别名（语义即私有异常的稳定名）
ScanLimitExceeded = _ScanLimitExceeded
InvalidTurnRecord = _InvalidTurnRecord
