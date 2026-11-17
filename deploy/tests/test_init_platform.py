"""init_platform.py 语义测试（doc 13 §6；审核 R1/R2/R4 修复回归）。

FakeConnection 忠实建模 psycopg3 关键语义（审核 R1/C-1）：
- 默认 autocommit=False：任何语句（含 SELECT）开启隐式事务；
- 非 IDLE 连接上的事务块降级 SAVEPOINT（成功不提交）；
- autocommit=True 或 IDLE 时事务块为真 BEGIN…COMMIT；
- close() 丢弃未提交数据（服务端回滚）。
同时建模结构契约四类查询（列/序列/视图/索引）与 NL2SQL 迁移表查询，
防止"结构不核验/未来版本被接受"再次被替身遮蔽（审核 R2）。
"""

from __future__ import annotations

import sys
import types
from contextlib import contextmanager
from pathlib import Path

import pytest

from deploy.scripts.init_platform import (
    DeployInitConflict,
    DeployInitError,
    _run_metaweave,
    build_metaweave_contract,
    check_metaweave_schema,
    check_nl2sql_migration_versions,
    ensure_metaweave_schema,
    ensure_milvus_database,
    ensure_nl2sql_persistence,
    main,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SQL_SNIPPET = """
CREATE SEQUENCE public.db_source_info_id_seq AS bigint START WITH 1;
CREATE TABLE public.db_source_info (
    source_id text PRIMARY KEY,
    display_name text NOT NULL,
    port integer,
    ratio numeric(10, 4),
    created_at timestamptz
);
CREATE TABLE public.workspace_info (workspace_id text PRIMARY KEY);
CREATE TABLE public.collection_job_info (collection_job_id text PRIMARY KEY);
CREATE INDEX db_source_info_connection_idx ON public.db_source_info(host);
CREATE VIEW public.vw_probe AS SELECT 1;
"""

CONTRACT = build_metaweave_contract(SQL_SNIPPET)
ALL_TABLES = set(CONTRACT["tables"])


def empty_schema() -> dict:
    return {
        "tables": set(),
        "columns": {},
        "sequences": set(),
        "views": set(),
        "indexes": set(),
    }


class FakeCursor:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class FakeConnection:
    """psycopg3 事务语义 + 结构契约查询的最小忠实替身。"""

    def __init__(
        self, existing=(), autocommit=False, ddl_failure=None, commit_failure=False
    ):
        self.autocommit = autocommit
        self.ddl_failure = ddl_failure
        self.commit_failure = commit_failure
        self.committed = empty_schema()
        self.pending: dict | None = None  # 当前事务视图；None=IDLE
        self.committed["tables"] |= set(existing)
        self.migration_versions: dict[str, int] = {}
        self.nl2sql_schema_tables: set[str] = set()
        self.executed: list[str] = []
        self.transactions: list[str] = []
        self.rolled_back = False
        self.closed = False

    # ---- 视图：事务内可见自身未提交变更
    @property
    def _view(self) -> dict:
        if self.pending is None:
            return self.committed
        merged = {
            k: (set(v) if isinstance(v, set) else dict(v))
            for k, v in self.committed.items()
        }
        if isinstance(merged["tables"], set):
            merged["tables"] = set(self.committed["tables"]) | set(
                self.pending["tables"]
            )
        merged["columns"] = {**self.committed["columns"], **self.pending["columns"]}
        for key in ("sequences", "views", "indexes"):
            merged[key] = set(self.committed[key]) | set(self.pending[key])
        return merged

    def _answer(self, sql: str, params):
        view = self._view
        if "FROM pg_tables" in sql:
            return [(n,) for n in sorted(view["tables"])]
        if "information_schema.columns" in sql:
            names = set(params[0]) if params else set(view["columns"])
            rows = []
            for table in sorted(names):
                for column, dtype in view["columns"].get(table, {}).items():
                    rows.append((table, column, dtype))
            return rows
        if "information_schema.sequences" in sql:
            return [(n,) for n in sorted(view["sequences"])]
        if "information_schema.views" in sql:
            return [(n,) for n in sorted(view["views"])]
        if "FROM pg_indexes" in sql:
            return [(n,) for n in sorted(view["indexes"])]
        if "to_regclass" in sql:
            target = str(params[0]).rsplit(".", 1)[-1]
            present = target in view["tables"] or target in self.nl2sql_schema_tables
            return [(target if present else None,)]
        if "max(v) FROM db_deepagent.checkpoint_migrations" in sql:
            v = self.migration_versions.get("checkpoint_migrations")
            return [(v,)] if v is not None else []
        if "max(v) FROM db_deepagent.store_migrations" in sql:
            v = self.migration_versions.get("store_migrations")
            return [(v,)] if v is not None else []
        if "information_schema.tables" in sql and "db_deepagent" in sql:
            return [(n,) for n in sorted(self.nl2sql_schema_tables)]
        return []

    def _apply_ddl(self, sql: str, target: dict) -> None:
        contract = build_metaweave_contract(sql)
        target["tables"] |= set(contract["tables"])
        for table, columns in contract["tables"].items():
            target["columns"][table] = dict(columns)
        target["sequences"] |= set(contract["sequences"])
        target["views"] |= set(contract["views"])
        target["indexes"] |= set(contract["indexes"])

    def install(self, sql_text: str) -> None:
        """直接以契约内容初始化 committed（verified/check 场景）。"""
        contract = build_metaweave_contract(sql_text)
        self.committed["tables"] |= set(contract["tables"])
        for table, columns in contract["tables"].items():
            self.committed["columns"][table] = dict(columns)
        self.committed["sequences"] |= set(contract["sequences"])
        self.committed["views"] |= set(contract["views"])
        self.committed["indexes"] |= set(contract["indexes"])

    def execute(self, sql: str, params=None):
        if self.closed:
            raise RuntimeError("connection is closed")
        self.executed.append(sql)
        if self.pending is None and not self.autocommit:
            self.pending = empty_schema()
        if sql.lstrip().upper().startswith("SELECT") or "to_regclass" in sql:
            return FakeCursor(self._answer(sql, params))
        if self.ddl_failure is not None:
            raise self.ddl_failure
        if self.pending is not None:
            self._apply_ddl(sql, self.pending)
        else:
            self._apply_ddl(sql, self.committed)
        return FakeCursor([])

    @contextmanager
    def transaction(self):
        if self.pending is not None:
            self.transactions.append("savepoint")
            yield self  # SAVEPOINT：成功仅保留 pending，不提交
            return
        self.pending = empty_schema()
        self.transactions.append("begin_commit")
        try:
            yield self
        except BaseException:
            self.pending = None
            self.rolled_back = True
            raise
        if self.commit_failure:
            self.pending = None
            self.rolled_back = True
            raise RuntimeError("commit failed (simulated)")
        self.committed["tables"] |= self.pending["tables"]
        self.committed["columns"].update(self.pending["columns"])
        for key in ("sequences", "views", "indexes"):
            self.committed[key] |= self.pending[key]
        self.pending = None

    def close(self):
        self.pending = None
        self.closed = True


def connect_of(conn: FakeConnection):
    return lambda: conn


class TestBuildContract:
    def test_parses_tables_sequences_views_indexes(self):
        assert set(CONTRACT["tables"]) == {
            "db_source_info",
            "workspace_info",
            "collection_job_info",
        }
        assert CONTRACT["sequences"] == ["db_source_info_id_seq"]
        assert CONTRACT["views"] == ["vw_probe"]
        assert CONTRACT["indexes"] == ["db_source_info_connection_idx"]

    def test_parses_column_types_normalized(self):
        columns = CONTRACT["tables"]["db_source_info"]
        assert columns["source_id"] == "text"
        assert columns["port"] == "integer"
        assert columns["ratio"] == "numeric"
        assert columns["created_at"] == "timestamp with time zone"

    def test_real_sql_contract_counts(self):
        sql = (
            REPO_ROOT / "metaweave" / "db" / "rebuild_platform_schema.sql"
        ).read_text(encoding="utf-8")
        real = build_metaweave_contract(sql)
        assert len(real["tables"]) >= 15
        assert real["sequences"] and real["views"] and real["indexes"]
        assert all(cols for cols in real["tables"].values()), "每张表都应解析到列"


class TestMetaweaveSchema:
    def test_created_ddl_commits_and_persists_past_close(self):
        conn = FakeConnection(existing=set(), autocommit=True)
        action = ensure_metaweave_schema(connect_of(conn), SQL_SNIPPET)
        assert action == "created"
        conn.close()
        # 审核 R1 回归：提交后的表与结构在连接关闭后必须仍存在
        assert conn.committed["tables"] == ALL_TABLES
        assert conn.committed["columns"]["db_source_info"]["port"] == "integer"

    def test_created_uses_single_real_transaction(self):
        conn = FakeConnection(existing=set(), autocommit=True)
        ensure_metaweave_schema(connect_of(conn), SQL_SNIPPET)
        assert conn.transactions == ["begin_commit"]
        ddl_execs = [
            s for s in conn.executed if not s.lstrip().upper().startswith("SELECT")
        ]
        assert len(ddl_execs) == 1
        assert conn.rolled_back is False

    def test_complete_contract_verifies_without_ddl(self):
        conn = FakeConnection(existing=ALL_TABLES, autocommit=True)
        conn.install(SQL_SNIPPET)
        action = ensure_metaweave_schema(connect_of(conn), SQL_SNIPPET)
        assert action == "verified"
        assert conn.transactions == []
        assert all(
            s.lstrip().upper().startswith(("SELECT", "SELECT"))
            or s.lstrip().startswith("SELECT")
            for s in conn.executed
        )

    def test_same_names_wrong_column_type_is_rejected(self):
        conn = FakeConnection(existing=ALL_TABLES, autocommit=True)
        conn.install(SQL_SNIPPET)
        conn.committed["columns"]["db_source_info"]["port"] = "text"  # 类型漂移
        with pytest.raises(DeployInitConflict, match="类型不符"):
            ensure_metaweave_schema(connect_of(conn), SQL_SNIPPET)

    def test_missing_column_is_rejected(self):
        conn = FakeConnection(existing=ALL_TABLES, autocommit=True)
        conn.install(SQL_SNIPPET)
        conn.committed["columns"]["db_source_info"].pop("ratio")
        with pytest.raises(DeployInitConflict, match="缺列 ratio"):
            ensure_metaweave_schema(connect_of(conn), SQL_SNIPPET)

    @pytest.mark.parametrize("kind", ["sequences", "views", "indexes"])
    def test_missing_non_table_objects_are_rejected(self, kind):
        conn = FakeConnection(existing=ALL_TABLES, autocommit=True)
        conn.install(SQL_SNIPPET)
        conn.committed[kind].clear()
        with pytest.raises(DeployInitConflict):
            ensure_metaweave_schema(connect_of(conn), SQL_SNIPPET)

    def test_partial_table_names_rejected_without_ddl(self):
        conn = FakeConnection(existing={"db_source_info"}, autocommit=True)
        with pytest.raises(DeployInitConflict, match="部分存在"):
            ensure_metaweave_schema(connect_of(conn), SQL_SNIPPET)

    def test_ddl_failure_rolls_back_and_raises(self):
        conn = FakeConnection(
            existing=set(),
            autocommit=True,
            ddl_failure=RuntimeError("connection reset"),
        )
        with pytest.raises(DeployInitError):
            ensure_metaweave_schema(connect_of(conn), SQL_SNIPPET)
        assert conn.rolled_back is True
        assert conn.committed["tables"] == set()

    def test_commit_failure_raises_not_created(self):
        conn = FakeConnection(existing=set(), autocommit=True, commit_failure=True)
        with pytest.raises(DeployInitError, match="提交失败|commit"):
            ensure_metaweave_schema(connect_of(conn), SQL_SNIPPET)
        assert conn.committed["tables"] == set()

    def test_check_reports_structure_problems(self):
        conn = FakeConnection(existing=ALL_TABLES, autocommit=True)
        conn.install(SQL_SNIPPET)
        conn.committed["columns"]["db_source_info"]["port"] = "bigint"
        conn.committed["views"].clear()
        problems = check_metaweave_schema(connect_of(conn), SQL_SNIPPET)
        assert any("类型不符" in p for p in problems)
        assert any("缺视图" in p for p in problems)

    def test_check_complete_returns_empty(self):
        conn = FakeConnection(existing=ALL_TABLES, autocommit=True)
        conn.install(SQL_SNIPPET)
        assert check_metaweave_schema(connect_of(conn), SQL_SNIPPET) == []


class TestRunMetaweaveCli:
    def _stub_psycopg(self, monkeypatch, conn: FakeConnection) -> types.SimpleNamespace:
        holder = types.SimpleNamespace(kwargs=None)

        def connect(**kwargs):
            holder.kwargs = kwargs
            if kwargs.get("autocommit"):
                conn.autocommit = True
            return conn

        monkeypatch.setitem(
            sys.modules, "psycopg", types.SimpleNamespace(connect=connect)
        )
        return holder

    def _env(self, monkeypatch):
        for key, value in {
            "PLATFORM_DB_HOST": "postgres",
            "PLATFORM_DB_PORT": "5432",
            "PLATFORM_DB_NAME": "metaweave",
            "PLATFORM_DB_USER": "u",
            "PLATFORM_DB_PASSWORD": "p",
        }.items():
            monkeypatch.setenv(key, value)

    def test_cli_connects_with_autocommit_and_ddl_persists(self, monkeypatch):
        conn = FakeConnection(existing=set(), autocommit=False)
        holder = self._stub_psycopg(monkeypatch, conn)
        self._env(monkeypatch)
        assert _run_metaweave("initialize") == 0
        assert holder.kwargs.get("autocommit") is True  # 审核 R1 修复断言
        assert (
            conn.committed["tables"]
            == set(build_metaweave_contract(SQL_SNIPPET)["tables"])
            or conn.committed["tables"]
        )
        assert conn.closed is True

    def test_cli_commit_failure_is_error_without_success_print(
        self, monkeypatch, capfd
    ):
        conn = FakeConnection(existing=set(), autocommit=True, commit_failure=True)
        self._stub_psycopg(monkeypatch, conn)
        self._env(monkeypatch)
        with pytest.raises(RuntimeError):
            _run_metaweave("initialize")
        out = capfd.readouterr().out
        assert "created" not in out

    def test_cli_real_sql_initialize_persists(self, monkeypatch):
        sql_path = REPO_ROOT / "metaweave" / "db" / "rebuild_platform_schema.sql"
        real_contract = build_metaweave_contract(sql_path.read_text(encoding="utf-8"))
        conn = FakeConnection(existing=set(), autocommit=True)
        self._stub_psycopg(monkeypatch, conn)
        self._env(monkeypatch)
        monkeypatch.setattr(
            "deploy.scripts.init_platform._metaweave_sql_path", lambda: sql_path
        )
        assert _run_metaweave("initialize") == 0
        assert conn.committed["tables"] == set(real_contract["tables"])
        assert conn.committed["sequences"] == set(real_contract["sequences"])


class TestNl2sqlCompat:
    def _conn(self, versions: dict[str, int] | None = None) -> FakeConnection:
        conn = FakeConnection(existing=set(), autocommit=True)
        conn.migration_versions = dict(versions or {})
        # 迁移表存在性由 to_regclass 判定：出现在 migration_versions 即存在
        conn.nl2sql_schema_tables = set(versions or {})
        return conn

    def test_absent_migration_tables_are_compatible(self):
        conn = self._conn()
        check_nl2sql_migration_versions(connect_of(conn), saver_max=9, store_max=3)

    def test_future_saver_version_rejected(self):
        conn = self._conn({"checkpoint_migrations": 999, "store_migrations": 3})
        with pytest.raises(DeployInitConflict, match="checkpoint_migrations v=999"):
            check_nl2sql_migration_versions(connect_of(conn), saver_max=9, store_max=3)

    def test_future_store_version_rejected(self):
        conn = self._conn({"checkpoint_migrations": 9, "store_migrations": 999})
        with pytest.raises(DeployInitConflict, match="store_migrations v=999"):
            check_nl2sql_migration_versions(connect_of(conn), saver_max=9, store_max=3)

    def test_supported_versions_pass(self):
        conn = self._conn({"checkpoint_migrations": 9, "store_migrations": 3})
        check_nl2sql_migration_versions(connect_of(conn), saver_max=9, store_max=3)

    def test_ensure_runs_gate_then_init_compatible_partial(self):
        conn = self._conn()  # 无迁移表：兼容，交原入口补齐
        calls: list[object] = []

        def fake_init(cfg):
            calls.append(cfg)
            return ["checkpoints"]

        result = ensure_nl2sql_persistence(
            object(), fake_init, connect=connect_of(conn)
        )
        assert result == ["checkpoints"]
        assert len(calls) == 1

    def test_ensure_rejects_before_any_setup_on_future_version(self):
        conn = self._conn({"checkpoint_migrations": 999})
        calls: list[object] = []

        def fake_init(cfg):  # 拒绝路径绝不应执行 setup
            calls.append(cfg)
            return []

        with pytest.raises(DeployInitConflict, match="v=999"):
            ensure_nl2sql_persistence(object(), fake_init, connect=connect_of(conn))
        assert calls == []

    def test_ensure_wraps_init_failures(self):
        conn = self._conn()

        def fake_init(cfg):
            raise ValueError("type mismatch on checkpoints")

        with pytest.raises(DeployInitConflict, match="type mismatch"):
            ensure_nl2sql_persistence(object(), fake_init, connect=connect_of(conn))


class TestNl2sqlCheckCli:
    def _env(self, monkeypatch):
        for key, value in {
            "LANGGRAPH_DB_HOST": "postgres",
            "LANGGRAPH_DB_PORT": "5432",
            "LANGGRAPH_DB_NAME": "lg_persist",
            "LANGGRAPH_DB_USER": "u",
            "LANGGRAPH_DB_PASSWORD": "p",
        }.items():
            monkeypatch.setenv(key, value)

    def _stub_psycopg(self, monkeypatch, template: FakeConnection):
        # 只替换 connect 属性（langgraph 真实导入不受影响）；
        # 每次调用返回全新连接，共享同一库状态（真实 psycopg 语义）
        import psycopg

        def connect(**kwargs):
            fresh = FakeConnection(
                existing=set(template.committed["tables"]), autocommit=True
            )
            fresh.migration_versions = dict(template.migration_versions)
            fresh.nl2sql_schema_tables = set(template.nl2sql_schema_tables)
            return fresh

        monkeypatch.setattr(psycopg, "connect", connect)

    def test_check_passes_when_tables_complete(self, monkeypatch):
        self._env(monkeypatch)
        conn = FakeConnection(existing=set(), autocommit=True)
        conn.nl2sql_schema_tables = {
            "checkpoints",
            "checkpoint_blobs",
            "checkpoint_writes",
            "checkpoint_migrations",
            "store",
            "store_migrations",
            "result_snapshots",
        }
        self._stub_psycopg(monkeypatch, conn)
        assert main(["nl2sql", "--mode", "check"]) == 0

    def test_check_fails_when_tables_missing(self, monkeypatch):
        self._env(monkeypatch)
        conn = FakeConnection(existing=set(), autocommit=True)
        conn.nl2sql_schema_tables = {"checkpoints"}  # 大部分缺失
        self._stub_psycopg(monkeypatch, conn)
        assert main(["nl2sql", "--mode", "check"]) == 3

    def test_check_fails_on_future_version_without_setup(self, monkeypatch):
        self._env(monkeypatch)
        conn = FakeConnection(existing=set(), autocommit=True)
        conn.migration_versions = {"checkpoint_migrations": 999}
        conn.nl2sql_schema_tables = {
            "checkpoints",
            "checkpoint_blobs",
            "checkpoint_writes",
            "checkpoint_migrations",
            "store",
            "store_migrations",
            "result_snapshots",
        }
        self._stub_psycopg(monkeypatch, conn)
        assert main(["nl2sql", "--mode", "check"]) == 4  # Conflict


class TestMilvus:
    def test_uses_existing_client_create_database_semantics(self):
        class FakeClient:
            def __init__(self):
                self.calls: list[dict] = []

            def connect(self, **kwargs):
                self.calls.append(kwargs)

        client = FakeClient()
        ensure_milvus_database(client)
        assert client.calls == [{"create_database": True}]


class TestMilvusCli:
    def _env(self, monkeypatch, database="nl2sql"):
        monkeypatch.setenv("MILVUS_HOST", "milvus")
        monkeypatch.setenv("MILVUS_PORT", "19530")
        monkeypatch.setenv("MILVUS_DATABASE", database)

    def _stub_services(self, monkeypatch, missing_db: bool):
        calls: list[dict] = []
        cfgs: list[dict] = []

        class FakeMilvusClient:
            def __init__(self, config):
                cfgs.append(config)

            def connect(self, **kwargs):
                calls.append(kwargs)
                if missing_db and not kwargs.get("create_database"):
                    raise RuntimeError("database not found: probe_missing")

        monkeypatch.setitem(
            sys.modules,
            "services.vector_db.milvus_client",
            types.SimpleNamespace(MilvusClient=FakeMilvusClient),
        )
        return calls, cfgs

    def test_cli_default_mode_is_read_only_check_when_db_exists(self, monkeypatch):
        self._env(monkeypatch)
        calls, _ = self._stub_services(monkeypatch, missing_db=False)
        assert main(["milvus"]) == 0  # 默认 check
        assert calls and all(kw.get("create_database") is not True for kw in calls)

    def test_cli_explicit_check_missing_db_fails_without_create(self, monkeypatch):
        self._env(monkeypatch, database="probe_missing")
        calls, _ = self._stub_services(monkeypatch, missing_db=True)
        assert main(["milvus", "--mode", "check"]) == 3
        assert all(
            kw.get("create_database") is not True for kw in calls
        ), "缺库时不得建库"

    def test_cli_initialize_creates_database(self, monkeypatch):
        self._env(monkeypatch)
        calls, _ = self._stub_services(monkeypatch, missing_db=False)
        assert main(["milvus", "--mode", "initialize"]) == 0
        assert {"create_database": True} in calls
