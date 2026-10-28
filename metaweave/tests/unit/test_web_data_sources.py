"""Web 数据源登记：真实路由、服务与校验，替换外部数据库边界。

断代改造后 source_id 为 `db_src_N` 短 ID（由数据库序列生成）；
删除数据源遇已关联工作区时 409 拒绝（doc 6.2 §6.1、A29），
无工作区时才允许删除并可清理该源 output/ 目录。
"""

import logging
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import psycopg
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy.exc import SQLAlchemyError

from backend.api.cors import parse_cors_origins, validate_cors_origin
from backend.api import main as api_main
from backend.api.main import create_app
from backend.api.routers import data_sources as source_router
from backend.data_sources import connection_check, registration_service
from backend.persistence.models import DbSourceInfo
from backend.persistence.platform_db import PlatformSettings
from backend.security.source_password import decrypt_password, encrypt_password
from metaweave.core.metadata.db_step import DbProfileValidationError, SchemaNoUsageError


class FakeSourceCursor:
    def __init__(self, connection):
        self.connection = connection
        self.sql = ""
        self.params = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, params=None):
        self.sql = sql
        self.params = params

    def fetchone(self):
        if "SHOW server_version_num" in self.sql:
            return (self.connection.version,)
        if "FROM pg_namespace WHERE nspname" in self.sql:
            name = self.params[0]
            if name not in self.connection.schemas:
                return None
            return (self.connection.schemas[name],)
        raise AssertionError(self.sql)

    def fetchall(self):
        if "FROM pg_namespace" in self.sql:
            return [
                (name, allowed) for name, allowed in self.connection.schemas.items()
            ]
        raise AssertionError(self.sql)


class FakeSourceConnection:
    def __init__(self, version="150006", schemas=None):
        self.version = version
        self.schemas = schemas or {
            "sales": True,
            "public": True,
            "hidden": False,
            "pg_catalog": True,
            "pg_temp_4": True,
        }
        self.closed = False
        self.autocommit = False

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.closed = True
        return False

    def cursor(self):
        return FakeSourceCursor(self)


class FakePlatformSession:
    """覆盖登记/列表/删除链的最小假会话（source_id 为短 ID）。"""

    def __init__(self, state):
        self.state = state
        self.pending = None
        self.pending_delete = None
        self.fail_get = bool(state["fail_get"])
        self.fail_next_commit = bool(state["fail_next_commit"])

    def _rows(self):
        return self.state["rows"]

    def get(self, _model, source_id):
        if self.fail_get:
            raise SQLAlchemyError("query failed")
        for row in self._rows():
            if row.source_id == source_id:
                return row
        return None

    def scalars(self, stmt):
        if stmt.whereclause is not None:
            clause = stmt.whereclause
            conditions = getattr(clause, "clauses", None) or [clause]
            return [
                row
                for row in self._rows()
                if all(
                    getattr(row, condition.left.name) == condition.right.value
                    for condition in conditions
                )
            ]
        return sorted(
            self._rows(),
            key=lambda row: (-row.created_at.timestamp(), str(row.source_id)),
        )

    def scalar(self, stmt):
        text_sql = str(stmt)
        if "workspace_info" in text_sql and "count" in text_sql:
            params = stmt.compile().params
            source_id = params.get("source_id_1")
            return sum(
                1 for row in self.state["workspaces"] if row.source_id == source_id
            )
        raise AssertionError(text_sql)

    def execute(self, stmt):
        if getattr(stmt, "is_delete", False):
            params = stmt.compile().params
            source_id = params.get("source_id_1")
            table = stmt.table.name
            pool = {
                "db_profile": "profiles",
                "table_definition": "definitions",
                "table_profile": "table_profiles",
            }[table]
            self.state[pool][:] = [
                row for row in self.state[pool] if row.source_id != source_id
            ]
            return None
        text_sql = str(stmt)
        if "collection_run_info" in text_sql:
            activity = self.state["collection_activity"]
        elif "publish_run_info" in text_sql:
            activity = self.state["publish_activity"]
        else:
            raise AssertionError(text_sql)
        grouped: dict = {}
        for source_id, moment in activity:
            current = grouped.get(source_id)
            if current is None or moment > current:
                grouped[source_id] = moment
        return SimpleNamespace(all=lambda: list(grouped.items()))

    def add(self, row):
        self.pending = row

    def delete(self, row):
        self.pending_delete = row

    def commit(self):
        if self.fail_next_commit:
            self.fail_next_commit = False
            self.state["fail_next_commit"] = False
            raise SQLAlchemyError("commit failed")
        if self.pending_delete is not None:
            self._rows().remove(self.pending_delete)
            self.pending_delete = None
        if self.pending is not None:
            row = self.pending
            # 模拟数据库默认值：短 ID 序列、created_at、is_active（doc 6.1 §13）
            self.state["id_sequence"] += 1
            row.source_id = f"db_src_{self.state['id_sequence']}"
            row.created_at = datetime(2026, 9, 23, 8, 30, 1, 123456, timezone.utc)
            row.is_active = True
            self._rows().append(row)
            self.pending = None
        for row in self._rows():
            updated_at = getattr(row, "updated_at", None)
            if updated_at is not None and not isinstance(updated_at, datetime):
                row.updated_at = datetime(2026, 9, 23, 10, 0, tzinfo=timezone.utc)


