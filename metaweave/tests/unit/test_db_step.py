"""db 采集核心与步骤 worker 的无外部数据库单测（doc 9、6.2 §7.1）。

断代改造适配（doc 6）：身份改短 ID（source_id=db_src_1、job_id 槽装
workspace_id=ws_1）；worker 新签名 `run_db_step(ctx: StepContext) ->
StepOutcome`，状态落库与 marker 由编排层负责，worker 不再访问平台库。
"""

import logging
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest
from click.testing import CliRunner

from metaweave.cli.metadata_cli import metadata_command
from metaweave.core.clean_targets import (
    resolve_db_output_dir,
    resolve_source_workspace_dir,
)
from metaweave.utils.file_utils import get_project_root
from metaweave.core.metadata import db_step
from metaweave.db.postgres_options import build_connect_kwargs
from metaweave.utils.source_id import (
    normalize_run_uuid,
    normalize_source_id,
    normalize_workspace_id,
)

from backend.data_sources import db_step_service as service
from backend.data_sources.db_step_service import (
    DbStepConfigError,
    SourceDisabledError,
)
from backend.data_sources.step_context import StepContext, StepOutcome
from backend.persistence.models import DbSourceInfo

SOURCE_ID = "db_src_1"
WORKSPACE_ID = "ws_1"
RUN_ID = UUID("11111111-2222-4333-8444-555555555555")

DEFAULT_VERSION_STRING = (
    "PostgreSQL 18.0 on x86_64-pc-linux-gnu, compiled by gcc, 64-bit"
)


class FakeCursor:
    def __init__(
        self,
        version="180000",
        statements=None,
        version_string=DEFAULT_VERSION_STRING,
        version_error=None,
        stats_error=None,
    ):
        self.version = version
        self.version_string = version_string
        self.version_error = version_error
        self.stats_error = stats_error
        self.sql = ""
        self.params = None
        self.statements = statements if statements is not None else []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, sql, params=None):
        self.sql = sql
        self.params = params
        self.statements.append((sql, params))

    def fetchone(self):
        if "SELECT version()" in self.sql:
            if self.version_error is not None:
                raise self.version_error
            return (self.version_string,)
        if "SHOW server_version_num" in self.sql:
            return (self.version,)
        if "has_schema_privilege" in self.sql:
            return (True,) if self.params[0] == "public" else None
        if "pg_database_size" in self.sql:
            return (123456,)
        if "FROM pg_class" in self.sql:
            if self.stats_error is not None:
                raise self.stats_error
            return (8, 1, 12, 2, 1)
        raise AssertionError(self.sql)

    def fetchall(self):
        if "FROM pg_namespace" in self.sql:
            return [
                ("sales", True),
                ("public", True),
                ("hidden", False),
                ("pg_catalog", True),
                ("information_schema", True),
                ("pg_toast", True),
                ("pg_temp_5", True),
                ("pg_toast_temp_5", True),
            ]
        raise AssertionError(self.sql)


class FakeConnection:
    def __init__(
        self,
        version="180000",
        version_string=DEFAULT_VERSION_STRING,
        version_error=None,
        stats_error=None,
    ):
        self.version = version
        self.version_string = version_string
        self.version_error = version_error
        self.stats_error = stats_error
        self.autocommit = False
        self.statements = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def cursor(self):
        return FakeCursor(
            self.version,
            self.statements,
            self.version_string,
            self.version_error,
            self.stats_error,
        )


def make_request(**overrides):
    values = dict(
        source_id=SOURCE_ID,
        job_id=WORKSPACE_ID,
        db_type="postgresql",
        host="localhost",
        port=5432,
        db_name="demo",
        username="reader",
        password="p a=s",
        sslmode=None,
        selected_schemas=("public",),
        clean=False,
    )
    values.update(overrides)
    return db_step.DbExecutionRequest(**values)


def _source(**overrides):
    """构造登记源快照（detached ORM 实例，StepContext 的输入形态）。"""
    values = dict(
        source_id=SOURCE_ID,
        display_name="测试源",
        description=None,
        db_type="postgresql",
        host="localhost",
        port=5432,
        db_name="demo",
        username="reader",
        password_ciphertext="cipher",
        sslmode=None,
        db_version=None,
        selected_schemas=["public"],
        is_active=True,
    )
    values.update(overrides)
    return DbSourceInfo(**values)


def _ctx(
    tmp_path,
    *,
    source=None,
    main_config=None,
    step_params=None,
    run_id=RUN_ID,
):
    return StepContext(
        settings=SimpleNamespace(),
        source=source if source is not None else _source(),
        main_config=main_config if main_config is not None else {"database": {}},
        config_path=Path("configs/metadata_config.yaml"),
        source_id=SOURCE_ID,
        workspace_id=WORKSPACE_ID,
        run_id=run_id,
        step_name="db",
        step_params=step_params,
    )


