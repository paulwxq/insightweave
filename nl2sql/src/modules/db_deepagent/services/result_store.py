"""ResultStore：结果文件的原子保存与按会话读取（docs/v4/5 §6.8 + doc 2 §4.5）

目录 <workspace>/results/<result_ref>/，与 backend 根目录互不包含。写入先落
临时目录再原子重命名；读取校验 rows_sha256；跨会话访问不泄露存在性（P10）。

单元格编码（doc 2 §4.5）：新文件 meta.format_version=2，行内每个 cell 为
``{kind, value}`` 信封（kind 闭集见 _CELL_KINDS）；文件与 Web 共用同一编码
规则，类型标签只解释信封层，不递归猜测业务 JSON 对象的键。缺少
format_version 的旧文件按 v1 兼容读取：按列 pg_type 解释已知 numeric 的
``$dec`` 或二进制占位，输出统一转成 cell。
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import tempfile
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time as dt_time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional, Sequence
from uuid import UUID

from src.modules.db_deepagent.context import DbAgentContext
from src.modules.db_deepagent.errors import ResultStoreError
from src.modules.db_deepagent.schemas import ColumnMeta, GuardEvidenceSnapshot
from src.utils.logger import get_module_logger

logger = get_module_logger("nl2sql.db_deepagent.result_store")

_RESULT_REF_RE = re.compile(r"^res_[0-9a-f]{32}$")

_FORMAT_VERSION = 2

# cell 信封 kind 闭集（doc 2 §4.5）；新增值须同步契约与测试
_CELL_KINDS = frozenset(
    {
        "null",
        "boolean",
        "text",
        "integer",
        "decimal",
        "float",
        "date",
        "datetime",
        "time",
        "uuid",
        "interval",
        "array",
        "json",
        "binary",
    }
)

_JSON_PG_TYPES = frozenset({"json", "jsonb"})
# cell 数组嵌套深度上限：PostgreSQL 实际多维数组远低于此，仅防损坏文件
_CELL_ARRAY_DEPTH_MAX = 16
_NUMERIC_PG_TYPES = frozenset(
    {"int2", "int4", "int8", "numeric", "float4", "float8", "money"}
)
_INTEGER_RE = re.compile(r"^-?[0-9]+$")
_DECIMAL_RE = re.compile(r"^-?[0-9]+(\.[0-9]+)?([eE][-+]?[0-9]+)?$")
_NON_FINITE_FLOATS = {
    float("nan"): "NaN",
    float("inf"): "Infinity",
    float("-inf"): "-Infinity",
}


def _is_json_column(pg_type: str | None) -> bool:
    """JSON 标量列识别（doc 2 §4.5"JSON/JSONB 必须先识别为业务 JSON"）。"""
    return (pg_type or "").lower() in _JSON_PG_TYPES


def _element_pg_type(pg_type: str | None) -> str | None:
    """数组元素的列类型：剥离 typname 下划线前缀（jsonb[] 报告为
    "_jsonb"）或 format_type 的 [] 后缀，使元素按其标量规则编码。"""
    normalized = pg_type or ""
    if normalized.startswith("_"):
        return normalized[1:]
    if normalized.endswith("[]"):
        return normalized[:-2]
    return normalized


def _encode_value(value: Any) -> Any:
    """工具输出预览的值编码（§6.8 v1 语义；结果文件走 v2 cell 信封）。"""
    if isinstance(value, Decimal):
        return {"$dec": str(value)}
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"$bin": len(value)}
    return value


def _encode_cell(value: Any, pg_type: str | None) -> dict[str, Any]:
    """单个值 → v2 cell 信封（doc 2 §4.5）。未知类型显式报编码错误。"""
    if value is None:
        return {"kind": "null", "value": None}
    if isinstance(value, bool):
        return {"kind": "boolean", "value": value}
    if isinstance(value, Decimal):
        return {"kind": "decimal", "value": str(value)}
    if isinstance(value, int):
        # 十进制字符串：不强制转 JS number，保留大整数精度（含 2^53 以上）
        return {"kind": "integer", "value": str(value)}
    if isinstance(value, float):
        if math.isfinite(value):
            return {"kind": "float", "value": value}
        return {"kind": "float", "value": _NON_FINITE_FLOATS.get(value, "NaN")}
    if _is_json_column(pg_type):
        # JSON/JSONB 优先按列类型识别为业务 JSON；值序列化为 JSON 文本
        return {"kind": "json", "value": json.dumps(value, ensure_ascii=False)}
    if isinstance(value, str):
        return {"kind": "text", "value": value}
    if isinstance(value, datetime):
        return {"kind": "datetime", "value": value.isoformat()}
    if isinstance(value, date):
        return {"kind": "date", "value": value.isoformat()}
    if isinstance(value, dt_time):
        return {"kind": "time", "value": value.isoformat()}
    if isinstance(value, timedelta):
        total_us = (
            value.days * 86_400_000_000 + value.seconds * 1_000_000 + value.microseconds
        )
        return {"kind": "interval", "value": str(total_us)}
    if isinstance(value, UUID):
        return {"kind": "uuid", "value": str(value)}
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {
            "kind": "binary",
            "value": {"length": len(value), "content_saved": False},
        }
    if isinstance(value, (list, tuple)):
        # 数组按元素递归（doc 2 §4.5）：元素用剥离数组标记后的列类型，
        # jsonb[] 元素因此识别为业务 JSON 而非不可编码对象
        element_type = _element_pg_type(pg_type)
        return {
            "kind": "array",
            "value": [_encode_cell(v, element_type) for v in value],
        }
    raise ResultStoreError(
        "result_encoding_error",
        f"结果包含不支持的值类型，无法安全编码: {type(value).__name__}"
        f"（列 pg_type={pg_type!r}）",
    )


def _decode_cell(cell: dict[str, Any]) -> Any:
    """v2 cell → Python 值（内部 read_rows 按需解码用；二进制原文未保存）。"""
    kind, value = cell["kind"], cell["value"]
    if kind == "null":
        return None
    if kind == "boolean":
        return value
    if kind in ("integer", "interval"):
        return int(value) if kind == "integer" else timedelta(microseconds=int(value))
    if kind == "decimal":
        return Decimal(value)
    if kind == "float":
        if isinstance(value, str):
            return {"NaN": math.nan, "Infinity": math.inf, "-Infinity": -math.inf}[
                value
            ]
        return float(value)
    if kind in ("date", "datetime", "time", "uuid"):
        # 内部读取沿用 v1 语义（§6.8）：时间/UUID 以 ISO 字符串返回
        return value
    if kind == "json":
        return json.loads(value)
    if kind == "array":
        return [_decode_cell(c) for c in value]
    if kind == "binary":
        # 原文未保存：内部读取保持 v1 占位语义（长度标记）
        return {"$bin": int(value.get("length", 0))}
    return value  # text


def _validate_cell(cell: Any, depth: int = 0) -> dict[str, Any]:
    """v2 信封严格校验（doc 2 §4.5）：未知 kind、缺/多字段、值类型不符
    均为 result_encoding_error，不猜测格式；数组元素按同规则递归校验
    （嵌套深度上限防止损坏文件触发递归崩溃）。"""
    if depth > _CELL_ARRAY_DEPTH_MAX:
        raise ResultStoreError(
            "result_encoding_error",
            f"数组嵌套深度超过 {_CELL_ARRAY_DEPTH_MAX}",
        )
    if not isinstance(cell, dict) or set(cell) != {"kind", "value"}:
        raise ResultStoreError(
            "result_encoding_error", "cell 信封必须恰含 kind/value 字段"
        )
    kind, value = cell["kind"], cell["value"]
    if kind not in _CELL_KINDS:
        raise ResultStoreError("result_encoding_error", f"未知 cell kind: {kind!r}")
    if kind == "null":
        ok = value is None
    elif kind == "boolean":
        ok = isinstance(value, bool)
    elif kind in ("text", "uuid", "date", "datetime", "time"):
        ok = isinstance(value, str)
    elif kind == "integer":
        ok = isinstance(value, str) and bool(_INTEGER_RE.match(value))
    elif kind in ("decimal", "interval"):
        # decimal 允许小数/科学计数文本；interval 为微秒整数字符串
        ok = isinstance(value, str) and bool(
            (_INTEGER_RE if kind == "interval" else _DECIMAL_RE).match(value)
        )
    elif kind == "float":
        ok = (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            or (isinstance(value, str) and value in _NON_FINITE_FLOATS.values())
        )
    elif kind == "json":
        ok = isinstance(value, str)
    elif kind == "array":
        if not isinstance(value, list):
            ok = False
        else:
            # 嵌套元素按同规则递归校验（非法 kind/值在此抛出）
            for element in value:
                _validate_cell(element, depth + 1)
            ok = True
    elif kind == "binary":
        ok = (
            isinstance(value, dict)
            and set(value) == {"length", "content_saved"}
            and isinstance(value["length"], int)
            and value["content_saved"] is False
        )
    else:  # pragma: no cover（闭集已覆盖）
        ok = False
    if not ok:
        raise ResultStoreError(
            "result_encoding_error", f"cell kind={kind!r} 的值类型不符"
        )
    return cell


def _v1_value_to_cell(value: Any, pg_type: str | None) -> dict[str, Any]:
    """旧 v1 行值 → cell（doc 2 §4.5 v1 兼容）：按列 pg_type 解释已知
    numeric 列的 $dec 与二进制列的 $bin 占位，JSON 列的同名键与数组值
    保持业务 JSON，不调用无条件标签解码；数组按元素列类型递归。"""
    pg = (pg_type or "").lower()
    json_column = _is_json_column(pg)
    effective = _element_pg_type(pg)  # 数组列按元素类型解释占位符
    if value is None:
        return {"kind": "null", "value": None}
    if isinstance(value, bool):
        return {"kind": "boolean", "value": value}
    if isinstance(value, dict) and "$dec" in value and len(value) == 1:
        # 仅已知 numeric 列解释 $dec；JSON 列的同名键保持普通 JSON
        if not json_column and effective in _NUMERIC_PG_TYPES:
            return {"kind": "decimal", "value": str(value["$dec"])}
        return {"kind": "json", "value": json.dumps(value, ensure_ascii=False)}
    if isinstance(value, dict) and "$bin" in value and len(value) == 1:
        # 仅已知二进制列解释 $bin；JSON 列的同名键保持普通 JSON
        if not json_column and effective == "bytea":
            raw = value["$bin"]
            if not isinstance(raw, int) or isinstance(raw, bool):
                raise ResultStoreError(
                    "result_encoding_error", "旧格式 $bin 占位必须为整数字节长度"
                )
            return {
                "kind": "binary",
                "value": {"length": raw, "content_saved": False},
            }
        return {"kind": "json", "value": json.dumps(value, ensure_ascii=False)}
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if isinstance(value, int):
            return {"kind": "integer", "value": str(value)}
        if not math.isfinite(value):
            return {"kind": "float", "value": _NON_FINITE_FLOATS.get(value, "NaN")}
        return {"kind": "float", "value": value}
    if isinstance(value, str):
        if pg in ("date",):
            return {"kind": "date", "value": value}
        if pg in ("timestamp", "timestamptz"):
            return {"kind": "datetime", "value": value}
        if pg in ("time", "timetz"):
            return {"kind": "time", "value": value}
        if pg == "uuid":
            return {"kind": "uuid", "value": value}
        if _is_json_column(pg):
            # JSON 标量字符串须序列化为 JSON 文本（kind=json 契约）：
            # "hello" → '"hello"'，避免被前端误读为非法 JSON 或布尔等
            return {"kind": "json", "value": json.dumps(value, ensure_ascii=False)}
        return {"kind": "text", "value": value}
    if isinstance(value, list):
        # JSON 列的数组值是业务 JSON 整体，不解释为 SQL 数组
        if json_column:
            return {"kind": "json", "value": json.dumps(value, ensure_ascii=False)}
        return {
            "kind": "array",
            "value": [_v1_value_to_cell(v, effective) for v in value],
        }
    if isinstance(value, dict):
        return {"kind": "json", "value": json.dumps(value, ensure_ascii=False)}
    raise ResultStoreError(
        "result_encoding_error",
        f"旧格式结果包含无法转换的值类型: {type(value).__name__}",
    )


def _v1_decode_value(value: Any, pg_type: str | None) -> Any:
    """v1 行值 → Python 值（内部 read_rows 兼容读取：numeric 列 $dec →
    Decimal，其余保持 JSON 原值）。"""
    if (
        isinstance(value, dict)
        and "$dec" in value
        and len(value) == 1
        and (pg_type or "").lower() in _NUMERIC_PG_TYPES
    ):
        return Decimal(value["$dec"])
    return value


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
    format_version: Optional[int] = None  # 缺失＝v1（doc 2 §4.5）
    # doc 6.3 §6.1：产生该结果的任务身份（probe/过渡记录为 None）
    source_task: Optional[dict[str, Any]] = None

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
            "format_version": self.format_version,
            "source_task": self.source_task,
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
            format_version=data.get("format_version"),
            source_task=data.get("source_task"),
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
        source_task: dict[str, Any] | None = None,
    ) -> ResultMeta:
        """原子写入 rows.jsonl 与 meta.json，返回 ResultMeta。

        单元格采用 v2 cell 编码（doc 2 §4.5）：按列 pg_type 与实际值编码，
        JSON 序列化禁用裸 NaN/Infinity（allow_nan=False），未知类型显式抛
        result_encoding_error，不以 str(任意对象) 静默掩盖。
        """
        result_ref = self.new_ref()
        target = self._root / result_ref
        col_types = [
            c.get("pg_type") if isinstance(c, dict) else c.pg_type for c in columns
        ]
        encoded_lines = [
            json.dumps(
                [_encode_cell(v, t) for v, t in zip(row, col_types)],
                ensure_ascii=False,
                allow_nan=False,
            )
            for row in rows
        ]
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
            format_version=_FORMAT_VERSION,
            source_task=source_task,
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
            "[run_id=%s] 结果已保存 %s（%d 行，purpose=%s，format=v2）",
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

    def _check_format_version(self, meta: ResultMeta) -> None:
        """仅接受 v1（format_version 缺失）与整数 2（doc 2 §4.5）。"""
        version = meta.format_version
        if version is None or (
            isinstance(version, int)
            and not isinstance(version, bool)
            and version == _FORMAT_VERSION
        ):
            return
        raise ResultStoreError(
            "result_encoding_error",
            f"不支持的结果格式版本: {version!r}（允许：缺失(v1) 或 2）",
        )

    def _read_raw_lines(self, path: Path, meta: ResultMeta) -> list[list[Any]]:
        """读取并校验 rows.jsonl 原始行（SHA256 完整性检查）。"""
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
            return [json.loads(ln) for ln in lines]
        except json.JSONDecodeError as exc:
            raise ResultStoreError(
                "result_integrity_error", "结果数据解析失败"
            ) from exc

    def _read_encoded_rows(self, path: Path, meta: ResultMeta) -> list[list[Any]]:
        """内部读取（解码到 Python 值）：v2 信封解码；v1 按列 pg_type 兼容。"""
        self._check_format_version(meta)
        raw = self._read_raw_lines(path, meta)
        if meta.format_version == _FORMAT_VERSION:
            return [[_decode_cell(_validate_cell(v)) for v in row] for row in raw]
        col_types = [c.get("pg_type") for c in meta.columns]
        return [
            [
                _v1_decode_value(v, col_types[i] if i < len(col_types) else None)
                for i, v in enumerate(row)
            ]
            for row in raw
        ]

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

    def read_rows_cells(
        self,
        result_ref: str,
        *,
        session_id: str,
        offset: int = 0,
        limit: int = 100,
    ) -> RowPage:
        """Web 结果读取（doc 2 §4.5）：返回 v2 cell 行（原始编码直通）。

        v2 文件按存储信封严格校验后原样返回；v1 旧文件按列 pg_type 转成
        cell。不经过 Python 值解码→默认 JSON 编码的往返，Decimal/大整数
        不会丢精度。返回的 RowPage.rows 每格为 {kind, value} 字典。
        """
        path = self._require_dir(result_ref)
        meta = self._read_meta(path)
        self._require_session(meta, session_id)
        self._check_format_version(meta)
        raw = self._read_raw_lines(path, meta)
        if meta.format_version == _FORMAT_VERSION:
            cells_rows = [[_validate_cell(v) for v in row] for row in raw]
        else:
            col_types = [c.get("pg_type") for c in meta.columns]
            cells_rows = [
                [
                    _v1_value_to_cell(v, col_types[i] if i < len(col_types) else None)
                    for i, v in enumerate(row)
                ]
                for row in raw
            ]
        page = cells_rows[offset : offset + limit]
        next_offset = offset + limit if offset + limit < len(cells_rows) else None
        return RowPage(
            rows=page,
            columns=meta.columns,
            offset=offset,
            next_offset=next_offset,
            saved_row_count=len(cells_rows),
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

    def delete(self, result_ref: str, *, session_id: str, lease: object = None) -> None:
        """受控删除一个结果目录（doc 6.3 §6.5：仅清理服务持会话锁后调用）。

        无有效 SessionGuardLease 直接拒绝——不留绕过运行/清理互斥的删除入口。
        """
        from src.modules.db_deepagent.persistence.session_guard import validate_lease

        validate_lease(lease, session_id)
        path = self._require_dir(result_ref)
        meta = self._read_meta(path)
        self._require_session(meta, session_id)
        shutil.rmtree(path, ignore_errors=True)

    def purge_expired(
        self,
        *,
        retention_days: int,
        now: datetime | None = None,
        lease: object = None,
        session_ids: list[str] | None = None,
    ) -> int:
        """按会话受控清理超过保留期的结果目录（doc 6.3 §6.5）。

        生产路径必须由清理服务在持会话锁、校验保留闭包后逐会话调用：
        lease 对应 session_ids 中全部会话有效；不提供任何绕过互斥的全局
        删除。闭包保护由调用方（MaintenanceService）负责，本方法只按
        年龄与会话归属删除。
        """
        from src.modules.db_deepagent.persistence.session_guard import validate_lease

        if not session_ids:
            return 0
        for sid in session_ids:
            validate_lease(lease, sid)

        def _aware(dt: datetime) -> datetime:
            # 朴素时间按本地时区解释（created_at 写入时带时区，此处兜底）
            return dt if dt.tzinfo is not None else dt.astimezone()

        now = _aware(now or datetime.now().astimezone())
        allowed = set(session_ids)
        removed = 0
        for path in self._root.iterdir():
            if not path.is_dir() or not _RESULT_REF_RE.match(path.name):
                continue
            meta_path = path / "meta.json"
            if not meta_path.exists():
                # 坏元数据目录归属不可确定：只报告不强删（doc 6.3 §6.5）
                logger.warning("结果目录缺少 meta.json，跳过删除: %s", path.name)
                continue
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                created = _aware(datetime.fromisoformat(meta["created_at"]))
            except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
                continue
            if meta.get("session_id") not in allowed:
                continue
            if (now - created).days >= retention_days:
                shutil.rmtree(path, ignore_errors=True)
                removed += 1
        if removed:
            logger.info("结果保留期清理：删除 %d 个结果", removed)
        return removed
