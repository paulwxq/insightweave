"""ResultStore：结果文件的原子保存与按会话读取（docs/v4/5 §6.8）

目录 <workspace>/results/<result_ref>/，与 backend 根目录互不包含。写入先落
临时目录再原子重命名；读取校验 rows_sha256；跨会话访问不泄露存在性（P10）。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import uuid
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional, Sequence

from src.modules.db_deepagent.context import DbAgentContext
from src.modules.db_deepagent.errors import ResultStoreError
from src.modules.db_deepagent.schemas import ColumnMeta, GuardEvidenceSnapshot
from src.utils.logger import get_module_logger

logger = get_module_logger("nl2sql.db_deepagent.result_store")

_RESULT_REF_RE = re.compile(r"^res_[0-9a-f]{32}$")


@dataclass(frozen=True)
class ResultMeta:
    """meta.json 的强类型视图（§6.8）；datasource_id 写入后不再修改"""

    result_ref: str
    session_id: str
    thread_id: str
    request_id: str
    run_id: str
    purpose: str
    datasource_id: str
    sql_template: Optional[str]
    executed_sql: Optional[str]
    executed_sql_sha256: str
    columns: list[ColumnMeta]
    saved_row_count: int
    source_truncated: bool
    created_at: str
    rows_sha256: str
    derived_from: list[str]
    guard_evidence: GuardEvidenceSnapshot

    def to_json(self) -> dict[str, Any]:
        return {
            "result_ref": self.result_ref,
            "session_id": self.session_id,
            "thread_id": self.thread_id,
            "request_id": self.request_id,
            "run_id": self.run_id,
            "purpose": self.purpose,
            "datasource_id": self.datasource_id,
            "sql_template": self.sql_template,
            "executed_sql": self.executed_sql,
            "executed_sql_sha256": self.executed_sql_sha256,
            "columns": self.columns,
            "saved_row_count": self.saved_row_count,
            "source_truncated": self.source_truncated,
            "created_at": self.created_at,
            "rows_sha256": self.rows_sha256,
            "derived_from": self.derived_from,
            "guard_evidence": self.guard_evidence,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "ResultMeta":
        return cls(
            result_ref=data["result_ref"],
            session_id=data["session_id"],
            thread_id=data["thread_id"],
            request_id=data["request_id"],
            run_id=data["run_id"],
            purpose=data["purpose"],
            datasource_id=data["datasource_id"],
            sql_template=data.get("sql_template"),
            executed_sql=data.get("executed_sql"),
            executed_sql_sha256=data.get("executed_sql_sha256", ""),
            columns=data.get("columns", []),
            saved_row_count=data.get("saved_row_count", 0),
            source_truncated=data.get("source_truncated", False),
            created_at=data.get("created_at", ""),
            rows_sha256=data.get("rows_sha256", ""),
            derived_from=data.get("derived_from", []),
            guard_evidence=data.get("guard_evidence") or {"time": [], "dim": []},
        )


@dataclass(frozen=True)
class RowPage:
    """read_rows 的一页"""

    rows: list[list[Any]]
    columns: list[ColumnMeta]
    offset: int
    next_offset: Optional[int]  # 无更多已保存行时 None
    saved_row_count: int
    source_truncated: bool


def _encode_value(value: Any) -> Any:
    """rows.jsonl 的值编码（§6.8）。"""
    if isinstance(value, Decimal):
        return {"$dec": str(value)}
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"$bin": len(value)}  # 不保存原文
    return value


def _decode_value(value: Any) -> Any:
    if isinstance(value, dict) and "$dec" in value:
        return Decimal(value["$dec"])
    return value


class ResultStore:
    """结果保存与读取；所有读取都做会话检查"""

    def __init__(self, workspace: Path):
        self._root = Path(workspace) / "results"
        self._root.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------ 写入

    def new_ref(self) -> str:
        return "res_" + uuid.uuid4().hex

    def save(
        self,
        *,
        ctx: DbAgentContext,
        purpose: str,
        sql_template: str | None,
        executed_sql: str | None,
        columns: Sequence[ColumnMeta],
        rows: Sequence[Sequence[Any]],
        source_truncated: bool,
        derived_from: Sequence[str],
        guard_evidence: GuardEvidenceSnapshot,
        datasource_id: str | None = None,
    ) -> ResultMeta:
        """原子写入 rows.jsonl 与 meta.json，返回 ResultMeta。"""
        result_ref = self.new_ref()
        target = self._root / result_ref
        encoded_lines = [json.dumps([_encode_value(v) for v in row]) for row in rows]
        rows_sha256 = hashlib.sha256("\n".join(encoded_lines).encode()).hexdigest()
        meta = ResultMeta(
            result_ref=result_ref,
            session_id=ctx.session_id,
            thread_id=ctx.thread_id,
            request_id=ctx.request_id,
            run_id=ctx.run_id,
            purpose=purpose,
            datasource_id=datasource_id or ctx.datasource_id,
            sql_template=sql_template,
            executed_sql=executed_sql,
            executed_sql_sha256=(
                hashlib.sha256(executed_sql.encode()).hexdigest()
                if executed_sql is not None
                else ""
            ),
            columns=list(columns),
            saved_row_count=len(rows),
            source_truncated=source_truncated,
            created_at=datetime.now().astimezone().isoformat(timespec="seconds"),
            rows_sha256=rows_sha256,
            derived_from=list(derived_from),
            guard_evidence=guard_evidence,
        )
        with tempfile.TemporaryDirectory(dir=self._root, prefix=".tmp-") as tmp:
            tmp_path = Path(tmp)
            (tmp_path / "rows.jsonl").write_text(
                "\n".join(encoded_lines) + ("\n" if encoded_lines else ""),
                encoding="utf-8",
            )
            (tmp_path / "meta.json").write_text(
                json.dumps(meta.to_json(), ensure_ascii=False, indent=1),
                encoding="utf-8",
            )
            os.replace(tmp_path, target)  # 原子重命名（同一文件系统内）
        logger.info(
            "[run_id=%s] 结果已保存 %s（%d 行，purpose=%s）",
            ctx.run_id,
            result_ref,
            len(rows),
            purpose,
        )
        return meta

    # ------------------------------------------------------------ 读取

    def _require_dir(self, result_ref: str) -> Path:
        if not _RESULT_REF_RE.match(result_ref or ""):
            raise ResultStoreError("invalid_result_ref", "非法结果引用格式")
        path = self._root / result_ref
        if not path.is_dir():
            raise ResultStoreError("result_not_found", "结果不存在或已过期")
        return path

    def _read_meta(self, path: Path) -> ResultMeta:
        try:
            data = json.loads((path / "meta.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ResultStoreError("result_not_found", "结果元数据不可读") from exc
        return ResultMeta.from_json(data)

    def _require_session(self, meta: ResultMeta, session_id: str) -> None:
        if meta.session_id != session_id:
            # 不泄露该引用是否存在于其他会话（P10）
            raise ResultStoreError(
                "result_access_denied", "结果不属于当前会话，无法访问"
            )

    def _read_encoded_rows(self, path: Path, meta: ResultMeta) -> list[list[Any]]:
        try:
            text = (path / "rows.jsonl").read_text(encoding="utf-8")
        except OSError as exc:
            raise ResultStoreError("result_not_found", "结果数据不可读") from exc
        lines = [ln for ln in text.splitlines() if ln]
        digest = hashlib.sha256("\n".join(lines).encode()).hexdigest()
        if digest != meta.rows_sha256:
            raise ResultStoreError(
                "result_integrity_error", "结果数据校验失败，可能已损坏"
            )
        try:
            return [[_decode_value(v) for v in json.loads(ln)] for ln in lines]
        except json.JSONDecodeError as exc:
            raise ResultStoreError(
                "result_integrity_error", "结果数据解析失败"
            ) from exc

    def load_meta(self, result_ref: str, *, session_id: str) -> ResultMeta:
        meta = self._read_meta(self._require_dir(result_ref))
        self._require_session(meta, session_id)
        return meta

    def verify_integrity(self, result_ref: str, *, session_id: str) -> ResultMeta:
        """§7.12.1 可读性核验：meta 可读、会话归属、rows_sha256 校验。

        不返回行（复用前检查用）；失败抛 ResultStoreError：
        result_not_found／result_access_denied／result_integrity_error。
        """
        path = self._require_dir(result_ref)
        meta = self._read_meta(path)
        self._require_session(meta, session_id)
        self._read_encoded_rows(path, meta)
        return meta

    def read_rows(
        self,
        result_ref: str,
        *,
        session_id: str,
        columns: list[str] | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> RowPage:
        path = self._require_dir(result_ref)
        meta = self._read_meta(path)
        self._require_session(meta, session_id)
        rows = self._read_encoded_rows(path, meta)
        if columns:
            names = [c["name"] for c in meta.columns]
            try:
                idx = [names.index(c) for c in columns]
            except ValueError as exc:
                raise ResultStoreError(
                    "invalid_argument", "请求的列不在结果中"
                ) from exc
            cols_meta = [meta.columns[i] for i in idx]
            rows = [[r[i] for i in idx] for r in rows]
        else:
            cols_meta = meta.columns
        page = rows[offset : offset + limit]
        next_offset = offset + limit if offset + limit < len(rows) else None
        return RowPage(
            rows=page,
            columns=cols_meta,
            offset=offset,
            next_offset=next_offset,
            saved_row_count=len(rows),
            source_truncated=meta.source_truncated,
        )

    def column_values(
        self, result_ref: str, *, session_id: str, column: str
    ) -> list[Any]:
        """某列全部已保存值（占位符展开用，§8.3）。"""
        path = self._require_dir(result_ref)
        meta = self._read_meta(path)
        self._require_session(meta, session_id)
        rows = self._read_encoded_rows(path, meta)
        names = [c["name"] for c in meta.columns]
        try:
            idx = names.index(column)
        except ValueError as exc:
            raise ResultStoreError(
                "invalid_argument", f"结果中没有列 {column!r}"
            ) from exc
        return [r[idx] for r in rows]

    # ------------------------------------------------------------ 维护

    def delete(self, result_ref: str, *, session_id: str) -> None:
        """删除一个结果目录（保留期清理用）。"""
        path = self._require_dir(result_ref)
        meta = self._read_meta(path)
        self._require_session(meta, session_id)
        shutil.rmtree(path, ignore_errors=True)

    def purge_expired(self, *, retention_days: int, now: datetime | None = None) -> int:
        """清理超过保留期的结果目录，返回删除数量。"""

        def _aware(dt: datetime) -> datetime:
            # 朴素时间按本地时区解释（created_at 写入时带时区，此处兜底）
            return dt if dt.tzinfo is not None else dt.astimezone()

        now = _aware(now or datetime.now().astimezone())
        removed = 0
        for path in self._root.iterdir():
            if not path.is_dir() or not _RESULT_REF_RE.match(path.name):
                continue
            meta_path = path / "meta.json"
            if not meta_path.exists():
                shutil.rmtree(path, ignore_errors=True)
                removed += 1
                continue
            try:
                created = _aware(
                    datetime.fromisoformat(
                        json.loads(meta_path.read_text(encoding="utf-8"))["created_at"]
                    )
                )
            except (OSError, ValueError, KeyError, json.JSONDecodeError):
                continue
            if (now - created).days >= retention_days:
                shutil.rmtree(path, ignore_errors=True)
                removed += 1
        if removed:
            logger.info("结果保留期清理：删除 %d 个结果", removed)
        return removed
