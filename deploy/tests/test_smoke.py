"""smoke.py 语义测试（doc 13 §8.1、§9.1；审核 R3/R5 修复回归）。

apps：public 明确不带令牌；maintenance 携带控制令牌；按现有端点做最小
响应契约检查（health JSON / 空对象 / 错误 code / 不符 data 结构必须失败，
真实合法空列表仍通过）。infra：真实检查器经注入的模块/契约替身验证；
CLI/default builder 的接线经 sys.modules 级驱动替换覆盖。
"""

from __future__ import annotations

import sys
import types

import httpx
import pytest

from deploy.scripts.smoke import (
    build_default_infra_checkers,
    main,
    run_apps_smoke,
    run_infra_smoke,
)

BASE = "http://localhost:18080"
TOKEN = "smoke-token"

CONTRACT = {
    "metaweave_schema": {
        "tables": {"db_source_info": {"source_id": "text", "port": "integer"}},
        "sequences": ["db_source_info_id_seq"],
        "views": ["vw_probe"],
        "indexes": ["db_source_info_connection_idx"],
    },
    "nl2sql_tables": [
        "checkpoints",
        "checkpoint_blobs",
        "checkpoint_writes",
        "checkpoint_migrations",
        "store",
        "store_migrations",
        "result_snapshots",
    ],
}


@pytest.fixture(autouse=True)
def _no_leaked_deploy_token(monkeypatch):
    monkeypatch.delenv("DEPLOY_CONTROL_TOKEN", raising=False)


def make_transport(
    responses: dict[str, tuple[int, str, str]], seen: list[httpx.Request]
):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        status, content_type, body = responses[request.url.path]
        return httpx.Response(status, headers={"content-type": content_type}, text=body)

    return httpx.MockTransport(handler)


def _envelope(data: str) -> str:
    return f'{{"code": 200, "message": "ok", "data": {data}, "trace_id": ""}}'


def ok_responses() -> dict[str, tuple[int, str, str]]:
    """与实际 API 契约一致的合法空环境成功样本（审核 R5）。"""
    html = "<html><body><div id='app'></div></body></html>"
    sessions_data = (
        '{"items": [], "limit": 1, "offset": 0, "has_more": false, '
        '"next_offset": null, "total_all": 0, "total_filtered": 0}'
    )
    return {
        "/": (200, "text/html", html),
        "/query": (200, "text/html", html),
        "/api/data-sources": (200, "application/json", '{"items": []}'),
        "/api/v2/health": (200, "application/json", _envelope('{"status": "ok"}')),
        "/api/v2/sessions": (200, "application/json", _envelope(sessions_data)),
    }


def _raise_bolt():
    raise RuntimeError("bolt unreachable")