# ---------- db_step 核心（execute_db / 画像契约） ----------


def test_db_request_repr_hides_password_and_config_secrets():
    password = "C3-unique-source-password-7391!"
    api_key = "C3-unique-llm-api-key-8264!"
    request = make_request(
        password=password,
        main_config={"llm": {"api_key": api_key}},
    )

    representation = repr(request)
    assert "DbExecutionRequest" in representation
    assert SOURCE_ID in representation
    assert WORKSPACE_ID in representation
    assert password not in representation
    assert api_key not in representation
    assert "main_config=" not in representation


@pytest.mark.parametrize("connection_fails", [False, True])
def test_db_step_connection_logs_hide_password_and_length(
    tmp_path, monkeypatch, caplog, connection_fails
):
    password = "C3-unique-source-password-7391!"
    target = tmp_path / "db"
    monkeypatch.setattr(db_step, "resolve_db_output_dir", lambda *_: target)

    def connect(**_kwargs):
        if connection_fails:
            raise RuntimeError(f"password={password}")
        return FakeConnection()

    monkeypatch.setattr(db_step.psycopg, "connect", connect)
    caplog.set_level(logging.INFO, logger="metaweave.db_step")

    result = db_step.execute_db(make_request(password=password))

    assert result["success"] is not connection_fails
    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert ("源库连接失败" if connection_fails else "数据库概览已写入") in messages
    assert password not in messages
    assert "Password:" not in messages
    assert "*" * len(password) not in messages


def test_legacy_connector_success_and_failure_logs_hide_password_length(
    monkeypatch, caplog
):
    from metaweave.core.metadata import connector as connector_module

    password = "C3-unique-source-password-7391!"
    config = {
        "host": "localhost",
        "port": 5432,
        "database": "demo",
        "user": "reader",
        "password": password,
    }
    caplog.set_level(logging.INFO, logger="metaweave.connector")
    monkeypatch.setattr(connector_module, "ConnectionPool", lambda *_a, **_kw: object())
    connector_module.DatabaseConnector(config)

    def fail_pool(*_args, **_kwargs):
        raise RuntimeError(f"password={password}")

    monkeypatch.setattr(connector_module, "ConnectionPool", fail_pool)
    with pytest.raises(RuntimeError, match="password="):
        connector_module.DatabaseConnector(config)

    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert "数据库连接池已创建" in messages
    assert "创建数据库连接池失败" in messages
    assert password not in messages
    assert "Password:" not in messages
    assert "*" * len(password) not in messages


def test_connection_kwargs_keep_values_separate():
    """doc 18.1 §4.1：参数独立传递 + sslmode 值域校验在建连前拒绝。"""
    kwargs = build_connect_kwargs(
        host="localhost",
        port=5432,
        dbname="demo",
        user="reader",
        password="p a=s",
        sslmode="require",
        connect_timeout_seconds=10,
        statement_timeout_ms=60000,
    )
    assert kwargs["password"] == "p a=s"
    assert kwargs["sslmode"] == "require"
    assert kwargs["options"] == "-c statement_timeout=60000"
    assert kwargs["connect_timeout"] == 10
    with pytest.raises(ValueError, match="sslmode"):
        build_connect_kwargs(
            host="localhost",
            port=5432,
            dbname="demo",
            user="reader",
            password="p a=s",
            sslmode="require host=other",
        )
    assert "sslmode" not in build_connect_kwargs(
        host="h",
        port=5432,
        dbname="d",
        user="u",
        password="p",
        sslmode=None,
        connect_timeout_seconds=10,
        statement_timeout_ms=60000,
    )


def test_profile_contract_and_partition_counts():
    connection = FakeConnection()
    version_string = "PostgreSQL 18.6 on x86_64-pc-linux-gnu, compiled by gcc, 64-bit"
    result = db_step.collect_database_profile(
        connection, make_request(), 180006, version_string
    )
    assert result["schema_version"] == 1
    assert result["source_id"] == SOURCE_ID
    assert result["schemas"][0]["partition_child_count"] == 12
    assert result["schemas"][0]["object_count"] == 12
    assert result["totals"]["object_count"] == 12
    assert result["database"]["record_count"] is None
    assert result["database"]["size_bytes"] == 123456
    assert result["database"]["server_version"] == "18.6"
    assert result["database"]["server_version_full"] == version_string
    assert result["database"]["os_platform"] == "Linux x86_64"
    assert result["collected_at"].endswith("Z")
    import re as _re

    assert _re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z", result["collected_at"]
    )
    assert "password" not in __import__("json").dumps(result)

    # fake 的固定计数只能检查列映射；这里锁住实际发出的五列 SQL 统计口径。
    count_queries = [
        (sql, params) for sql, params in connection.statements if "FROM pg_class" in sql
    ]
    assert len(count_queries) == 1
    sql, params = count_queries[0]
    assert params == ("public",)
    normalized_sql = " ".join(sql.split())
    assert normalized_sql == (
        "SELECT "
        "count(*) FILTER (WHERE c.relkind = 'r' AND NOT c.relispartition), "
        "count(*) FILTER (WHERE c.relkind = 'p' AND NOT c.relispartition), "
        "count(*) FILTER (WHERE c.relispartition AND c.relkind IN ('r', 'p')), "
        "count(*) FILTER (WHERE c.relkind = 'v'), "
        "count(*) FILTER (WHERE c.relkind = 'm') "
        "FROM pg_class AS c "
        "JOIN pg_namespace AS n ON n.oid = c.relnamespace "
        "WHERE n.nspname = %s"
    )


