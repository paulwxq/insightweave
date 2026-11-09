"""创建会话与查询执行流（doc 2 §4.4/§5.5.2）：写序、失效规则、收尾钩子。"""

from __future__ import annotations

import re
import threading
import uuid as uuid_mod

import pytest

from src.api.v2 import codes
from src.api.v2 import webstore as ws
from src.api.v2.readiness import ReadinessError, source_fingerprint
from src.api.v2.runs import _make_finalize_hook, create_web_session, execute_web_query
from src.tests.unit.api.v2.fakes import make_result

pytestmark = pytest.mark.unit

SID_RE = re.compile(r"^s_[0-9a-f]{32}$")
SOURCE_ID = "db_src_1"


def _hook_runner(calls=None, *, invoke_hook=True, **result_overrides):
    """fake query_runner：模拟真实 Runner——锁内调用 finalize hook，
    hook 返回替换投影时以替换结果作为终态。invoke_hook=False 模拟 P0
    前置拒绝（session_busy/lock_backend_error 不进入收尾段）。"""

    def runner(**kwargs):
        if calls is not None:
            calls.append(dict(kwargs))
        result = make_result(
            session_id=kwargs["session_id"],
            request_id=kwargs["request_id"],
            **result_overrides,
        )
        if not invoke_hook:
            return result
        hook = _make_finalize_hook(
            kwargs["webstore"],
            kwargs["readiness"],
            SOURCE_ID,
            kwargs["holder"],
        )
        replacement = hook(
            question=kwargs["question"],
            request_id=kwargs["request_id"],
            run_id=result.run_id,
            session_id=kwargs["session_id"],
            result=result,
        )
        return replacement["result"] if replacement else result

    return runner


class _AlwaysIn:
    def __contains__(self, _item):
        return True


def _flip_after(backend, attr, n, **mutations):
    """前 n 次调用按原样委托，第 n+1 次起对 backend 施加变更。"""
    state = {"n": 0}
    orig = getattr(backend, attr)

    def wrapped(*args, **kwargs):
        state["n"] += 1
        if state["n"] > n:
            for key, value in mutations.items():
                setattr(backend, key, value)
        return orig(*args, **kwargs)

    setattr(backend, attr, wrapped)
    return state


# ----------------------------------------------------------------------------
# create_web_session
# ----------------------------------------------------------------------------