class TestAppsSmoke:
    def test_all_pass_and_no_token_header(self):
        seen: list[httpx.Request] = []
        failures = run_apps_smoke(
            BASE,
            mode="public",
            token=None,
            transport=make_transport(ok_responses(), seen),
        )
        assert failures == []
        assert seen
        for request in seen:
            assert "x-insightweave-deploy-token" not in request.headers

    def test_maintenance_mode_sends_token_header(self):
        seen: list[httpx.Request] = []
        failures = run_apps_smoke(
            BASE,
            mode="maintenance",
            token=TOKEN,
            transport=make_transport(ok_responses(), seen),
        )
        assert failures == []
        assert all(r.headers.get("x-insightweave-deploy-token") == TOKEN for r in seen)

    def test_maintenance_without_token_is_config_error(self):
        with pytest.raises(RuntimeError, match="DEPLOY_CONTROL_TOKEN"):
            run_apps_smoke(BASE, mode="maintenance", token=None)

    def test_health_json_on_business_paths_is_failure(self):
        """业务路径被转发到 health 响应时必须失败（审核 R5 复现场景）。"""
        responses = ok_responses()
        health_body = _envelope('{"status": "ok"}')
        responses["/api/data-sources"] = (200, "application/json", health_body)
        responses["/api/v2/sessions"] = (200, "application/json", health_body)
        failures = run_apps_smoke(
            BASE, mode="public", token=None, transport=make_transport(responses, [])
        )
        assert any("data-sources" in f for f in failures), failures
        assert any("sessions" in f for f in failures), failures

    def test_empty_object_body_is_failure(self):
        responses = ok_responses()
        responses["/api/data-sources"] = (200, "application/json", "{}")
        failures = run_apps_smoke(
            BASE, mode="public", token=None, transport=make_transport(responses, [])
        )
        assert any("data-sources" in f for f in failures), failures

    def test_wrong_envelope_code_is_failure(self):
        responses = ok_responses()
        responses["/api/v2/sessions"] = (
            200,
            "application/json",
            '{"code": 500, "message": "boom", "data": {"items": [], "total_all": 0}, "trace_id": ""}',
        )
        failures = run_apps_smoke(
            BASE, mode="public", token=None, transport=make_transport(responses, [])
        )
        assert any("code" in f and "sessions" in f for f in failures), failures

    def test_sessions_data_wrong_shape_is_failure(self):
        responses = ok_responses()
        responses["/api/v2/sessions"] = (200, "application/json", _envelope("[]"))
        failures = run_apps_smoke(
            BASE, mode="public", token=None, transport=make_transport(responses, [])
        )
        assert any("sessions" in f for f in failures), failures

    def test_sessions_missing_pagination_is_failure(self):
        responses = ok_responses()
        responses["/api/v2/sessions"] = (
            200,
            "application/json",
            _envelope('{"items": []}'),
        )
        failures = run_apps_smoke(
            BASE, mode="public", token=None, transport=make_transport(responses, [])
        )
        assert any("total_all" in f for f in failures), failures

    def test_non_200_is_failure(self):
        responses = ok_responses()
        responses["/api/data-sources"] = (503, "application/json", '{"error": "x"}')
        failures = run_apps_smoke(
            BASE, mode="public", token=None, transport=make_transport(responses, [])
        )
        assert any("data-sources" in f for f in failures), failures

    def test_spa_fallback_missing_is_failure(self):
        responses = ok_responses()
        responses["/query"] = (404, "text/plain", "not found")
        failures = run_apps_smoke(
            BASE, mode="public", token=None, transport=make_transport(responses, [])
        )
        assert any("/query" in f or "SPA" in f for f in failures), failures


class TestInfraProtocol:
    def test_checkers_protocol_collects_failures(self):
        failures = run_infra_smoke(
            {"postgres": lambda: None, "neo4j": _raise_bolt, "milvus": lambda: None}
        )
        assert failures == ["neo4j: bolt unreachable"]

    def test_all_ok_no_failures(self):
        assert run_infra_smoke({"a": lambda: None}) == []


# ---------------------------------------------------------------- 驱动替身


class _Cursor:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