@pytest.fixture
def web_env(monkeypatch):
    rows = []
    connections = []
    connection_kwargs = []
    state = {
        "version": "150006",
        "schemas": None,
        "source_connects": 0,
        "platform_sessions": 0,
        "id_sequence": 0,
        "rows": rows,
        "workspaces": [],
        "profiles": [],
        "definitions": [],
        "table_profiles": [],
        "collection_activity": [],
        "publish_activity": [],
        "fail_get": False,
        "fail_next_commit": False,
    }

    def fake_connect(**kwargs):
        assert state["platform_sessions"] == 0
        state["source_connects"] += 1
        connection_kwargs.append(kwargs)
        conn = FakeSourceConnection(state["version"], state["schemas"])
        connections.append(conn)
        return conn

    @contextmanager
    def fake_platform_session(_settings):
        state["platform_sessions"] += 1
        try:
            session = FakePlatformSession(state)
            yield session
        finally:
            state["platform_sessions"] -= 1

    monkeypatch.setattr(connection_check.psycopg, "connect", fake_connect)
    monkeypatch.setattr(registration_service, "platform_session", fake_platform_session)
    monkeypatch.setenv("SOURCE_CREDENTIAL_KEY", Fernet.generate_key().decode())
    settings = PlatformSettings(
        host="platform", port=5432, name="metaweave", user="admin", password="secret"
    )
    with TestClient(
        create_app(settings, cors_origins=[]),
        raise_server_exceptions=False,
    ) as client:
        yield client, rows, connections, connection_kwargs, state


def payload(**overrides):
    result = {
        "db_type": "postgresql",
        "host": "db.example.test",
        "port": 5432,
        "db_name": "orders",
        "username": "reader",
        "password": "distinct-web-test-secret",
        "sslmode": None,
    }
    result.update(overrides)
    return result


def create_payload(**overrides):
    result = {
        **payload(),
        "display_name": "订单业务库",
        "selected_schemas": ["public"],
        "description": "  业务数据  ",
    }
    result.update(overrides)
    return result


def make_source(**overrides):
    values = {
        "source_id": "db_src_1",
        "display_name": "已有数据源",
        "db_type": "postgresql",
        "host": "db.example.test",
        "port": 5432,
        "db_name": "orders",
        "username": "reader",
        "password_ciphertext": "opaque-ciphertext",
        "sslmode": None,
        "selected_schemas": ["public"],
        "is_active": True,
        "description": None,
        "db_version": None,
        "created_by_user_id": None,
        "created_at": datetime(2026, 9, 22, 8, 0, 0, tzinfo=timezone.utc),
    }
    values.update(overrides)
    return DbSourceInfo(**values)


# ---- 连接检查与登记 ----


def test_connection_check_returns_sorted_names_and_closes_connection(web_env):
    client, _rows, connections, kwargs, _state = web_env
    response = client.post("/api/data-sources/connection-check", json=payload())
    assert response.status_code == 200
    assert response.json() == {"server_version": "15.6", "schemas": ["public", "sales"]}
    assert connections[0].closed is True
    assert kwargs[0]["host"] == "db.example.test"
    assert kwargs[0]["port"] == 5432
    assert kwargs[0]["password"] == "distinct-web-test-secret"
    assert "sslmode" not in kwargs[0]


@pytest.mark.parametrize("password", ["", None])
def test_open_source_connection_rejects_blank_password(monkeypatch, password):
    def fail_connect(**_kwargs):
        raise AssertionError("空密码不应进入 psycopg")

    monkeypatch.setattr(connection_check.psycopg, "connect", fail_connect)
    fields = connection_check.SourceConnectionInput(
        db_type="postgresql",
        host="db.example.test",
        port=5432,
        db_name="orders",
        username="reader",
        password=password,
    )
    with pytest.raises(DbProfileValidationError, match="源库密码不能为空"):
        with connection_check.open_source_connection(fields):
            pass


def test_create_returns_short_id_then_list_safe_fields(web_env):
    client, rows, connections, kwargs, _state = web_env
    created = client.post("/api/data-sources", json=create_payload(sslmode="require"))
    assert created.status_code == 201
    item = created.json()
    # 短 ID 由登记侧生成（doc 6.1 §13）：db_src_ 前缀、非 UUID
    assert item["source_id"].startswith("db_src_")
    assert len(item["source_id"]) != 36
    assert item["description"] == "业务数据"
    assert item["created_at"] == "2026-09-23T08:30:01.123456Z"
    assert item["is_active"] is True  # 新登记的数据源恒为激活（doc 11 §3）
    assert rows[0].password_ciphertext != "distinct-web-test-secret"
    assert kwargs[0]["sslmode"] == "require"
    assert all(conn.closed for conn in connections)

    listed = client.get("/api/data-sources")
    assert listed.status_code == 200
    assert listed.json() == {
        "items": [{**item, "last_collected_at": None, "last_published_at": None}]
    }
    assert item["db_version"] == "15.6"
    assert item["sslmode"] == "require"
    assert set(item) == {
        "source_id",
        "display_name",
        "db_type",
        "host",
        "port",
        "db_name",
        "username",
        "sslmode",
        "selected_schemas",
        "description",
        "db_version",
        "created_at",
        "is_active",
        "last_collected_at",
        "last_published_at",
    }
    fetched = client.get(f"/api/data-sources/{item['source_id']}")
    assert fetched.status_code == 200
    assert fetched.json()["source_id"] == item["source_id"]


def test_duplicate_reports_all_matches_without_connecting_source(web_env):
    client, rows, _connections, _kwargs, state = web_env
    first = make_source(source_id="db_src_1", display_name="第一条")
    second = make_source(source_id="db_src_2", display_name="第二条")
    rows.extend([first, second])
    response = client.post("/api/data-sources", json=create_payload())
    assert response.status_code == 409
    body = response.json()
    assert body["code"] == "duplicate_source"
    assert body["details"] == [
        {"source_id": "db_src_1", "display_name": "第一条"},
        {"source_id": "db_src_2", "display_name": "第二条"},
    ]
    assert state["source_connects"] == 0
    assert len(rows) == 2

    confirmed = client.post(
        "/api/data-sources", json=create_payload(confirm_duplicate=True)
    )
    assert confirmed.status_code == 201
    assert state["source_connects"] == 1
    assert len(rows) == 3


@pytest.mark.parametrize(
    ("field", "different_value"),
    [
        ("host", "another.example.test"),
        ("port", 5433),
        ("db_name", "another_db"),
        ("username", "another_user"),
    ],
)
def test_one_different_connection_field_is_not_duplicate(
    web_env, field, different_value
):
    client, rows, _connections, _kwargs, state = web_env
    rows.append(make_source(**{field: different_value}))

    response = client.post("/api/data-sources", json=create_payload())

    assert response.status_code == 201
    assert state["source_connects"] == 1
    assert len(rows) == 2