@pytest.mark.parametrize(
    ("version_string", "expected"),
    [
        ("PostgreSQL 12.22 on x86_64-pc-linux-musl, compiled by gcc", "Linux x86_64"),
        ("PostgreSQL 18.6 on x86_64-pc-linux-gnu, compiled by gcc", "Linux x86_64"),
        ("PostgreSQL 18.1 on x86_64-windows, compiled by msvc", "Windows x86_64"),
        ("PostgreSQL 16.1 on x86_64-w64-mingw32, compiled by gcc", "Windows x86_64"),
        ("PostgreSQL 16.1 on x86_64-pc-cygwin, compiled by gcc", "Windows x86_64"),
        (
            "PostgreSQL 16.1 on aarch64-apple-darwin23.2.0, compiled by clang",
            "macOS aarch64",
        ),
        (
            "PostgreSQL 16.1 on powerpc64le-unknown-linux-gnu, compiled by gcc",
            "Linux powerpc64le",
        ),
        ("PostgreSQL 16.1 on sparc-sun-solaris2.11, compiled by gcc", "Solaris sparc"),
        ("PostgreSQL 16.1 on rs6000-ibm-aix7.2.0.0, compiled by gcc", "AIX rs6000"),
        ("PostgreSQL 16.1 on i386-unknown-openbsd7.4, compiled by gcc", "OpenBSD i386"),
        (
            "PostgreSQL 16.1 on x86_64-unknown-freebsd13.2, compiled by clang",
            "FreeBSD x86_64",
        ),
        ("PostgreSQL 16.1 on linux-gnu, compiled by gcc", "Linux"),
        ("PostgreSQL 16.1 on some-unknown-target, compiled by gcc", None),
        ("PostgreSQL 12.3, compiled by Visual C++ build 1914, 64-bit", None),
        ("PostgreSQL 12 on , compiled by gcc", None),
        ("", None),
        (None, None),
    ],
)
def test_normalize_os_platform(version_string, expected):
    assert db_step.normalize_os_platform(version_string) == expected


def test_version_string_failure_keeps_collection_and_nulls_platform(
    tmp_path, monkeypatch, caplog
):
    target = tmp_path / "db"
    monkeypatch.setattr(db_step, "resolve_db_output_dir", lambda *_: target)
    monkeypatch.setattr(
        db_step.psycopg,
        "connect",
        lambda **_: FakeConnection(version_error=RuntimeError("version unavailable")),
    )
    caplog.set_level(logging.WARNING, logger="metaweave.db_step")

    result = db_step.execute_db(make_request())

    assert result["success"] is True
    profile = __import__("json").loads(
        (target / "database.json").read_text(encoding="utf-8")
    )
    assert profile["database"]["server_version_full"] is None
    assert profile["database"]["os_platform"] is None
    warnings = [
        record.getMessage()
        for record in caplog.records
        if record.levelno >= logging.WARNING
    ]
    assert any(
        "RuntimeError" in message and WORKSPACE_ID in message for message in warnings
    )
    assert all("version unavailable" not in message for message in warnings)


def test_dead_connection_after_version_read_fails_collection(tmp_path, monkeypatch):
    target = tmp_path / "db"
    monkeypatch.setattr(db_step, "resolve_db_output_dir", lambda *_: target)
    monkeypatch.setattr(
        db_step.psycopg,
        "connect",
        lambda **_: FakeConnection(
            version_error=RuntimeError("connection lost"),
            stats_error=RuntimeError("connection lost"),
        ),
    )

    result = db_step.execute_db(make_request())

    assert result["success"] is False
    assert "schema 校验或对象统计" in result["message"]
    assert "采集完成" not in result["message"]


def test_schema_names_preserve_spaces_and_reject_invalid():
    assert db_step.validate_schema_names(["sales "]) == ("sales ",)
    for names in ([], [""], ["  "], ["public", "public"], [None]):
        with pytest.raises(ValueError):
            db_step.validate_schema_names(names)
    with pytest.raises(ValueError, match="不存在"):
        db_step.validate_selected_schemas(FakeConnection(), ["missing"])