class TestCreateSession:
    def test_happy_path_write_order_and_record(self, web_env):
        info = create_web_session(web_env.runtime, SOURCE_ID)
        assert SID_RE.match(info["session_id"])
        assert info["state"] == "active" and info["continuable"] is True
        assert info["turn_count"] == 0 and info["title"] == ws.DEFAULT_TITLE

        sid = info["session_id"]
        webstore = web_env.runtime.get_webstore()
        record = webstore.get_registration(sid)
        assert record["pending_run_id"] is None  # 创建收尾已确认
        assert record["source_fingerprint"] == source_fingerprint(
            web_env.backend.registration
        )
        assert record["knowledge_snapshot"]["table_schema"] == ["ws-1", "pub-ts-1"]
        assert record["knowledge_snapshot"]["rel"] == ["ws-1", "pub-rel-1"]
        # 通用绑定（写序第一步）存在且带 source_id
        generic = web_env.store.get(ws.GENERIC_SESSIONS_NS, sid)
        assert generic is not None and generic.value["source_id"] == SOURCE_ID
        assert generic.value["current_thread_id"] is None

    def test_not_ready_maps_source_not_ready(self, web_env):
        web_env.backend.business_fail = True
        with pytest.raises(codes.WebApiError) as ei:
            create_web_session(web_env.runtime, SOURCE_ID)
        assert ei.value.error_code == codes.SOURCE_NOT_READY
        assert ei.value.status_code == 409
        assert ei.value.data["readiness"]["ready"] is False

    def test_lookup_unavailable_between_checks(self, web_env):
        _flip_after(web_env.backend, "lookup_registration", 1, lookup_status="inactive")
        with pytest.raises(codes.WebApiError) as ei:
            create_web_session(web_env.runtime, SOURCE_ID)
        assert ei.value.error_code == codes.SOURCE_INACTIVE

    def test_publish_read_failure_maps_dependency(self, web_env):
        def _boom(_sid):
            raise ReadinessError("publish read failed")

        web_env.backend.load_publish_records = _boom
        with pytest.raises(codes.WebApiError) as ei:
            create_web_session(web_env.runtime, SOURCE_ID)
        assert ei.value.error_code == codes.DEPENDENCY_UNAVAILABLE

    def test_post_write_lookup_not_found_invalidates_source_changed(self, web_env):
        _flip_after(
            web_env.backend, "lookup_registration", 2, lookup_status="not_found"
        )
        with pytest.raises(codes.WebApiError) as ei:
            create_web_session(web_env.runtime, SOURCE_ID)
        assert ei.value.error_code == codes.SOURCE_CHANGED
        # 登记已失效：不留下可续聊的 active 会话
        leftovers = web_env.store.search(ws.WEB_SESSIONS_NS, limit=10)
        assert leftovers and leftovers[0].value["state"] == "invalidated"
        assert leftovers[0].value["invalid_reason"] == "source_changed"

    def test_post_write_fingerprint_change_invalidates(self, web_env):
        state = {"n": 0}
        orig = web_env.backend.lookup_registration

        def wrapped(sid):
            state["n"] += 1
            if state["n"] > 2:
                object.__setattr__(web_env.backend.registration, "port", 5433)
            return orig(sid)

        web_env.backend.lookup_registration = wrapped
        with pytest.raises(codes.WebApiError) as ei:
            create_web_session(web_env.runtime, SOURCE_ID)
        assert ei.value.error_code == codes.SOURCE_CHANGED
        rec = web_env.store.search(ws.WEB_SESSIONS_NS, limit=10)[0].value
        assert rec["invalid_reason"] == "source_changed"

    def test_post_write_snapshot_change_invalidates(self, web_env):
        state = {"n": 0}
        orig = web_env.backend.load_publish_records

        def wrapped(sid):
            state["n"] += 1
            if state["n"] > 2:
                web_env.backend.publish_run_id_rel = "pub-rel-2"
            return orig(sid)

        web_env.backend.load_publish_records = wrapped
        with pytest.raises(codes.WebApiError) as ei:
            create_web_session(web_env.runtime, SOURCE_ID)
        assert ei.value.error_code == codes.KNOWLEDGE_CHANGED
        rec = web_env.store.search(ws.WEB_SESSIONS_NS, limit=10)[0].value
        assert rec["invalid_reason"] == "knowledge_changed"

    def test_store_write_failure_maps_dependency(self, web_env):
        web_env.store.fail_writes = True
        with pytest.raises(codes.WebApiError) as ei:
            create_web_session(web_env.runtime, SOURCE_ID)
        assert ei.value.error_code == codes.DEPENDENCY_UNAVAILABLE

    def test_pending_clear_unconfirmed_leaves_protection(self, web_env, monkeypatch):
        webstore = web_env.runtime.get_webstore()

        def _boom(_sid, _rec):
            raise RuntimeError("clear failed (fake)")

        monkeypatch.setattr(webstore, "clear_pending", _boom)
        with pytest.raises(codes.WebApiError) as ei:
            create_web_session(web_env.runtime, SOURCE_ID)
        assert ei.value.error_code == codes.DEPENDENCY_UNAVAILABLE
        rec = web_env.store.search(ws.WEB_SESSIONS_NS, limit=10)[0].value
        # 收尾未确认：pending 保留 create_ 前缀，遗留规则接管
        assert rec["pending_run_id"].startswith("create_")

    def test_id_collision_maps_internal_error(self, web_env, monkeypatch):
        fixed = uuid_mod.UUID("12345678123456781234567812345678")
        monkeypatch.setattr(uuid_mod, "uuid4", lambda: fixed)
        # 预置同 ID 的 Web 登记触发碰撞检查
        web_env.store.put(
            ws.WEB_SESSIONS_NS,
            "s_" + fixed.hex,
            {
                "owner": "guest",
                "origin": "web",
                "session_id": "s_" + fixed.hex,
                "source_id": SOURCE_ID,
            },
        )
        with pytest.raises(codes.WebApiError) as ei:
            create_web_session(web_env.runtime, SOURCE_ID)
        assert ei.value.error_code == codes.INTERNAL_ERROR

    def test_lock_busy_and_backend_error(self, web_env):
        web_env.locks.busy = _AlwaysIn()
        with pytest.raises(codes.WebApiError) as ei:
            create_web_session(web_env.runtime, SOURCE_ID)
        assert ei.value.error_code == codes.SESSION_BUSY
        web_env.locks.busy = set()
        web_env.locks.backend_error = _AlwaysIn()
        with pytest.raises(codes.WebApiError) as ei:
            create_web_session(web_env.runtime, SOURCE_ID)
        assert ei.value.error_code == codes.LOCK_BACKEND_ERROR


# ----------------------------------------------------------------------------
# execute_web_query
# ----------------------------------------------------------------------------


