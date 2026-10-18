"""登记源解析与注入测试（doc 4 §4.7 端到端来源一致）

- resolve_source_registration：平台库读取、Fernet 解密、不存在/禁用/
  类型不符/非法 UUID 拒绝、平台库错误不泄密；
- apply_registration_to_config：业务库连接五键 + 三键运行配置取登记值；
- build_services 构造失败按逆序回收已创建资源（doc 4 §4.7 每轮独立构造
  的资源泄漏防护）。
"""

from __future__ import annotations

import pytest

from src.modules.db_deepagent.source_registry import (
    SourceRegistration,
    SourceResolutionError,
    apply_registration_to_config,
    resolve_source_registration,
)
from src.tests.db_deepagent.conftest import TEST_SOURCE_ID

pytestmark = pytest.mark.unit

ROW = (
    TEST_SOURCE_ID,
    "postgresql",
    "10.0.0.9",
    5433,
    "orders-b",
    "svc_etl",
    "CIPHER",
    "require",
    ["public", "sales"],
    True,
)


class _FakeCursor:
    def __init__(self, row):
        self._row = row

    def fetchone(self):
        return self._row

    def execute(self, query, params):
        self.query, self.params = query, params
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeConnect:
    """psycopg.connect 替身：可编程返回行或抛错。"""

    row = None
    error: Exception | None = None
    conninfo: str | None = None

    def __init__(self, conninfo, **kwargs):
        type(self).conninfo = conninfo
        if type(self).error is not None:
            raise type(self).error

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, query, params):
        return _FakeCursor(type(self).row)


@pytest.fixture()
def fernet_env(monkeypatch):
    """提供可用 SOURCE_CREDENTIAL_KEY，并把行内密文换为该密钥的真密文。"""
    from cryptography.fernet import Fernet

    key = Fernet.generate_key().decode("ascii")
    monkeypatch.setenv("SOURCE_CREDENTIAL_KEY", key)
    monkeypatch.setenv("PLATFORM_DB_HOST", "platform-db")
    monkeypatch.setenv("PLATFORM_DB_USER", "pg")
    monkeypatch.setenv("PLATFORM_DB_PASSWORD", "secret")
    return Fernet(key.encode("ascii"))


@pytest.fixture()
def patched_connect(monkeypatch):
    import psycopg

    monkeypatch.setattr(psycopg, "connect", _FakeConnect)
    _FakeConnect.row = None
    _FakeConnect.error = None
    yield _FakeConnect
    _FakeConnect.row = None
    _FakeConnect.error = None