def test_schema_discovery_and_validation_share_selectable_scope():
    assert db_step.discover_selectable_schemas(FakeConnection()) == [
        "public",
        "sales",
    ]
    for name in ("pg_catalog", "information_schema", "pg_toast", "pg_temp_5"):
        with pytest.raises(db_step.SchemaNotSelectableError):
            db_step.validate_selected_schemas(FakeConnection(), [name])
    with pytest.raises(db_step.SchemaMissingError):
        db_step.validate_selected_schemas(FakeConnection(), ["missing"])

    class NoUsageConnection(FakeConnection):
        def cursor(self):
            cursor = FakeCursor()
            cursor.fetchone = lambda: (False,)
            return cursor

    with pytest.raises(db_step.SchemaNoUsageError):
        db_step.validate_selected_schemas(NoUsageConnection(), ["sales"])


@pytest.mark.parametrize("name", [" sales", "sales "])
def test_schema_error_shows_name_boundaries(name):
    with pytest.raises(db_step.DbProfileValidationError) as exc_info:
        db_step.validate_selected_schemas(FakeConnection(), [name])
    assert str(exc_info.value) == f"所选 schema 不存在: {name!r}"

    class NoUsageConnection(FakeConnection):
        def cursor(self):
            cursor = FakeCursor()
            cursor.fetchone = lambda: (False,)
            return cursor

    with pytest.raises(db_step.DbProfileValidationError) as exc_info:
        db_step.validate_selected_schemas(NoUsageConnection(), [name])
    assert str(exc_info.value) == f"对所选 schema 无 USAGE 权限: {name!r}"


@pytest.mark.parametrize("version", ["140000", "140012", "invalid"])
def test_version_failure_does_not_touch_existing_workspace(
    tmp_path, monkeypatch, version
):
    target = tmp_path / "db"
    target.mkdir()
    (target / "database.json").write_text("old", encoding="utf-8")
    (target / ".workspace_id").write_text("old-workspace", encoding="utf-8")
    monkeypatch.setattr(db_step, "resolve_db_output_dir", lambda *_: target)
    monkeypatch.setattr(db_step.psycopg, "connect", lambda **_: FakeConnection(version))
    result = db_step.execute_db(make_request(clean=True))
    assert result["success"] is False
    assert "版本" in result["message"]
    assert (target / "database.json").read_text() == "old"
    assert (target / ".workspace_id").exists()


@pytest.mark.parametrize(
    ("version", "display_version"),
    [
        ("150000", "15.0"),
        ("150006", "15.6"),
        ("160000", "16.0"),
        ("170000", "17.0"),
        ("180006", "18.6"),
    ],
)
def test_supported_versions_enter_same_collection_flow(
    tmp_path, monkeypatch, version, display_version
):
    target = tmp_path / "db"
    monkeypatch.setattr(db_step, "resolve_db_output_dir", lambda *_: target)
    monkeypatch.setattr(db_step.psycopg, "connect", lambda **_: FakeConnection(version))

    result = db_step.execute_db(make_request())

    assert result["success"] is True
    profile = __import__("json").loads(
        (target / "database.json").read_text(encoding="utf-8")
    )
    assert profile["source_id"] == SOURCE_ID
    assert profile["database"]["server_version"] == display_version
    assert profile["schema_version"] == 1


def test_success_replaces_json_and_leaves_stale_marker_untouched(tmp_path, monkeypatch):
    """doc 9 §4：新路径不创建调试标记，也不再删除旧标记。"""
    target = tmp_path / "db"
    target.mkdir()
    (target / "database.json").write_text("old", encoding="utf-8")
    (target / ".workspace_id").write_text("old-workspace", encoding="utf-8")
    monkeypatch.setattr(db_step, "resolve_db_output_dir", lambda *_: target)
    monkeypatch.setattr(db_step.psycopg, "connect", lambda **_: FakeConnection())
    result = db_step.execute_db(make_request())
    assert result["success"] is True
    assert (
        __import__("json").loads((target / "database.json").read_text())["source_id"]
        == SOURCE_ID
    )
    assert (target / ".workspace_id").read_text(encoding="utf-8") == "old-workspace"


def test_clean_only_removes_top_level_files(tmp_path, monkeypatch):
    from metaweave.utils import file_utils

    target = tmp_path / "db"
    child_dir = target / "nested"
    child_dir.mkdir(parents=True)
    (target / "obsolete.txt").write_text("old")
    (target / ".workspace_id").write_text("old-workspace")
    (child_dir / "keep.txt").write_text("keep")
    monkeypatch.setattr(file_utils, "get_project_root", lambda: tmp_path)
    monkeypatch.setattr(db_step, "resolve_db_output_dir", lambda *_: target)
    monkeypatch.setattr(db_step.psycopg, "connect", lambda **_: FakeConnection())
    result = db_step.execute_db(make_request(clean=True))
    assert result["success"] is True
    assert not (target / "obsolete.txt").exists()
    assert not (target / ".workspace_id").exists()
    assert (child_dir / "keep.txt").exists()