@pytest.mark.parametrize(
    ("submitted", "code"),
    [
        ({"db_type": "mysql"}, "invalid_request"),
        pytest.param({"port": 70000}, "invalid_request"),
        pytest.param({"hostaddr": "127.0.0.1"}, "invalid_request"),
    ],
)
def test_invalid_request_is_rejected_before_source_connection(web_env, submitted, code):
    client, rows, _connections, _kwargs, state = web_env
    response = client.post(
        "/api/data-sources/connection-check", json=payload(**submitted)
    )
    assert response.status_code == 422
    # 严格 schema（doc 11 契约）恢复后：db_type/port 在请求层即被 422 拒绝
    assert response.json() == {"code": "invalid_request", "message": "请求字段不合法"}
    assert state["source_connects"] == 0
    assert not rows


def test_version_and_schema_failures_do_not_insert(web_env):
    client, rows, connections, _kwargs, state = web_env
    state["version"] = "140012"
    response = client.post("/api/data-sources/connection-check", json=payload())
    assert response.status_code == 422
    assert response.json()["code"] == "unsupported_postgresql_version"
    assert "details" not in response.json()
    assert connections[-1].closed is True

    state["version"] = "150006"
    state["schemas"] = {"public": False, "sales": True}
    response = client.post("/api/data-sources", json=create_payload())
    assert response.status_code == 422
    assert response.json()["code"] == "schema_no_usage"
    assert connections[-1].closed is True
    assert not rows

    response = client.post(
        "/api/data-sources", json=create_payload(selected_schemas=["missing"])
    )
    assert response.status_code == 422
    assert response.json()["code"] == "schema_missing"
    assert not rows

    response = client.post(
        "/api/data-sources", json=create_payload(selected_schemas=["pg_catalog"])
    )
    assert response.status_code == 422
    assert response.json()["code"] == "schema_not_selectable"
    assert not rows


def test_request_validation_and_logs_do_not_disclose_password(web_env, caplog):
    client, _rows, _connections, _kwargs, _state = web_env
    secret = "C3-web-password-unique-9831"
    caplog.set_level(logging.DEBUG)
    response = client.post(
        "/api/data-sources/connection-check",
        json=payload(password=secret, port="bad"),
    )
    assert response.status_code == 422
    assert response.json() == {"code": "invalid_request", "message": "请求字段不合法"}
    assert secret not in str(response.json())
    assert secret not in "\n".join(record.getMessage() for record in caplog.records)


def test_error_mapper_uses_domain_exception_class(web_env, monkeypatch):
    client, _rows, _connections, _kwargs, _state = web_env

    def fail(_settings, _fields):
        raise SchemaNoUsageError("包含不应透传的内部细节")

    monkeypatch.setattr(source_router, "create_data_source", fail)
    response = client.post("/api/data-sources", json=create_payload())
    assert response.status_code == 422
    assert response.json() == {
        "code": "schema_no_usage",
        "message": "对所选 Schema 没有访问权限",
    }


def test_source_connection_failure_is_safe(web_env, monkeypatch):
    client, rows, _connections, _kwargs, _state = web_env
    driver_detail = "driver-secret-detail-9831"

    def fail_connect(**_kwargs):
        raise psycopg.OperationalError(driver_detail)

    monkeypatch.setattr(connection_check.psycopg, "connect", fail_connect)
    response = client.post("/api/data-sources/connection-check", json=payload())

    assert response.status_code == 422
    assert response.json() == {
        "code": "connection_failed",
        "message": "源库连接或查询失败",
    }
    assert driver_detail not in response.text
    assert not rows


def test_platform_failure_is_safe_and_does_not_connect_source(web_env, monkeypatch):
    client, rows, _connections, _kwargs, state = web_env
    database_detail = "platform-secret-detail-9831"

    def fail_platform_session(_settings):
        raise SQLAlchemyError(database_detail)

    monkeypatch.setattr(registration_service, "platform_session", fail_platform_session)
    response = client.post("/api/data-sources", json=create_payload())

    assert response.status_code == 503
    assert response.json() == {
        "code": "platform_unavailable",
        "message": "平台库暂不可用",
    }
    assert database_detail not in response.text
    assert state["source_connects"] == 0
    assert not rows


@pytest.mark.parametrize("credential_key", [None, "not-a-fernet-key"])
def test_bad_credential_key_is_internal_error_not_platform_failure(
    web_env, monkeypatch, caplog, credential_key
):
    client, rows, connections, _kwargs, _state = web_env
    if credential_key is None:
        monkeypatch.delenv("SOURCE_CREDENTIAL_KEY")
    else:
        monkeypatch.setenv("SOURCE_CREDENTIAL_KEY", credential_key)
    caplog.set_level(logging.ERROR)

    response = client.post("/api/data-sources", json=create_payload())

    assert response.status_code == 500
    assert response.json() == {
        "code": "internal_error",
        "message": "服务暂时无法处理请求",
    }
    assert "distinct-web-test-secret" not in response.text
    assert "distinct-web-test-secret" not in "\n".join(
        record.getMessage() for record in caplog.records
    )
    assert connections[0].closed is True
    assert not rows


# ---- 列表：排序与最近活动时间 ----


def test_list_uses_created_at_order_and_no_source_connection(web_env):
    client, rows, _connections, _kwargs, state = web_env
    rows.extend(
        [
            make_source(
                source_id="db_src_1",
                display_name="旧",
                created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            ),
            make_source(
                source_id="db_src_2",
                display_name="新",
                created_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
            ),
        ]
    )
    response = client.get("/api/data-sources")
    assert response.status_code == 200
    assert [item["display_name"] for item in response.json()["items"]] == ["新", "旧"]
    assert state["source_connects"] == 0
    assert response.json()["items"][0]["last_collected_at"] is None
    assert response.json()["items"][0]["last_published_at"] is None