class _PgConn:
    """按库名应答的 PG 连接替身：postgres=库清单，metaweave=结构契约，
    lg_persist=db_deepagent 表。"""

    def __init__(self, dbname: str, db: _PgDb):
        self._dbname = dbname
        self._db = db

    def execute(self, sql, params=None):
        db = self._db
        if "FROM pg_database" in sql:
            return _Cursor([(n,) for n in sorted(db.databases)])
        if "information_schema.columns" in sql:
            rows = []
            for table, columns in db.schema["tables"].items():
                for column, dtype in columns.items():
                    rows.append((table, column, dtype))
            return _Cursor(rows)
        if "information_schema.sequences" in sql:
            return _Cursor([(n,) for n in sorted(db.schema["sequences"])])
        if "information_schema.views" in sql:
            return _Cursor([(n,) for n in sorted(db.schema["views"])])
        if "FROM pg_indexes" in sql:
            return _Cursor([(n,) for n in sorted(db.schema["indexes"])])
        if "information_schema.tables" in sql and "db_deepagent" in sql:
            return _Cursor([(n,) for n in sorted(db.nl2sql_tables)])
        raise AssertionError(f"未预期的查询：{sql}")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _PgDb:
    def __init__(self):
        self.databases = {"metaweave", "lg_persist"}
        self.schema = {
            "tables": {
                t: dict(c) for t, c in CONTRACT["metaweave_schema"]["tables"].items()
            },
            "sequences": list(CONTRACT["metaweave_schema"]["sequences"]),
            "views": list(CONTRACT["metaweave_schema"]["views"]),
            "indexes": list(CONTRACT["metaweave_schema"]["indexes"]),
        }
        self.nl2sql_tables = set(CONTRACT["nl2sql_tables"])

    def module(self):
        db = self

        class _Module:
            @staticmethod
            def connect(**kwargs):
                return _PgConn(kwargs["dbname"], db)

        return _Module()


class _Result:
    def __init__(self, record):
        self._record = record

    def single(self):
        return self._record


def _neo4j_module(fail: bool, without_apoc: bool = False):
    class _Session:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def run(self, query):
            if fail:
                raise RuntimeError("bolt unreachable")
            if "apoc.version" in query:
                return _Result({"v": "5.26.19"} if not without_apoc else None)
            return _Result({"ok": 1})

    class _Driver:
        def session(self, **kwargs):
            return _Session()

        def close(self):
            pass

    class _GraphDatabase:
        @staticmethod
        def driver(uri, auth=None):
            if fail:
                raise RuntimeError("bolt unreachable")
            return _Driver()

    return types.SimpleNamespace(GraphDatabase=_GraphDatabase)


def _milvus_factory(created: list):
    class _Client:
        def __init__(self, **kwargs):
            created.append(("init", kwargs))

        def list_collections(self):
            created.append(("list", None))
            return []

    return _Client


def _infra_env(monkeypatch):
    for key, value in {
        "PLATFORM_DB_HOST": "postgres",
        "PLATFORM_DB_PORT": "5432",
        "PLATFORM_DB_NAME": "metaweave",
        "PLATFORM_DB_USER": "u",
        "PLATFORM_DB_PASSWORD": "p",
        "NEO4J_URI": "bolt://neo4j:7687",
        "NEO4J_USER": "neo4j",
        "NEO4J_PASSWORD": "pw",
        "NEO4J_DATABASE": "neo4j",
        "MILVUS_HOST": "milvus",
        "MILVUS_PORT": "19530",
        "MILVUS_DATABASE": "nl2sql",
    }.items():
        monkeypatch.setenv(key, value)