def test_clean_does_not_touch_sibling_workspace_directory(tmp_path, monkeypatch):
    """doc 9 §4 / doc 6 §2：--clean 只清本工作区 db/，不越界到其他工作区。"""
    from metaweave.utils import file_utils

    monkeypatch.setattr(file_utils, "get_project_root", lambda: tmp_path)
    own = tmp_path / "output" / SOURCE_ID / WORKSPACE_ID / "db"
    other = tmp_path / "output" / SOURCE_ID / "ws_2" / "db"
    own.mkdir(parents=True)
    other.mkdir(parents=True)
    (own / "obsolete.txt").write_text("old", encoding="utf-8")
    (other / "keep.txt").write_text("keep", encoding="utf-8")

    monkeypatch.setattr(db_step.psycopg, "connect", lambda **_: FakeConnection())
    result = db_step.execute_db(make_request(job_id=WORKSPACE_ID, clean=True))

    assert result["success"] is True
    assert not (own / "obsolete.txt").exists()
    assert (own / "database.json").exists()
    assert (other / "keep.txt").read_text(encoding="utf-8") == "keep"
    assert not (other / "database.json").exists()


def test_execute_db_logs_carry_workspace_and_source(tmp_path, monkeypatch, caplog):
    """doc 9 §5：步骤日志带 job_id（工作区）、source_id，便于按源检索。"""
    target = tmp_path / "db"
    monkeypatch.setattr(db_step, "resolve_db_output_dir", lambda *_: target)
    monkeypatch.setattr(db_step.psycopg, "connect", lambda **_: FakeConnection())
    caplog.set_level(logging.INFO, logger="metaweave.db_step")

    result = db_step.execute_db(make_request())

    assert result["success"] is True
    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert WORKSPACE_ID in messages
    assert SOURCE_ID in messages


# ---------- 身份与目录（短 ID，doc 6 §1 / §2） ----------


def test_short_id_identity_and_workspace_paths(tmp_path, monkeypatch):
    """短 ID 是唯一身份形态；旧 UUID 值不再合法（doc 6.2 §4.1）。"""
    monkeypatch.setattr("metaweave.utils.file_utils.get_project_root", lambda: tmp_path)
    assert normalize_source_id(SOURCE_ID) == SOURCE_ID
    assert normalize_workspace_id(WORKSPACE_ID) == WORKSPACE_ID
    for legacy_uuid in (
        "b9a4943d-44d6-4f35-b553-d25207f9d8c1",
        str(UUID("b9a4943d-44d6-4f35-b553-d25207f9d8c1")).upper(),
    ):
        with pytest.raises(ValueError):
            normalize_source_id(legacy_uuid)
        with pytest.raises(ValueError):
            normalize_workspace_id(legacy_uuid)
    # 运行 UUID 与短 ID 分开校验（doc 6.2 §4.1）
    assert normalize_run_uuid(str(RUN_ID).upper()) == RUN_ID
    with pytest.raises(ValueError):
        normalize_run_uuid(SOURCE_ID)

    resolved = resolve_db_output_dir(SOURCE_ID, WORKSPACE_ID)
    assert resolved.parts[-4:] == ("output", SOURCE_ID, WORKSPACE_ID, "db")
    assert (
        resolve_source_workspace_dir(SOURCE_ID)
        == resolved.parents[1]
        == tmp_path.resolve() / "output" / SOURCE_ID
    )


def test_source_workspace_dir_rejects_project_root_and_outside(monkeypatch):
    def as_root(_path):
        return get_project_root()

    monkeypatch.setattr("metaweave.core.clean_targets.resolve_project_path", as_root)
    with pytest.raises(ValueError):
        resolve_source_workspace_dir(SOURCE_ID)

    def outside(_path):
        return Path("/tmp/metaweave-outside-workspace")

    monkeypatch.setattr("metaweave.core.clean_targets.resolve_project_path", outside)
    with pytest.raises(ValueError):
        resolve_source_workspace_dir(SOURCE_ID)


def test_source_workspace_dir_rejects_symlinked_output_parent(monkeypatch, tmp_path):
    monkeypatch.setattr("metaweave.utils.file_utils.get_project_root", lambda: tmp_path)
    target = tmp_path / "store" / SOURCE_ID
    target.mkdir(parents=True)
    (target / "keep.txt").write_text("keep", encoding="utf-8")
    (tmp_path / "output").symlink_to(target.parent, target_is_directory=True)
    with pytest.raises(ValueError, match="数据源工作区路径包含符号链接"):
        resolve_source_workspace_dir(SOURCE_ID)
    assert (target / "keep.txt").read_text(encoding="utf-8") == "keep"