class TestResolveRegistration:
    def test_resolves_and_decrypts(self, fernet_env, patched_connect):
        patched_connect.row = (
            ROW[0],
            ROW[1],
            ROW[2],
            ROW[3],
            ROW[4],
            ROW[5],
            fernet_env.encrypt("s3cret".encode()).decode(),
            ROW[7],
            ROW[8],
            ROW[9],
        )
        registration = resolve_source_registration(TEST_SOURCE_ID)
        assert registration.source_id == TEST_SOURCE_ID
        assert registration.db_name == "orders-b"
        assert (registration.host, registration.port) == ("10.0.0.9", 5433)
        assert registration.username == "svc_etl"
        assert registration.password == "s3cret"  # 已解密
        assert registration.sslmode == "require"
        assert registration.selected_schemas == ("public", "sales")

    def test_not_registered_rejected(self, fernet_env, patched_connect):
        patched_connect.row = None
        with pytest.raises(SourceResolutionError, match="未在平台库登记"):
            resolve_source_registration(TEST_SOURCE_ID)

    def test_disabled_rejected(self, fernet_env, patched_connect):
        patched_connect.row = (*ROW[:9], False)
        with pytest.raises(SourceResolutionError, match="禁用"):
            resolve_source_registration(TEST_SOURCE_ID)

    def test_non_postgres_rejected(self, fernet_env, patched_connect):
        patched_connect.row = ROW[:1] + ("mysql",) + ROW[2:]
        with pytest.raises(SourceResolutionError, match="db_type"):
            resolve_source_registration(TEST_SOURCE_ID)

    def test_invalid_uuid_rejected(self, fernet_env, patched_connect):
        with pytest.raises(SourceResolutionError, match="UUID"):
            resolve_source_registration("not-a-uuid")

    def test_platform_db_error_no_secret_leak(self, fernet_env, patched_connect):
        import psycopg

        class _Exploding(psycopg.OperationalError):
            def __init__(self):
                super().__init__("connection failed password=supersecret")

        patched_connect.error = _Exploding()
        with pytest.raises(SourceResolutionError) as exc_info:
            resolve_source_registration(TEST_SOURCE_ID)
        assert "supersecret" not in str(exc_info.value)

    def test_missing_platform_env_rejected(self, monkeypatch, fernet_env):
        monkeypatch.delenv("PLATFORM_DB_HOST", raising=False)
        with pytest.raises(SourceResolutionError, match="平台库环境变量"):
            resolve_source_registration(TEST_SOURCE_ID)

    def test_missing_credential_key_rejected(
        self, monkeypatch, fernet_env, patched_connect
    ):
        monkeypatch.delenv("SOURCE_CREDENTIAL_KEY", raising=False)
        patched_connect.row = ROW
        with pytest.raises(SourceResolutionError, match="SOURCE_CREDENTIAL_KEY"):
            resolve_source_registration(TEST_SOURCE_ID)


class TestApplyRegistration:
    def test_business_connection_and_runtime_keys_injected(self, db_agent_config):
        registration = SourceRegistration(
            source_id=TEST_SOURCE_ID,
            db_name="orders-b",
            host="10.0.0.9",
            port=5433,
            username="svc_etl",
            password="s3cret",
            sslmode="require",
            selected_schemas=("public", "sales"),
            is_active=True,
        )
        updated = apply_registration_to_config(db_agent_config, registration)
        b = updated.business_db
        assert (b.host, b.port, b.user, b.password, b.sslmode) == (
            "10.0.0.9",
            5433,
            "svc_etl",
            "s3cret",
            "require",
        )
        # 三键运行配置同源（doc 4 §4.7：不得沿用固定前缀）
        assert b.database == b.expected_database == b.table_key_prefix == "orders-b"
        # 登记 selected_schemas 落入运行约束（范围外对象不进查询链路）
        assert b.allowed_schemas == ["public", "sales"]
        # 原配置对象不被修改
        assert db_agent_config.business_db.host != "10.0.0.9"
        assert db_agent_config.business_db.allowed_schemas is None


class TestBuildServicesCleanup:
    def test_partial_construction_closes_created_resources(
        self, db_agent_config, monkeypatch
    ):
        """构造链中段失败：已创建的 business_db/milvus 按逆序关闭。"""
        import src.modules.db_deepagent.services as services_mod

        closed = []

        class _FakeBusinessDb:
            def __init__(self, cfg):
                pass

            def open(self):
                pass

            def close(self):
                closed.append("business_db")

        class _FakeMilvus:
            def __init__(self, cfg, embedding, *, source_id=None):
                pass

            def close(self):
                closed.append("milvus")

        def _boom(*args, **kwargs):
            raise RuntimeError("embedding 一致性检查失败")

        monkeypatch.setattr(services_mod, "BusinessDbService", _FakeBusinessDb)
        monkeypatch.setattr(services_mod, "DbMilvusRetrieval", _FakeMilvus)
        monkeypatch.setattr(services_mod, "check_embedding_consistency", _boom)

        with pytest.raises(RuntimeError, match="一致性检查失败"):
            services_mod.build_services(db_agent_config, source_id=TEST_SOURCE_ID)

        assert closed == ["milvus", "business_db"]  # 逆序回收

    def test_join_graph_failure_closes_milvus_and_business_db(
        self, db_agent_config, monkeypatch
    ):
        import src.modules.db_deepagent.services as services_mod

        closed = []

        class _FakeBusinessDb:
            def __init__(self, cfg):
                pass

            def open(self):
                pass

            def close(self):
                closed.append("business_db")

        class _FakeMilvus:
            def __init__(self, cfg, embedding, *, source_id=None):
                pass

            def close(self):
                closed.append("milvus")

        monkeypatch.setattr(services_mod, "BusinessDbService", _FakeBusinessDb)
        monkeypatch.setattr(services_mod, "DbMilvusRetrieval", _FakeMilvus)
        monkeypatch.setattr(
            services_mod,
            "check_embedding_consistency",
            lambda *a, **k: {},
        )

        def _boom(cfg):
            raise RuntimeError("neo4j 配置不可用")

        monkeypatch.setattr(services_mod, "_neo4j_manager", _boom)

        with pytest.raises(RuntimeError, match="neo4j"):
            services_mod.build_services(db_agent_config, source_id=TEST_SOURCE_ID)

        assert closed == ["milvus", "business_db"]