class TestDefaultInfraCheckers:
    def test_all_pass_with_injected_modules(self, monkeypatch):
        _infra_env(monkeypatch)
        checkers = build_default_infra_checkers(
            psycopg=_PgDb().module(),
            neo4j=_neo4j_module(fail=False),
            milvus_client_factory=_milvus_factory([]),
            contract=CONTRACT,
        )
        assert run_infra_smoke(checkers) == []

    def test_missing_pg_database_reported(self, monkeypatch):
        _infra_env(monkeypatch)
        db = _PgDb()
        db.databases = {"metaweave"}
        checkers = build_default_infra_checkers(
            psycopg=db.module(),
            neo4j=_neo4j_module(fail=False),
            milvus_client_factory=_milvus_factory([]),
            contract=CONTRACT,
        )
        failures = run_infra_smoke(checkers)
        assert any("lg_persist" in f for f in failures), failures

    def test_structure_contract_violation_reported(self, monkeypatch):
        _infra_env(monkeypatch)
        db = _PgDb()
        db.schema["tables"]["db_source_info"]["port"] = "text"  # 类型漂移
        checkers = build_default_infra_checkers(
            psycopg=db.module(),
            neo4j=_neo4j_module(fail=False),
            milvus_client_factory=_milvus_factory([]),
            contract=CONTRACT,
        )
        failures = run_infra_smoke(checkers)
        assert any("类型不符" in f for f in failures), failures

    def test_missing_nl2sql_tables_reported(self, monkeypatch):
        _infra_env(monkeypatch)
        db = _PgDb()
        db.nl2sql_tables = {"checkpoints"}
        checkers = build_default_infra_checkers(
            psycopg=db.module(),
            neo4j=_neo4j_module(fail=False),
            milvus_client_factory=_milvus_factory([]),
            contract=CONTRACT,
        )
        failures = run_infra_smoke(checkers)
        assert any("store_migrations" in f or "缺表" in f for f in failures), failures

    def test_neo4j_without_apoc_reported(self, monkeypatch):
        _infra_env(monkeypatch)
        checkers = build_default_infra_checkers(
            psycopg=_PgDb().module(),
            neo4j=_neo4j_module(fail=False, without_apoc=True),
            milvus_client_factory=_milvus_factory([]),
            contract=CONTRACT,
        )
        failures = run_infra_smoke(checkers)
        assert any("APOC" in f for f in failures), failures

    def test_neo4j_unreachable_reported(self, monkeypatch):
        _infra_env(monkeypatch)
        checkers = build_default_infra_checkers(
            psycopg=_PgDb().module(),
            neo4j=_neo4j_module(fail=True),
            milvus_client_factory=_milvus_factory([]),
            contract=CONTRACT,
        )
        failures = run_infra_smoke(checkers)
        assert any("neo4j" in f and "bolt" in f for f in failures), failures

    def test_milvus_checker_is_read_only(self, monkeypatch):
        _infra_env(monkeypatch)
        created: list = []
        checkers = build_default_infra_checkers(
            psycopg=_PgDb().module(),
            neo4j=_neo4j_module(fail=False),
            milvus_client_factory=_milvus_factory(created),
            contract=CONTRACT,
        )
        assert run_infra_smoke(checkers) == []
        init_kwargs = [kw for tag, kw in created if tag == "init"]
        assert init_kwargs
        assert all("create_database" not in (kw or {}) for kw in init_kwargs)
        assert ("list", None) in created


class TestInfraCliWiring:
    """CLI → main → build_default_infra_checkers 的真实接线（审核 R3）。

    默认 builder 加载真实 deploy/contract.json，故 fake PG 按真实契约应答。
    """

    def _real_contract_db(self) -> _PgDb:
        import json
        from pathlib import Path

        contract = json.loads(
            (Path(__file__).resolve().parents[1] / "contract.json").read_text(
                encoding="utf-8"
            )
        )
        db = _PgDb()
        db.schema = contract["metaweave_schema"]
        db.nl2sql_tables = set(contract["nl2sql_tables"])
        return db

    def test_cli_infra_passes_via_default_builders(self, monkeypatch):
        _infra_env(monkeypatch)
        monkeypatch.setitem(sys.modules, "psycopg", self._real_contract_db().module())
        monkeypatch.setitem(sys.modules, "neo4j", _neo4j_module(fail=False))
        monkeypatch.setitem(
            sys.modules,
            "pymilvus",
            types.SimpleNamespace(MilvusClient=_milvus_factory([])),
        )
        assert main(["--scope", "infra"]) == 0

    def test_cli_infra_fails_nonzero_when_neo4j_down(self, monkeypatch):
        _infra_env(monkeypatch)
        monkeypatch.setitem(sys.modules, "psycopg", _PgDb().module())
        monkeypatch.setitem(sys.modules, "neo4j", _neo4j_module(fail=True))
        monkeypatch.setitem(
            sys.modules,
            "pymilvus",
            types.SimpleNamespace(MilvusClient=_milvus_factory([])),
        )
        assert main(["--scope", "infra"]) == 1