# ---------- 步骤 worker：run_db_step(ctx) ----------


@pytest.fixture
def decrypt(monkeypatch):
    """把凭据解密换成直通替身，worker 不触碰真实加密。"""
    import backend.security.source_password as crypto

    monkeypatch.setattr(crypto, "decrypt_password", lambda _cipher: "secret")


def test_worker_success_builds_request_and_reports_output(
    tmp_path, monkeypatch, decrypt
):
    captured = []

    def fake_execute(request):
        captured.append(request)
        return {"success": True, "message": ""}

    monkeypatch.setattr(service, "execute_db", fake_execute)
    ctx = _ctx(tmp_path, main_config={"database": {"host": "legacy"}})
    outcome = service.run_db_step(ctx)

    assert isinstance(outcome, StepOutcome)
    assert outcome.success is True
    assert "数据库画像采集完成" in outcome.message  # 空消息回退为完成文案
    assert outcome.output_files == ["db/database.json"]
    (request,) = captured
    # 双库边界（doc 18.1 §5.1）：请求取登记行连接参数，不取主配置连接
    assert request.source_id == SOURCE_ID
    assert request.job_id == WORKSPACE_ID  # job_id 槽装工作区短 ID（doc 6 §2）
    assert request.host == "localhost" and request.port == 5432
    assert request.db_name == "demo" and request.username == "reader"
    assert request.password == "secret"
    assert request.clean is False
    assert request.main_config == {"database": {"host": "legacy"}}


def test_worker_preserves_generator_message(tmp_path, monkeypatch, decrypt):
    monkeypatch.setattr(
        service,
        "execute_db",
        lambda _request: {"success": False, "message": "源库连接失败"},
    )
    outcome = service.run_db_step(_ctx(tmp_path))
    assert outcome.success is False
    assert outcome.message == "源库连接失败"
    assert outcome.output_files == []


def test_worker_generator_crash_is_step_failure(tmp_path, monkeypatch, decrypt, caplog):
    """生成器内部异常按步骤失败计，且不回显异常原文（凭据可能在内）。"""

    def boom(_request):
        raise RuntimeError("password=secret")

    monkeypatch.setattr(service, "execute_db", boom)
    with caplog.at_level("ERROR"):
        outcome = service.run_db_step(_ctx(tmp_path))
    assert outcome.success is False
    assert outcome.message == "数据库画像采集失败（RuntimeError）"
    assert "secret" not in outcome.message
    assert any(record.levelno == logging.ERROR for record in caplog.records)


@pytest.mark.parametrize(
    ("source_kwargs", "exc_type", "fragment"),
    [
        (dict(db_type="mysql"), DbStepConfigError, "仅支持 PostgreSQL"),
        (dict(username=""), DbStepConfigError, "必要的连接字段"),
        (dict(host=""), DbStepConfigError, "必要的连接字段"),
        (dict(db_name=""), DbStepConfigError, "必要的连接字段"),
        (dict(selected_schemas=[]), DbStepConfigError, "selected_schemas"),
        (dict(selected_schemas=["a", "a"]), DbStepConfigError, "重复"),
    ],
)
def test_worker_rejects_bad_registration_before_execute(
    tmp_path, monkeypatch, decrypt, source_kwargs, exc_type, fragment
):
    """登记字段/schema 非法在执行前拒绝；blocked 由编排层记录，不采集。"""
    calls = []
    monkeypatch.setattr(service, "execute_db", lambda _r: calls.append("execute"))
    with pytest.raises(exc_type, match=fragment):
        service.run_db_step(_ctx(tmp_path, source=_source(**source_kwargs)))
    assert calls == []


def test_worker_rejects_disabled_source_before_decrypt(tmp_path, monkeypatch):
    """禁用数据源：不解密凭据、不采集（doc 11 §4；编排层映射 blocked）。"""
    import backend.security.source_password as crypto

    monkeypatch.setattr(
        crypto,
        "decrypt_password",
        lambda _cipher: pytest.fail("禁用数据源不应解密凭据"),
    )
    calls = []
    monkeypatch.setattr(service, "execute_db", lambda _r: calls.append("execute"))
    with pytest.raises(SourceDisabledError) as exc_info:
        service.run_db_step(_ctx(tmp_path, source=_source(is_active=False)))
    assert "已禁用" in str(exc_info.value)
    assert calls == []
    # CLI/编排层依赖这个继承关系把该错误映射为 exit 2 / blocked
    assert issubclass(SourceDisabledError, DbStepConfigError)


def test_worker_does_not_touch_platform_db(tmp_path, monkeypatch, decrypt):
    """断代改造后的边界：worker 不再读写平台库（状态由编排层落 run_step）。"""
    detonated = []

    def fail(name):
        def _boom(*_args, **_kwargs):
            detonated.append(name)
            raise AssertionError(f"不应访问平台库: {name}")

        return _boom

    monkeypatch.setattr(service, "execute_db", lambda _r: {"success": True})
    ctx = _ctx(tmp_path)
    outcome = service.run_db_step(ctx)
    assert outcome.success is True
    assert detonated == []