def test_list_uses_latest_collection_and_publish_times(web_env):
    client, rows, _connections, _kwargs, state = web_env
    source = make_source(source_id="db_src_1", display_name="订单库")
    other = make_source(source_id="db_src_2", display_name="另一库")
    rows.extend([source, other])
    state["collection_activity"].extend(
        [
            ("db_src_1", datetime(2026, 9, 20, 1, 0, tzinfo=timezone.utc)),
            ("db_src_1", datetime(2026, 9, 27, 8, 30, tzinfo=timezone.utc)),
            ("db_src_2", datetime(2026, 9, 1, 0, 0, tzinfo=timezone.utc)),
        ]
    )
    state["publish_activity"].extend(
        [
            ("db_src_1", datetime(2026, 9, 21, 2, 0, tzinfo=timezone.utc)),
            ("db_src_1", datetime(2026, 9, 28, 7, 48, 35, tzinfo=timezone.utc)),
        ]
    )
    response = client.get("/api/data-sources")
    assert response.status_code == 200
    by_name = {item["display_name"]: item for item in response.json()["items"]}
    assert by_name["订单库"]["last_collected_at"] == "2026-09-27T08:30:00.000000Z"
    assert by_name["订单库"]["last_published_at"] == "2026-09-28T07:48:35.000000Z"
    assert by_name["另一库"]["last_collected_at"] == "2026-09-01T00:00:00.000000Z"
    assert by_name["另一库"]["last_published_at"] is None


def test_naive_registered_time_yields_controlled_internal_error(web_env):
    """doc 19.3 §5 异常验收：驱动返回无时区时间时，API 落受控 500。"""
    client, rows, _connections, _kwargs, state = web_env
    source = make_source(source_id="db_src_1", display_name="订单库")
    rows.append(source)
    state["collection_activity"].append(
        ("db_src_1", datetime(2026, 9, 27, 8, 30))  # 无时区：违反时间契约
    )
    response = client.get("/api/data-sources")
    assert response.status_code == 500
    assert response.json() == {
        "code": "internal_error",
        "message": "服务暂时无法处理请求",
    }


# ---- CORS ----


@pytest.mark.parametrize(
    "origin",
    [
        "http://127.0.0.1:5173",
        "http://localhost:5173",
        "https://xn--r8jz45g.jp:5173",
        "http://[::1]:5173",
    ],
)
def test_validate_cors_origin_accepts_canonical_values(origin):
    assert validate_cors_origin(origin) == origin


@pytest.mark.parametrize(
    "origin",
    [
        "http://127.0.0.1:5173/",
        "HTTP://127.0.0.1:5173",
        "http://LOCALHOST:5173",
        "http://example.com:80",
        "https://example.com:443",
        "http://例え.jp:5173",
        "http://127.0.0.1:5173/api",
        "http://user:secret@example.com",
        "http://exa mple.com",
        "http://127.1:5173",
        "http://a:0",
        "*",
        " http://localhost:5173 ",
    ],
)
def test_validate_cors_origin_rejects_noncanonical_values(origin):
    with pytest.raises(ValueError):
        validate_cors_origin(origin)


def test_parse_cors_origins_handles_empty_variable_and_separator_whitespace():
    assert parse_cors_origins(None) == ()
    assert parse_cors_origins(" \t ") == ()
    assert parse_cors_origins(" http://a:1 ,\nhttp://b:2 ") == (
        "http://a:1",
        "http://b:2",
    )


@pytest.mark.parametrize(
    ("raw", "index"),
    [
        ("http://a:1,", 2),
        ("http://a:1,,http://b:2", 2),
        (",http://a:1", 1),
        ("http://a:1,http://b:2/", 2),
    ],
)
def test_parse_cors_origins_reports_invalid_item_position(raw, index):
    with pytest.raises(ValueError, match=f"PLATFORM_CORS_ORIGINS 第 {index} 项"):
        parse_cors_origins(raw)


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        ("http://a:1,http://127.1:5173", "IPv4 地址必须是规范形式"),
        ("http://a:0", "端口必须在 1–65535 之间"),
    ],
)
def test_parse_cors_origins_keeps_specific_reason_without_echoing_value(raw, reason):
    with pytest.raises(ValueError) as error:
        parse_cors_origins(raw)
    assert reason in str(error.value)

    secret = "distinct-cors-password"
    with pytest.raises(ValueError) as credential_error:
        parse_cors_origins(f"http://user:{secret}@a:1")
    assert secret not in str(credential_error.value)


def test_create_app_cors_injection_is_independent_of_env(monkeypatch):
    settings = PlatformSettings(
        host="platform", port=5432, name="metaweave", user="admin", password="secret"
    )
    monkeypatch.setenv("PLATFORM_CORS_ORIGINS", "http://env.example:5173")
    monkeypatch.setattr(api_main, "load_project_env", lambda: None)
    preflight = {
        "Origin": "http://env.example:5173",
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "Content-Type",
    }

    with TestClient(create_app(settings), raise_server_exceptions=False) as client:
        response = client.options("/api/data-sources", headers=preflight)
        assert response.status_code == 200
        assert response.headers["access-control-allow-origin"] == preflight["Origin"]

    with TestClient(create_app(settings, cors_origins=[])) as client:
        response = client.options("/api/data-sources", headers=preflight)
        assert response.status_code == 400
        assert "access-control-allow-origin" not in response.headers

    with pytest.raises(ValueError):
        create_app(settings, cors_origins=[" http://env.example:5173 "])