class TestExecuteQuery:
    def test_happy_path_full_chain(self, web_env, web_session):
        calls = []
        web_env.runtime._query_runner = _hook_runner(calls)
        result = execute_web_query(
            web_env.runtime,
            session_id=web_session["session_id"],
            question="首个问题",
            source_id=SOURCE_ID,
        )
        assert result.status == "completed" and result.error_code is None
        assert result.history_saved is True
        assert result.source_id == SOURCE_ID
        # 装配参数完整传递（登记与钩子随请求传递）
        assert calls[0]["registration"] is web_env.backend.registration
        assert calls[0]["webstore"] is web_env.runtime.get_webstore()
        assert calls[0]["holder"] is not None
        # 轮次已写入、元数据增量、pending 清除
        webstore = web_env.runtime.get_webstore()
        turn = webstore.read_turn(web_session["session_id"], result.request_id)
        assert turn["question"] == "首个问题" and turn["answer"] == "答案文本"
        record = webstore.get_registration(web_session["session_id"])
        assert record["turn_count"] == 1
        assert record["title"] == "首个问题"  # 首问替换默认标题
        assert record["pending_run_id"] is None

    def test_session_not_found(self, web_env):
        with pytest.raises(codes.WebApiError) as ei:
            execute_web_query(
                web_env.runtime,
                session_id="s_" + "9" * 32,
                question="问",
                source_id=SOURCE_ID,
            )
        assert ei.value.error_code == codes.SESSION_NOT_FOUND
        assert ei.value.data["request_id"].startswith("req_")

    def test_source_mismatch(self, web_env, web_session):
        with pytest.raises(codes.WebApiError) as ei:
            execute_web_query(
                web_env.runtime,
                session_id=web_session["session_id"],
                question="问",
                source_id="db_src_2",
            )
        assert ei.value.error_code == codes.SOURCE_MISMATCH

    def _patch_record(self, web_env, sid, mutate):
        webstore = web_env.runtime.get_webstore()
        record = webstore.get_registration(sid)
        mutate(record)
        webstore.put_registration(sid, record)

    def test_metadata_invalid(self, web_env, web_session):
        sid = web_session["session_id"]
        self._patch_record(web_env, sid, lambda r: r.update(title=" "))
        with pytest.raises(codes.WebApiError) as ei:
            execute_web_query(
                web_env.runtime, session_id=sid, question="问", source_id=SOURCE_ID
            )
        assert ei.value.error_code == codes.SESSION_METADATA_INVALID
        assert "title" in ei.value.data["problem_fields"]

    def test_binding_missing(self, web_env, web_session):
        sid = web_session["session_id"]
        self._patch_record(web_env, sid, lambda r: r.pop("pending_run_id"))
        with pytest.raises(codes.WebApiError) as ei:
            execute_web_query(
                web_env.runtime, session_id=sid, question="问", source_id=SOURCE_ID
            )
        assert ei.value.error_code == codes.SESSION_BINDING_MISSING

    def test_invalidated_session_rejected(self, web_env, web_session):
        sid = web_session["session_id"]
        webstore = web_env.runtime.get_webstore()
        webstore.invalidate(sid, webstore.get_registration(sid), "knowledge_changed")
        with pytest.raises(codes.WebApiError) as ei:
            execute_web_query(
                web_env.runtime, session_id=sid, question="问", source_id=SOURCE_ID
            )
        assert ei.value.error_code == codes.SESSION_INVALIDATED
        assert "知识版本" in ei.value.message

    def test_lookup_error_before_ready(self, web_env, web_session):
        web_env.backend.lookup_status = "unsupported"
        with pytest.raises(codes.WebApiError) as ei:
            execute_web_query(
                web_env.runtime,
                session_id=web_session["session_id"],
                question="问",
                source_id=SOURCE_ID,
            )
        assert ei.value.error_code == codes.SOURCE_UNSUPPORTED
        assert ei.value.status_code == 422

    def test_light_recheck_dependency_failure(self, web_env, web_session):
        def _boom(_sid):
            raise ReadinessError("recheck failed")

        web_env.backend.load_publish_records = _boom
        with pytest.raises(codes.WebApiError) as ei:
            execute_web_query(
                web_env.runtime,
                session_id=web_session["session_id"],
                question="问",
                source_id=SOURCE_ID,
            )
        assert ei.value.error_code == codes.DEPENDENCY_UNAVAILABLE

    def test_fingerprint_change_invalidates_session(self, web_env, web_session):
        sid = web_session["session_id"]
        object.__setattr__(web_env.backend.registration, "host", "other.example.com")
        with pytest.raises(codes.WebApiError) as ei:
            execute_web_query(
                web_env.runtime, session_id=sid, question="问", source_id=SOURCE_ID
            )
        assert ei.value.error_code == codes.SOURCE_CHANGED
        rec = web_env.runtime.get_webstore().get_registration(sid)
        assert (
            rec["state"] == "invalidated" and rec["invalid_reason"] == "source_changed"
        )

    def test_snapshot_change_invalidates_session(self, web_env, web_session):
        sid = web_session["session_id"]
        web_env.backend.publish_run_id_rel = "pub-rel-9"
        with pytest.raises(codes.WebApiError) as ei:
            execute_web_query(
                web_env.runtime, session_id=sid, question="问", source_id=SOURCE_ID
            )
        assert ei.value.error_code == codes.KNOWLEDGE_CHANGED
        rec = web_env.runtime.get_webstore().get_registration(sid)
        assert rec["invalid_reason"] == "knowledge_changed"

    def test_full_check_failure_at_query(self, web_env, web_session):
        web_env.backend.business_fail = True
        web_env.readiness.invalidate(SOURCE_ID)  # 绕过创建时的 30 秒成功缓存
        with pytest.raises(codes.WebApiError) as ei:
            execute_web_query(
                web_env.runtime,
                session_id=web_session["session_id"],
                question="问",
                source_id=SOURCE_ID,
            )
        assert ei.value.error_code == codes.SOURCE_NOT_READY
        assert ei.value.data["readiness"]["ready"] is False

    def test_busy_lock_rejected_with_retry_after(self, web_env, web_session):
        sid = web_session["session_id"]
        web_env.locks.busy.add(sid)
        with pytest.raises(codes.WebApiError) as ei:
            execute_web_query(
                web_env.runtime, session_id=sid, question="问", source_id=SOURCE_ID
            )
        assert ei.value.error_code == codes.SESSION_BUSY
        assert ei.value.headers["Retry-After"] == "5"

    def test_lock_backend_error_rejected(self, web_env, web_session):
        sid = web_session["session_id"]
        web_env.locks.backend_error.add(sid)
        with pytest.raises(codes.WebApiError) as ei:
            execute_web_query(
                web_env.runtime, session_id=sid, question="问", source_id=SOURCE_ID
            )
        assert ei.value.error_code == codes.LOCK_BACKEND_ERROR
        assert ei.value.status_code == 503

    def test_leftover_pending_conservative_invalidation(self, web_env, web_session):
        sid = web_session["session_id"]
        leftover = "req_" + "c" * 28
        self._patch_record(web_env, sid, lambda r: r.update(pending_run_id=leftover))
        with pytest.raises(codes.WebApiError) as ei:
            execute_web_query(
                web_env.runtime, session_id=sid, question="问", source_id=SOURCE_ID
            )
        assert ei.value.error_code == codes.SESSION_INVALIDATED
        rec = web_env.runtime.get_webstore().get_registration(sid)
        assert rec["invalid_reason"] == "dependency_unavailable"
        assert rec["pending_run_id"] == leftover  # 保留供排查，不自动清除

    def test_lock_handoff_concurrent_request_gets_busy_not_invalidated(
        self, web_env, web_session
    ):
        """锁交接无间隙（回归：释放-重取窗口）：Runner 执行期间第二个
        请求得到 session_busy，不把在途 pending 当遗留而失效活动会话。"""
        sid = web_session["session_id"]
        entered = threading.Event()
        proceed = threading.Event()
        base = _hook_runner()

        def _blocking_runner(**kwargs):
            entered.set()  # 已进入 Runner：旧实现在此之前已释放会话锁
            assert proceed.wait(10), "测试未释放执行闸"
            return base(**kwargs)

        web_env.runtime._query_runner = _blocking_runner
        outcome: dict = {}

        def _first():
            try:
                outcome["result"] = execute_web_query(
                    web_env.runtime,
                    session_id=sid,
                    question="第一问",
                    source_id=SOURCE_ID,
                )
            except Exception as exc:  # noqa: BLE001
                outcome["error"] = exc

        thread = threading.Thread(target=_first)
        thread.start()
        try:
            assert entered.wait(10)
            with pytest.raises(codes.WebApiError) as ei:
                execute_web_query(
                    web_env.runtime,
                    session_id=sid,
                    question="第二问",
                    source_id=SOURCE_ID,
                )
            assert ei.value.error_code == codes.SESSION_BUSY
            assert ei.value.headers["Retry-After"] == "5"
            rec = web_env.runtime.get_webstore().get_registration(sid)
            assert rec["state"] == "active" and rec["invalid_reason"] is None
            assert str(rec["pending_run_id"]).startswith(
                "req_"
            )  # 在途 pending 未被误判
        finally:
            proceed.set()
            thread.join(10)
        assert not thread.is_alive()
        assert outcome["result"].status == "completed"
        rec = web_env.runtime.get_webstore().get_registration(sid)
        assert rec["pending_run_id"] is None and rec["turn_count"] == 1

    def test_pending_write_failure_skips_runner(self, web_env, web_session):
        calls = []
        web_env.runtime._query_runner = _hook_runner(calls)
        web_env.store.fail_writes = True
        try:
            with pytest.raises(codes.WebApiError) as ei:
                execute_web_query(
                    web_env.runtime,
                    session_id=web_session["session_id"],
                    question="问",
                    source_id=SOURCE_ID,
                )
        finally:
            web_env.store.fail_writes = False
        assert ei.value.error_code == codes.DEPENDENCY_UNAVAILABLE
        assert calls == []  # 保护标记未确认写入时不执行查询

    def test_runner_web_api_error_clears_pending(self, web_env, web_session):
        sid = web_session["session_id"]

        def _conflict(**_kwargs):
            raise codes.WebApiError(codes.REQUEST_ID_CONFLICT, "同会话运行标识冲突")

        web_env.runtime._query_runner = _conflict
        with pytest.raises(codes.WebApiError) as ei:
            execute_web_query(
                web_env.runtime, session_id=sid, question="问", source_id=SOURCE_ID
            )
        assert ei.value.error_code == codes.REQUEST_ID_CONFLICT
        assert (
            web_env.runtime.get_webstore().get_registration(sid)["pending_run_id"]
            is None
        )

    def test_runner_generic_error_maps_dependency(self, web_env, web_session):
        sid = web_session["session_id"]

        def _boom(**_kwargs):
            raise RuntimeError("assembly failed")

        web_env.runtime._query_runner = _boom
        with pytest.raises(codes.WebApiError) as ei:
            execute_web_query(
                web_env.runtime, session_id=sid, question="问", source_id=SOURCE_ID
            )
        assert ei.value.error_code == codes.DEPENDENCY_UNAVAILABLE
        assert (
            web_env.runtime.get_webstore().get_registration(sid)["pending_run_id"]
            is None
        )

    def test_execution_started_error_keeps_pending(self, web_env, web_session):
        # 执行已产生副作用后抛未知异常：不得清除 pending（doc 2 §4.4），
        # 下一请求按遗留 pending 保守失效（真实链路中 run_agent_query 会把
        # Runner 阶段的未知异常包装为 ExecutionStartedError）
        from src.api.v2.runs import ExecutionStartedError

        sid = web_session["session_id"]
        captured: dict = {}

        def _side_effect_then_boom(**kwargs):
            captured.update(kwargs)
            web_env.runtime.get_webstore().write_turn(
                kwargs["session_id"],
                kwargs["request_id"],
                question=kwargs["question"],
                answer="部分执行的答案",
                run_id="run_partial",
                status="completed",
            )
            raise ExecutionStartedError()

        web_env.runtime._query_runner = _side_effect_then_boom
        with pytest.raises(codes.WebApiError) as ei:
            execute_web_query(
                web_env.runtime, session_id=sid, question="问", source_id=SOURCE_ID
            )
        assert ei.value.error_code == codes.DEPENDENCY_UNAVAILABLE
        assert "未确认" in ei.value.message
        # 副作用已落盘且 pending 保留（不自动清除、不自动重放）
        assert captured["request_id"]
        record = web_env.runtime.get_webstore().read_turn(sid, captured["request_id"])
        assert record is not None and record["answer"] == "部分执行的答案"
        assert (
            web_env.runtime.get_webstore().get_registration(sid)["pending_run_id"]
            == captured["request_id"]
        )
        # 下一请求：遗留 pending → 保守失效，不重放
        with pytest.raises(codes.WebApiError) as next_err:
            execute_web_query(
                web_env.runtime, session_id=sid, question="再问", source_id=SOURCE_ID
            )
        assert next_err.value.error_code == codes.SESSION_INVALIDATED

    def test_busy_primary_result_raises_with_data_result(self, web_env, web_session):
        web_env.runtime._query_runner = _hook_runner(
            invoke_hook=False,
            status="failed",
            errors=[
                {
                    "phase": "P0",
                    "kind": "session_busy",
                    "message": "占用中",
                    "primary": True,
                }
            ],
        )
        sid = web_session["session_id"]
        with pytest.raises(codes.WebApiError) as ei:
            execute_web_query(
                web_env.runtime, session_id=sid, question="问", source_id=SOURCE_ID
            )
        assert ei.value.error_code == codes.SESSION_BUSY
        assert ei.value.headers["Retry-After"] == "5"
        assert ei.value.data["result"]["error_code"] == "session_busy"
        assert ei.value.data["result"]["history_saved"] is None
        # 前置拒绝未进执行段：pending 由本层清理
        assert (
            web_env.runtime.get_webstore().get_registration(sid)["pending_run_id"]
            is None
        )

    def test_lock_backend_primary_result_503_with_data_result(
        self, web_env, web_session
    ):
        web_env.runtime._query_runner = _hook_runner(
            invoke_hook=False,
            status="failed",
            errors=[
                {
                    "phase": "P0",
                    "kind": "lock_backend_error",
                    "message": "锁后端异常",
                    "primary": True,
                }
            ],
        )
        with pytest.raises(codes.WebApiError) as ei:
            execute_web_query(
                web_env.runtime,
                session_id=web_session["session_id"],
                question="问",
                source_id=SOURCE_ID,
            )
        assert ei.value.error_code == codes.LOCK_BACKEND_ERROR
        assert ei.value.data["result"]["error_code"] == "lock_backend_error"

    def test_special_200_encoding_error_passthrough(self, web_env, web_session):
        web_env.runtime._query_runner = _hook_runner(
            status="failed",
            errors=[
                {
                    "phase": "P4",
                    "kind": "result_encoding_error",
                    "message": "编码失败",
                    "primary": True,
                }
            ],
        )
        result = execute_web_query(
            web_env.runtime,
            session_id=web_session["session_id"],
            question="问",
            source_id=SOURCE_ID,
        )
        # HTTP 200：error_code 用执行中一致性异常优先覆盖
        assert result.error_code == "result_encoding_error"
        assert result.history_saved is True  # 钩子正常路径已写轮次

    def test_needs_clarification_answer_fallback(self, web_env, web_session):
        web_env.runtime._query_runner = _hook_runner(
            status="needs_clarification",
            answer="",
        )
        result = execute_web_query(
            web_env.runtime,
            session_id=web_session["session_id"],
            question="问个含糊的问题",
            source_id=SOURCE_ID,
        )
        assert result.error_code == "needs_clarification"
        turn = web_env.runtime.get_webstore().read_turn(
            web_session["session_id"], result.request_id
        )
        # open_questions 为空时回退到状态说明
        assert turn["answer"].startswith("本轮未生成答案（状态：needs_clarification）")

    def test_no_data_answer_fallback(self, web_env, web_session):
        web_env.runtime._query_runner = _hook_runner(status="no_data", answer="")
        result = execute_web_query(
            web_env.runtime,
            session_id=web_session["session_id"],
            question="问",
            source_id=SOURCE_ID,
        )
        assert result.error_code == "no_data"
        turn = web_env.runtime.get_webstore().read_turn(
            web_session["session_id"], result.request_id
        )
        assert turn["answer"] == "本轮未生成答案（状态：no_data）"

    def test_plain_failed_maps_query_failed(self, web_env, web_session):
        web_env.runtime._query_runner = _hook_runner(status="failed")
        result = execute_web_query(
            web_env.runtime,
            session_id=web_session["session_id"],
            question="问",
            source_id=SOURCE_ID,
        )
        assert result.error_code == "query_failed"


