"""统一封装、错误闭集与入口守卫（doc 2 §4.2）。"""

from __future__ import annotations

import pytest

from src.api.v2 import codes

pytestmark = pytest.mark.unit


LOCAL = {"Host": "127.0.0.1:8008"}


class TestClosedSets:
    def test_web_api_error_rejects_unknown_code(self):
        with pytest.raises(ValueError):
            codes.WebApiError("not_a_code", "x")

    def test_error_codes_are_closed_and_mapped(self):
        for code in codes.ERROR_CODES:
            assert codes.HTTP_STATUS[code] >= 400
        # 非 200 闭集不含 HTTP 200 专用的业务终态码
        assert "partial" not in codes.ERROR_CODES
        assert codes.KNOWLEDGE_CHANGED in codes.TERMINAL_ERROR_CODES

    def test_status_property(self):
        err = codes.WebApiError(codes.SESSION_BUSY, "忙")
        assert err.status_code == 409


class TestEnvelope:
    def test_health_fixed_shape(self, web_env, client):
        r = client.get("/api/v2/health", headers=LOCAL)
        assert r.status_code == 200
        body = r.json()
        assert body["code"] == 200 and body["message"] == "success"
        assert body["data"] == {"status": "ok"}
        assert len(body["trace_id"]) == 32

    def test_unknown_route_enveloped(self, web_env, client):
        r = client.get("/api/v2/nope", headers=LOCAL)
        assert r.status_code == 404
        assert r.json()["data"]["error_code"] == "route_not_found"

    def test_method_not_allowed_keeps_allow(self, web_env, client):
        r = client.delete("/api/v2/sessions", headers=LOCAL)
        assert r.status_code == 405
        assert r.json()["data"]["error_code"] == "method_not_allowed"
        assert "Allow" in r.headers

    def test_validation_error_no_echo(self, web_env, client):
        r = client.post(
            "/api/v2/sessions",
            headers={**LOCAL, "Content-Type": "application/json"},
            json={"source_id": "not-a-uuid", "secret": "pw"},
        )
        assert r.status_code == 422
        body = r.json()
        assert body["data"]["error_code"] == "invalid_request"
        assert "secret" in body["message"] or "source_id" in body["message"]
        assert "pw" not in body["message"]

    def test_query_extra_fields_rejected(self, web_env, client):
        r = client.post(
            "/api/v2/sessions/s_" + "a" * 32 + "/query",
            headers={**LOCAL, "Content-Type": "application/json"},
            json={
                "question": "问",
                "source_id": "db_src_1",
                "request_id": "req_x",
            },
        )
        assert r.status_code == 422
        assert r.json()["data"]["error_code"] == "invalid_request"


class TestGuards:
    def test_origin_rejected(self, web_env, client):
        r = client.get(
            "/api/v2/health",
            headers={**LOCAL, "Origin": "http://evil.example.com"},
        )
        assert r.status_code == 403
        assert r.json()["data"]["error_code"] == "origin_not_allowed"

    def test_origin_allowed_passes(self, web_env, client):
        r = client.get(
            "/api/v2/health",
            headers={**LOCAL, "Origin": "http://127.0.0.1:5173"},
        )
        assert r.status_code == 200

    def test_host_rejected(self, web_env, client):
        r = client.get("/api/v2/health", headers={"Host": "evil.example.com"})
        assert r.status_code == 400
        assert r.json()["data"]["error_code"] == "host_not_allowed"

    def test_media_type_rejected_before_business(self, web_env, client):
        r = client.post(
            "/api/v2/sessions",
            headers={**LOCAL, "Content-Type": "text/plain"},
            content="x",
        )
        assert r.status_code == 415
        assert r.json()["data"]["error_code"] == "unsupported_media_type"

    def test_trace_id_on_guard_response(self, web_env, client):
        r = client.get("/api/v2/health", headers={"Host": "evil.example.com"})
        assert len(r.json()["trace_id"]) == 32