def test_cors_allows_handled_responses_but_not_unexpected_500(web_env, monkeypatch):
    _client, rows, _connections, _kwargs, _state = web_env
    origin = "http://127.0.0.1:5173"
    settings = PlatformSettings(
        host="platform", port=5432, name="metaweave", user="admin", password="secret"
    )
    app = create_app(settings, cors_origins=[origin])

    @app.get("/api/unexpected-error")
    def unexpected_error():
        raise RuntimeError("internal-secret-detail")

    with TestClient(app, raise_server_exceptions=False) as client:
        allowed = {"Origin": origin}
        response = client.get("/api/data-sources", headers=allowed)
        assert response.status_code == 200
        assert response.headers["access-control-allow-origin"] == origin

        response = client.post("/api/data-sources", headers=allowed, json={})
        assert response.status_code == 422
        assert response.headers["access-control-allow-origin"] == origin

        rows.append(make_source())
        response = client.post(
            "/api/data-sources", headers=allowed, json=create_payload()
        )
        assert response.status_code == 409
        assert response.headers["access-control-allow-origin"] == origin

        def fail_platform_session(_settings):
            raise SQLAlchemyError("platform-secret-detail")

        monkeypatch.setattr(
            registration_service, "platform_session", fail_platform_session
        )
        response = client.get("/api/data-sources", headers=allowed)
        assert response.status_code == 503
        assert response.headers["access-control-allow-origin"] == origin

        response = client.get("/api/unexpected-error", headers=allowed)
        assert response.status_code == 500
        assert response.json()["code"] == "internal_error"
        assert "access-control-allow-origin" not in response.headers

        response = client.get(
            "/api/unexpected-error", headers={"Origin": "http://unlisted:5173"}
        )
        assert response.status_code == 500
        assert "access-control-allow-origin" not in response.headers


# ---- 修改登记 ----


def update_payload(source, **overrides):
    result = {
        "db_type": source.db_type,
        "host": source.host,
        "port": source.port,
        "db_name": source.db_name,
        "username": source.username,
        "sslmode": source.sslmode,
        "display_name": source.display_name,
        "selected_schemas": list(source.selected_schemas),
        "description": source.description,
    }
    result.update(overrides)
    return result


def test_list_and_get_return_saved_sslmode_and_null_version(web_env):
    client, rows, _connections, _kwargs, state = web_env
    source = make_source(sslmode="require", db_version=None)
    rows.append(source)
    listed = client.get("/api/data-sources")
    assert listed.status_code == 200
    assert listed.json()["items"][0]["sslmode"] == "require"
    assert listed.json()["items"][0]["db_version"] is None
    fetched = client.get(f"/api/data-sources/{source.source_id}")
    assert fetched.status_code == 200
    assert fetched.json()["sslmode"] == "require"
    assert fetched.json()["db_version"] is None
    assert "password" not in fetched.json()
    assert state["source_connects"] == 0


def test_saved_connection_check_uses_stored_password_and_does_not_write(web_env):
    client, rows, connections, kwargs, state = web_env
    source = make_source(
        password_ciphertext=encrypt_password("stored-secret"), sslmode="require"
    )
    rows.append(source)
    # 直接调用服务层（响应模型见 /connection-check 用例）
    settings = PlatformSettings(
        host="platform", port=5432, name="metaweave", user="admin", password="secret"
    )
    result = registration_service.check_saved_connection(settings, source.source_id)
    assert result["server_version"] == "15.6"
    assert result["schemas"] == ["public", "sales"]
    assert kwargs[0]["password"] == "stored-secret"
    assert kwargs[0]["host"] == source.host
    assert kwargs[0]["sslmode"] == "require"
    assert connections[0].closed is True
    assert rows[0].password_ciphertext != "stored-secret"
    assert state["source_connects"] == 1
    assert len(rows) == 1