# ---------- CLI 参数校验（--step db 受管入口） ----------


@pytest.mark.parametrize(
    ("args", "expected_error"),
    [
        (["--step", "db"], "--step db 必须指定 --source-id"),
        (
            ["--step", "db", "--source-id", SOURCE_ID],
            "--step db 必须指定 --workspace-id",
        ),
        (
            ["--step", "db", "--source-id", "bad", "--workspace-id", WORKSPACE_ID],
            "不是合法短 ID",
        ),
        (
            ["--step", "db", "--source-id", SOURCE_ID, "--workspace-id", "bad"],
            "不是合法短 ID",
        ),
        (
            [
                "--step",
                "db",
                "--source-id",
                str(RUN_ID),
                "--workspace-id",
                WORKSPACE_ID,
            ],
            "不是合法短 ID",  # UUID 不再是合法 source_id（doc 6 §1）
        ),
        (
            [
                "--step",
                "db",
                "--source-id",
                SOURCE_ID,
                "--workspace-id",
                WORKSPACE_ID,
                "--schemas",
                "public",
            ],
            "--step db 不接受",
        ),
        (
            [
                "--step",
                "db",
                "--source-id",
                SOURCE_ID,
                "--workspace-id",
                WORKSPACE_ID,
                "--max-workers",
                "4",
            ],
            "--step db 不接受",
        ),
        (
            # --step ddl 带 --source-id 是合法受管入口（doc 16 §4），换 json 验证拒绝
            ["--step", "json", "--source-id", SOURCE_ID],
            "--source-id 仅适用于",
        ),
        (
            [
                "--step",
                "db",
                "--source-id",
                SOURCE_ID,
                "--source-id",
                SOURCE_ID,
            ],
            "--source-id 仅允许传入一次",
        ),
        (
            [
                "--step",
                "db",
                "--source-id",
                SOURCE_ID,
                "--workspace-id",
                WORKSPACE_ID,
                "--workspace-id",
                WORKSPACE_ID,
            ],
            "--workspace-id 仅允许传入一次",
        ),
        (
            ["--step", "ddl", "--workspace-id", WORKSPACE_ID],
            "--workspace-id 仅在受管模式",
        ),
    ],
)
def test_cli_rejects_invalid_db_options_before_dispatch(
    tmp_path, monkeypatch, args, expected_error
):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("database: {}\n", encoding="utf-8")
    monkeypatch.setattr(
        service,
        "run_db_step",
        lambda _ctx: pytest.fail("参数校验失败不应派发 worker"),
    )
    result = CliRunner().invoke(metadata_command, ["-c", str(cfg), *args])
    assert result.exit_code == 2, result.output
    assert expected_error in result.output


# ---------- 登记脚本与平台库设置（与 db 采集共享的公共段） ----------


@pytest.mark.parametrize(
    ("answers", "expected"),
    [
        (["public,sales"], ("public", "sales")),
        (["public, sales", "no", "public,sales"], ("public", "sales")),
        (["public, sales", "yes"], ("public", " sales")),
        (["sales ", "yes"], ("sales ",)),
    ],
)
def test_registration_schema_prompt_confirms_whitespace(
    monkeypatch, capsys, answers, expected
):
    from scripts import register_db_source as register

    responses = iter(answers)
    monkeypatch.setattr("builtins.input", lambda _: next(responses))
    assert register._prompt_schema_names() == expected
    output = capsys.readouterr().out
    if any(name != name.strip() for name in expected) or len(answers) > 1:
        assert "' sales'" in output or "'sales '" in output
    else:
        assert "首尾空格" not in output


def test_registration_rejects_blank_display_name_before_connect(monkeypatch):
    from scripts import register_db_source as register

    monkeypatch.setattr(register, "load_project_env", lambda: None)
    monkeypatch.setattr(register, "load_platform_settings_from_env", lambda: object())
    monkeypatch.setattr("builtins.input", lambda _: "  ")
    monkeypatch.setattr(
        register, "platform_session", lambda _: pytest.fail("should not connect")
    )
    with pytest.raises(ValueError, match="显示名称"):
        register.main()


def test_source_password_round_trip_and_safe_failure(monkeypatch):
    from cryptography.fernet import Fernet
    from backend.security.source_password import encrypt_password, decrypt_password

    monkeypatch.setenv("SOURCE_CREDENTIAL_KEY", Fernet.generate_key().decode())
    ciphertext = encrypt_password("secret with spaces")
    assert "secret" not in ciphertext
    assert decrypt_password(ciphertext) == "secret with spaces"
    with pytest.raises(ValueError, match="无法解密") as exc:
        decrypt_password("broken ciphertext")
    assert "broken ciphertext" not in str(exc.value)