class TestPlatformConninfo:
    def test_password_with_spaces_survives_parsing(self, monkeypatch):
        """make_conninfo 组装：含空格密码可被 conninfo 解析还原（修复对照）。"""
        from src.modules.db_deepagent.source_registry import _platform_conninfo

        monkeypatch.setenv("PLATFORM_DB_HOST", "pg host")  # 主机名含空格同样受保护
        monkeypatch.setenv("PLATFORM_DB_PORT", "5432")
        monkeypatch.setenv("PLATFORM_DB_NAME", "metaweave")
        monkeypatch.setenv("PLATFORM_DB_USER", "svc user")
        monkeypatch.setenv("PLATFORM_DB_PASSWORD", "pa ss word")
        from psycopg.conninfo import conninfo_to_dict

        parsed = conninfo_to_dict(_platform_conninfo())
        assert parsed["password"] == "pa ss word"
        assert parsed["host"] == "pg host"
        assert parsed["user"] == "svc user"


class TestGetColumnsSchemaScope:
    def test_out_of_scope_schema_rejected_before_pool(self, db_agent_config):
        """allowed_schemas 范围外的 schema 在进连接池之前即被拒绝。"""
        from src.modules.db_deepagent.errors import DbExecutionError
        from src.modules.db_deepagent.services.business_db import BusinessDbService

        cfg = db_agent_config.model_copy(deep=True)
        cfg.business_db.allowed_schemas = ["public"]
        svc = BusinessDbService.__new__(BusinessDbService)  # 不建池
        svc._cfg = cfg.business_db
        with pytest.raises(DbExecutionError, match="selected_schemas"):
            svc.get_columns("other_schema", "some_table")

    def test_conninfo_includes_search_path(self, db_agent_config):
        """allowed_schemas 非空时连接 options 追加 search_path（一层防线）。"""
        from src.modules.db_deepagent.services.business_db import (
            _make_business_conninfo,
        )

        cfg = db_agent_config.model_copy(deep=True)
        cfg.business_db.allowed_schemas = ["public", "sales"]
        conninfo = _make_business_conninfo(cfg.business_db)
        # 按 PG 标识符规则加引号，保留实际大小写（未加引号会被折叠小写）
        assert 'search_path="public","sales"' in conninfo
        # 缺省（None）不追加
        plain = _make_business_conninfo(db_agent_config.business_db)
        assert "search_path=" not in plain

    def test_search_path_quotes_case_sensitive_registration(self, db_agent_config):
        """登记大小写敏感 schema（"Sales"）：search_path 逐字引用，不折叠。"""
        from src.modules.db_deepagent.services.business_db import (
            _make_business_conninfo,
        )

        cfg = db_agent_config.model_copy(deep=True)
        cfg.business_db.allowed_schemas = ["Sales"]
        conninfo = _make_business_conninfo(cfg.business_db)
        assert 'search_path="Sales"' in conninfo