# ----------------------------------------------------------------------------
# finalize hook（直接单测：安全投影与 history_saved 判定）
# ----------------------------------------------------------------------------


class TestFinalizeHook:
    def _hook(self, web_env, *, results=None, snapshot_writer=None):
        webstore = web_env.runtime.get_webstore()
        holder = {}
        hook = _make_finalize_hook(
            webstore,
            web_env.readiness,
            SOURCE_ID,
            holder,
            results=results,
            snapshot_writer=snapshot_writer,
        )
        return hook, holder, webstore

    def test_normal_branch_writes_snapshot_evidence(self, web_env, web_session):
        from src.tests.unit.api.v2.fakes import make_evidence

        class _Writer:
            def __init__(self):
                self.calls = []

            def write(self, **kwargs):
                self.calls.append(kwargs)

        sid = web_session["session_id"]
        ref = "res_" + "a" * 32
        writer = _Writer()
        hook, holder, _ = self._hook(web_env, results=object(), snapshot_writer=writer)
        request_id = "req_" + "6" * 28
        hook(
            question="问",
            request_id=request_id,
            run_id="r",
            session_id=sid,
            result=make_result(
                session_id=sid,
                request_id=request_id,
                evidence=[make_evidence(result_ref=ref)],
            ),
        )
        assert holder["history_saved"] is True
        assert len(writer.calls) == 1
        assert writer.calls[0]["evidence"][0]["result_ref"] == ref

    def test_unsafe_branch_does_not_write_snapshot(self, web_env, web_session):
        from src.tests.unit.api.v2.fakes import make_evidence

        class _Writer:
            def __init__(self):
                self.calls = []

            def write(self, **kwargs):
                self.calls.append(kwargs)

        sid = web_session["session_id"]
        writer = _Writer()
        hook, holder, _ = self._hook(web_env, results=object(), snapshot_writer=writer)
        web_env.backend.publish_run_id_ts = "pub-ts-2"  # 知识变化 → unsafe 分支
        hook(
            question="问",
            request_id="req_" + "7" * 28,
            run_id="r",
            session_id=sid,
            result=make_result(
                session_id=sid,
                evidence=[make_evidence(result_ref="res_" + "b" * 32)],
            ),
        )
        assert holder["history_saved"] is True
        assert writer.calls == []

    def _setup_pending(self, webstore, sid, request_id):
        record = webstore.get_registration(sid)
        webstore.set_pending(sid, record, request_id)

    def test_changed_writes_safe_turn_and_keeps_pending(self, web_env, web_session):
        sid = web_session["session_id"]
        request_id = "req_" + "1" * 28
        hook, holder, webstore = self._hook(web_env)
        self._setup_pending(webstore, sid, request_id)
        web_env.backend.publish_run_id_ts = "pub-ts-2"  # 快照变化
        result = make_result(session_id=sid, request_id=request_id)
        replacement = hook(
            question="问",
            request_id=request_id,
            run_id="run_x",
            session_id=sid,
            result=result,
        )
        projected = replacement["result"]
        assert projected.error_code == codes.KNOWLEDGE_CHANGED
        assert projected.answer == "本轮知识一致性无法确认，请新建会话"
        assert projected.findings == [] and projected.evidence == []
        assert holder["history_saved"] is True
        record = webstore.get_registration(sid)
        assert record["invalid_reason"] == "knowledge_changed"
        assert record["pending_run_id"] == request_id  # 失效路径保留 pending
        turn = webstore.read_turn(sid, request_id)
        assert turn["status"] == "failed"

    def test_changed_with_lookup_not_found_is_source_changed(
        self, web_env, web_session
    ):
        sid = web_session["session_id"]
        request_id = "req_" + "2" * 28
        hook, holder, webstore = self._hook(web_env)
        self._setup_pending(webstore, sid, request_id)
        web_env.backend.lookup_status = "not_found"
        replacement = hook(
            question="问",
            request_id=request_id,
            run_id="run_x",
            session_id=sid,
            result=make_result(session_id=sid, request_id=request_id),
        )
        assert replacement["result"].error_code == codes.KNOWLEDGE_CHANGED
        assert webstore.get_registration(sid)["invalid_reason"] == "source_changed"

    def test_recheck_unavailable_maps_dependency(self, web_env, web_session):
        sid = web_session["session_id"]
        request_id = "req_" + "3" * 28
        hook, holder, webstore = self._hook(web_env)
        self._setup_pending(webstore, sid, request_id)

        def _boom(_sid):
            raise ReadinessError("recheck unavailable")

        web_env.readiness.light_recheck = _boom
        replacement = hook(
            question="问",
            request_id=request_id,
            run_id="run_x",
            session_id=sid,
            result=make_result(session_id=sid, request_id=request_id),
        )
        projected = replacement["result"]
        assert projected.error_code == codes.DEPENDENCY_UNAVAILABLE
        assert projected.status == "persistence_error"
        assert (
            webstore.get_registration(sid)["invalid_reason"] == "dependency_unavailable"
        )
        assert webstore.read_turn(sid, request_id)["status"] == "persistence_error"

    def test_missing_registration_history_null(self, web_env, web_session):
        sid = web_session["session_id"]
        hook, holder, _ = self._hook(web_env)
        web_env.runtime.get_webstore().put_registration(
            sid,
            {**web_env.runtime.get_webstore().get_registration(sid), "origin": "cli"},
        )
        out = hook(
            question="问",
            request_id="req_" + "4" * 28,
            run_id="r",
            session_id=sid,
            result=make_result(),
        )
        assert out is None and holder["history_saved"] is None

    def test_turn_write_failure_history_null_pending_kept(self, web_env, web_session):
        sid = web_session["session_id"]
        request_id = "req_" + "5" * 28
        hook, holder, webstore = self._hook(web_env)
        self._setup_pending(webstore, sid, request_id)
        web_env.store.fail_writes = True
        try:
            out = hook(
                question="问",
                request_id=request_id,
                run_id="r",
                session_id=sid,
                result=make_result(session_id=sid, request_id=request_id),
            )
        finally:
            web_env.store.fail_writes = False
        assert out is None
        # 写失败但回读成功确认未保存：False（确认未保存）而非 None（无法确认）
        assert holder["history_saved"] is False
        # 元数据未增量、pending 因写失败而保留保护
        record = webstore.get_registration(sid)
        assert record["turn_count"] == 0
        assert record["pending_run_id"] == request_id

    def test_second_turn_keeps_derived_title(self, web_env, web_session):
        sid = web_session["session_id"]
        web_env.runtime._query_runner = _hook_runner()
        execute_web_query(
            web_env.runtime, session_id=sid, question="第一问", source_id=SOURCE_ID
        )
        execute_web_query(
            web_env.runtime, session_id=sid, question="第二问", source_id=SOURCE_ID
        )
        record = web_env.runtime.get_webstore().get_registration(sid)
        assert record["title"] == "第一问"
        assert record["turn_count"] == 2


