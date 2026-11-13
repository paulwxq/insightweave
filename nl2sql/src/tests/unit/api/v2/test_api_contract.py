"""/api/v2 接口契约测试（doc 2 §4.2/§4.5）：TestClient 走真实路由与封装。"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from src.api.v2 import webstore as ws
from src.api.v2.runs import create_web_session
from src.modules.db_deepagent.context import DbAgentContext
from src.modules.db_deepagent.services.result_store import ResultStore
from src.tests.unit.api.v2.fakes import make_result

pytestmark = pytest.mark.unit

LOCAL = {"Host": "127.0.0.1:8008"}
JSON_HEAD = {**LOCAL, "Content-Type": "application/json"}
SOURCE_ID = "db_src_1"
OTHER_SOURCE = "db_src_2"

_COLUMNS = [
    {"name": "id", "pg_type": "integer"},
    {"name": "amount", "pg_type": "numeric"},
]


def test_max_display_rows_config(monkeypatch):
    from src.api.v2.router import _max_display_rows

    monkeypatch.setattr(
        "src.services.config_loader.get_config",
        lambda: {"api": {"max_display_rows": 50}},
    )
    assert _max_display_rows() == 50
    monkeypatch.setattr(
        "src.services.config_loader.get_config",
        lambda: {"api": {"max_display_rows": 0}},
    )
    assert _max_display_rows() == 200


def _ctx(session_id: str) -> DbAgentContext:
    return DbAgentContext(
        session_id=session_id,
        thread_id=f"{session_id}-1",
        request_id="req-contract",
        run_id="run-contract",
        datasource_id="orders",
        validation_fingerprint="fp",
        now=datetime(2026, 10, 13, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
        timezone="Asia/Shanghai",
        user_texts=("契约测试",),
        carried_evidence=(),
        deadline_monotonic=0.0,
    )


def _hook_runner(**result_overrides):
    """契约层 fake runner：调用 finalize hook 后返回终态（同真实 Runner）。"""

    def runner(**kwargs):
        from src.api.v2.runs import _make_finalize_hook

        result = make_result(
            session_id=kwargs["session_id"],
            request_id=kwargs["request_id"],
            **result_overrides,
        )
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


@pytest.fixture()
def api_session(web_env):
    """经业务层创建的会话（供多数端点共用）。"""
    return create_web_session(web_env.runtime, SOURCE_ID)


class TestReadinessEndpoint:
    def test_ready(self, client):
        r = client.get(f"/api/v2/sources/{SOURCE_ID}/readiness", headers=LOCAL)
        assert r.status_code == 200
        data = r.json()["data"]
        assert data["compatible"] is True and data["ready"] is True
        names = {c["name"] for c in data["checks"]}
        assert {
            "registry",
            "publish_records",
            "knowledge_consistency",
            "embedding",
        } <= names
        assert "error_code" not in data

    def test_source_not_found(self, web_env, client):
        web_env.backend.lookup_status = "not_found"
        web_env.readiness.invalidate(SOURCE_ID)  # 绕过 30 秒成功缓存
        r = client.get(f"/api/v2/sources/{SOURCE_ID}/readiness", headers=LOCAL)
        assert r.status_code == 404
        assert r.json()["data"]["error_code"] == "source_not_found"

    def test_not_ready_returns_200_with_checks(self, web_env, client):
        """兼容且完成检查（含业务库不匹配/缺知识）：200、ready=false、
        data 直接为 readiness（doc 2 §5.5），前端可展示检查项。"""
        web_env.backend.business_fail = True
        web_env.readiness.invalidate(SOURCE_ID)
        r = client.get(f"/api/v2/sources/{SOURCE_ID}/readiness", headers=LOCAL)
        assert r.status_code == 200
        data = r.json()["data"]
        assert data["compatible"] is True and data["ready"] is False
        fails = [c for c in data["checks"] if c["state"] == "fail"]
        assert fails and fails[0]["error_code"] == "business_target_mismatch"
        assert "error_code" not in data

    def test_dependency_failure_503_with_readiness_payload(self, web_env, client):
        """依赖读取无法完成：503 错误封装，data 附 readiness（doc 2 §5.5）。"""
        web_env.backend.dependency_fail = True
        web_env.readiness.invalidate(SOURCE_ID)
        r = client.get(f"/api/v2/sources/{SOURCE_ID}/readiness", headers=LOCAL)
        assert r.status_code == 503
        body = r.json()
        assert body["data"]["error_code"] == "dependency_unavailable"
        assert body["data"]["readiness"]["ready"] is False

    def test_bad_uuid_422(self, client):
        r = client.get("/api/v2/sources/not-a-uuid/readiness", headers=LOCAL)
        assert r.status_code == 422
        assert r.json()["data"]["error_code"] == "invalid_request"


class TestSessionsEndpoints:
    def test_create_detail_turns_flow(self, client):
        r = client.post(
            "/api/v2/sessions", headers=JSON_HEAD, json={"source_id": SOURCE_ID}
        )
        assert r.status_code == 200
        info = r.json()["data"]
        assert info["session_id"].startswith("s_")
        assert info["state"] == "active" and info["continuable"] is True

        sid = info["session_id"]
        detail = client.get(f"/api/v2/sessions/{sid}", headers=LOCAL)
        assert detail.status_code == 200
        assert detail.json()["data"]["session_id"] == sid

        turns = client.get(f"/api/v2/sessions/{sid}/turns", headers=LOCAL)
        assert turns.status_code == 200
        assert turns.json()["data"]["items"] == []

    def test_detail_not_found(self, web_env, client):
        r = client.get("/api/v2/sessions/s_" + "9" * 32, headers=LOCAL)
        assert r.status_code == 404
        assert r.json()["data"]["error_code"] == "session_not_found"

    def test_detail_bad_id_format(self, web_env, client):
        r = client.get("/api/v2/sessions/not%20a%20sid", headers=LOCAL)
        assert r.status_code == 422
        assert r.json()["data"]["error_code"] == "invalid_request"

    def test_detail_metadata_invalid(self, web_env, client, api_session):
        sid = api_session["session_id"]
        webstore = web_env.runtime.get_webstore()
        record = webstore.get_registration(sid)
        record["title"] = " "
        webstore.put_registration(sid, record)
        r = client.get(f"/api/v2/sessions/{sid}", headers=LOCAL)
        assert r.status_code == 409
        assert r.json()["data"]["error_code"] == "session_metadata_invalid"
        assert "title" in r.json()["data"]["problem_fields"]

    def test_list_filter_and_pagination(self, web_env, client):
        a = create_web_session(web_env.runtime, SOURCE_ID)
        b = create_web_session(web_env.runtime, OTHER_SOURCE)
        r = client.get(f"/api/v2/sessions?source_id={SOURCE_ID}", headers=LOCAL)
        ids = [i["session_id"] for i in r.json()["data"]["items"]]
        assert a["session_id"] in ids and b["session_id"] not in ids

        r = client.get("/api/v2/sessions?limit=1", headers=LOCAL)
        data = r.json()["data"]
        assert len(data["items"]) == 1 and data["has_more"] is True
        assert data["next_offset"] == 1

    def test_list_title_search_totals_and_q_bounds(self, web_env, client):
        first = create_web_session(web_env.runtime, SOURCE_ID)
        second = create_web_session(web_env.runtime, OTHER_SOURCE)
        webstore = web_env.runtime.get_webstore()
        for info, title in ((first, "Order"), (second, "order")):
            record = webstore.get_registration(info["session_id"])
            record["title"] = title
            webstore.put_registration(info["session_id"], record)

        exact = client.get("/api/v2/sessions?q=Order", headers=LOCAL)
        assert exact.status_code == 200
        data = exact.json()["data"]
        assert [item["session_id"] for item in data["items"]] == [first["session_id"]]
        assert data["total_filtered"] == 1 and data["total_all"] == 2

        blank = client.get("/api/v2/sessions?q=%20", headers=LOCAL)
        assert blank.status_code == 200
        assert blank.json()["data"]["total_filtered"] == 2

        accepted = client.get("/api/v2/sessions?q=" + ("测" * 100), headers=LOCAL)
        assert accepted.status_code == 200
        assert accepted.json()["data"]["total_filtered"] == 0
        assert accepted.json()["data"]["total_all"] == 2

        rejected = client.get("/api/v2/sessions?q=" + ("测" * 101), headers=LOCAL)
        assert rejected.status_code == 422
        assert rejected.json()["data"]["error_code"] == "invalid_request"

    def test_list_total_turns_all_contract(self, web_env, client):
        # doc 12 §5 任务 1：total_turns_all 契约——不受筛选影响、随列表返回。
        # 登记缓存设为 3/2 并把 metadata_reconciled_at 固定为当前时间、
        # retry_after 置 None：新鲜校正时间使 reconcile_needed 为 False，
        # 防止列表读取按需安排的异步元数据校正把 turn_count 重算为日志
        # 条数（0）导致统计回退；勿删这两个字段赋值。
        first = create_web_session(web_env.runtime, SOURCE_ID)
        second = create_web_session(web_env.runtime, OTHER_SOURCE)
        webstore = web_env.runtime.get_webstore()
        for info, count in ((first, 3), (second, 2)):
            record = webstore.get_registration(info["session_id"])
            record["turn_count"] = count
            record["metadata_reconciled_at"] = webstore.now_iso()
            record["metadata_reconcile_retry_after"] = None
            webstore.put_registration(info["session_id"], record)

        r = client.get("/api/v2/sessions?limit=1", headers=LOCAL)
        assert r.status_code == 200
        data = r.json()["data"]
        assert len(data["items"]) == 1 and data["has_more"] is True
        assert data["next_offset"] == 1
        assert data["total_all"] == 2 and data["total_filtered"] == 2
        assert data["total_turns_all"] == 5

        r = client.get("/api/v2/sessions?q=" + "不存在的标题", headers=LOCAL)
        assert r.status_code == 200
        data = r.json()["data"]
        assert data["items"] == [] and data["total_filtered"] == 0
        assert data["total_all"] == 2 and data["total_turns_all"] == 5

    def test_list_store_failure_503(self, web_env, client, api_session):
        web_env.store.fail_reads = True
        r = client.get("/api/v2/sessions", headers=LOCAL)
        assert r.status_code == 503
        assert r.json()["data"]["error_code"] == "dependency_unavailable"


class TestTurnsEndpoint:
    def test_order_and_d3_shape(self, web_env, client, api_session):
        sid = api_session["session_id"]
        webstore = web_env.runtime.get_webstore()
        webstore.write_turn(
            sid,
            "req_a",
            question="第一问",
            answer="答一",
            run_id="r",
            status="completed",
        )
        webstore.write_turn(
            sid,
            "req_b",
            question="第二问",
            answer="答二",
            run_id="r",
            status="completed",
        )
        r = client.get(f"/api/v2/sessions/{sid}/turns", headers=LOCAL)
        assert r.status_code == 200
        items = r.json()["data"]["items"]
        assert [t["question"] for t in items] == ["第一问", "第二问"]
        # 未附带展示投影的旧写入：证据为空，不把内部字段透出；
        # doc 6.3 §8.3：open_questions 保存值透传（缺省 None=未知）
        assert all(
            set(t.keys())
            == {
                "question",
                "answer",
                "saved_at",
                "elapsed_ms",
                "evidence",
                "open_questions",
            }
            for t in items
        )
        assert all(t["evidence"] == [] and t["elapsed_ms"] is None for t in items)
        assert all(t["open_questions"] is None for t in items)

    def test_session_not_found(self, web_env, client):
        r = client.get("/api/v2/sessions/s_" + "8" * 32 + "/turns", headers=LOCAL)
        assert r.status_code == 404

    def test_invalid_record_500(self, web_env, client, api_session):
        sid = api_session["session_id"]
        web_env.store.put(ws.web_turns_ns(sid), "req_bad", {"question": "x"})
        r = client.get(f"/api/v2/sessions/{sid}/turns", headers=LOCAL)
        assert r.status_code == 500
        assert r.json()["data"]["error_code"] == "history_record_invalid"

    def test_scan_limit_503(self, web_env, client, api_session, monkeypatch):
        sid = api_session["session_id"]
        web_env.runtime.get_webstore().write_turn(
            sid, "req_a", question="问", answer="答", run_id="r", status="completed"
        )
        monkeypatch.setattr(
            ws.ScanBudget,
            "create",
            classmethod(lambda cls, **kw: ws.ScanBudget(max_records=0, deadline=1e18)),
        )
        r = client.get(f"/api/v2/sessions/{sid}/turns", headers=LOCAL)
        assert r.status_code == 503
        assert r.json()["data"]["error_code"] == "history_unavailable"


class TestQueryEndpoint:
    def test_happy_path(self, web_env, client, api_session):
        web_env.runtime._query_runner = _hook_runner()
        sid = api_session["session_id"]
        r = client.post(
            f"/api/v2/sessions/{sid}/query",
            headers=JSON_HEAD,
            json={"question": "契约层提问", "source_id": SOURCE_ID},
        )
        assert r.status_code == 200
        data = r.json()["data"]
        assert data["status"] == "completed" and data["error_code"] is None
        assert data["history_saved"] is True
        assert data["session_id"] == sid and data["source_id"] == SOURCE_ID
        assert data["request_id"].startswith("req_")
        assert isinstance(data["stats"], dict) and "elapsed_ms" in data["stats"]
        # 轮次随后可在历史中读取
        turns = client.get(f"/api/v2/sessions/{sid}/turns", headers=LOCAL)
        assert turns.json()["data"]["items"][0]["question"] == "契约层提问"

    def test_snapshot_table_missing_rejects_query(
        self, web_env, client, api_session, monkeypatch
    ):
        web_env.runtime._query_runner = _hook_runner()
        monkeypatch.setattr(web_env.runtime, "snapshot_table_ready", lambda: False)
        sid = api_session["session_id"]
        r = client.post(
            f"/api/v2/sessions/{sid}/query",
            headers=JSON_HEAD,
            json={"question": "表未建", "source_id": SOURCE_ID},
        )
        assert r.status_code == 503
        assert r.json()["data"]["error_code"] == "dependency_unavailable"

    def test_rate_limited_with_retry_after(self, web_env, client, api_session):
        web_env.runtime._query_runner = _hook_runner()
        web_env.runtime.rate_limit_per_minute = 1
        sid = api_session["session_id"]
        first = client.post(
            f"/api/v2/sessions/{sid}/query",
            headers=JSON_HEAD,
            json={"question": "第一问", "source_id": SOURCE_ID},
        )
        assert first.status_code == 200
        second = client.post(
            f"/api/v2/sessions/{sid}/query",
            headers=JSON_HEAD,
            json={"question": "第二问", "source_id": SOURCE_ID},
        )
        assert second.status_code == 429
        assert second.json()["data"]["error_code"] == "rate_limited"
        assert int(second.headers["Retry-After"]) >= 1

    def test_busy_conflict_with_headers(self, web_env, client, api_session):
        sid = api_session["session_id"]
        web_env.locks.busy.add(sid)
        r = client.post(
            f"/api/v2/sessions/{sid}/query",
            headers=JSON_HEAD,
            json={"question": "问", "source_id": SOURCE_ID},
        )
        assert r.status_code == 409
        assert r.json()["data"]["error_code"] == "session_busy"
        assert r.headers["Retry-After"] == "5"

    def test_session_not_found(self, web_env, client):
        r = client.post(
            "/api/v2/sessions/s_" + "7" * 32 + "/query",
            headers=JSON_HEAD,
            json={"question": "问", "source_id": SOURCE_ID},
        )
        assert r.status_code == 404
        assert r.json()["data"]["error_code"] == "session_not_found"

    def test_blank_question_422(self, web_env, client, api_session):
        r = client.post(
            f"/api/v2/sessions/{api_session['session_id']}/query",
            headers=JSON_HEAD,
            json={"question": "   ", "source_id": SOURCE_ID},
        )
        assert r.status_code == 422
        assert r.json()["data"]["error_code"] == "invalid_request"

    def test_invalidated_session_409(self, web_env, client, api_session):
        sid = api_session["session_id"]
        webstore = web_env.runtime.get_webstore()
        webstore.invalidate(sid, webstore.get_registration(sid), "knowledge_changed")
        r = client.post(
            f"/api/v2/sessions/{sid}/query",
            headers=JSON_HEAD,
            json={"question": "问", "source_id": SOURCE_ID},
        )
        assert r.status_code == 409
        assert r.json()["data"]["error_code"] == "session_invalidated"


class TestResultEndpoint:
    @pytest.fixture()
    def result_env(self, web_env, tmp_path, monkeypatch):
        """结果读取指向 tmp 工作区（不碰真实 workspace）。"""
        fake_cfg = SimpleNamespace(workspace=SimpleNamespace(root=str(tmp_path)))
        monkeypatch.setattr(
            "src.modules.db_deepagent.config.load_base_db_agent_config",
            lambda: fake_cfg,
        )
        return ResultStore(tmp_path)

    def _save(self, store, sid, rows):
        return store.save(
            ctx=_ctx(sid),
            purpose="query",
            sql_template="SELECT 1",
            executed_sql="SELECT 1",
            columns=_COLUMNS,
            rows=rows,
            source_truncated=False,
            derived_from=[],
            guard_evidence={"time": [], "dim": []},
        )

    def test_read_v2_cells_and_pagination(
        self, web_env, client, api_session, result_env
    ):
        sid = api_session["session_id"]
        meta = self._save(
            result_env,
            sid,
            [(1, Decimal("9.90")), (2, Decimal("8.80")), (3, Decimal("7.70"))],
        )
        r = client.get(
            f"/api/v2/sessions/{sid}/results/{meta.result_ref}?limit=2",
            headers=LOCAL,
        )
        assert r.status_code == 200
        data = r.json()["data"]
        assert [c["name"] for c in data["columns"]] == ["id", "amount"]
        assert data["rows"][0] == [
            {"kind": "integer", "value": "1"},
            {"kind": "decimal", "value": "9.90"},
        ]
        assert data["has_more"] is True and data["next_offset"] == 2
        assert data["saved_row_count"] == 3 and data["source_truncated"] is False
        assert data["max_display_rows"] == 200
        assert data["data_from_snapshot"] is False

        r2 = client.get(
            f"/api/v2/sessions/{sid}/results/{meta.result_ref}?limit=2&offset=2",
            headers=LOCAL,
        )
        data2 = r2.json()["data"]
        assert len(data2["rows"]) == 1 and data2["has_more"] is False

    def test_unknown_ref_404(self, web_env, client, api_session, result_env):
        sid = api_session["session_id"]
        r = client.get(f"/api/v2/sessions/{sid}/results/res_{'0' * 32}", headers=LOCAL)
        assert r.status_code == 404
        assert r.json()["data"]["error_code"] == "result_not_found"

    def test_malformed_ref_404(self, web_env, client, api_session):
        r = client.get(
            f"/api/v2/sessions/{api_session['session_id']}/results/res_xyz",
            headers=LOCAL,
        )
        assert r.status_code == 404

    def test_other_session_denied(self, web_env, client, api_session, result_env):
        """URL 会话登记有效、结果文件属于另一会话：404/result_not_found（Q2）。"""
        sid = api_session["session_id"]
        other = create_web_session(web_env.runtime, SOURCE_ID)["session_id"]
        meta = self._save(result_env, sid, [(1, Decimal("1.00"))])
        r = client.get(
            f"/api/v2/sessions/{other}/results/{meta.result_ref}",
            headers=LOCAL,
        )
        assert r.status_code == 404
        assert r.json()["data"]["error_code"] == "result_not_found"

    def test_missing_registration_404(self, web_env, client, api_session, result_env):
        """先核对 Web 会话登记（doc 2 §8.1）：登记缺失时不读结果文件。"""
        sid = api_session["session_id"]
        meta = self._save(result_env, sid, [(1, Decimal("1.00"))])
        web_env.store.delete(ws.WEB_SESSIONS_NS, sid)
        r = client.get(
            f"/api/v2/sessions/{sid}/results/{meta.result_ref}", headers=LOCAL
        )
        assert r.status_code == 404
        assert r.json()["data"]["error_code"] == "session_not_found"

    def test_encoding_error_500(
        self, web_env, client, api_session, result_env, monkeypatch
    ):
        """结果无法安全读取/转码：500/result_encoding_error（doc 2 §4.2），
        不混同参数非法。"""
        sid = api_session["session_id"]
        meta = self._save(result_env, sid, [(1, Decimal("1.00"))])
        from src.modules.db_deepagent.errors import ResultStoreError

        def _boom(_self, *_args, **_kwargs):
            raise ResultStoreError("result_encoding_error", "未知 cell kind (fake)")

        monkeypatch.setattr(ResultStore, "read_rows_cells", _boom)
        r = client.get(
            f"/api/v2/sessions/{sid}/results/{meta.result_ref}", headers=LOCAL
        )
        assert r.status_code == 500
        assert r.json()["data"]["error_code"] == "result_encoding_error"

    def test_snapshot_fallback_when_files_missing(self, web_env, client, api_session):
        sid = api_session["session_id"]
        ref = "res_" + "c" * 32
        web_env.snapshots.rows[(sid, ref)] = {
            "rows_snapshot": [[{"kind": "integer", "value": "7"}]],
            "snapshot_row_count": 1,
            "saved_row_count": 1,
            "meta_jsonb": {
                "columns": [{"name": "id", "pg_type": "int4"}],
                "source_truncated": False,
            },
        }
        r = client.get(f"/api/v2/sessions/{sid}/results/{ref}?limit=10", headers=LOCAL)
        assert r.status_code == 200
        data = r.json()["data"]
        assert data["data_from_snapshot"] is True
        assert data["snapshot_rows_available"] == 1
        assert data["max_display_rows"] == 200
        assert data["rows"] == [[{"kind": "integer", "value": "7"}]]

    def test_export_file_all_rows_without_sha(
        self, web_env, client, api_session, result_env
    ):
        """导出不校验 SHA：文件内容变化仍导出全部行。"""
        import json as _json

        sid = api_session["session_id"]
        meta = self._save(result_env, sid, [(1, Decimal("1.00")), (2, Decimal("2.00"))])
        path = result_env._root / meta.result_ref / "rows.jsonl"
        lines = path.read_text(encoding="utf-8").splitlines()
        row = _json.loads(lines[0])
        row[0]["value"] = "99"
        lines[0] = _json.dumps(row, ensure_ascii=False)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        r = client.get(
            f"/api/v2/sessions/{sid}/results/{meta.result_ref}/export",
            headers=LOCAL,
        )
        assert r.status_code == 200
        assert "text/csv" in r.headers["content-type"]
        assert f"{meta.result_ref}.csv" in r.headers["content-disposition"]
        text = r.content.decode("utf-8-sig")
        assert text.splitlines()[0] == "id,amount"
        assert len(text.splitlines()) == 3
        assert "99" in text

    def test_export_snapshot_fallback(self, web_env, client, api_session):
        sid = api_session["session_id"]
        ref = "res_" + "d" * 32
        web_env.snapshots.rows[(sid, ref)] = {
            "rows_snapshot": [
                [{"kind": "integer", "value": "7"}],
                [{"kind": "integer", "value": "8"}],
            ],
            "snapshot_row_count": 2,
            "saved_row_count": 2,
            "meta_jsonb": {
                "columns": [{"name": "id", "pg_type": "int4"}],
                "source_truncated": False,
            },
        }
        r = client.get(f"/api/v2/sessions/{sid}/results/{ref}/export", headers=LOCAL)
        assert r.status_code == 200
        assert f"{ref}.snapshot.csv" in r.headers["content-disposition"]
        text = r.content.decode("utf-8-sig")
        assert text.splitlines() == ["id", "7", "8"]

    def test_export_other_session_denied(
        self, web_env, client, api_session, result_env
    ):
        sid = api_session["session_id"]
        other = create_web_session(web_env.runtime, SOURCE_ID)["session_id"]
        meta = self._save(result_env, sid, [(1, Decimal("1.00"))])
        r = client.get(
            f"/api/v2/sessions/{other}/results/{meta.result_ref}/export",
            headers=LOCAL,
        )
        assert r.status_code == 404
        assert r.json()["data"]["error_code"] == "result_not_found"