def test_platform_settings_have_no_source_db_fallback(monkeypatch):
    from backend.persistence.platform_db import load_platform_settings_from_env

    monkeypatch.setenv("DB_HOST", "source-host")
    monkeypatch.delenv("PLATFORM_DB_HOST", raising=False)
    monkeypatch.setenv("PLATFORM_DB_USER", "platform-user")
    monkeypatch.setenv("PLATFORM_DB_PASSWORD", "platform-pass")
    with pytest.raises(ValueError, match="PLATFORM_DB_HOST"):
        load_platform_settings_from_env()
    monkeypatch.setenv("PLATFORM_DB_HOST", "platform-host")
    monkeypatch.delenv("PLATFORM_DB_PORT", raising=False)
    monkeypatch.delenv("PLATFORM_DB_NAME", raising=False)
    monkeypatch.delenv("PLATFORM_DB_SSLMODE", raising=False)
    settings = load_platform_settings_from_env()
    assert settings.host == "platform-host"
    assert settings.port == 5432
    assert settings.name == "metaweave"
    assert settings.sslmode is None
    assert "platform-pass" not in repr(settings)


def test_platform_session_passes_optional_sslmode_as_one_argument(monkeypatch):
    from backend.persistence import platform_db

    seen = []

    class FakeEngine:
        def dispose(self):
            seen.append("disposed")

    class FakeSession:
        def __init__(self, engine):
            assert isinstance(engine, FakeEngine)

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    def fake_create_engine(url, connect_args):
        seen.append((url, connect_args))
        return FakeEngine()

    monkeypatch.setattr(platform_db, "create_engine", fake_create_engine)
    monkeypatch.setattr(platform_db, "Session", FakeSession)
    base = dict(host="h", port=5432, name="metaweave", user="u", password="secret")
    with platform_db.platform_session(platform_db.PlatformSettings(**base)):
        pass
    assert seen[0][1] == {"connect_timeout": 10}
    assert seen[1] == "disposed"
    seen.clear()
    # doc 18.1 §4.1：非法 sslmode 在建引擎前被拒（旧断言曾要求原样透传，
    # 由 libpq 拒绝——该口径已被值域校验取代）
    with pytest.raises(ValueError, match="sslmode"):
        with platform_db.platform_session(
            platform_db.PlatformSettings(**base, sslmode="require host=other")
        ):
            pass
    assert seen == []  # 引擎未被再次创建


def test_registration_uses_shared_connection_builder(monkeypatch, capsys):
    from scripts import register_db_source as register

    events = []
    monkeypatch.setattr(register, "load_project_env", lambda: None)
    monkeypatch.setattr(register, "load_platform_settings_from_env", lambda: object())
    answers = iter(["测试源", "localhost", "5432", "demo", "reader", "", "public"])
    monkeypatch.setattr("builtins.input", lambda _: next(answers))
    monkeypatch.setattr(register, "getpass", lambda _: "p a=s")

    @contextmanager
    def fake_session(_settings):
        events.append("platform_open")
        try:
            yield object()
        finally:
            events.append("platform_close")

    monkeypatch.setattr(register, "platform_session", fake_session)
    monkeypatch.setattr(register, "find_db_sources_by_connection", lambda *_: [])
    monkeypatch.setattr(
        register, "validate_selected_schemas", lambda _conn, names: names
    )
    monkeypatch.setattr(register, "encrypt_password", lambda _: "cipher")
    seen = {}

    def fake_connect(**kwargs):
        assert events == ["platform_open", "platform_close"]
        events.append("source_connect")
        seen["kwargs"] = kwargs
        return FakeConnection()

    monkeypatch.setattr(register.psycopg, "connect", fake_connect)
    from types import SimpleNamespace

    def fake_insert(_session, fields):
        seen.setdefault("fields", fields)
        return SimpleNamespace(source_id="db_src_1")

    monkeypatch.setattr(register, "insert_db_source", fake_insert)
    register.main()
    assert seen["kwargs"] == build_connect_kwargs(
        host="localhost",
        port=5432,
        dbname="demo",
        user="reader",
        password="p a=s",
        sslmode=None,
        connect_timeout_seconds=db_step.SOURCE_CONNECT_TIMEOUT_SECONDS,
        statement_timeout_ms=db_step.SOURCE_STATEMENT_TIMEOUT_MS,
    )
    assert seen["fields"]["display_name"] == "测试源"
    assert seen["fields"]["password_ciphertext"] == "cipher"
    assert seen["fields"]["description"] is None
    assert seen["fields"]["db_version"] == "18.0"
    assert events == [
        "platform_open",
        "platform_close",
        "source_connect",
        "platform_open",
        "platform_close",
    ]
    assert "p a=s" not in capsys.readouterr().out
