"""/api/v2 路由（doc 2 §4.2）：8 个端点，统一封装与稳定错误码。

阻塞读取（Store/文件/知识 SDK）经 runtime 的受限读取线程执行；query 在
全局名额 + 提交频率限制下提交到专用运行 executor 同步等待结果（D4）。
历史（sessions/turns）与结果读取不检查来源就绪或可续聊状态（P1 读取
独立），列表/详情触发按需元数据校正（不等待完成）。
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any, Callable, Optional

from fastapi import APIRouter, Query, Request
from fastapi.responses import Response

from src.api.v2 import codes
from src.api.v2.dto import QueryRequest, SessionCreateRequest, SessionListData
from src.api.v2.envelope import success_response
from src.api.v2.readiness import ReadinessError
from src.api.v2.runtime import capacity_slot, get_web_runtime
from src.api.v2.webstore import (
    InvalidTurnRecord,
    ScanLimitExceeded,
    SessionMetadataInvalid,
    StoreUnavailable,
    core_ownership_ok,
)
from src.utils.logger import get_module_logger

logger = get_module_logger("nl2sql.api.v2.router")

router = APIRouter(prefix="/api/v2", tags=["nl2sql-web-v2"])

_SESSION_ID_RE = re.compile(r"^s_[A-Za-z0-9_-]{1,61}$")
_RESULT_REF_RE = re.compile(r"^res_[0-9a-f]{32}$")


def _validate_session_id(session_id: str) -> None:
    if not _SESSION_ID_RE.match(session_id or ""):
        raise codes.WebApiError(
            codes.INVALID_REQUEST,
            "session_id 非法（应为 s_ 前缀的 [A-Za-z0-9_-] 标识）",
            data={"session_id": session_id},
        )


async def _run_read(request: Request, fn: Callable[[], Any]) -> Any:
    """在受限读取线程执行阻塞读取并统一映射依赖异常。"""
    runtime = get_web_runtime()
    try:
        return await asyncio.wrap_future(runtime.submit_read(fn))
    except (
        codes.WebApiError,
        SessionMetadataInvalid,
        InvalidTurnRecord,
        ScanLimitExceeded,
    ):
        raise
    except StoreUnavailable:
        raise codes.WebApiError(
            codes.DEPENDENCY_UNAVAILABLE, "会话存储暂时不可用"
        ) from None
    except ReadinessError:
        raise codes.WebApiError(
            codes.DEPENDENCY_UNAVAILABLE, "依赖读取未能完成"
        ) from None


def _max_display_rows() -> int:
    """前端表格展示上限（config.yaml api.max_display_rows，默认 200）。"""
    from src.services.config_loader import get_config

    value = get_config().get("api", {}).get("max_display_rows", 200)
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 200
    return parsed if parsed > 0 else 200


def _require_web_session(webstore, session_id: str) -> None:
    """结果读取/导出共用的 Web 会话登记归属校验。"""
    try:
        record = webstore.get_registration(session_id)
    except StoreUnavailable:
        raise codes.WebApiError(
            codes.DEPENDENCY_UNAVAILABLE, "会话存储暂时不可用"
        ) from None
    if record is None or not core_ownership_ok(record, session_id):
        raise codes.WebApiError(codes.SESSION_NOT_FOUND, "会话不存在")


def _result_dir(root: str, result_ref: str) -> Path:
    return Path(root) / "results" / result_ref


def _complete_files(path: Path) -> bool:
    return (
        path.is_dir()
        and (path / "meta.json").is_file()
        and (path / "rows.jsonl").is_file()
    )


def _snapshot_page(
    runtime,
    session_id: str,
    result_ref: str,
    *,
    offset: int,
    limit: int,
) -> dict:
    """按 session_id + result_ref 读取快照并切片；表缺失按 404 处理。"""
    from src.api.v2.snapshots import SnapshotTableMissing

    try:
        snapshot = runtime.get_snapshot_store().fetch(session_id, result_ref)
    except SnapshotTableMissing:
        logger.warning(
            "快照表不存在，按无快照处理 session=%s ref=%s", session_id, result_ref
        )
        raise codes.WebApiError(
            codes.RESULT_NOT_FOUND, "结果不存在或不可访问"
        ) from None
    except Exception as exc:  # noqa: BLE001（查询失败 ≠ 无快照）
        logger.warning(
            "快照查询失败 session=%s ref=%s: %s",
            session_id,
            result_ref,
            type(exc).__name__,
        )
        raise codes.WebApiError(
            codes.DEPENDENCY_UNAVAILABLE, "结果快照读取暂时不可用"
        ) from None
    if snapshot is None:
        raise codes.WebApiError(codes.RESULT_NOT_FOUND, "结果不存在或不可访问")
    rows = snapshot.get("rows_snapshot") or []
    total = int(snapshot.get("snapshot_row_count") or len(rows))
    meta = snapshot.get("meta_jsonb") or {}
    page_rows = rows[offset : offset + limit]
    next_offset = offset + limit if offset + limit < total else None
    return {
        "columns": [
            {"name": c["name"], "pg_type": c["pg_type"]}
            for c in (meta.get("columns") or [])
        ],
        "rows": page_rows,
        "offset": offset,
        "limit": limit,
        "next_offset": next_offset,
        "has_more": next_offset is not None,
        "saved_row_count": int(snapshot.get("saved_row_count") or 0),
        "source_truncated": bool(meta.get("source_truncated", False)),
        "data_from_snapshot": True,
        "snapshot_rows_available": int(snapshot.get("snapshot_row_count") or len(rows)),
        "max_display_rows": _max_display_rows(),
    }


def _cell_text(cell: Any) -> str:
    """v2 cell 信封 → CSV 文本（与页面展示语义一致）。"""
    if not isinstance(cell, dict):
        return "" if cell is None else str(cell)
    kind = cell.get("kind")
    value = cell.get("value")
    if kind == "null":
        return "NULL"
    if kind == "boolean":
        return "true" if value else "false"
    if kind == "binary":
        length = int((value or {}).get("length", 0))
        return f"二进制 {length} 字节"
    if kind == "array":
        return "{" + ", ".join(_cell_text(v) for v in (value or [])) + "}"
    return "" if value is None else str(value)


def _csv_escape(value: str) -> str:
    if any(ch in value for ch in ('"', ",", "\n", "\r")):
        return '"' + value.replace('"', '""') + '"'
    return value


def _rows_to_csv(columns: list[dict], rows: list[list[Any]]) -> str:
    lines = [",".join(_csv_escape(str(c.get("name", ""))) for c in columns)]
    for row in rows:
        lines.append(",".join(_csv_escape(_cell_text(cell)) for cell in row))
    return "﻿" + "\n".join(lines) + "\n"


# ----------------------------------------------------------------------------
# 按需元数据校正（doc 2 §4.4：列表/详情触发，不等待完成）
# ----------------------------------------------------------------------------


def _suppress_after_lock_failure(webstore, session_id: str) -> None:
    """锁后端故障 → 进程内 300 秒抑制（doc 2 §4.4）：列表/详情持续触发
    会反复安排注定失败的任务；登记读取 best-effort。"""
    try:
        record = webstore.get_registration(session_id)
        if record is not None and core_ownership_ok(record, session_id):
            webstore.mark_suppressed(session_id, record)
    except Exception:  # noqa: BLE001
        pass


def _run_reconcile_task(runtime, session_id: str) -> None:
    """锁内全量校正；失败设 retry_after（进程内抑制由调用方标记）。

    锁后端故障（构造或获取异常）同样进入 300 秒抑制；明确的锁忙碌保持
    现有重试语义，不抑制。"""
    webstore = runtime.get_webstore()
    try:
        lock = runtime.acquire_session_lock(session_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("校正锁构造失败 session=%s: %s", session_id, type(exc).__name__)
        _suppress_after_lock_failure(webstore, session_id)
        return
    try:
        try:
            if not lock.try_acquire():
                return
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "校正锁获取失败 session=%s: %s", session_id, type(exc).__name__
            )
            _suppress_after_lock_failure(webstore, session_id)
            return
        try:
            record = webstore.get_registration(session_id)
            if record is None or not core_ownership_ok(record, session_id):
                return
            try:
                updated = webstore.reconcile_metadata(session_id, record)
                webstore.mark_suppressed(session_id, updated)
            except Exception as exc:  # noqa: BLE001（坏记录/超限 → 退避）
                logger.info(
                    "元数据校正未完成 session=%s: %s", session_id, type(exc).__name__
                )
                try:
                    webstore.mark_reconcile_failure(session_id, record)
                except Exception:  # noqa: BLE001
                    pass
                webstore.mark_suppressed(session_id, record)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "校正登记读取失败 session=%s: %s", session_id, type(exc).__name__
            )
    finally:
        lock.release()


def _schedule_reconcile(runtime, webstore, session_id: str, record: dict) -> None:
    """需要且未抑制时安排受限校正；队列满改走进程内抑制（doc 2 §4.4）。"""
    try:
        if not webstore.reconcile_needed(record):
            return
        if webstore.suppressed(session_id, record):
            return
    except Exception:  # noqa: BLE001
        return

    def _task() -> None:
        _run_reconcile_task(runtime, session_id)

    if not runtime.schedule_reconcile(session_id, _task):
        webstore.mark_suppressed(session_id, record)


# ----------------------------------------------------------------------------
# 端点
# ----------------------------------------------------------------------------


@router.get("/health")
async def health(request: Request):
    """新链存活检查（doc 2 §4.2）：固定 200，不连库、不做 readiness。"""
    return success_response({"status": "ok"}, request)


@router.get("/sources/{source_id}/readiness")
async def source_readiness(source_id: str, request: Request):
    """按源完整就绪检查（doc 2 §5.5）：不调用问答模型。

    兼容且完成检查时即使 ready=false（含缺知识）也返回 200、data 直接为
    readiness；仅不兼容/登记配置非法（422）、依赖读取无法完成（503）、
    源不存在/停用（404/409）按错误封装拒绝。创建/提问的未就绪拒绝仍走
    409/source_not_ready（runs._readiness_error）。"""
    from fastapi import HTTPException

    from src.api.v2.runs import _READY_503, _readiness_error
    from src.modules.db_deepagent.source_registry import (
        normalize_short_source_id,
    )

    try:
        sid = normalize_short_source_id(source_id)
    except ValueError:
        raise HTTPException(status_code=422) from None

    def _work() -> dict:
        runtime = get_web_runtime()
        outcome = runtime.get_readiness().full_check(sid)
        if outcome.compatible is True and outcome.terminal_error_code not in _READY_503:
            return outcome.to_data()
        raise _readiness_error(outcome)

    data = await _run_read(request, _work)
    return success_response(data, request)


@router.post("/sessions")
async def create_session(payload: SessionCreateRequest, request: Request):
    """创建 Web 会话（doc 2 §4.4 写序）；返回 SessionInfo。"""
    from src.api.v2.runs import create_web_session

    runtime = get_web_runtime()
    source_id = str(payload.source_id)

    def _work() -> dict:
        return create_web_session(runtime, source_id)

    data = await _run_read(request, _work)
    return success_response(data, request)


@router.get("/sessions")
async def list_sessions(
    request: Request,
    source_id: Optional[str] = Query(None),
    q: str = Query("", max_length=100),
    limit: int = Query(30, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    """guest Web 会话列表（doc 10 §6）：last_active 降序分页，标题原文包含。"""
    runtime = get_web_runtime()
    filter_source = str(source_id) if source_id is not None else None
    keyword = q.strip()

    def _work() -> dict:
        webstore = runtime.get_webstore()
        (
            infos,
            has_more,
            next_offset,
            skipped,
            total_all,
            total_filtered,
            total_turns_all,
        ) = webstore.list_session_infos(
            source_id=filter_source,
            q=keyword,
            limit=limit,
            offset=offset,
        )
        # 按需校正（列表页条目 + 跳过的损坏登记；不等待完成）
        for info in infos:
            record = webstore.get_registration(info["session_id"])
            if record is not None:
                _schedule_reconcile(runtime, webstore, info["session_id"], record)
        for item in skipped:
            record = webstore.get_registration(item["session_id"])
            if record is not None:
                _schedule_reconcile(runtime, webstore, item["session_id"], record)
        return SessionListData(
            items=infos,
            limit=limit,
            offset=offset,
            has_more=has_more,
            next_offset=next_offset,
            total_all=total_all,
            total_filtered=total_filtered,
            total_turns_all=total_turns_all,
        ).model_dump()

    data = await _run_read(request, _work)
    return success_response(data, request)


@router.get("/sessions/{session_id}")
async def session_detail(session_id: str, request: Request):
    """会话详情（doc 2 §4.2）：不依赖列表页是否包含该会话。"""
    _validate_session_id(session_id)
    runtime = get_web_runtime()

    def _work() -> dict:
        webstore = runtime.get_webstore()
        try:
            record = webstore.get_registration(session_id)
        except StoreUnavailable:
            raise codes.WebApiError(
                codes.DEPENDENCY_UNAVAILABLE, "会话存储暂时不可用"
            ) from None
        if record is None or not core_ownership_ok(record, session_id):
            raise codes.WebApiError(codes.SESSION_NOT_FOUND, "会话不存在")
        _schedule_reconcile(runtime, webstore, session_id, record)
        try:
            return webstore.session_info(session_id, record)
        except SessionMetadataInvalid as exc:
            raise codes.WebApiError(
                codes.SESSION_METADATA_INVALID,
                "会话展示信息不完整",
                data={
                    "session_id": session_id,
                    "problem_fields": list(exc.problem_fields),
                },
            ) from None

    data = await _run_read(request, _work)
    return success_response(data, request)


@router.get("/sessions/{session_id}/turns")
async def list_turns(
    session_id: str,
    request: Request,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """历史轮次：问答文本，以及可回放的 SQL/结果引用；读取不做就绪/续聊检查。"""
    _validate_session_id(session_id)
    runtime = get_web_runtime()

    def _work() -> dict:
        webstore = runtime.get_webstore()
        try:
            record = webstore.get_registration(session_id)
        except StoreUnavailable:
            raise codes.WebApiError(
                codes.DEPENDENCY_UNAVAILABLE, "会话存储暂时不可用"
            ) from None
        if record is None or not core_ownership_ok(record, session_id):
            raise codes.WebApiError(codes.SESSION_NOT_FOUND, "会话不存在")
        try:
            turns, has_more, next_offset = webstore.list_turns(
                session_id, limit=limit, offset=offset
            )
        except ScanLimitExceeded:
            raise codes.WebApiError(
                codes.HISTORY_UNAVAILABLE, "历史读取超出资源上限，请稍后重试"
            ) from None
        except InvalidTurnRecord:
            logger.warning("Web 轮次记录损坏 session=%s", session_id)
            raise codes.WebApiError(
                codes.HISTORY_RECORD_INVALID, "历史记录损坏，无法安全读取"
            ) from None
        return {
            "items": turns,
            "limit": limit,
            "offset": offset,
            "has_more": has_more,
            "next_offset": next_offset,
        }

    data = await _run_read(request, _work)
    return success_response(data, request)


@router.post("/sessions/{session_id}/query")
async def submit_query(session_id: str, payload: QueryRequest, request: Request):
    """执行一轮问答（D4 同步返回）：全局名额 + 提交频率限制 → 运行 executor。"""
    from src.api.v2.runs import execute_web_query

    _validate_session_id(session_id)
    runtime = get_web_runtime()
    allowed, retry_after = runtime.check_rate_limit()
    if not allowed:
        raise codes.WebApiError(
            codes.RATE_LIMITED,
            "提交过于频繁，请稍后重试",
            headers={"Retry-After": str(retry_after)},
        )

    def _work():
        return execute_web_query(
            runtime,
            session_id=session_id,
            question=payload.question,
            source_id=str(payload.source_id),
        )

    with capacity_slot():
        future = runtime.submit_run(_work)
        result = await asyncio.wrap_future(future)
    return success_response(result.model_dump(), request)


@router.get("/sessions/{session_id}/results/{result_ref}")
async def read_result(
    session_id: str,
    result_ref: str,
    request: Request,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    """结果分页读取（doc 2 §4.5）：文件完整时读结果目录；目录或必需文件
    缺失时按 session_id + result_ref 回退快照表。"""
    _validate_session_id(session_id)
    if not _RESULT_REF_RE.match(result_ref or ""):
        raise codes.WebApiError(codes.RESULT_NOT_FOUND, "结果不存在或不可访问")

    def _work() -> dict:
        from src.modules.db_deepagent.config import load_base_db_agent_config
        from src.modules.db_deepagent.errors import ResultStoreError
        from src.modules.db_deepagent.services.result_store import ResultStore

        runtime = get_web_runtime()
        webstore = runtime.get_webstore()
        _require_web_session(webstore, session_id)

        cfg = load_base_db_agent_config()
        store = ResultStore(Path(cfg.workspace.root))
        path = _result_dir(cfg.workspace.root, result_ref)
        if _complete_files(path):
            try:
                page = store.read_rows_cells(
                    result_ref, session_id=session_id, offset=offset, limit=limit
                )
            except ResultStoreError as exc:
                # TOCTOU：检查后文件被清理 → 复检并回退快照
                if exc.error_type == "result_not_found" and not _complete_files(path):
                    return _snapshot_page(
                        runtime,
                        session_id,
                        result_ref,
                        offset=offset,
                        limit=limit,
                    )
                if exc.error_type in (
                    "result_not_found",
                    "result_access_denied",
                    "invalid_result_ref",
                ):
                    raise codes.WebApiError(
                        codes.RESULT_NOT_FOUND, "结果不存在或不可访问"
                    ) from None
                if exc.error_type == "result_integrity_error":
                    raise codes.WebApiError(
                        codes.RESULT_INTEGRITY_ERROR, "结果完整性校验失败"
                    ) from None
                if exc.error_type == "result_encoding_error":
                    raise codes.WebApiError(
                        codes.RESULT_ENCODING_ERROR, "结果无法安全读取或转码"
                    ) from None
                raise codes.WebApiError(codes.INVALID_REQUEST, "请求参数非法") from None
            return {
                "columns": [
                    {"name": c["name"], "pg_type": c["pg_type"]} for c in page.columns
                ],
                "rows": [list(row) for row in page.rows],
                "offset": page.offset,
                "limit": limit,
                "next_offset": page.next_offset,
                "has_more": page.next_offset is not None,
                "saved_row_count": page.saved_row_count,
                "source_truncated": page.source_truncated,
                "data_from_snapshot": False,
                "snapshot_rows_available": None,
                "max_display_rows": _max_display_rows(),
            }
        return _snapshot_page(
            runtime, session_id, result_ref, offset=offset, limit=limit
        )

    data = await _run_read(request, _work)
    return success_response(data, request)


@router.get("/sessions/{session_id}/results/{result_ref}/export")
async def export_result(
    session_id: str,
    result_ref: str,
    request: Request,
):
    """CSV 导出：文件完整时导出全部已保存行；否则导出快照行。

    与分页读取共用 Web 会话归属校验；导出不执行 rows_sha256 校验。
    """
    _validate_session_id(session_id)
    if not _RESULT_REF_RE.match(result_ref or ""):
        raise codes.WebApiError(codes.RESULT_NOT_FOUND, "结果不存在或不可访问")

    def _work() -> Response:
        from src.api.v2.snapshots import SnapshotTableMissing
        from src.modules.db_deepagent.config import load_base_db_agent_config
        from src.modules.db_deepagent.services.result_store import ResultStore

        runtime = get_web_runtime()
        webstore = runtime.get_webstore()
        _require_web_session(webstore, session_id)

        cfg = load_base_db_agent_config()
        path = _result_dir(cfg.workspace.root, result_ref)
        if _complete_files(path):
            from src.modules.db_deepagent.errors import ResultStoreError

            store = ResultStore(Path(cfg.workspace.root))
            try:
                meta = store.load_meta(result_ref, session_id=session_id)
            except ResultStoreError as exc:
                if exc.error_type in (
                    "result_not_found",
                    "result_access_denied",
                    "invalid_result_ref",
                ):
                    raise codes.WebApiError(
                        codes.RESULT_NOT_FOUND, "结果不存在或不可访问"
                    ) from None
                if exc.error_type == "result_encoding_error":
                    raise codes.WebApiError(
                        codes.RESULT_ENCODING_ERROR, "结果无法安全转换"
                    ) from None
                raise codes.WebApiError(codes.INVALID_REQUEST, "请求参数非法") from None
            try:
                text = (path / "rows.jsonl").read_text(encoding="utf-8")
                rows = [json.loads(line) for line in text.splitlines() if line]
            except (OSError, json.JSONDecodeError) as exc:
                raise codes.WebApiError(
                    codes.RESULT_ENCODING_ERROR, "结果无法安全转换"
                ) from exc
            csv = _rows_to_csv(meta.columns, rows)
            filename = f"{result_ref}.csv"
        else:
            try:
                snapshot = runtime.get_snapshot_store().fetch(session_id, result_ref)
            except SnapshotTableMissing:
                logger.warning(
                    "快照表不存在，导出按无结果处理 session=%s ref=%s",
                    session_id,
                    result_ref,
                )
                raise codes.WebApiError(
                    codes.RESULT_NOT_FOUND, "结果不存在或不可访问"
                ) from None
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "快照查询失败 session=%s ref=%s: %s",
                    session_id,
                    result_ref,
                    type(exc).__name__,
                )
                raise codes.WebApiError(
                    codes.DEPENDENCY_UNAVAILABLE, "结果快照读取暂时不可用"
                ) from None
            if snapshot is None:
                raise codes.WebApiError(codes.RESULT_NOT_FOUND, "结果不存在或不可访问")
            meta = snapshot.get("meta_jsonb") or {}
            csv = _rows_to_csv(
                meta.get("columns") or [], snapshot.get("rows_snapshot") or []
            )
            filename = f"{result_ref}.snapshot.csv"
        return Response(
            content=csv,
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    return await _run_read(request, _work)
