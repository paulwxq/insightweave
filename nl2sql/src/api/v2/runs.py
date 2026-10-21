"""Web 执行链装配与业务流（doc 2 §4.4/§5.5.2）。

三个入口：

- ``create_web_session``：§4.4 创建写序（碰撞检查 → 通用绑定 → Web 登记
  pending=creation_id → 写后复核 → 清除 pending），保存的指纹与
  knowledge_snapshot 始终取自通过完整检查的快照 S，第二次读取只做比较。
- ``execute_web_query``：每轮查询流——Web 登记校验 → 轻量复核（登记 +
  current 发布标识；指纹/快照变化在锁内失效为对应原因）→ 完整就绪检查 →
  锁内确认写入 pending_run_id → Runner 执行（自身再取会话锁）→ 终态投影。
- ``run_agent_query``：真实装配（基础配置 → 登记注入 → build_services →
  平台池/persistence/agent/Runner，finalize_hook 收尾），每轮独立构造、
  逆序回收；CLI/测试会话不经此路径。
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from src.api.v2 import codes
from src.api.v2.dto import WebQueryResult
from src.api.v2.readiness import ReadinessError, RegistrationLookup
from src.api.v2.webstore import (
    DEFAULT_TITLE,
    WebStore,
    core_ownership_ok,
    derive_title,
    display_field_problems,
    display_from_result,
    state_projection,
    web_turn_valid,
)
from src.utils.logger import get_module_logger

logger = get_module_logger("nl2sql.api.v2.runs")


class ExecutionStartedError(Exception):
    """Runner 执行已开始后的未知异常：本轮副作用是否发生无法确定，
    不得清除 pending（doc 2 §4.4 保守收尾规则）。"""


# HTTP 200 的 error_code 特例（doc 2 §4.2）：执行中一致性异常优先覆盖
_SPECIAL_200_KINDS = frozenset(
    {codes.RESULT_ENCODING_ERROR, codes.KNOWLEDGE_CHANGED, codes.DEPENDENCY_UNAVAILABLE}
)
_SNAPSHOT_KEYS = ("table_schema", "rel", "dim_value", "sql_example")
_INVALID_REASON_TEXT = {
    "knowledge_changed": "知识版本已变化",
    "dependency_unavailable": "依赖或收尾状态未确认",
    "source_changed": "来源连接或范围已变化",
    "session_binding_missing": "会话绑定缺失",
}

# readiness 终止码 → 稳定错误映射（创建/查询/GET readiness 共用）
_READY_404 = frozenset({codes.SOURCE_NOT_FOUND})
_READY_409_SAME = frozenset({codes.SOURCE_INACTIVE})
_READY_422 = frozenset({codes.SOURCE_UNSUPPORTED, codes.SOURCE_CONFIG_INVALID})
_READY_503 = frozenset(
    {codes.SOURCE_REGISTRY_UNAVAILABLE, codes.DEPENDENCY_UNAVAILABLE}
)


def _snapshot_pairs(snapshot: Any) -> dict:
    """knowledge_snapshot 的版本对投影（backend_summary 等不参与比较）。"""
    if not isinstance(snapshot, dict):
        return {}
    out: dict = {}
    for key in _SNAPSHOT_KEYS:
        value = snapshot.get(key)
        out[key] = tuple(value) if isinstance(value, list) else value
    return out


def _knowledge_changed(recorded: Any, current: Any) -> bool:
    return _snapshot_pairs(recorded) != _snapshot_pairs(current)


def _readback_state(record: Optional[dict], request_id: str) -> Optional[bool]:
    """收尾回读判定（doc 2 §4.4 history_saved）：None→false，有效→true，
    存在但不合谓词→null。"""
    if record is None:
        return False
    return True if web_turn_valid(request_id, record) else None


def _lookup_error(
    lookup: RegistrationLookup, *, request_id: Optional[str] = None
) -> codes.WebApiError:
    data = {"request_id": request_id, "run_id": None}
    if lookup.status == "not_found":
        return codes.WebApiError(codes.SOURCE_NOT_FOUND, "登记源不存在", data=data)
    if lookup.status == "inactive":
        return codes.WebApiError(codes.SOURCE_INACTIVE, "登记源已停用", data=data)
    if lookup.status == "unsupported":
        return codes.WebApiError(
            codes.SOURCE_UNSUPPORTED, lookup.detail or "登记源类型不受支持", data=data
        )
    if lookup.status == "config_invalid":
        return codes.WebApiError(
            codes.SOURCE_CONFIG_INVALID, lookup.detail or "登记源配置非法", data=data
        )
    return codes.WebApiError(
        codes.SOURCE_REGISTRY_UNAVAILABLE, "登记库暂时不可用", data=data
    )


def _first_fail_message(outcome) -> str:
    for check in outcome.checks:
        if check.get("state") == "fail":
            return str(check.get("message") or "")
    return ""


def _readiness_error(outcome, *, request_id: Optional[str] = None) -> codes.WebApiError:
    """readiness 失败结果 → 稳定错误（doc 2 §4.2/§5.5）。"""
    terminal = outcome.terminal_error_code
    data = {
        "request_id": request_id,
        "run_id": None,
        "readiness": outcome.to_data(),
    }
    if terminal in _READY_404:
        return codes.WebApiError(codes.SOURCE_NOT_FOUND, "登记源不存在", data=data)
    if terminal in _READY_409_SAME:
        return codes.WebApiError(codes.SOURCE_INACTIVE, "登记源已停用", data=data)
    if terminal in _READY_422:
        message = _first_fail_message(outcome) or "登记源配置不受支持"
        return codes.WebApiError(terminal, message, data=data)
    if terminal in _READY_503:
        return codes.WebApiError(terminal, "依赖暂时不可用", data=data)
    message = _first_fail_message(outcome)
    return codes.WebApiError(
        codes.SOURCE_NOT_READY,
        f"数据源暂未就绪{('：' + message) if message else ''}",
        data=data,
    )


def _invalidate_locked(
    runtime, webstore: WebStore, session_id: str, reason: str
) -> None:
    """短取会话锁完成失效写入（best-effort；他方持锁/后端异常则留给
    遗留 pending 规则保护）。仅限调用方未持该会话锁的路径。"""
    try:
        lock = runtime.acquire_session_lock(session_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("失效锁构造失败 session=%s: %s", session_id, type(exc).__name__)
        return
    try:
        try:
            if not lock.try_acquire():
                return
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "失效锁获取失败 session=%s: %s", session_id, type(exc).__name__
            )
            return
        try:
            fresh = webstore.get_registration(session_id)
            if fresh is not None and core_ownership_ok(fresh, session_id):
                webstore.invalidate(session_id, fresh, reason)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "失效写入失败 session=%s: %s", session_id, type(exc).__name__
            )
    finally:
        lock.release()


def _invalidate_inlock(
    webstore: WebStore, session_id: str, record: dict, reason: str
) -> None:
    """调用方已持会话锁时的直接失效写入（创建写后复核/Runner 钩子内）；
    best-effort，写失败留给遗留 pending 规则保护。"""
    try:
        webstore.invalidate(session_id, record, reason)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "锁内失效写入失败 session=%s: %s", session_id, type(exc).__name__
        )


def _clear_pending_quiet(webstore: WebStore, session_id: str) -> None:
    """执行前置失败后清掉本请求写入的 pending（best-effort）。"""
    try:
        record = webstore.get_registration(session_id)
        if record is not None and record.get("state") == "active":
            webstore.clear_pending(session_id, record)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "pending 清理失败 session=%s: %s", session_id, type(exc).__name__
        )


def _close_quietly(resource, label: str) -> None:
    """资源回收 best-effort：失败只记录日志，不覆盖调用方已有的
    返回值/异常（否则关闭异常会让执行已开始的轮次被误判为装配失败）。"""
    try:
        resource.close()
    except Exception as exc:  # noqa: BLE001
        logger.warning("资源回收失败（%s）: %s", label, type(exc).__name__)


# ----------------------------------------------------------------------------
# 创建会话（doc 2 §4.4）
# ----------------------------------------------------------------------------


def create_web_session(runtime, source_id: str) -> dict:
    """创建 Web 会话并返回 SessionInfo dict；失败抛 WebApiError。"""
    import uuid

    webstore = runtime.get_webstore()
    readiness = runtime.get_readiness()

    outcome = readiness.full_check(source_id)
    if not outcome.compatible or not outcome.ready:
        raise _readiness_error(outcome)
    try:
        lookup, fingerprint, snapshot = readiness.light_recheck(source_id)
    except ReadinessError:
        raise codes.WebApiError(
            codes.DEPENDENCY_UNAVAILABLE, "登记读取暂时不可用"
        ) from None
    if lookup.status != "ok" or lookup.registration is None:
        raise _lookup_error(lookup)
    # 保存值一律取自通过检查的 S（§5.5.2 第 4 条）；ready 结果必带两者
    s_fingerprint = outcome.fingerprint or fingerprint
    s_snapshot = outcome.knowledge_snapshot or snapshot

    session_id = "s_" + uuid.uuid4().hex
    creation_id = "create_" + uuid.uuid4().hex
    locate = {"session_id": session_id, "creation_id": creation_id}
    lock = runtime.acquire_session_lock(session_id)
    try:
        from src.modules.db_deepagent.persistence.checkpoint import LockBackendError

        try:
            acquired = lock.try_acquire()
        except LockBackendError:
            raise codes.WebApiError(
                codes.LOCK_BACKEND_ERROR, "锁服务暂时不可用", data=locate
            )
        if not acquired:
            raise codes.WebApiError(
                codes.SESSION_BUSY, "会话标识刚被占用，请重试", data=locate
            )
        # 碰撞检查（极小概率；随机 128 位标识命中既有记录视为内部错误）
        try:
            if webstore.get_registration(
                session_id
            ) is not None or webstore.generic_session_exists(session_id):
                raise codes.WebApiError(
                    codes.INTERNAL_ERROR, "会话标识冲突，请重试", data=locate
                )
        except codes.WebApiError:
            raise
        except Exception as exc:  # noqa: BLE001（Store 故障 → 依赖不可用）
            raise codes.WebApiError(
                codes.DEPENDENCY_UNAVAILABLE, "会话登记读取失败", data=locate
            ) from exc
        # 写序（doc 2 §4.4）：通用绑定在先，Web 登记（pending=creation_id）在后
        now_iso = webstore.now_iso()
        try:
            webstore.put_generic_session(
                session_id,
                {
                    "session_id": session_id,
                    "current_thread_id": None,
                    "thread_seq": 0,
                    "source_id": source_id,
                    "created_at": now_iso,
                    "last_active_at": now_iso,
                    "active": False,
                    "carried_evidence": [],
                },
            )
            record = webstore.new_registration(
                session_id=session_id,
                source_id=source_id,
                source_fingerprint=s_fingerprint,
                knowledge_snapshot=s_snapshot,
                creation_id=creation_id,
            )
            webstore.put_registration(session_id, record)
        except Exception as exc:  # noqa: BLE001
            raise codes.WebApiError(
                codes.DEPENDENCY_UNAVAILABLE,
                "会话登记写入失败，请重试",
                data=locate,
            ) from exc
        # 写后复核：读不到或值变化不返回成功（§5.5.2 第 4 条）
        try:
            lookup2, fingerprint2, snapshot2 = readiness.light_recheck(source_id)
        except ReadinessError:
            raise codes.WebApiError(
                codes.DEPENDENCY_UNAVAILABLE, "创建后复核未能完成，会话未确认可用"
            )
        if lookup2.status != "ok" or lookup2.registration is None:
            if lookup2.status in ("not_found", "inactive"):
                _invalidate_inlock(webstore, session_id, record, "source_changed")
                raise codes.WebApiError(
                    codes.SOURCE_CHANGED, "来源在创建过程中发生变化，请重试"
                )
            raise _lookup_error(lookup2)
        if fingerprint2 != s_fingerprint:
            _invalidate_inlock(webstore, session_id, record, "source_changed")
            raise codes.WebApiError(
                codes.SOURCE_CHANGED, "来源连接或范围在创建过程中变化，请重试"
            )
        if _knowledge_changed(s_snapshot, snapshot2):
            _invalidate_inlock(webstore, session_id, record, "knowledge_changed")
            raise codes.WebApiError(
                codes.KNOWLEDGE_CHANGED, "知识版本在创建过程中变化，请重试"
            )
        # 清除创建 pending；结果不明时回读核对，仍含 pending 则不返回成功
        confirmed: Optional[dict] = None
        try:
            fresh = webstore.get_registration(session_id)
            webstore.clear_pending(session_id, fresh or record)
            confirmed = webstore.get_registration(session_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "创建 pending 清除未确认 session=%s: %s", session_id, type(exc).__name__
            )
            try:
                confirmed = webstore.get_registration(session_id)
            except Exception:  # noqa: BLE001
                confirmed = None
        if (
            confirmed is None
            or confirmed.get("pending_run_id") is not None
            or confirmed.get("state") != "active"
        ):
            raise codes.WebApiError(
                codes.DEPENDENCY_UNAVAILABLE, "会话创建收尾未确认，请重试", data=locate
            )
        return webstore.session_info(session_id, confirmed)
    finally:
        lock.release()


# ----------------------------------------------------------------------------
# 终态收尾钩子（doc 2 §4.4：Runner 锁内调用）
# ----------------------------------------------------------------------------


def _make_finalize_hook(
    webstore: WebStore,
    readiness,
    source_id: str,
    holder: dict,
    *,
    results: Any = None,
    snapshot_writer: Any = None,
) -> Callable[..., Optional[dict]]:
    """构造 Runner 收尾钩子：终态轻量复核 → 安全投影或文本轮次写入 +
    元数据增量更新 + pending 清除。history_saved 经 holder 传出。"""

    def hook(**kwargs: Any) -> Optional[dict]:
        session_id = kwargs.get("session_id")
        request_id = kwargs.get("request_id")
        run_id = kwargs.get("run_id")
        question = kwargs.get("question") or ""
        result = kwargs.get("result")
        if not session_id or not request_id or result is None:
            holder["history_saved"] = None
            return None
        try:
            record = webstore.get_registration(session_id)
        except Exception as exc:  # noqa: BLE001（登记不可读：不写日志轮次）
            logger.warning(
                "收尾登记读取失败 session=%s: %s", session_id, type(exc).__name__
            )
            holder["history_saved"] = None
            return None
        if record is None or not core_ownership_ok(record, session_id):
            holder["history_saved"] = None
            return None

        # 终态轻量复核（§5.5.1）：True=确定变化，False=未变化，None=无法复核
        changed: Optional[bool]
        lookup_status = ""
        try:
            lookup, fingerprint, snapshot = readiness.light_recheck(source_id)
            lookup_status = lookup.status
            if lookup.status != "ok" or lookup.registration is None:
                changed = True
            elif fingerprint != record.get("source_fingerprint"):
                changed = True
            else:
                changed = _knowledge_changed(record.get("knowledge_snapshot"), snapshot)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "终态复核未能完成 session=%s: %s", session_id, type(exc).__name__
            )
            changed = None

        # 变化或无法复核都不得回放原答案（doc 2 §4.4）；仅 False 走正常收尾
        if changed is not False:
            if changed and lookup_status in ("not_found", "inactive"):
                reason = "source_changed"
            elif changed:
                reason = "knowledge_changed"
            else:
                reason = "dependency_unavailable"
            error_code = (
                codes.KNOWLEDGE_CHANGED if changed else codes.DEPENDENCY_UNAVAILABLE
            )
            safe_answer = "本轮知识一致性无法确认，请新建会话"
            projected = WebQueryResult.unsafe_projection(
                result,
                source_id=source_id,
                history_saved=None,
                error_code=error_code,
                safe_answer=safe_answer,
            )
            turn_status = "failed" if changed else "persistence_error"
            if not changed:
                projected = projected.model_copy(update={"status": "persistence_error"})
            try:
                webstore.invalidate(session_id, record, reason, keep_pending=True)
            except Exception as exc:  # noqa: BLE001（失效失败保留 pending 保护）
                logger.warning(
                    "收尾失效写入失败 session=%s: %s", session_id, type(exc).__name__
                )
            try:
                webstore.write_turn(
                    session_id,
                    request_id,
                    question=question,
                    answer=safe_answer,
                    run_id=run_id or "",
                    status=turn_status,
                )
                rec2 = webstore.read_turn(session_id, request_id)
                holder["history_saved"] = _readback_state(rec2, request_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "安全摘要轮写入失败 session=%s: %s", session_id, type(exc).__name__
                )
                try:
                    rec2 = webstore.read_turn(session_id, request_id)
                    holder["history_saved"] = _readback_state(rec2, request_id)
                except Exception:  # noqa: BLE001
                    holder["history_saved"] = None
            return {"result": projected}

        # 复核通过：写文本轮次（§4.4）→ history_saved 回读判定 → 元数据增量
        answer = (result.answer or "").strip() or None
        if answer is None:
            if result.status == "needs_clarification" and result.open_questions:
                answer = "需要澄清：" + "；".join(result.open_questions)
            else:
                answer = f"本轮未生成答案（状态：{result.status}）"
        saved: Optional[bool] = None
        rec2: Optional[dict] = None
        try:
            webstore.write_turn(
                session_id,
                request_id,
                question=question,
                answer=answer,
                run_id=run_id or "",
                status=result.status,
                display=display_from_result(result),
            )
            rec2 = webstore.read_turn(session_id, request_id)
            saved = _readback_state(rec2, request_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Web 轮次写入失败 session=%s: %s", session_id, type(exc).__name__
            )
            try:
                rec2 = webstore.read_turn(session_id, request_id)
                saved = _readback_state(rec2, request_id)
            except Exception:  # noqa: BLE001
                saved = None
        holder["history_saved"] = saved
        # 历史轮次确认落库后写快照（仅正常收尾分支；失败只 WARN，不阻断收尾）
        if saved is True and snapshot_writer is not None and results is not None:
            display = (rec2 or {}).get("display") or display_from_result(result)
            evidence = [
                item
                for item in (display.get("evidence") or [])
                if isinstance(item, dict) and item.get("result_ref")
            ]
            if evidence:
                try:
                    snapshot_writer.write(
                        session_id=session_id,
                        request_id=request_id,
                        evidence=evidence,
                        results=results,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "快照写入失败 session=%s request=%s: %s: %s",
                        session_id,
                        request_id,
                        type(exc).__name__,
                        exc,
                    )
        updated = dict(record)
        if saved is True:
            updated["turn_count"] = int(record.get("turn_count") or 0) + 1
            title = record.get("title")
            if (
                not isinstance(title, str)
                or not title.strip()
                or title == DEFAULT_TITLE
            ):
                updated["title"] = derive_title(question)
            updated["last_active_at"] = webstore.now_iso()
        # 复核通过即清除 pending（§4.4；与元数据同一次写入，失败则保留保护）
        updated["pending_run_id"] = None
        try:
            webstore.put_registration(session_id, updated)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "收尾元数据/pending 写入失败 session=%s: %s",
                session_id,
                type(exc).__name__,
            )
        return None

    return hook


# ----------------------------------------------------------------------------
# 查询执行流（doc 2 §4.4/§5.5.2）
# ----------------------------------------------------------------------------


def execute_web_query(
    runtime,
    *,
    session_id: str,
    question: str,
    source_id: str,
) -> WebQueryResult:
    """执行一轮 Web 问答；成功返回 WebQueryResult（HTTP 200），失败抛
    WebApiError（含 busy/lock 后端等附 data.result 的前置拒绝）。"""
    import uuid

    webstore = runtime.get_webstore()
    readiness = runtime.get_readiness()
    request_id = "req_" + uuid.uuid4().hex

    def _err(
        error_code: str, message: str, data: Optional[dict] = None, headers=None
    ) -> "codes.WebApiError":
        payload = {"request_id": request_id, "run_id": None}
        payload.update(data or {})
        return codes.WebApiError(error_code, message, data=payload, headers=headers)

    # 1) Web 登记读取与校验（P1 同规则；CLI/测试会话不当作 Web 会话公开）
    try:
        record = webstore.get_registration(session_id)
    except Exception as exc:  # noqa: BLE001
        raise _err(codes.DEPENDENCY_UNAVAILABLE, "会话登记暂时不可读") from exc
    if record is None or not core_ownership_ok(record, session_id):
        raise _err(codes.SESSION_NOT_FOUND, "会话不存在")
    if record.get("source_id") != source_id:
        raise _err(codes.SOURCE_MISMATCH, "请求来源与会话绑定的来源不一致")
    problems = display_field_problems(record)
    if problems:
        raise _err(
            codes.SESSION_METADATA_INVALID,
            "会话展示信息不完整",
            {"session_id": session_id, "problem_fields": problems},
        )
    projection = state_projection(record)
    if not projection["state_valid"]:
        raise _err(codes.SESSION_BINDING_MISSING, "会话状态字段缺失或矛盾，不能续聊")
    if projection["state"] == "invalidated":
        reason_text = _INVALID_REASON_TEXT.get(
            projection["invalid_reason"] or "", "状态未确认"
        )
        raise _err(
            codes.SESSION_INVALIDATED,
            f"会话已失效（{reason_text}），请新建会话",
        )

    # 2) 执行前轻量复核（§5.5.2 第 1 条）
    try:
        lookup, fingerprint, snapshot = readiness.light_recheck(source_id)
    except ReadinessError:
        raise _err(codes.DEPENDENCY_UNAVAILABLE, "执行前复核未能完成") from None
    registration = lookup.registration
    if lookup.status != "ok" or registration is None:
        raise _lookup_error(lookup, request_id=request_id)
    if fingerprint != record.get("source_fingerprint"):
        _invalidate_locked(runtime, webstore, session_id, "source_changed")
        raise _err(codes.SOURCE_CHANGED, "来源连接或范围已变化，请新建会话")
    if _knowledge_changed(record.get("knowledge_snapshot"), snapshot):
        _invalidate_locked(runtime, webstore, session_id, "knowledge_changed")
        raise _err(codes.KNOWLEDGE_CHANGED, "知识版本已变化，请新建会话")

    # 3) 完整就绪检查（30 秒缓存；§5.5.2 第 2 条）
    outcome = readiness.full_check(source_id)
    if not outcome.compatible or not outcome.ready:
        raise _readiness_error(outcome, request_id=request_id)

    # 4) 快照表门禁：未建表时拒绝新查询，避免产生永远无快照的轮次
    try:
        snapshot_ready = runtime.snapshot_table_ready()
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "快照表状态核对失败 session=%s: %s", session_id, type(exc).__name__
        )
        raise _err(codes.DEPENDENCY_UNAVAILABLE, "结果快照表状态无法确认") from exc
    if not snapshot_ready:
        raise _err(
            codes.DEPENDENCY_UNAVAILABLE,
            "结果快照表未初始化，请先运行 maintenance init-platform",
        )

    # 5) 锁内确认写入 pending（§4.4 收尾保护）后交 Runner 执行
    holder: dict = {}
    lock = runtime.acquire_session_lock(session_id)
    pending_written = False
    try:
        from src.modules.db_deepagent.persistence.checkpoint import LockBackendError

        try:
            acquired = lock.try_acquire()
        except LockBackendError:
            raise _err(
                codes.LOCK_BACKEND_ERROR,
                "锁服务暂时不可用",
                headers={"Retry-After": "5"},
            )
        if not acquired:
            raise _err(
                codes.SESSION_BUSY,
                "会话正在执行上一轮问答",
                headers={"Retry-After": "5"},
            )
        try:
            fresh = webstore.get_registration(session_id)
        except Exception as exc:  # noqa: BLE001
            raise _err(codes.DEPENDENCY_UNAVAILABLE, "会话登记暂时不可读") from exc
        if fresh is None or not core_ownership_ok(fresh, session_id):
            raise _err(codes.SESSION_NOT_FOUND, "会话不存在")
        proj2 = state_projection(fresh)
        if not proj2["state_valid"]:
            raise _err(
                codes.SESSION_BINDING_MISSING, "会话状态字段缺失或矛盾，不能续聊"
            )
        if proj2["state"] == "invalidated":
            raise _err(
                codes.SESSION_INVALIDATED,
                "会话已失效"
                f"（{_INVALID_REASON_TEXT.get(proj2['invalid_reason'] or '', '状态未确认')}），请新建会话",
            )
        if proj2["pending_run_id"] is not None:
            # 遗留 pending（§4.4）：保守失效 dependency_unavailable，不自动重放
            try:
                webstore.invalidate(
                    session_id, fresh, "dependency_unavailable", keep_pending=True
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "遗留 pending 失效写入失败 session=%s: %s",
                    session_id,
                    type(exc).__name__,
                )
            raise _err(
                codes.SESSION_INVALIDATED,
                "上一轮收尾未确认，已保守失效会话，请新建会话",
            )
        try:
            webstore.set_pending(session_id, fresh, request_id)
            pending_written = True
            confirmed = webstore.get_registration(session_id)
        except Exception as exc:  # noqa: BLE001
            raise _err(
                codes.DEPENDENCY_UNAVAILABLE, "收尾保护标记写入失败，未执行查询"
            ) from exc
        if confirmed.get("pending_run_id") != request_id:
            raise _err(
                codes.DEPENDENCY_UNAVAILABLE, "收尾保护标记未能确认写入，未执行查询"
            )
        # 锁直接交接给 Runner（同线程持锁至终态收尾，外层 finally 释放）：
        # 不在中途释放重取，避免间隙内他请求把在途 pending 误判为遗留
        # 而失效会话（竞争请求应得到 session_busy 且不改动活动会话）
        try:
            result = runtime.run_query(
                session_id=session_id,
                question=question,
                request_id=request_id,
                source_id=source_id,
                registration=registration,
                webstore=webstore,
                readiness=readiness,
                holder=holder,
                session_lock=lock,
            )
        except codes.WebApiError:
            if pending_written:
                _clear_pending_quiet(webstore, session_id)
            raise
        except ExecutionStartedError as exc:
            # 执行已开始、终态未确认（§4.4）：保留 pending，下一请求按
            # 遗留 pending 保守失效；既不自动清除也不自动重放
            logger.exception("查询执行异常（执行已开始）session=%s", session_id)
            raise _err(
                codes.DEPENDENCY_UNAVAILABLE,
                "查询执行异常，本轮结果未确认，请稍后刷新查看",
            ) from exc
        except Exception as exc:  # noqa: BLE001（装配失败 → 依赖不可用）
            logger.exception("查询装配失败 session=%s", session_id)
            if pending_written:
                _clear_pending_quiet(webstore, session_id)
            raise _err(codes.DEPENDENCY_UNAVAILABLE, "查询装配未能完成") from exc
        primary = next((e for e in result.errors if e.primary), None)
        if primary is not None and primary.kind in (
            "session_busy",
            "lock_backend_error",
        ):
            # 前置失败未进 _run_locked：钩子未运行，pending 由本层清理
            if pending_written:
                _clear_pending_quiet(webstore, session_id)
        return _project_result(
            result,
            source_id=source_id,
            request_id=request_id,
            holder=holder,
        )
    finally:
        lock.release()


def _project_result(
    result,
    *,
    source_id: str,
    request_id: str,
    holder: dict,
) -> WebQueryResult:
    """DbAgentResult → WebQueryResult / 前置拒绝 WebApiError（doc 2 §4.2）。"""
    primary = next((e for e in result.errors if e.primary), None)
    if primary is not None and primary.kind == "session_busy":
        projected = WebQueryResult.project(
            result,
            source_id=source_id,
            history_saved=None,
            error_code=codes.SESSION_BUSY,
        )
        raise codes.WebApiError(
            codes.SESSION_BUSY,
            "会话正在执行上一轮问答",
            data={
                "request_id": request_id,
                "run_id": result.run_id,
                "result": projected.model_dump(),
            },
            headers={"Retry-After": "5"},
        )
    if primary is not None and primary.kind == "lock_backend_error":
        projected = WebQueryResult.project(
            result,
            source_id=source_id,
            history_saved=None,
            error_code=codes.LOCK_BACKEND_ERROR,
        )
        raise codes.WebApiError(
            codes.LOCK_BACKEND_ERROR,
            "锁服务暂时不可用",
            data={
                "request_id": request_id,
                "run_id": result.run_id,
                "result": projected.model_dump(),
            },
        )
    if result.status == "completed":
        error_code = None
    elif primary is not None and primary.kind in _SPECIAL_200_KINDS:
        error_code = primary.kind
    elif result.status == "failed":
        error_code = "query_failed"
    else:
        error_code = result.status
    return WebQueryResult.project(
        result,
        source_id=source_id,
        history_saved=holder.get("history_saved"),
        error_code=error_code,
    )


# ----------------------------------------------------------------------------
# 真实装配（runtime.run_query 默认实现）
# ----------------------------------------------------------------------------


def _open_platform_pool(cfg):
    from src.modules.db_deepagent.persistence.platform_pool import (
        build_platform_pool,
        check_platform_tables,
    )

    pool = build_platform_pool(cfg.platform_db)
    try:
        try:
            pool.open(wait=False)
            pool.wait()
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"平台库连接失败: {exc}") from exc
        missing = check_platform_tables(pool, cfg.platform_db.schema_name)
        if missing:
            raise RuntimeError(
                f"平台库缺表 {', '.join(missing)}；请先运行 maintenance init-platform"
            )
    except BaseException:
        pool.close()
        raise
    return pool


def run_agent_query(
    *,
    session_id: str,
    question: str,
    request_id: str,
    source_id: str,
    registration,
    finalize_hook=None,
    webstore=None,
    readiness=None,
    holder=None,
    session_lock=None,
):
    """每轮独立装配并执行 Runner（doc 2 §5.3 顺序：基础配置 → 登记注入 →
    build_services(source_id) → 平台池/persistence/agent → Runner）。

    session_lock：外层（execute_web_query）已持有的会话锁直接交接给 Runner，
    消除释放-重取间隙；由外层负责最终释放。"""
    from src.modules.db_deepagent.agent import build_db_agent
    from src.modules.db_deepagent.config import load_base_db_agent_config
    from src.modules.db_deepagent.persistence.archive import QaArchive
    from src.modules.db_deepagent.persistence.checkpoint import build_persistence
    from src.modules.db_deepagent.runner import DbAgentRunner, RequestIdConflictError
    from src.modules.db_deepagent.services import build_services
    from src.modules.db_deepagent.source_registry import apply_registration_to_config

    locate = {"request_id": request_id, "run_id": None}
    holder = holder if holder is not None else {}
    cfg = apply_registration_to_config(load_base_db_agent_config(), registration)
    # verify_embedding=False：readiness 在本请求内已对同一配置核验过
    # Embedding 一致性（300 秒探针缓存），不再每题重复一次 embed_query
    services = build_services(cfg, source_id=source_id, verify_embedding=False)
    platform_pool = None
    persistence = None
    try:
        platform_pool = _open_platform_pool(cfg)
        persistence = build_persistence(cfg, platform_pool=platform_pool)
        hook = finalize_hook
        if hook is None and webstore is not None and readiness is not None:
            from src.api.v2.snapshots import SnapshotStore

            snapshot_writer = SnapshotStore(
                platform_pool, max_rows=cfg.thread.snapshot_max_rows
            )
            hook = _make_finalize_hook(
                webstore,
                readiness,
                source_id,
                holder,
                results=services.results,
                snapshot_writer=snapshot_writer,
            )
        handle = build_db_agent(
            services,
            session_id=session_id,
            checkpointer=persistence.checkpointer,
            store=persistence.store,
        )
        archive = QaArchive(persistence.store)
        # 通用 sessions 绑定核对（doc 2 §4.4：新查询另须核对通用记录）
        generic = archive.get_session(session_id)
        if generic is None:
            raise codes.WebApiError(
                codes.SESSION_BINDING_MISSING,
                "通用会话绑定记录缺失，不能续聊",
                data=locate,
            )
        if str(generic.get("source_id") or "") != source_id:
            raise codes.WebApiError(
                codes.SOURCE_MISMATCH,
                "会话绑定的来源与请求来源不一致",
                data=locate,
            )
        runner = DbAgentRunner(
            services=services,
            handle=handle,
            archive=archive,
            persistence=persistence,
            finalize_hook=hook,
        )
        try:
            return runner.run(
                question,
                session_id=session_id,
                request_id=request_id,
                source_id=source_id,
                session_lock=session_lock,
            )
        except (codes.WebApiError, RequestIdConflictError):
            raise
        except Exception as exc:  # noqa: BLE001
            # Runner 已启动：异常发生时 checkpoint/历史可能已写入，本轮
            # 副作用不明，标记为"执行已开始"交由外层保留 pending（§4.4）
            raise ExecutionStartedError() from exc
    except RequestIdConflictError:
        raise codes.WebApiError(
            codes.REQUEST_ID_CONFLICT, "同会话运行标识冲突", data=locate
        )
    finally:
        # 回收失败只记录日志：若让关闭异常覆盖运行结果或
        # ExecutionStartedError，外层会按装配失败误清 pending（此时
        # checkpoint/历史可能已写入，doc 2 §4.4）
        if persistence is not None:
            _close_quietly(persistence, "persistence")
        elif platform_pool is not None:
            _close_quietly(platform_pool, "platform_pool")
        _close_quietly(services, "services")