def test_patch_without_password_keeps_ciphertext_and_updates_name(web_env):
    client, rows, _connections, _kwargs, state = web_env
    ciphertext = encrypt_password("stored-secret")
    source = make_source(password_ciphertext=ciphertext, sslmode="require")
    created_at = source.created_at
    rows.append(source)
    response = client.patch(
        f"/api/data-sources/{source.source_id}",
        json=update_payload(source, display_name="新的订单库"),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["source_id"] == source.source_id
    assert body["display_name"] == "新的订单库"
    assert body["created_at"] == "2026-09-22T08:00:00.000000Z"
    assert body["db_version"] == "15.6"
    assert body["sslmode"] == "require"
    assert rows[0].password_ciphertext == ciphertext
    assert rows[0].created_at == created_at
    assert rows[0].source_id == source.source_id
    assert state["source_connects"] == 1
    listed = client.get("/api/data-sources")
    assert listed.json()["items"][0]["display_name"] == "新的订单库"
    assert len(rows) == 1


def test_patch_keeps_disabled_source_disabled(web_env):
    """修改连接信息不改变启用状态（doc 11 §3/§9 第 8 条）：禁用态改完仍是禁用。"""
    client, rows, _connections, _kwargs, _state = web_env
    source = make_source(
        is_active=False, password_ciphertext=encrypt_password("stored-secret")
    )
    rows.append(source)
    response = client.patch(
        f"/api/data-sources/{source.source_id}",
        json=update_payload(source, display_name="禁用中改个名字"),
    )
    assert response.status_code == 200
    assert response.json()["is_active"] is False
    assert rows[0].is_active is False
    listed = client.get("/api/data-sources")
    assert listed.json()["items"][0]["is_active"] is False


def test_patch_with_new_password_replaces_ciphertext(web_env, caplog):
    client, rows, _connections, kwargs, state = web_env
    old_ciphertext = encrypt_password("stored-secret")
    source = make_source(password_ciphertext=old_ciphertext, sslmode="require")
    rows.append(source)
    new_password = "new-secret"
    caplog.set_level(logging.DEBUG)
    response = client.patch(
        f"/api/data-sources/{source.source_id}",
        json=update_payload(source, password=new_password),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["source_id"] == source.source_id
    assert "password" not in body
    assert new_password not in response.text
    saved = rows[0].password_ciphertext
    assert saved != old_ciphertext
    assert decrypt_password(saved) == new_password
    assert kwargs[0]["password"] == new_password
    assert state["source_connects"] == 1
    logs = "\n".join(record.getMessage() for record in caplog.records)
    assert new_password not in logs
    assert saved not in logs


def test_patch_changed_host_without_password_does_not_connect(web_env):
    client, rows, _connections, _kwargs, state = web_env
    source = make_source(password_ciphertext=encrypt_password("stored-secret"))
    rows.append(source)
    response = client.patch(
        f"/api/data-sources/{source.source_id}",
        json=update_payload(source, host="other.example.test"),
    )
    assert response.status_code == 422
    assert response.json() == {
        "code": "password_required",
        "message": "连接信息已修改，请填写密码后重试",
    }
    assert "details" not in response.json()
    assert state["source_connects"] == 0
    assert rows[0].host == "db.example.test"
    assert rows[0].db_version is None


@pytest.mark.parametrize("missing_key", ["sslmode", "description"])
def test_patch_missing_nullable_key_defaults_to_none(web_env, missing_key):
    """断代改造后请求模型对可空键给默认值：缺省按 None 写入（非 422）。"""
    client, rows, _connections, _kwargs, state = web_env
    source = make_source(password_ciphertext=encrypt_password("stored-secret"))
    rows.append(source)
    body = update_payload(source)
    del body[missing_key]
    response = client.patch(f"/api/data-sources/{source.source_id}", json=body)
    assert response.status_code == 200
    assert getattr(rows[0], missing_key) is None
    assert state["source_connects"] == 1


def test_patch_duplicate_excludes_self_and_blocks_other_match(web_env):
    client, rows, _connections, _kwargs, state = web_env
    current = make_source(
        source_id="db_src_1",
        display_name="当前",
        password_ciphertext=encrypt_password("stored-secret"),
    )
    other = make_source(
        source_id="db_src_2", display_name="另一条", host="other.example.test"
    )
    rows.extend([current, other])
    same = client.patch(
        f"/api/data-sources/{current.source_id}",
        json=update_payload(current, display_name="只改名称"),
    )
    assert same.status_code == 200
    assert state["source_connects"] == 1

    conflict = client.patch(
        f"/api/data-sources/{current.source_id}",
        json=update_payload(
            current,
            host="other.example.test",
            password="new-secret",
            display_name="只改名称",
        ),
    )
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "duplicate_source"
    assert "新建" not in conflict.json()["message"]
    assert conflict.json()["details"] == [
        {"source_id": "db_src_2", "display_name": "另一条"}
    ]
    assert state["source_connects"] == 1
    assert rows[0].host == "db.example.test"


def test_patch_missing_source_is_not_found_before_duplicate(web_env):
    client, rows, _connections, _kwargs, state = web_env
    other = make_source(display_name="另一条")
    rows.append(other)
    response = client.patch(
        "/api/data-sources/db_src_99",
        json=update_payload(other, password="new-secret"),
    )
    assert response.status_code == 404
    assert response.json() == {"code": "not_found", "message": "数据源不存在"}
    assert state["source_connects"] == 0
    assert len(rows) == 1


def test_missing_source_on_get_and_saved_check(web_env):
    client, _rows, _connections, _kwargs, state = web_env
    for path in (
        "/api/data-sources/db_src_99",
        "/api/data-sources/db_src_99/connection-check",
    ):
        response = client.get(path) if path.endswith("db_src_99") else client.post(path)
        assert response.status_code == 404
        assert response.json() == {"code": "not_found", "message": "数据源不存在"}
    assert state["source_connects"] == 0


def test_patch_schema_failure_does_not_update_row(web_env):
    client, rows, _connections, _kwargs, _state = web_env
    source = make_source(password_ciphertext=encrypt_password("stored-secret"))
    rows.append(source)
    response = client.patch(
        f"/api/data-sources/{source.source_id}",
        json=update_payload(
            source, selected_schemas=["missing"], display_name="不会写入"
        ),
    )
    assert response.status_code == 422
    assert response.json()["code"] == "schema_missing"
    assert rows[0].display_name == "已有数据源"
    assert rows[0].db_version is None


def test_patch_decrypt_failure_is_internal_error(web_env, caplog):
    client, rows, _connections, _kwargs, state = web_env
    source = make_source(password_ciphertext="opaque-ciphertext")
    rows.append(source)
    caplog.set_level(logging.ERROR)
    response = client.patch(
        f"/api/data-sources/{source.source_id}", json=update_payload(source)
    )
    assert response.status_code == 500
    assert response.json() == {
        "code": "internal_error",
        "message": "服务暂时无法处理请求",
    }
    assert "opaque-ciphertext" not in response.text
    assert "opaque-ciphertext" not in "\n".join(
        record.getMessage() for record in caplog.records
    )
    assert state["source_connects"] == 0
    assert rows[0].display_name == "已有数据源"


def test_cors_preflight_allows_patch_and_delete(monkeypatch):
    settings = PlatformSettings(
        host="platform", port=5432, name="metaweave", user="admin", password="secret"
    )
    monkeypatch.setattr(api_main, "load_project_env", lambda: None)
    origin = "http://127.0.0.1:5173"
    app = create_app(settings, cors_origins=[origin])
    with TestClient(app, raise_server_exceptions=False) as client:
        for method in ("PATCH", "DELETE"):
            response = client.options(
                "/api/data-sources/db_src_1",
                headers={
                    "Origin": origin,
                    "Access-Control-Request-Method": method,
                    "Access-Control-Request-Headers": "Content-Type",
                },
            )
            assert response.status_code == 200
            assert method in response.headers["access-control-allow-methods"]


# ---- 删除（doc 6.2 §6.1、A29：已关联工作区一律拒绝） ----


def _use_output_root(monkeypatch, root: Path) -> None:
    monkeypatch.setattr("metaweave.utils.file_utils.get_project_root", lambda: root)


def _secret_absent(response, caplog) -> None:
    blob = (
        response.text
        + "\n"
        + "\n".join(record.getMessage() for record in caplog.records)
    )
    assert "opaque-ciphertext" not in blob
    assert "distinct-web-test-secret" not in blob


def test_delete_with_workspaces_is_409(web_env, monkeypatch, tmp_path, caplog):
    """A29：已关联工作区拒绝删除，不级联、不借 delete_output 删目录。"""
    client, rows, _connections, _kwargs, state = web_env
    source = make_source()
    rows.append(source)
    state["workspaces"].append(SimpleNamespace(source_id=source.source_id))
    _use_output_root(monkeypatch, tmp_path)
    workspace = tmp_path / "output" / source.source_id / "ws_1" / "db"
    workspace.mkdir(parents=True)
    (workspace / "database.json").write_text("{}", encoding="utf-8")

    caplog.set_level(logging.ERROR)
    for delete_output in (False, True):
        response = client.delete(
            f"/api/data-sources/{source.source_id}",
            params={"delete_output": delete_output},
        )
        assert response.status_code == 409
        assert response.json() == {
            "code": "source_has_workspaces",
            "message": "该数据源已关联工作区，不能删除；请先人工处理工作区相关记录",
        }
    assert rows[0].source_id == source.source_id
    # 工作区目录不被级联删除
    assert (workspace / "database.json").read_text(encoding="utf-8") == "{}"
    assert state["source_connects"] == 0
    _secret_absent(response, caplog)


def test_delete_without_output_flag_does_not_resolve_workspace(
    web_env, monkeypatch, caplog
):
    client, rows, _connections, _kwargs, state = web_env
    source = make_source()
    rows.append(source)

    def fail_resolve(_source_id):
        raise AssertionError("无工作区且不带 delete_output 时不应解析工作区目录")

    monkeypatch.setattr(
        registration_service, "resolve_source_workspace_dir", fail_resolve
    )
    caplog.set_level(logging.ERROR)
    response = client.delete(f"/api/data-sources/{source.source_id}")
    assert response.status_code == 204
    assert response.content == b""
    assert rows == []
    assert state["source_connects"] == 0
    _secret_absent(response, caplog)

    again = client.delete(
        f"/api/data-sources/{source.source_id}", params={"delete_output": True}
    )
    assert again.status_code == 404
    assert again.json() == {"code": "not_found", "message": "数据源不存在"}
    assert state["source_connects"] == 0


def test_delete_output_removes_only_that_workspace(
    web_env, monkeypatch, tmp_path, caplog
):
    client, rows, _connections, _kwargs, state = web_env
    source = make_source()
    rows.append(source)
    _use_output_root(monkeypatch, tmp_path)
    workspace = tmp_path / "output" / source.source_id
    nested = workspace / "db"
    nested.mkdir(parents=True)
    (nested / "database.json").write_text("{}", encoding="utf-8")
    other = tmp_path / "output" / "db_src_9"
    other.mkdir()
    (other / "keep.txt").write_text("other", encoding="utf-8")

    caplog.set_level(logging.ERROR)
    response = client.delete(
        f"/api/data-sources/{source.source_id}", params={"delete_output": True}
    )
    assert response.status_code == 204
    assert response.content == b""
    assert rows == []
    assert not workspace.exists()
    assert (other / "keep.txt").read_text(encoding="utf-8") == "other"
    assert state["source_connects"] == 0
    _secret_absent(response, caplog)


def test_delete_missing_source_keeps_orphan_directory(
    web_env, monkeypatch, tmp_path, caplog
):
    client, rows, _connections, _kwargs, state = web_env
    _use_output_root(monkeypatch, tmp_path)
    orphan = tmp_path / "output" / "db_src_99"
    orphan.mkdir(parents=True)
    (orphan / "keep.txt").write_text("stay", encoding="utf-8")
    caplog.set_level(logging.ERROR)
    response = client.delete(
        "/api/data-sources/db_src_99", params={"delete_output": True}
    )
    assert response.status_code == 404
    assert response.json() == {"code": "not_found", "message": "数据源不存在"}
    assert (orphan / "keep.txt").read_text(encoding="utf-8") == "stay"
    assert rows == []
    assert state["source_connects"] == 0
    _secret_absent(response, caplog)


def test_delete_symlink_workspace_keeps_target_and_row(
    web_env, monkeypatch, tmp_path, caplog
):
    client, rows, _connections, _kwargs, state = web_env
    source = make_source()
    rows.append(source)
    _use_output_root(monkeypatch, tmp_path)
    target = tmp_path / "output" / "db_src_9"
    target.mkdir(parents=True)
    (target / "keep.txt").write_text("b", encoding="utf-8")
    link = tmp_path / "output" / source.source_id
    link.symlink_to(target, target_is_directory=True)
    caplog.set_level(logging.ERROR)
    response = client.delete(
        f"/api/data-sources/{source.source_id}", params={"delete_output": True}
    )
    assert response.status_code == 500
    assert response.json() == {
        "code": "output_delete_failed",
        "message": "该数据源的输出目录删除失败，登记记录仍保留",
    }
    assert rows[0].source_id == source.source_id
    assert (target / "keep.txt").read_text(encoding="utf-8") == "b"
    assert state["source_connects"] == 0
    _secret_absent(response, caplog)


def test_delete_permission_error_keeps_row(web_env, monkeypatch, tmp_path, caplog):
    client, rows, _connections, _kwargs, state = web_env
    source = make_source()
    rows.append(source)
    _use_output_root(monkeypatch, tmp_path)
    workspace = tmp_path / "output" / source.source_id
    workspace.mkdir(parents=True)
    (workspace / "file.txt").write_text("keep", encoding="utf-8")

    def deny(_path):
        raise PermissionError("busy")

    monkeypatch.setattr(registration_service.shutil, "rmtree", deny)
    caplog.set_level(logging.ERROR)
    response = client.delete(
        f"/api/data-sources/{source.source_id}", params={"delete_output": True}
    )
    assert response.status_code == 500
    assert response.json() == {
        "code": "output_delete_failed",
        "message": "该数据源的输出目录删除失败，登记记录仍保留",
    }
    assert rows[0].source_id == source.source_id
    assert (workspace / "file.txt").read_text(encoding="utf-8") == "keep"
    assert state["source_connects"] == 0
    _secret_absent(response, caplog)


def test_delete_commit_failure_keeps_row_after_workspace_removed(
    web_env, monkeypatch, tmp_path
):
    client, rows, _connections, _kwargs, state = web_env
    source = make_source()
    rows.append(source)
    _use_output_root(monkeypatch, tmp_path)
    workspace = tmp_path / "output" / source.source_id
    workspace.mkdir(parents=True)
    (workspace / "file.txt").write_text("gone", encoding="utf-8")
    state["fail_next_commit"] = True
    response = client.delete(
        f"/api/data-sources/{source.source_id}", params={"delete_output": True}
    )
    assert response.status_code == 503
    assert response.json() == {
        "code": "platform_unavailable",
        "message": "平台库暂不可用",
    }
    assert rows[0].source_id == source.source_id
    assert not workspace.exists()
    assert state["source_connects"] == 0


def test_delete_query_failure_does_not_touch_workspace(web_env, monkeypatch, tmp_path):
    client, rows, _connections, _kwargs, state = web_env
    source = make_source()
    rows.append(source)
    _use_output_root(monkeypatch, tmp_path)
    workspace = tmp_path / "output" / source.source_id
    workspace.mkdir(parents=True)
    (workspace / "file.txt").write_text("stay", encoding="utf-8")
    calls = []

    def fail_if_called(source_id):
        calls.append(source_id)
        raise AssertionError("首次查询失败不应删除目录")

    monkeypatch.setattr(
        registration_service, "resolve_source_workspace_dir", fail_if_called
    )
    state["fail_get"] = True
    response = client.delete(
        f"/api/data-sources/{source.source_id}", params={"delete_output": True}
    )
    assert response.status_code == 503
    assert response.json() == {
        "code": "platform_unavailable",
        "message": "平台库暂不可用",
    }
    assert calls == []
    assert (workspace / "file.txt").read_text(encoding="utf-8") == "stay"
    assert rows[0].source_id == source.source_id
    assert state["source_connects"] == 0


def test_delete_purges_published_content_and_keeps_other_source(web_env, caplog):
    """删源清当前已发布内容（doc 6.1 §14.1）：画像、定义与对象行删除，
    其他数据源的行不受影响。"""
    client, rows, _connections, _kwargs, state = web_env
    source = make_source()
    other = make_source(source_id="db_src_2")
    rows.extend([source, other])
    state["profiles"].extend(
        [
            SimpleNamespace(source_id=source.source_id),
            SimpleNamespace(source_id=other.source_id),
        ]
    )
    state["definitions"].extend(
        [
            SimpleNamespace(source_id=source.source_id, define_type="ddl"),
            SimpleNamespace(source_id=source.source_id, define_type="md"),
            SimpleNamespace(source_id=other.source_id, define_type="ddl"),
        ]
    )
    state["table_profiles"].extend(
        [
            SimpleNamespace(source_id=source.source_id),
            SimpleNamespace(source_id=other.source_id),
        ]
    )

    caplog.set_level(logging.ERROR)
    response = client.delete(f"/api/data-sources/{source.source_id}")
    assert response.status_code == 204
    assert [row.source_id for row in rows] == [other.source_id]
    assert [row.source_id for row in state["profiles"]] == [other.source_id]
    assert [row.source_id for row in state["definitions"]] == [other.source_id]
    assert [row.source_id for row in state["table_profiles"]] == [other.source_id]
    assert state["source_connects"] == 0
    _secret_absent(response, caplog)


# --- doc 18.1 §4.1：sslmode 值域校验接入 API 输入 ----------------------------


@pytest.mark.parametrize(
    "endpoint_factory",
    [
        lambda client: client.post(
            "/api/data-sources/connection-check", json=payload(sslmode="reqiure")
        ),
        lambda client: client.post(
            "/api/data-sources", json=create_payload(sslmode="reqiure")
        ),
        lambda client: client.patch(
            "/api/data-sources/db_src_1",
            json={**update_payload(make_source(), sslmode="reqiure")},
        ),
    ],
    ids=["connection-check", "create", "update"],
)
@pytest.mark.xfail(
    reason="源码问题：断代改造丢失 doc 18.1 §4.1 的请求级 sslmode 校验，"
    "非法值现在在连接期抛 ValueError（500）；修复后本用例应恢复通过",
    strict=False,
)
def test_invalid_sslmode_rejected_as_invalid_request_before_connecting(
    web_env, endpoint_factory
):
    """非法模式在请求校验阶段 422，不尝试连接、不保存登记（doc 18.1 §4.1）。"""
    client, rows, _connections, _kwargs, state = web_env
    response = endpoint_factory(client)
    assert response.status_code == 422
    assert response.json() == {"code": "invalid_request", "message": "请求字段不合法"}
    assert state["source_connects"] == 0
    assert not rows


@pytest.mark.xfail(
    reason="源码问题：同上，请求级 sslmode 校验丢失；修复后本用例应恢复通过",
    strict=False,
)
def test_invalid_sslmode_update_rejected_without_touching_existing_row(web_env):
    client, rows, _connections, _kwargs, state = web_env
    source = make_source(password_ciphertext=encrypt_password("stored-secret"))
    rows.append(source)
    response = client.patch(
        f"/api/data-sources/{source.source_id}",
        # 带 password 避免触发 PasswordRequiredError（同为 422）混入断言
        json=update_payload(
            source, sslmode="require host=other", password="stored-secret"
        ),
    )
    assert response.status_code == 422
    assert response.json() == {"code": "invalid_request", "message": "请求字段不合法"}
    assert rows[0].sslmode is None
    assert state["source_connects"] == 0


def test_create_connects_with_normalized_sslmode(web_env):
    """连接参数规范化为 `require`（build_connect_kwargs）。

    源码问题：doc 18.1 §4.1 的保存侧规范化在断代改造中丢失，登记行保留
    原始输入（" Require "）；修复后本用例应断言入库值亦为 "require"。
    """
    client, rows, _connections, kwargs, _state = web_env
    created = client.post("/api/data-sources", json=create_payload(sslmode=" Require "))
    assert created.status_code == 201
    assert kwargs[0]["sslmode"] == "require"
    assert rows[0].sslmode == " Require "
