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

# 归档契约版本（doc 6.2 §7.4 / v4/5 §13.3）：写读共用，集中定义，不得各处硬编码。
# v1=旧记录（无版本号，answer 保存原模型提交 payload）；v2=新增 delivery_answer/
# answer_source，answer 继续保留原值。新增格式须保留旧版本读取分支并同步测试。
LEGACY_ARCHIVE_SCHEMA_VERSION = 1
ARCHIVE_SCHEMA_VERSION = 2

# doc 6.3 §8.2：运行来源快照独立版本（不与 QA 归档 v2 混用）；未知版本
# 拒绝作为成果来源。
RUN_SNAPSHOT_SCHEMA_VERSION = 1


def _ns(template: str, session_id: str) -> tuple[str, ...]:
    return tuple(part.format(session_id=session_id) for part in template.split("/"))


def refs_of_record(record: dict[str, Any]) -> set[str]:
    """一条执行／分析记录引用的全部 result_ref（§13.5 保护口径统一）。

    执行记录取 result_ref；分析记录取 output_result_ref 与全部
    input_result_refs——分析在原运行已经发生，其输入引用同样必须保护。
    doc 6.3 §8.2：input_result_refs 为结构化条目时从条目提取 result_ref
    字符串（不比较 dict/漏保护输入文件）。
    """
    refs: set[str] = set()
    if record.get("result_ref"):
        refs.add(record["result_ref"])
    if record.get("output_result_ref"):
        refs.add(record["output_result_ref"])
    for entry in record.get("input_result_refs") or []:
        if isinstance(entry, str):
            if entry:
                refs.add(entry)
        elif isinstance(entry, dict):
            ref = entry.get("result_ref")
            if ref:
                refs.add(ref)
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

    @staticmethod
    def read_delivery_answer(record: dict[str, Any]) -> dict[str, Any]:
        """按归档契约版本返回历史交付浅投影（doc 6.2 §7.4）。

        职责边界：仅版本路由与字典容器检查，不验证内容（status/answer/
        findings 是否有效由调用方另行验证）；只改读取副本，不回写、不迁移。

        - v1：answer 的浅拷贝，并把模型提交状态 status 显式改名为
          submitted_status——v1 answer.status 是模型状态，与 v2
          delivery_answer.status（Runner 状态）同名异义，不得混用于展示或
          统计；跨版本状态读取统一用归档顶层 ``record["status"]``。原键
          缺失则不补造；非字典 answer 返回空视图，不改写原档。
        - v2：delivery_answer 的浅拷贝（缺失或非字典容器报错）。
        - 未知版本或非整数版本号报错（bool 不算整数版本）。
        """
        version = record.get("archive_schema_version", LEGACY_ARCHIVE_SCHEMA_VERSION)
        if type(version) is not int:
            raise ValueError("归档契约版本必须为整数")
        if version == LEGACY_ARCHIVE_SCHEMA_VERSION:
            raw = record["answer"] if "answer" in record else {}
            view = dict(raw) if isinstance(raw, dict) else {}
            # v1 的 status 是模型提交状态，不能伪装为 Runner 状态。
            if "status" in view:
                view["submitted_status"] = view.pop("status")
            return view
        if version == ARCHIVE_SCHEMA_VERSION:
            raw = record.get("delivery_answer")
            if not isinstance(raw, dict):
                raise ValueError("归档 v2 缺少字典型 delivery_answer")
            return dict(raw)
        raise ValueError(f"不支持的归档契约版本: {version}")

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
        source_id: str | None = None,
    ) -> dict[str, Any]:
        """sessions 记录构造（doc 4 §5.2：会话创建即绑定 source_id 并在
        upsert 整体覆盖时保留）。"""
        return {
            "session_id": session_id,
            "current_thread_id": thread_id,
            "thread_seq": thread_seq,
            "source_id": source_id,
            "created_at": created_at.astimezone().isoformat(timespec="seconds"),
            "last_active_at": last_active_at.astimezone().isoformat(timespec="seconds"),
            "active": active,
            "carried_evidence": carried_evidence or [],
            "written_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        }

    # ------------------------------------------------------------ 运行来源快照（doc 6.3 §8.2）

    @staticmethod
    def _run_snapshot_ns(session_id: str) -> tuple[str, ...]:
        return ("db_deepagent", "run_snapshots", session_id)

    @staticmethod
    def _run_snapshot_key(thread_id: str, run_id: str) -> str:
        import json as _json

        return _json.dumps(
            [thread_id, run_id], ensure_ascii=False, separators=(",", ":")
        )

    def write_run_snapshot(
        self,
        session_id: str,
        thread_id: str | None,
        run_id: str,
        record: dict[str, Any],
    ) -> None:
        """封存运行来源快照（不可变；相同内容幂等，不同内容冲突报错）。

        写入边界（§8.2）：Runner 装配最终快照后、写 QA/run_errors 前调用；
        持会话 guard。R1-02：写入前核验记录内部 scope 与寻址参数一致，
        不接受把其他会话/运行的记录存进本寻址。
        """
        if (
            record.get("session_id") != session_id
            or record.get("run_id") != run_id
            or (record.get("thread_id") or "") != (thread_id or "")
        ):
            raise ValueError("快照内部作用域与寻址参数不符，拒绝写入")
        namespace = self._run_snapshot_ns(session_id)
        key = self._run_snapshot_key(thread_id or "", run_id)
        existing = self._store.get(namespace, key)
        if existing is not None:
            if dict(existing.value) == record:
                return  # 幂等重写
            raise ValueError(
                f"运行快照冲突：{session_id}/{run_id} 已存在不同内容，不能覆盖"
            )
        self._store.put(namespace, key, record)

    def read_run_snapshot(
        self, session_id: str, thread_id: str | None, run_id: str
    ) -> dict[str, Any] | None:
        """读取并核验快照（R1-02）：内部 scope 与寻址一致、版本为整数 1。

        不一致按损坏拒绝（返回 None 会掩盖串读），抛 ValueError。
        """
        item = self._store.get(
            self._run_snapshot_ns(session_id),
            self._run_snapshot_key(thread_id or "", run_id),
        )
        if item is None:
            return None
        record = dict(item.value)
        version = record.get("run_snapshot_schema_version")
        if type(version) is not int or version != RUN_SNAPSHOT_SCHEMA_VERSION:
            raise ValueError(f"未知运行快照协议版本: {version!r}")
        if (
            record.get("session_id") != session_id
            or record.get("run_id") != run_id
            or (record.get("thread_id") or "") != (thread_id or "")
        ):
            # R2-02：thread 必须同时核对，否则可在 [thread1,run1] 键下注入
            # 内部 thread=WRONG 的记录并骗取读取。
            raise ValueError("快照内部作用域与寻址不符，拒绝读取")
        return record

    def delete_run_snapshot(
        self, session_id: str, thread_id: str | None, run_id: str
    ) -> None:
        """孤儿快照清理（doc 6.3 §8.2：仅清理服务在持会话锁后调用）。"""
        self._store.delete(
            self._run_snapshot_ns(session_id),
            self._run_snapshot_key(thread_id or "", run_id),
        )

    @staticmethod
    def read_task_snapshot(
        snapshot: dict[str, Any], sub_task_id: str, revision: int
    ) -> dict[str, Any] | None:
        """按完整身份读取快照内任务（全部 revision 均封存）。

        R1-02：同 (task, revision) 多条匹配即账本损坏——拒绝并报错，
        不取第一条掩盖歧义。
        """
        version = snapshot.get("run_snapshot_schema_version")
        if type(version) is not int or version != RUN_SNAPSHOT_SCHEMA_VERSION:
            raise ValueError(f"未知运行快照协议版本: {version!r}")
        matched = [
            dict(record)
            for record in snapshot.get("tasks") or []
            if record.get("sub_task_id") == sub_task_id
            and record.get("revision") == revision
        ]
        if len(matched) > 1:
            raise ValueError(
                f"快照内任务 {sub_task_id}@{revision} 存在多条记录（账本损坏）"
            )
        return matched[0] if matched else None

    @staticmethod
    def read_evidence_snapshot(
        snapshot: dict[str, Any], evidence_id: str
    ) -> dict[str, Any] | None:
        """按完整身份读取快照内原始执行/分析记录（含结构化输入血缘）。

        R1-02：同 evidence_id 多条匹配即损坏，拒绝。
        """
        version = snapshot.get("run_snapshot_schema_version")
        if type(version) is not int or version != RUN_SNAPSHOT_SCHEMA_VERSION:
            raise ValueError(f"未知运行快照协议版本: {version!r}")
        matched = [
            dict(record)
            for record in [
                *(snapshot.get("executions") or []),
                *(snapshot.get("analyses") or []),
            ]
            if record.get("evidence_id") == evidence_id
        ]
        if len(matched) > 1:
            raise ValueError(f"快照内证据 {evidence_id} 存在多条记录（损坏）")
        return matched[0] if matched else None

    def list_run_snapshots(self, session_id: str) -> list[dict[str, Any]]:
        """该会话全部运行快照（清理闭包与孤儿发现用）。"""
        out: list[dict[str, Any]] = []
        offset = 0
        while True:
            page = self._store.search(
                self._run_snapshot_ns(session_id), limit=_PAGE, offset=offset
            )
            items = getattr(page, "items", page)
            if not items:
                break
            out.extend(dict(i.value) for i in items)
            if len(items) < _PAGE:
                break
            offset += _PAGE
        return out

    def list_run_snapshot_sessions(self) -> set[str]:
        """run_snapshots 命名空间中出现的会话 ID（会话发现补全，§8.2）。"""
        out: set[str] = set()
        offset = 0
        while True:
            page = self._store.search(
                ("db_deepagent", "run_snapshots"), limit=_PAGE, offset=offset
            )
            items = getattr(page, "items", page)
            if not items:
                break
            for item in items:
                namespace = tuple(getattr(item, "namespace", ()) or ())
                if len(namespace) >= 3:
                    out.add(namespace[-1])
            if len(items) < _PAGE:
                break
            offset += _PAGE
        return out

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
