"""Store 归档（docs/v4/5 §13.3）

qa／run_errors／sessions 三个命名空间；写入条件判定在 Runner（条件 a／b），
本模块只负责读写与序列化。§13.5 的归档保留期清理（qa／run_errors 按
archive_retention_days）与结果引用收集也在此实现。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from langgraph.store.base import BaseStore

from src.utils.logger import get_module_logger

logger = get_module_logger("nl2sql.db_deepagent.persistence.archive")

_NS_SESSIONS = ("db_deepagent", "sessions")
_ARCHIVE_PREFIXES = (("db_deepagent", "qa"), ("db_deepagent", "run_errors"))
_PAGE = 100


def _ns(template: str, session_id: str) -> tuple[str, ...]:
    return tuple(part.format(session_id=session_id) for part in template.split("/"))


def refs_of_record(record: dict[str, Any]) -> set[str]:
    """一条执行／分析记录引用的全部 result_ref（§13.5 保护口径统一）。

    执行记录取 result_ref；分析记录取 output_result_ref 与全部
    input_result_refs——分析在原运行已经发生，其输入引用同样必须保护。
    """
    refs: set[str] = set()
    if record.get("result_ref"):
        refs.add(record["result_ref"])
    if record.get("output_result_ref"):
        refs.add(record["output_result_ref"])
    refs.update(ref for ref in record.get("input_result_refs") or [] if ref)
    return refs


def _as_utc(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class QaArchive:
    """问答档案与运行错误的 Store 读写（§13.3）"""

    def __init__(self, store: BaseStore):
        self._store = store

    def write_qa(
        self, session_id: str, request_id: str, record: dict[str, Any]
    ) -> None:
        self._store.put(
            _ns("db_deepagent/qa/{session_id}", session_id), request_id, record
        )

    def write_run_errors(
        self, session_id: str, request_id: str, record: dict[str, Any]
    ) -> None:
        self._store.put(
            _ns("db_deepagent/run_errors/{session_id}", session_id), request_id, record
        )

    def upsert_session(self, session_id: str, record: dict[str, Any]) -> None:
        """sessions 记录整体覆盖写（当前 thread_id、序号、活动时间）。"""
        self._store.put(_NS_SESSIONS, session_id, record)

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        item = self._store.get(_NS_SESSIONS, session_id)
        return dict(item.value) if item is not None else None

    # ------------------------------------------------------------ threads 轨迹（doc 7 §2.1）

    def _threads_ns(self, session_id: str) -> tuple[str, ...]:
        return ("db_deepagent", "threads", session_id)

    def upsert_thread(self, session_id: str, record: dict[str, Any]) -> None:
        thread_id = record["thread_id"]
        existing = self._store.get(self._threads_ns(session_id), thread_id)
        merged = dict(existing.value) if existing is not None else {}
        merged.update(record)
        self._store.put(self._threads_ns(session_id), thread_id, merged)

    def get_thread(self, session_id: str, thread_id: str) -> dict[str, Any] | None:
        item = self._store.get(self._threads_ns(session_id), thread_id)
        return dict(item.value) if item is not None else None

    def list_threads(self, session_id: str) -> list[dict[str, Any]]:
        """分页遍历该会话全部线程轨迹（doc 7 §2.1：不依赖默认第一页）。"""
        out: list[dict[str, Any]] = []
        offset = 0
        while True:
            page = self._store.search(
                self._threads_ns(session_id), limit=_PAGE, offset=offset
            )
            items = getattr(page, "items", None)
            if items is None:  # 兼容直接返回 list 的实现
                items = page
            if not items:
                break
            out.extend(dict(i.value) for i in items)
            if len(items) < _PAGE:
                break
            offset += _PAGE
        return out

    def session_record(
        self,
        *,
        session_id: str,
        thread_id: str,
        thread_seq: int,
        created_at: datetime,
        last_active_at: datetime,
        active: bool,
        carried_evidence: list[Any] | None = None,
    ) -> dict[str, Any]:
        return {
            "session_id": session_id,
            "current_thread_id": thread_id,
            "thread_seq": thread_seq,
            "created_at": created_at.astimezone().isoformat(timespec="seconds"),
            "last_active_at": last_active_at.astimezone().isoformat(timespec="seconds"),
            "active": active,
            "carried_evidence": carried_evidence or [],
            "written_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        }

    # ------------------------------------------------------------ 保留期清理（§13.5）

    def _iter_archive_items(self, session_id: str | None = None):
        """分页遍历 qa／run_errors 条目（含 namespace/key/created_at）。

        session_id 为 None 时遍历全部；否则只遍历该会话的两个命名空间
        （§13.5 按会话在同锁范围内清理）。
        """
        if session_id is None:
            prefixes = _ARCHIVE_PREFIXES
        else:
            prefixes = tuple((*prefix, session_id) for prefix in _ARCHIVE_PREFIXES)
        for prefix in prefixes:
            offset = 0
            while True:
                page = self._store.search(prefix, limit=_PAGE, offset=offset)
                items = getattr(page, "items", page)
                if not items:
                    break
                for item in items:
                    yield item
                if len(items) < _PAGE:
                    break
                offset += _PAGE

    def list_archive_sessions(self) -> set[str]:
        """qa／run_errors 中出现过的会话 ID（孤儿归档清理发现用）。"""
        out: set[str] = set()
        for item in self._iter_archive_items():
            namespace = tuple(getattr(item, "namespace", ()) or ())
            if len(namespace) >= 3:
                out.add(namespace[-1])
        return out

    def collect_result_refs(self, session_id: str | None = None) -> set[str]:
        """qa 归档证据仍引用的 result_ref（结果目录删除保护用，§13.5 第 4 点）。

        证据含执行与分析记录，统一经 refs_of_record 提取（含分析输入引用）。
        """
        refs: set[str] = set()
        for item in self._iter_archive_items(session_id):
            value = getattr(item, "value", None) or {}
            for evidence in value.get("evidence") or []:
                refs |= refs_of_record(dict(evidence))
        return refs

    def purge_archives(
        self, *, session_id: str, before: datetime, apply: bool = True
    ) -> list[tuple[str, str]]:
        """删除该会话中 created_at 早于 before 的 qa／run_errors 条目（§13.5 第 5 点）。

        返回拟删／已删 (qa|run_errors, key) 列表；dry-run（apply=False）只列出。
        """
        targets: list[tuple[tuple[str, ...], str, str]] = []
        for item in self._iter_archive_items(session_id):
            created = _as_utc(getattr(item, "created_at", None))
            if created is None or created >= before:
                continue
            namespace = tuple(getattr(item, "namespace", ()) or ())
            key = getattr(item, "key", "")
            if not key:
                continue
            # kind 取命名空间第二段：db_deepagent/qa|run_errors/<session>
            kind = namespace[1] if len(namespace) > 1 else ""
            targets.append((namespace, key, kind))
        if apply:
            for namespace, key, _kind in targets:
                self._store.delete(namespace, key)
            if targets:
                logger.info(
                    "归档保留期清理：会话 %s 删除 %d 条 qa/run_errors",
                    session_id,
                    len(targets),
                )
        return [(kind, key) for _ns, key, kind in targets]