class TestRunAgentQueryCloseFailure:
    """Runner 启动后的资源回收失败不得覆盖运行结果或 ExecutionStartedError
    （否则外层会按装配失败误清 pending，doc 2 §4.4）。"""

    def _patch_assembly(self, monkeypatch, *, runner_run, services_close):
        from types import SimpleNamespace

        import src.api.v2.runs as runs_mod

        calls = {"services_closed": 0}

        class _Services:
            def close(self):
                calls["services_closed"] += 1
                services_close()

        class _Runner:
            def run(self, *_args, **_kwargs):
                return runner_run()

        class _Persistence:
            checkpointer = None
            store = None

            def close(self):
                pass

        archive = SimpleNamespace(get_session=lambda _sid: {"source_id": SOURCE_ID})
        monkeypatch.setattr(
            "src.modules.db_deepagent.config.load_base_db_agent_config",
            lambda: object(),
        )
        monkeypatch.setattr(
            "src.modules.db_deepagent.source_registry.apply_registration_to_config",
            lambda cfg, _registration: cfg,
        )
        monkeypatch.setattr(
            "src.modules.db_deepagent.services.build_services",
            lambda _cfg, **_kwargs: _Services(),
        )
        monkeypatch.setattr(runs_mod, "_open_platform_pool", lambda _cfg: object())
        monkeypatch.setattr(
            "src.modules.db_deepagent.persistence.checkpoint.build_persistence",
            lambda _cfg, platform_pool: _Persistence(),
        )
        monkeypatch.setattr(
            "src.modules.db_deepagent.agent.build_db_agent",
            lambda *_args, **_kwargs: object(),
        )
        monkeypatch.setattr(
            "src.modules.db_deepagent.persistence.archive.QaArchive",
            lambda _store: archive,
        )
        monkeypatch.setattr(
            "src.modules.db_deepagent.runner.DbAgentRunner",
            lambda **_kwargs: _Runner(),
        )
        return calls

    def _invoke(self):
        from src.api.v2.runs import run_agent_query
        from src.tests.unit.api.v2.fakes import make_registration

        return run_agent_query(
            session_id="s_close",
            question="问",
            request_id="req_close",
            source_id=SOURCE_ID,
            registration=make_registration(),
            finalize_hook=None,
        )

    def test_close_failure_does_not_mask_execution_started(self, monkeypatch):
        from src.api.v2.runs import ExecutionStartedError

        def _run():
            raise RuntimeError("runner boom")

        def _close():
            raise RuntimeError("close boom")

        calls = self._patch_assembly(
            monkeypatch, runner_run=_run, services_close=_close
        )
        with pytest.raises(ExecutionStartedError) as ei:
            self._invoke()
        # 原始异常是 Runner 的，不是 close 的；回收确已尝试
        assert isinstance(ei.value.__cause__, RuntimeError)
        assert str(ei.value.__cause__) == "runner boom"
        assert calls["services_closed"] == 1

    def test_close_failure_does_not_mask_result(self, monkeypatch):
        expected = make_result()

        def _run():
            return expected

        def _close():
            raise RuntimeError("close boom")

        calls = self._patch_assembly(
            monkeypatch, runner_run=_run, services_close=_close
        )
        assert self._invoke() is expected
        assert calls["services_closed"] == 1


