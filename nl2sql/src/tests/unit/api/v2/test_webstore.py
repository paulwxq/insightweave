"""WebStore 登记投影、列表、轮次与元数据规则（doc 2 §4.2/§4.4）。"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from src.api.v2 import webstore as ws
from src.api.v2.webstore import (
    WebStore,
    derive_title,
    web_turn_valid,
)
from src.tests.unit.api.v2.fakes import FakeStore

pytestmark = pytest.mark.unit

NOW = datetime(
    2026, 10, 18, 12, 0, tzinfo=ws.parse_tz_datetime("2026-10-18T12:00:00+08:00").tzinfo
)


def _now_fixed():
    return datetime(2026, 10, 18, 12, 0, tzinfo=NOW.tzinfo)


@pytest.fixture()
def store():
    return WebStore(FakeStore(), now=_now_fixed)


def _registration(
    session_id="s_" + "a" * 32,
    *,
    source_id="db_src_1",
    **overrides,
):
    base = {
        "owner": "guest",
        "origin": "web",
        "session_id": session_id,
        "source_id": source_id,
        "created_at": "2026-10-17T10:00:00+08:00",
        "title": "新对话",
        "last_active_at": "2026-10-17T11:00:00+08:00",
        "turn_count": 0,
        "source_fingerprint": "fp1",
        "knowledge_snapshot": {"table_schema": ["j1", "p1"]},
        "state": "active",
        "invalid_reason": None,
        "invalidated_at": None,
        "pending_run_id": None,
        "metadata_reconciled_at": None,
        "metadata_reconcile_retry_after": None,
    }
    base.update(overrides)
    return base


class TestStateProjection:
    def test_active_valid(self):
        p = ws.state_projection(_registration())
        assert p["state"] == "active" and p["continuable"] is True
        assert p["state_valid"] is True and p["pending_run_id"] is None

    def test_active_with_pending_not_continuable(self):
        p = ws.state_projection(_registration(pending_run_id="req_" + "b" * 28))
        assert p["state"] == "active" and p["continuable"] is False

    def test_invalidated_valid(self):
        p = ws.state_projection(
            _registration(
                state="invalidated",
                invalid_reason="knowledge_changed",
                invalidated_at="2026-10-18T10:00:00+08:00",
            )
        )
        assert (
            p["state"] == "invalidated" and p["invalid_reason"] == "knowledge_changed"
        )

    def test_missing_pending_key_projected_binding_missing(self):
        rec = _registration()
        rec.pop("pending_run_id")
        p = ws.state_projection(rec)
        assert p["state"] == "invalidated"
        assert p["invalid_reason"] == "session_binding_missing"
        assert p["state_valid"] is False and p["invalidated_at"] is None

    def test_contradictory_state_projected(self):
        rec = _registration(state="active", invalid_reason="source_changed")
        p = ws.state_projection(rec)
        assert p["state_valid"] is False
        assert p["invalid_reason"] == "session_binding_missing"

    def test_invalidated_missing_time_projected(self):
        rec = _registration(
            state="invalidated", invalid_reason="source_changed", invalidated_at=None
        )
        assert ws.state_projection(rec)["state_valid"] is False

    def test_unknown_reason_projected(self):
        rec = _registration(
            state="invalidated",
            invalid_reason="something_else",
            invalidated_at="2026-10-18T10:00:00+08:00",
        )
        assert ws.state_projection(rec)["state_valid"] is False


class TestDisplayFields:
    def test_valid(self):
        assert ws.display_field_problems(_registration()) == []

    def test_missing_created(self):
        rec = _registration()
        rec["created_at"] = "not-a-time"
        assert "created_at" in ws.display_field_problems(rec)

    def test_last_before_created(self):
        rec = _registration(
            created_at="2026-10-18T10:00:00+08:00",
            last_active_at="2026-10-17T10:00:00+08:00",
        )
        assert "last_active_at<created_at" in ws.display_field_problems(rec)

    def test_blank_title_and_bool_turn_count(self):
        rec = _registration(title="  ", turn_count=True)
        problems = ws.display_field_problems(rec)
        assert "title" in problems and "turn_count" in problems


class TestOwnership:
    def test_cli_session_not_web(self):
        rec = _registration()
        rec["origin"] = "cli"
        assert not ws.core_ownership_ok(rec, rec["session_id"])

    def test_session_id_mismatch(self):
        rec = _registration()
        assert not ws.core_ownership_ok(rec, "s_" + "b" * 32)

    def test_bad_source_uuid(self):
        rec = _registration(source_id="db_src_x")
        assert not ws.core_ownership_ok(rec, rec["session_id"])


class TestListSessions:
    def _put(self, store, sid, **overrides):
        store.put_registration(sid, _registration(session_id=sid, **overrides))

    def test_sort_last_active_desc_then_sid_asc(self, store):
        self._put(store, "s_" + "0" * 32, last_active_at="2026-10-17T11:00:00+08:00")
        self._put(store, "s_" + "1" * 32, last_active_at="2026-10-18T09:00:00+08:00")
        # 同刻并列：session_id 升序
        self._put(store, "s_" + "a" * 32, last_active_at="2026-10-17T11:00:00+08:00")
        self._put(store, "s_" + "9" * 32, last_active_at="2026-10-17T11:00:00+08:00")
        infos, has_more, next_offset, skipped, _all, _filtered, _turns_all = (
            store.list_session_infos()
        )
        order = [i["session_id"] for i in infos]
        assert order == [
            "s_" + "1" * 32,
            "s_" + "0" * 32,
            "s_" + "9" * 32,
            "s_" + "a" * 32,
        ]

    def test_source_filter(self, store):
        self._put(store, "s_" + "1" * 32)
        self._put(store, "s_" + "2" * 32, source_id="db_src_2")
        infos, _, _, _, total_all, total_filtered, _turns_all = store.list_session_infos(
            source_id="db_src_1"
        )
        assert total_all == 2 and total_filtered == 1
        assert [i["session_id"] for i in infos] == ["s_" + "1" * 32]

    def test_invalid_metadata_skipped_with_fields(self, store):
        self._put(store, "s_" + "1" * 32)
        self._put(store, "s_" + "2" * 32, title="")
        infos, _, _, skipped, total_all, total_filtered, _turns_all = (
            store.list_session_infos()
        )
        assert total_all == 1 and total_filtered == 1
        assert len(infos) == 1
        assert skipped[0]["session_id"] == "s_" + "2" * 32
        assert "title" in skipped[0]["problem_fields"]

    def test_pagination_counts_valid_only(self, store):
        for i in range(5):
            self._put(store, f"s_{i:032d}")
        self._put(store, "s_" + "z" * 32, turn_count=-1)
        infos, has_more, next_offset, _, total_all, _filtered, _turns_all = (
            store.list_session_infos(limit=2, offset=0)
        )
        assert total_all == 5
        assert len(infos) == 2 and has_more
        assert next_offset == 2
        infos2, has_more2, next2, _, _, _, _ = store.list_session_infos(limit=3, offset=2)
        assert len(infos2) == 3 and not has_more2 and next2 is None

    def test_scan_limit_maps_to_exception(self, store, monkeypatch):
        self._put(store, "s_" + "1" * 32)
        monkeypatch.setattr(
            ws.ScanBudget,
            "create",
            classmethod(lambda cls, **kw: ws.ScanBudget(max_records=0, deadline=1e18)),
        )
        with pytest.raises(ws.ScanLimitExceeded):
            store.list_session_infos()

    def test_store_failure_raises_unavailable(self, store):
        store._store.fail_reads = True
        with pytest.raises(ws.StoreUnavailable):
            store.list_session_infos()

    def test_title_search_is_case_sensitive_and_ignores_bad_records(self, store):
        self._put(store, "s_" + "1" * 32, title="Order", source_id="db_src_1")
        self._put(store, "s_" + "2" * 32, title="order", source_id="db_src_2")
        self._put(store, "s_" + "3" * 32, title=None)
        self._put(store, "s_" + "4" * 32, title=12)
        self._put(store, "s_" + "5" * 32, title="   ")
        bare = _registration(session_id="s_" + "6" * 32)
        del bare["title"]
        store.put_registration("s_" + "6" * 32, bare)
        calls: list[dict] = []
        original = store._store.search

        def wrapped(*args, **kwargs):
            calls.append(kwargs)
            return original(*args, **kwargs)

        store._store.search = wrapped
        infos, _, _, skipped, total_all, total_filtered, _turns_all = (
            store.list_session_infos(q="Order")
        )
        assert [item["session_id"] for item in infos] == ["s_" + "1" * 32]
        assert total_all == 2 and total_filtered == 1
        assert {item["session_id"] for item in skipped} == {
            "s_" + "3" * 32,
            "s_" + "4" * 32,
            "s_" + "5" * 32,
            "s_" + "6" * 32,
        }
        assert calls and all("filter" not in call and "query" not in call for call in calls)

        infos, _, _, _, total_all, total_filtered, _turns_all = store.list_session_infos(
            source_id="db_src_2", q="order"
        )
        assert [item["session_id"] for item in infos] == ["s_" + "2" * 32]
        assert total_all == 2 and total_filtered == 1

        infos, _, _, _, total_all, total_filtered, _turns_all = store.list_session_infos(q="   ")
        assert total_all == total_filtered == 2 and len(infos) == 2

        long_keyword = "问" * 41
        infos, _, _, _, total_all, total_filtered, _turns_all = store.list_session_infos(
            q=long_keyword
        )
        assert infos == [] and total_all == 2 and total_filtered == 0

    def test_question_tail_beyond_title_is_not_searched(self, store):
        question = "前" * 40 + "后半段独有"
        self._put(store, "s_" + "1" * 32, title=derive_title(question))
        infos, _, _, _, _, total_filtered, _turns_all = store.list_session_infos(
            q="后半段独有"
        )
        assert infos == [] and total_filtered == 0
        infos, _, _, _, _, total_filtered, _turns_all = store.list_session_infos(
            q="前" * 40
        )
        assert len(infos) == 1 and total_filtered == 1

    def test_total_turns_all_before_filter_and_pagination(self, store):
        # doc 12 §5 任务 1：total_turns_all 在 total_all 后、来源/标题筛选与
        # 排序分页前累计，覆盖全部有效会话而不只当前页。
        self._put(
            store,
            "s_" + "a" * 32,
            turn_count=3,
            last_active_at="2026-10-18T09:00:00+08:00",
        )
        self._put(
            store,
            "s_" + "b" * 32,
            turn_count=2,
            state="invalidated",
            invalid_reason="knowledge_changed",
            invalidated_at="2026-10-18T10:00:00+08:00",
            last_active_at="2026-10-18T08:00:00+08:00",
        )
        self._put(
            store,
            "s_" + "c" * 32,
            turn_count=0,
            last_active_at="2026-10-17T11:00:00+08:00",
        )
        # 展示字段损坏的两条坏登记：跳过且计入 skipped_invalid
        self._put(store, "s_" + "d" * 32, turn_count=99, title="   ")
        self._put(store, "s_" + "e" * 32, turn_count=-1)
        # 非 Web（归属不符）登记：静默跳过，不进 skipped_invalid
        self._put(store, "s_" + "f" * 32, turn_count=50, origin="cli")

        for offset in (0, 1, 2):
            (
                infos,
                has_more,
                next_offset,
                skipped,
                total_all,
                total_filtered,
                total_turns_all,
            ) = store.list_session_infos(limit=1, offset=offset)
            assert total_all == 3 and total_turns_all == 5
            assert total_filtered == 3 and len(infos) == 1
            assert has_more == (offset < 2)
            assert next_offset == (offset + 1 if offset < 2 else None)
            assert {item["session_id"] for item in skipped} == {
                "s_" + "d" * 32,
                "s_" + "e" * 32,
            }

        infos, _, _, _, _, total_filtered, total_turns_all = store.list_session_infos(
            source_id="db_src_404"
        )
        assert infos == [] and total_filtered == 0 and total_turns_all == 5

        infos, _, _, _, _, total_filtered, total_turns_all = store.list_session_infos(
            q="不存在的标题"
        )
        assert infos == [] and total_filtered == 0 and total_turns_all == 5

    def test_total_turns_all_empty_and_zero_turn_sessions(self, store):
        # doc 12 §5 任务 1：空 Store 与全零 turn_count 的空值语义。
        infos, _, _, skipped, total_all, total_filtered, total_turns_all = (
            store.list_session_infos()
        )
        assert (infos, skipped, total_all, total_filtered, total_turns_all) == (
            [],
            [],
            0,
            0,
            0,
        )
        for i in range(2):
            self._put(store, f"s_{i:032d}", turn_count=0)
        infos, _, _, _, total_all, total_filtered, total_turns_all = (
            store.list_session_infos()
        )
        assert len(infos) == 2 and total_all == 2 and total_filtered == 2
        assert total_turns_all == 0


class TestTurns:
    def _turn(self, request_id="req_" + "1" * 28, **overrides):
        base = {
            "question": "问",
            "answer": "答",
            "request_id": request_id,
            "run_id": "run_x",
            "status": "completed",
            "saved_at": "2026-10-18T10:00:00+08:00",
        }
        base.update(overrides)
        return base

    def test_turn_valid_predicate(self):
        assert web_turn_valid("req_x", self._turn(request_id="req_x"))
        assert not web_turn_valid("req_x", self._turn(request_id="req_y"))
        assert not web_turn_valid("req_x", self._turn(question="  "))
        assert not web_turn_valid("req_x", self._turn(answer=""))
        assert not web_turn_valid("req_x", self._turn(saved_at="2026-10-18T10:00:00"))
        assert not web_turn_valid("req_x", self._turn(saved_at="bad"))
        assert not web_turn_valid("req_x", None)
        # run_id/status 为审计字段：缺失不影响有效性
        assert web_turn_valid(
            "req_x", self._turn(request_id="req_x", run_id=None, status=None)
        )

    def test_list_turns_projects_display_and_drops_extra_fields(self, store):
        sid = "s_" + "3" * 32
        store.write_turn(
            sid,
            "req_a",
            question="q",
            answer="a",
            run_id="r",
            status="completed",
            display={
                "elapsed_ms": 380,
                "evidence": [
                    {
                        "sql": "SELECT 1",
                        "result_ref": "res_abc",
                        "row_count": 6,
                        "source_truncated": False,
                        "operation": {"secret": 1},
                    }
                ],
            },
        )
        turns, has_more, _next = store.list_turns(sid)
        assert not has_more
        item = turns[0]
        assert item["elapsed_ms"] == 380
        assert item["evidence"] == [
            {
                "sql": "SELECT 1",
                "result_ref": "res_abc",
                "row_count": 6,
                "source_truncated": False,
            }
        ]
        assert isinstance(item["saved_at"], str) and item["saved_at"]

    def test_list_turns_order_and_pagination(self, store):
        sid = "s_" + "1" * 32
        store.write_turn(
            sid, "req_b", question="q2", answer="a2", run_id="r", status="completed"
        )
        store.write_turn(
            sid, "req_a", question="q1", answer="a1", run_id="r", status="completed"
        )
        turns, has_more, next_offset = store.list_turns(sid)
        assert [t["question"] for t in turns] == ["q1", "q2"]
        assert not has_more and next_offset is None
        turns, has_more, next_offset = store.list_turns(sid, limit=1)
        assert has_more and next_offset == 1

    def test_invalid_turn_record_fails_whole_request(self, store):
        sid = "s_" + "1" * 32
        store.write_turn(
            sid, "req_a", question="q", answer="a", run_id="r", status="completed"
        )
        store._store.put(ws.web_turns_ns(sid), "req_bad", {"question": "x"})
        with pytest.raises(ws.InvalidTurnRecord):
            store.list_turns(sid)


class TestReconcile:
    def test_reconcile_metadata_rules(self, store):
        sid = "s_" + "1" * 32
        # 已有合法标题（含默认“新对话”）保留：§4.4 校正不重写标题
        store.put_registration(sid, _registration(session_id=sid))
        store.write_turn(
            sid,
            "req_b",
            question="  第二个问题  ",
            answer="a2",
            run_id="r",
            status="completed",
        )
        store._store.put(
            ws.web_turns_ns(sid),
            "req_a",
            {
                "question": "首问标题来源",
                "answer": "a1",
                "request_id": "req_a",
                "run_id": "r",
                "status": "completed",
                "saved_at": "2026-10-17T12:00:00+08:00",
            },
        )
        updated = store.reconcile_metadata(sid, store.get_registration(sid))
        assert updated["turn_count"] == 2
        assert updated["title"] == "新对话"
        assert updated["metadata_reconciled_at"] == _now_fixed().isoformat()
        assert updated["metadata_reconcile_retry_after"] is None

    def test_reconcile_derives_title_when_invalid(self, store):
        sid = "s_" + "2" * 32
        store.put_registration(sid, _registration(session_id=sid, title=" "))
        store.write_turn(
            sid,
            "req_a",
            question="首问标题来源",
            answer="a1",
            run_id="r",
            status="completed",
        )
        updated = store.reconcile_metadata(sid, store.get_registration(sid))
        assert updated["title"] == "首问标题来源"

    def test_reconcile_bad_created_raises(self, store):
        sid = "s_" + "1" * 32
        store.put_registration(sid, _registration(session_id=sid, created_at="bad"))
        with pytest.raises(ws.SessionMetadataInvalid):
            store.reconcile_metadata(sid, store.get_registration(sid))

    def test_reconcile_needed_freshness_and_backoff(self, store):
        rec = _registration()
        # 缺失 → 需要
        assert store.reconcile_needed(rec) is True
        rec["metadata_reconciled_at"] = _now_fixed().isoformat()
        assert store.reconcile_needed(rec) is False
        rec["metadata_reconciled_at"] = (
            _now_fixed() - timedelta(seconds=301)
        ).isoformat()
        assert store.reconcile_needed(rec) is True
        # 未来 60 秒内容忍
        rec["metadata_reconciled_at"] = (
            _now_fixed() + timedelta(seconds=30)
        ).isoformat()
        assert store.reconcile_needed(rec) is False
        # 退避：合法未来 ≤1h 阻止
        rec["metadata_reconciled_at"] = (
            _now_fixed() - timedelta(seconds=301)
        ).isoformat()
        rec["metadata_reconcile_retry_after"] = (
            _now_fixed() + timedelta(seconds=60)
        ).isoformat()
        assert store.reconcile_needed(rec) is False
        # 超过 1h 的退避视为非法
        rec["metadata_reconcile_retry_after"] = (
            _now_fixed() + timedelta(seconds=7200)
        ).isoformat()
        assert store.reconcile_needed(rec) is True

    def test_suppression_release_on_value_change(self, store):
        sid = "s_" + "1" * 32
        rec = _registration(session_id=sid)
        store.put_registration(sid, rec)
        store.mark_suppressed(sid, rec)
        assert store.suppressed(sid, rec) is True
        # 持久化时间值变化即解除
        rec2 = dict(rec, metadata_reconciled_at=_now_fixed().isoformat())
        assert store.suppressed(sid, rec2) is False


class TestTitle:
    def test_derive_title_40_codepoints(self):
        assert derive_title("  " + "问" * 50) == "问" * 40
        assert derive_title("   ") == "新对话"