class TestReconcileSuppression:
    """锁后端故障 → 进程内 300 秒抑制；锁忙碌保持重试语义不抑制。"""

    def test_lock_backend_error_engages_suppression(self, web_env, web_session):
        from src.api.v2.router import _run_reconcile_task

        sid = web_session["session_id"]
        web_env.locks.backend_error.add(sid)
        _run_reconcile_task(web_env.runtime, sid)
        webstore = web_env.runtime.get_webstore()
        record = webstore.get_registration(sid)
        assert webstore.suppressed(sid, record) is True

    def test_lock_construction_error_engages_suppression(
        self, web_env, web_session, monkeypatch
    ):
        from src.api.v2.router import _run_reconcile_task

        sid = web_session["session_id"]

        def _broken(_session_id):
            raise RuntimeError("lock construction failed")

        monkeypatch.setattr(web_env.runtime, "acquire_session_lock", _broken)
        _run_reconcile_task(web_env.runtime, sid)
        webstore = web_env.runtime.get_webstore()
        record = webstore.get_registration(sid)
        assert webstore.suppressed(sid, record) is True

    def test_lock_busy_not_suppressed(self, web_env, web_session):
        from src.api.v2.router import _run_reconcile_task

        sid = web_session["session_id"]
        web_env.locks.busy.add(sid)
        _run_reconcile_task(web_env.runtime, sid)
        webstore = web_env.runtime.get_webstore()
        record = webstore.get_registration(sid)
        assert webstore.suppressed(sid, record) is False
