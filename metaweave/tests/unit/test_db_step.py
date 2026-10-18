"""阶段一 db 采集与登记入口的无外部数据库单测。"""

import json
import logging
import re
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import UUID

import pytest
import yaml
from click.testing import CliRunner

from metaweave.cli.metadata_cli import metadata_command
from metaweave.core.clean_targets import (
    resolve_db_output_dir,
    resolve_source_workspace_dir,
)
from metaweave.utils.file_utils import get_project_root
from metaweave.core.metadata import db_step
from metaweave.db.postgres_options import build_connect_kwargs
from metaweave.utils.source_id import normalize_source_id
from sqlalchemy.exc import SQLAlchemyError

from backend.data_sources.db_step_service import (
    DbStepConfigError,
    SourceDisabledError,
)
from backend.jobs.repository import (
    SourceRegistrationMissingError,
    StepAlreadyRunningError,
    WorkspaceMissingError,
    WorkspaceSourceMismatchError,
)


SOURCE_ID = UUID("b9a4943d-44d6-4f35-b553-d25207f9d8c1")
JOB_ID = UUID("550e8400-e29b-41d4-a716-446655440000")


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
        job_id=JOB_ID,
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


def test_db_request_repr_hides_password_and_config_secrets():
    password = "C3-unique-source-password-7391!"
    api_key = "C3-unique-llm-api-key-8264!"
    request = make_request(
        password=password,
        main_config={"llm": {"api_key": api_key}},
    )

    representation = repr(request)
    assert "DbExecutionRequest" in representation
    assert str(SOURCE_ID) in representation
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


def _run_db_logging_probe(base_dir: str, early_marker: str) -> None:
    """在子进程中用真实 logging.yaml 路由，避免重配 pytest 的全局 logging。"""
    from backend.data_sources import db_step_service as service
    from metaweave.utils.logger import setup_metaweave_logging

    base = Path(base_dir)
    config = yaml.safe_load(Path("configs/logging.yaml").read_text(encoding="utf-8"))
    for name, handler in config["handlers"].items():
        if "filename" in handler:
            filename = "db.log" if name == "db_file" else f"{name}.log"
            handler["filename"] = str(base / "logs" / filename)
    logging_config_path = base / "logging.yaml"
    logging_config_path.write_text(
        yaml.safe_dump(config, allow_unicode=True), encoding="utf-8"
    )
    setup_metaweave_logging(str(logging_config_path))

    def load_snapshot(_path):
        logging.getLogger("backend.platform").info("%s", early_marker)
        return {}

    @contextmanager
    def fake_session(_settings):
        yield object()

    service.load_main_config_snapshot = load_snapshot
    service.load_platform_settings_from_env = lambda: object()
    service.platform_session = fake_session
    service.get_db_source = lambda _session, _source_id: SimpleNamespace(
        db_type="postgresql",
        host="localhost",
        port=5432,
        db_name="demo",
        username="reader",
        password_ciphertext="ciphertext",
        sslmode=None,
        selected_schemas=["public"],
        is_active=True,
    )
    service.decrypt_password = lambda _: "C3-unique-source-password-7391!"
    service.start_step = lambda *_a, **_kw: None
    service.finish_step = lambda *_a, **_kw: None
    db_step.resolve_db_output_dir = lambda *_: base / "output" / "db"
    db_step.psycopg.connect = lambda **_kwargs: FakeConnection()

    result = service.run_db_step("config.yaml", SOURCE_ID, False)
    assert result["success"] is True, result
    logging.shutdown()


def test_db_log_file_contains_this_run_and_actual_collection_line(tmp_path):
    marker = f"C3-early-platform-read-{tmp_path.name}"
    probe = (
        "from tests.unit.test_db_step import _run_db_logging_probe; "
        "import sys; _run_db_logging_probe(sys.argv[1], sys.argv[2])"
    )
    run = subprocess.run(
        [sys.executable, "-c", probe, str(tmp_path), marker],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        check=False,
    )
    assert run.returncode == 0, run.stdout + run.stderr

    db_log = tmp_path / "logs" / "db.log"
    contents = db_log.read_text(encoding="utf-8")
    profile_path = tmp_path / "output" / "db" / "database.json"
    assert marker in contents
    assert "数据库概览已写入:" in contents
    assert str(profile_path) in contents
    assert "[db]" in contents
    assert "C3-unique-source-password-7391!" not in contents
    assert marker not in (tmp_path / "logs" / "ddl_file.log").read_text(
        encoding="utf-8"
    )


def test_uuid_forms_share_identity():
    for raw in (
        str(SOURCE_ID).upper(),
        SOURCE_ID.hex.upper(),
        "{" + str(SOURCE_ID) + "}",
    ):
        assert normalize_source_id(raw) == SOURCE_ID
    assert normalize_source_id(str(JOB_ID).upper()) == JOB_ID
    assert resolve_db_output_dir(
        str(SOURCE_ID).upper(), str(JOB_ID).upper()
    ) == resolve_db_output_dir(SOURCE_ID, JOB_ID)
    assert resolve_db_output_dir(SOURCE_ID, JOB_ID).parts[-4:] == (
        "output",
        str(SOURCE_ID),
        str(JOB_ID),
        "db",
    )
    assert (
        resolve_source_workspace_dir(SOURCE_ID)
        == resolve_db_output_dir(SOURCE_ID, JOB_ID).parents[1]
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
    target = tmp_path / "store" / str(SOURCE_ID)
    target.mkdir(parents=True)
    (target / "keep.txt").write_text("keep", encoding="utf-8")
    (tmp_path / "output").symlink_to(target.parent, target_is_directory=True)
    with pytest.raises(ValueError, match="数据源工作区路径包含符号链接"):
        resolve_source_workspace_dir(SOURCE_ID)
    assert (target / "keep.txt").read_text(encoding="utf-8") == "keep"


def test_connection_kwargs_keep_values_separate():
    """doc 18.1 §4.1：参数独立传递 + sslmode 值域校验在建连前拒绝。

    旧版（doc 2/3 口径）用 `require host=other` 断言非法文本原样透传、
    由 libpq 拒绝；值域校验落地后改为公共模块在建连前拒绝。
    """
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
    assert result["source_id"] == str(SOURCE_ID)
    assert result["schemas"][0]["partition_child_count"] == 12
    assert result["schemas"][0]["object_count"] == 12
    assert result["totals"]["object_count"] == 12
    assert result["database"]["record_count"] is None
    assert result["database"]["size_bytes"] == 123456
    assert result["database"]["server_version"] == "18.6"
    assert result["database"]["server_version_full"] == version_string
    assert result["database"]["os_platform"] == "Linux x86_64"
    assert result["collected_at"].endswith("Z")
    assert re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z", result["collected_at"]
    )
    assert "password" not in json.dumps(result)

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
    profile = json.loads((target / "database.json").read_text(encoding="utf-8"))
    assert profile["database"]["server_version_full"] is None
    assert profile["database"]["os_platform"] is None
    warnings = [
        record.getMessage()
        for record in caplog.records
        if record.levelno >= logging.WARNING
    ]
    assert any(
        "RuntimeError" in message and str(JOB_ID) in message for message in warnings
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


@pytest.mark.parametrize("version", ["140000", "140012", "invalid"])
def test_version_failure_does_not_touch_existing_workspace(
    tmp_path, monkeypatch, version
):
    target = tmp_path / "db"
    target.mkdir()
    (target / "database.json").write_text("old", encoding="utf-8")
    (target / ".job_id").write_text("old-job", encoding="utf-8")
    monkeypatch.setattr(db_step, "resolve_db_output_dir", lambda *_: target)
    monkeypatch.setattr(db_step.psycopg, "connect", lambda **_: FakeConnection(version))
    result = db_step.execute_db(make_request(clean=True))
    assert result["success"] is False
    assert "版本" in result["message"]
    assert (target / "database.json").read_text() == "old"
    assert (target / ".job_id").exists()


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
    profile = json.loads((target / "database.json").read_text(encoding="utf-8"))
    assert profile["source_id"] == str(SOURCE_ID)
    assert profile["database"]["server_version"] == display_version
    assert profile["schema_version"] == 1


def test_success_replaces_json_and_leaves_stale_marker_untouched(tmp_path, monkeypatch):
    """doc 9 §4：新路径不创建调试标记，也不再删除旧标记。"""
    target = tmp_path / "db"
    target.mkdir()
    (target / "database.json").write_text("old", encoding="utf-8")
    (target / ".job_id").write_text("old-job", encoding="utf-8")
    monkeypatch.setattr(db_step, "resolve_db_output_dir", lambda *_: target)
    monkeypatch.setattr(db_step.psycopg, "connect", lambda **_: FakeConnection())
    result = db_step.execute_db(make_request())
    assert result["success"] is True
    assert json.loads((target / "database.json").read_text())["source_id"] == str(
        SOURCE_ID
    )
    assert (target / ".job_id").read_text(encoding="utf-8") == "old-job"


def test_clean_only_removes_top_level_files(tmp_path, monkeypatch):
    from metaweave.utils import file_utils

    target = tmp_path / "db"
    child_dir = target / "nested"
    child_dir.mkdir(parents=True)
    (target / "obsolete.txt").write_text("old")
    (target / ".job_id").write_text("old-job")
    (child_dir / "keep.txt").write_text("keep")
    monkeypatch.setattr(file_utils, "get_project_root", lambda: tmp_path)
    monkeypatch.setattr(db_step, "resolve_db_output_dir", lambda *_: target)
    monkeypatch.setattr(db_step.psycopg, "connect", lambda **_: FakeConnection())
    result = db_step.execute_db(make_request(clean=True))
    assert result["success"] is True
    assert not (target / "obsolete.txt").exists()
    assert not (target / ".job_id").exists()
    assert (child_dir / "keep.txt").exists()


@pytest.mark.parametrize(
    ("args", "expected_error"),
    [
        (["--step", "db"], "--step db 必须指定 --source-id"),
        (["--step", "db", "--source-id", "bad"], "source_id 不是合法 UUID"),
        (
            ["--step", "db", "--source-id", str(SOURCE_ID), "--schemas", "public"],
            "--step db 不接受 --schemas/-s",
        ),
        (
            # --step ddl 带 --source-id 已是合法受管入口（doc 16 §4），换 json 验证拒绝
            ["--step", "json", "--source-id", str(SOURCE_ID)],
            "--source-id 仅适用于 --step db、受管 --step ddl 或 --step cql",
        ),
        (
            [
                "--step",
                "db",
                "--source-id",
                str(SOURCE_ID),
                "--source-id",
                str(SOURCE_ID),
            ],
            "--source-id 仅允许传入一次",
        ),
        (
            ["--step", "db", "--source-id", str(SOURCE_ID), "--job-id", "bad"],
            "--job-id 不是合法 UUID",
        ),
        (
            ["--step", "ddl", "--job-id", str(JOB_ID)],
            "--job-id 仅在受管模式（--source-id）下可用",
        ),
        (
            [
                "--step",
                "db",
                "--source-id",
                str(SOURCE_ID),
                "--job-id",
                str(JOB_ID),
                "--job-id",
                str(JOB_ID),
            ],
            "--job-id 仅允许传入一次",
        ),
    ],
)
def test_cli_rejects_invalid_db_options_before_loading_platform(
    tmp_path, monkeypatch, args, expected_error
):
    from backend.data_sources import db_step_service

    cfg = tmp_path / "config.yaml"
    cfg.write_text("database: {}\n", encoding="utf-8")
    run_db_step = Mock(
        return_value={"success": True, "execution_time": 0, "message": "不应执行"}
    )
    load_platform_settings = Mock(side_effect=AssertionError("不应读取平台库配置"))
    monkeypatch.setattr(db_step_service, "run_db_step", run_db_step)
    monkeypatch.setattr(
        db_step_service, "load_platform_settings_from_env", load_platform_settings
    )

    result = CliRunner().invoke(metadata_command, ["-c", str(cfg), *args])
    assert result.exit_code == 2, result.output
    assert expected_error in result.output
    run_db_step.assert_not_called()
    load_platform_settings.assert_not_called()


def test_cli_reports_disabled_source_as_usage_error(tmp_path, monkeypatch):
    """禁用数据源：CLI 以 exit 2 结束且消息含"已禁用"（doc 11 §9 第 5 条）。"""
    cfg = tmp_path / "config.yaml"
    cfg.write_text("database: {}\n", encoding="utf-8")
    from backend.data_sources import db_step_service

    def disabled(*_args, **_kwargs):
        raise SourceDisabledError("该数据源已禁用，不能执行作业")

    monkeypatch.setattr(db_step_service, "run_db_step", disabled)
    result = CliRunner().invoke(
        metadata_command,
        ["-c", str(cfg), "--step", "db", "--source-id", str(SOURCE_ID)],
    )
    assert result.exit_code == 2, result.output
    assert "已禁用" in result.output


def test_cli_db_dispatch_and_result_mapping(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("database: {}\n", encoding="utf-8")
    from backend.data_sources import db_step_service

    seen = []
    monkeypatch.setattr(
        db_step_service,
        "run_db_step",
        lambda path, source_id, clean, job_id=None: (
            seen.append((source_id, clean, job_id))
            or {
                "success": True,
                "execution_time": 0,
                "message": "完成",
                "job_id": str(JOB_ID),
            }
        ),
    )
    result = CliRunner().invoke(
        metadata_command,
        [
            "-c",
            str(cfg),
            "--step",
            "db",
            "--source-id",
            str(SOURCE_ID).upper(),
            "--clean",
        ],
    )
    assert result.exit_code == 0, result.output
    assert seen == [(SOURCE_ID, True, None)]
    assert str(JOB_ID) in result.output

    reused = CliRunner().invoke(
        metadata_command,
        [
            "-c",
            str(cfg),
            "--step",
            "db",
            "--source-id",
            str(SOURCE_ID),
            "--job-id",
            str(JOB_ID).upper(),
        ],
    )
    assert reused.exit_code == 0, reused.output
    assert seen[-1] == (SOURCE_ID, False, JOB_ID)
    assert str(JOB_ID) in reused.output

    monkeypatch.setattr(
        db_step_service,
        "run_db_step",
        lambda *_a, **_kw: {
            "success": False,
            "execution_time": 0,
            "message": "统计失败",
            "job_id": str(JOB_ID),
        },
    )
    failed = CliRunner().invoke(
        metadata_command,
        ["-c", str(cfg), "--step", "db", "--source-id", str(SOURCE_ID)],
    )
    assert failed.exit_code == 1
    assert "统计失败" in failed.output
    assert str(JOB_ID) in failed.output

    def bad_config(*_a, **_kw):
        raise db_step_service.DbStepConfigError("未找到指定 source_id 的数据源")

    monkeypatch.setattr(db_step_service, "run_db_step", bad_config)
    invalid = CliRunner().invoke(
        metadata_command,
        ["-c", str(cfg), "--step", "db", "--source-id", str(SOURCE_ID)],
    )
    assert invalid.exit_code == 2
    assert "未找到指定 source_id" in invalid.output


def test_service_loads_one_snapshot_and_passes_registered_source(monkeypatch):
    from backend.data_sources import db_step_service as service

    seen = []
    started = []
    finished = []
    monkeypatch.setattr(
        service,
        "load_main_config_snapshot",
        lambda path: seen.append(path) or {"database": {}},
    )
    from backend.persistence.platform_db import PlatformSettings

    platform_settings = PlatformSettings(
        host="platform.example.test",
        port=5433,
        name="metaweave",
        user="platform_admin",
        password="platform-secret",
    )
    monkeypatch.setattr(
        service, "load_platform_settings_from_env", lambda: platform_settings
    )

    received_settings = []

    @contextmanager
    def fake_session(settings):
        # 平台库目标也必须被锁定：会话收到的就是这份明确地址的设置
        received_settings.append(settings)
        yield object()

    monkeypatch.setattr(service, "platform_session", fake_session)
    monkeypatch.setattr(
        service,
        "get_db_source",
        lambda _session, _id: SimpleNamespace(
            db_type="postgresql",
            host="localhost",
            port=5432,
            db_name="demo",
            username="reader",
            password_ciphertext="cipher",
            sslmode=None,
            selected_schemas=["public"],
            is_active=True,
        ),
    )
    monkeypatch.setattr(service, "decrypt_password", lambda _: "secret")
    monkeypatch.setattr(
        service,
        "start_step",
        lambda _session, job_id, source_id, step_name: started.append(
            (job_id, source_id, step_name)
        ),
    )
    monkeypatch.setattr(
        service,
        "finish_step",
        lambda _session, job_id, step_name, status: finished.append(
            (job_id, step_name, status)
        ),
    )

    def fake_execute(request):
        captured.append(request)
        assert request.source_id == SOURCE_ID
        assert request.password == "secret"
        assert request.main_config == {"database": {}}
        assert request.clean is True
        return {"success": True, "execution_time": 0, "message": "ok"}

    captured: list = []
    monkeypatch.setattr(service, "execute_db", fake_execute)
    result = service.run_db_step("cfg.yaml", SOURCE_ID, True)
    assert result["success"] is True
    assert seen == ["cfg.yaml"]
    assert len(started) == 1
    job_uuid, started_source, step_name = started[0]
    assert started_source == SOURCE_ID and step_name == "db"
    assert captured[0].job_id == job_uuid
    assert result["job_id"] == str(job_uuid)
    assert finished == [(job_uuid, "db", "success")]


def test_service_reuses_registered_workspace(monkeypatch):
    from backend.data_sources import db_step_service as service

    checked = []
    monkeypatch.setattr(service, "load_main_config_snapshot", lambda _: {})
    from backend.persistence.platform_db import PlatformSettings

    platform_settings = PlatformSettings(
        host="platform.example.test",
        port=5433,
        name="metaweave",
        user="platform_admin",
        password="platform-secret",
    )
    monkeypatch.setattr(
        service, "load_platform_settings_from_env", lambda: platform_settings
    )

    received_settings = []

    @contextmanager
    def fake_session(settings):
        # 平台库目标也必须被锁定：会话收到的就是这份明确地址的设置
        received_settings.append(settings)
        yield object()

    monkeypatch.setattr(service, "platform_session", fake_session)
    monkeypatch.setattr(
        service,
        "get_db_source",
        lambda _session, _id: SimpleNamespace(
            db_type="postgresql",
            host="localhost",
            port=5432,
            db_name="demo",
            username="reader",
            password_ciphertext="cipher",
            sslmode=None,
            selected_schemas=["public"],
            is_active=True,
        ),
    )
    monkeypatch.setattr(service, "decrypt_password", lambda _: "secret")
    monkeypatch.setattr(
        service,
        "assert_workspace_owner",
        lambda _session, job_id, source_id: checked.append((job_id, source_id)),
    )
    monkeypatch.setattr(service, "start_step", lambda *_a, **_kw: None)
    monkeypatch.setattr(service, "finish_step", lambda *_a, **_kw: None)
    monkeypatch.setattr(
        service,
        "execute_db",
        lambda request: {
            "success": True,
            "execution_time": 0,
            "message": "ok",
        },
    )

    result = service.run_db_step("cfg.yaml", SOURCE_ID, False, job_id=JOB_ID)

    assert checked == [(JOB_ID, SOURCE_ID)]
    assert result["job_id"] == str(JOB_ID)


@pytest.mark.parametrize(
    "error",
    [
        WorkspaceMissingError("指定的 job_id 不存在"),
        WorkspaceSourceMismatchError("指定的 job_id 不属于该数据源"),
    ],
)
def test_service_rejects_bad_job_id_before_any_side_effect(monkeypatch, error):
    from backend.data_sources import db_step_service as service

    calls = []
    monkeypatch.setattr(service, "load_main_config_snapshot", lambda _: {})
    from backend.persistence.platform_db import PlatformSettings

    platform_settings = PlatformSettings(
        host="platform.example.test",
        port=5433,
        name="metaweave",
        user="platform_admin",
        password="platform-secret",
    )
    monkeypatch.setattr(
        service, "load_platform_settings_from_env", lambda: platform_settings
    )

    received_settings = []

    @contextmanager
    def fake_session(settings):
        # 平台库目标也必须被锁定：会话收到的就是这份明确地址的设置
        received_settings.append(settings)
        yield object()

    monkeypatch.setattr(service, "platform_session", fake_session)
    monkeypatch.setattr(
        service,
        "get_db_source",
        lambda _session, _id: SimpleNamespace(
            db_type="postgresql",
            host="localhost",
            port=5432,
            db_name="demo",
            username="reader",
            password_ciphertext="cipher",
            sslmode=None,
            selected_schemas=["public"],
            is_active=True,
        ),
    )
    monkeypatch.setattr(service, "decrypt_password", lambda _: "secret")

    def fail(_session, _job_id, _source_id):
        raise error

    monkeypatch.setattr(service, "assert_workspace_owner", fail)
    monkeypatch.setattr(service, "start_step", lambda *_a, **_kw: calls.append("start"))
    monkeypatch.setattr(service, "execute_db", lambda _request: calls.append("execute"))

    with pytest.raises(DbStepConfigError) as exc_info:
        service.run_db_step("cfg.yaml", SOURCE_ID, False, job_id=JOB_ID)

    assert "job_id" in str(exc_info.value)
    assert calls == []


def test_service_maps_missing_registration_to_source_not_found(monkeypatch):
    """删除先提交：start_step 锁下查不到登记 → 报数据源不存在、不采集（doc 16 §6）。"""
    from backend.data_sources import db_step_service as service

    calls = []
    monkeypatch.setattr(service, "load_main_config_snapshot", lambda _: {})
    from backend.persistence.platform_db import PlatformSettings

    platform_settings = PlatformSettings(
        host="platform.example.test",
        port=5433,
        name="metaweave",
        user="platform_admin",
        password="platform-secret",
    )
    monkeypatch.setattr(
        service, "load_platform_settings_from_env", lambda: platform_settings
    )

    received_settings = []

    @contextmanager
    def fake_session(settings):
        # 平台库目标也必须被锁定：会话收到的就是这份明确地址的设置
        received_settings.append(settings)
        yield object()

    monkeypatch.setattr(service, "platform_session", fake_session)
    monkeypatch.setattr(
        service,
        "get_db_source",
        lambda _session, _id: SimpleNamespace(
            db_type="postgresql",
            host="localhost",
            port=5432,
            db_name="demo",
            username="reader",
            password_ciphertext="cipher",
            sslmode=None,
            selected_schemas=["public"],
            is_active=True,
        ),
    )
    monkeypatch.setattr(service, "decrypt_password", lambda _: "secret")

    def deleted(_session, _job_id, _source_id, _step_name):
        raise SourceRegistrationMissingError("数据源登记已不存在")

    monkeypatch.setattr(service, "start_step", deleted)
    monkeypatch.setattr(service, "execute_db", lambda _request: calls.append("execute"))
    monkeypatch.setattr(
        service, "finish_step", lambda *_a, **_kw: calls.append("finish")
    )

    with pytest.raises(DbStepConfigError, match="未找到指定 source_id 的数据源"):
        service.run_db_step("cfg.yaml", SOURCE_ID, False)

    assert calls == []


def test_service_rejects_disabled_source_before_any_side_effect(monkeypatch):
    """禁用数据源在 start_step 之前被拦：不登记作业、不解密凭据、不采集（doc 11 §4）。"""
    from backend.data_sources import db_step_service as service

    calls = []
    monkeypatch.setattr(service, "load_main_config_snapshot", lambda _: {})
    from backend.persistence.platform_db import PlatformSettings

    platform_settings = PlatformSettings(
        host="platform.example.test",
        port=5433,
        name="metaweave",
        user="platform_admin",
        password="platform-secret",
    )
    monkeypatch.setattr(
        service, "load_platform_settings_from_env", lambda: platform_settings
    )

    received_settings = []

    @contextmanager
    def fake_session(settings):
        # 平台库目标也必须被锁定：会话收到的就是这份明确地址的设置
        received_settings.append(settings)
        yield object()

    monkeypatch.setattr(service, "platform_session", fake_session)
    monkeypatch.setattr(
        service,
        "get_db_source",
        lambda _session, _id: SimpleNamespace(
            db_type="postgresql",
            host="localhost",
            port=5432,
            db_name="demo",
            username="reader",
            password_ciphertext="cipher",
            sslmode=None,
            selected_schemas=["public"],
            is_active=False,
        ),
    )
    monkeypatch.setattr(
        service, "decrypt_password", lambda _: pytest.fail("禁用数据源不应解密凭据")
    )
    monkeypatch.setattr(
        service,
        "assert_workspace_owner",
        lambda *_a, **_kw: pytest.fail("不应检查工作区"),
    )
    monkeypatch.setattr(service, "start_step", lambda *_a, **_kw: calls.append("start"))
    monkeypatch.setattr(service, "execute_db", lambda _r: calls.append("execute"))

    with pytest.raises(SourceDisabledError) as exc_info:
        service.run_db_step("cfg.yaml", SOURCE_ID, False, job_id=JOB_ID)

    assert "已禁用" in str(exc_info.value)
    assert calls == []
    # CLI 依赖这个继承关系把该错误映射为 exit 2
    assert issubclass(SourceDisabledError, DbStepConfigError)


def test_service_step_already_running_is_task_failure(monkeypatch):
    from backend.data_sources import db_step_service as service

    calls = []
    monkeypatch.setattr(service, "load_main_config_snapshot", lambda _: {})
    from backend.persistence.platform_db import PlatformSettings

    platform_settings = PlatformSettings(
        host="platform.example.test",
        port=5433,
        name="metaweave",
        user="platform_admin",
        password="platform-secret",
    )
    monkeypatch.setattr(
        service, "load_platform_settings_from_env", lambda: platform_settings
    )

    received_settings = []

    @contextmanager
    def fake_session(settings):
        # 平台库目标也必须被锁定：会话收到的就是这份明确地址的设置
        received_settings.append(settings)
        yield object()

    monkeypatch.setattr(service, "platform_session", fake_session)
    monkeypatch.setattr(
        service,
        "get_db_source",
        lambda _session, _id: SimpleNamespace(
            db_type="postgresql",
            host="localhost",
            port=5432,
            db_name="demo",
            username="reader",
            password_ciphertext="cipher",
            sslmode=None,
            selected_schemas=["public"],
            is_active=True,
        ),
    )
    monkeypatch.setattr(service, "decrypt_password", lambda _: "secret")

    monkeypatch.setattr(service, "assert_workspace_owner", lambda *_a, **_kw: None)

    def busy(*_a, **_kw):
        raise StepAlreadyRunningError("当前步骤可能已有作业正在执行")

    monkeypatch.setattr(service, "start_step", busy)
    monkeypatch.setattr(service, "execute_db", lambda _r: calls.append("execute"))
    monkeypatch.setattr(service, "finish_step", lambda *_a, **_kw: None)

    result = service.run_db_step("cfg.yaml", SOURCE_ID, False, job_id=JOB_ID)

    assert result["success"] is False
    assert "当前步骤可能已有作业正在执行" in result["message"]
    assert result["job_id"] == str(JOB_ID)
    assert calls == []


def test_service_state_registration_failure_skips_collection(monkeypatch):
    from backend.data_sources import db_step_service as service

    calls = []
    monkeypatch.setattr(service, "load_main_config_snapshot", lambda _: {})
    from backend.persistence.platform_db import PlatformSettings

    platform_settings = PlatformSettings(
        host="platform.example.test",
        port=5433,
        name="metaweave",
        user="platform_admin",
        password="platform-secret",
    )
    monkeypatch.setattr(
        service, "load_platform_settings_from_env", lambda: platform_settings
    )

    received_settings = []

    @contextmanager
    def fake_session(settings):
        # 平台库目标也必须被锁定：会话收到的就是这份明确地址的设置
        received_settings.append(settings)
        yield object()

    monkeypatch.setattr(service, "platform_session", fake_session)
    monkeypatch.setattr(
        service,
        "get_db_source",
        lambda _session, _id: SimpleNamespace(
            db_type="postgresql",
            host="localhost",
            port=5432,
            db_name="demo",
            username="reader",
            password_ciphertext="cipher",
            sslmode=None,
            selected_schemas=["public"],
            is_active=True,
        ),
    )
    monkeypatch.setattr(service, "decrypt_password", lambda _: "secret")

    def broken(*_a, **_kw):
        raise SQLAlchemyError("platform-secret-detail")

    monkeypatch.setattr(service, "start_step", broken)
    monkeypatch.setattr(service, "execute_db", lambda _r: calls.append("execute"))

    result = service.run_db_step("cfg.yaml", SOURCE_ID, False)

    assert result["success"] is False
    assert "作业状态登记失败" in result["message"]
    assert "platform-secret-detail" not in result["message"]
    assert UUID(result["job_id"])  # 规范化的小写 UUID 字符串
    assert calls == []


def test_service_finish_step_failure_hides_artifact_path(monkeypatch, caplog):
    from backend.data_sources import db_step_service as service

    monkeypatch.setattr(service, "load_main_config_snapshot", lambda _: {})
    from backend.persistence.platform_db import PlatformSettings

    platform_settings = PlatformSettings(
        host="platform.example.test",
        port=5433,
        name="metaweave",
        user="platform_admin",
        password="platform-secret",
    )
    monkeypatch.setattr(
        service, "load_platform_settings_from_env", lambda: platform_settings
    )

    received_settings = []

    @contextmanager
    def fake_session(settings):
        # 平台库目标也必须被锁定：会话收到的就是这份明确地址的设置
        received_settings.append(settings)
        yield object()

    monkeypatch.setattr(service, "platform_session", fake_session)
    monkeypatch.setattr(
        service,
        "get_db_source",
        lambda _session, _id: SimpleNamespace(
            db_type="postgresql",
            host="localhost",
            port=5432,
            db_name="demo",
            username="reader",
            password_ciphertext="cipher",
            sslmode=None,
            selected_schemas=["public"],
            is_active=True,
        ),
    )
    monkeypatch.setattr(service, "decrypt_password", lambda _: "secret")
    monkeypatch.setattr(service, "start_step", lambda *_a, **_kw: None)

    monkeypatch.setattr(service, "assert_workspace_owner", lambda *_a, **_kw: None)

    def broken(*_a, **_kw):
        raise SQLAlchemyError("platform-secret-detail")

    monkeypatch.setattr(service, "finish_step", broken)
    monkeypatch.setattr(
        service,
        "execute_db",
        lambda _r: {"success": True, "execution_time": 0, "message": "ok"},
    )

    result = service.run_db_step("cfg.yaml", SOURCE_ID, False, job_id=JOB_ID)

    assert result["success"] is False
    assert "采集产物可能已生成，但作业状态更新失败" in result["message"]
    assert str(JOB_ID) in result["message"]
    # 产物路径只进日志，API 回显的消息不含路径（受控路径不出错误消息）
    artifact = f"output/{SOURCE_ID}/{JOB_ID}/db/database.json"
    assert artifact not in result["message"]
    assert "platform-secret-detail" not in result["message"]
    assert artifact in caplog.text


def test_service_collection_failure_marks_step_failed(monkeypatch):
    from backend.data_sources import db_step_service as service

    finished = []
    monkeypatch.setattr(service, "load_main_config_snapshot", lambda _: {})
    from backend.persistence.platform_db import PlatformSettings

    platform_settings = PlatformSettings(
        host="platform.example.test",
        port=5433,
        name="metaweave",
        user="platform_admin",
        password="platform-secret",
    )
    monkeypatch.setattr(
        service, "load_platform_settings_from_env", lambda: platform_settings
    )

    received_settings = []

    @contextmanager
    def fake_session(settings):
        # 平台库目标也必须被锁定：会话收到的就是这份明确地址的设置
        received_settings.append(settings)
        yield object()

    monkeypatch.setattr(service, "platform_session", fake_session)
    monkeypatch.setattr(
        service,
        "get_db_source",
        lambda _session, _id: SimpleNamespace(
            db_type="postgresql",
            host="localhost",
            port=5432,
            db_name="demo",
            username="reader",
            password_ciphertext="cipher",
            sslmode=None,
            selected_schemas=["public"],
            is_active=True,
        ),
    )
    monkeypatch.setattr(service, "decrypt_password", lambda _: "secret")
    monkeypatch.setattr(service, "start_step", lambda *_a, **_kw: None)
    monkeypatch.setattr(
        service,
        "finish_step",
        lambda _session, job_id, step_name, status: finished.append(
            (job_id, step_name, status)
        ),
    )
    monkeypatch.setattr(
        service,
        "execute_db",
        lambda _r: {
            "success": False,
            "execution_time": 0,
            "message": "源库连接失败",
        },
    )

    result = service.run_db_step("cfg.yaml", SOURCE_ID, False)

    assert result["success"] is False
    assert result["job_id"] == str(finished[0][0])
    assert finished[0][1:] == ("db", "failed")


def test_clean_does_not_touch_sibling_job_directory(tmp_path, monkeypatch):
    """doc 9 §4：--clean 只清本次 job_id 的 db/，不越界到其他工作区。"""
    from metaweave.utils import file_utils

    monkeypatch.setattr(file_utils, "get_project_root", lambda: tmp_path)
    other_job = UUID("11111111-2222-3333-4444-555555555555")
    own = tmp_path / "output" / str(SOURCE_ID) / str(JOB_ID) / "db"
    other = tmp_path / "output" / str(SOURCE_ID) / str(other_job) / "db"
    own.mkdir(parents=True)
    other.mkdir(parents=True)
    (own / "obsolete.txt").write_text("old", encoding="utf-8")
    (other / "keep.txt").write_text("keep", encoding="utf-8")

    monkeypatch.setattr(db_step.psycopg, "connect", lambda **_: FakeConnection())
    result = db_step.execute_db(make_request(job_id=JOB_ID, clean=True))

    assert result["success"] is True
    assert not (own / "obsolete.txt").exists()
    assert (own / "database.json").exists()
    assert (other / "keep.txt").read_text(encoding="utf-8") == "keep"
    assert not (other / "database.json").exists()


def test_execute_db_logs_carry_job_and_source(tmp_path, monkeypatch, caplog):
    """doc 9 §5：步骤日志带 job_id、source_id，便于按作业检索。"""
    target = tmp_path / "db"
    monkeypatch.setattr(db_step, "resolve_db_output_dir", lambda *_: target)
    monkeypatch.setattr(db_step.psycopg, "connect", lambda **_: FakeConnection())
    caplog.set_level(logging.INFO, logger="metaweave.db_step")

    result = db_step.execute_db(make_request())

    assert result["success"] is True
    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert str(JOB_ID) in messages
    assert str(SOURCE_ID) in messages


def test_service_platform_failure_is_runtime_failure_without_secret(monkeypatch):
    from backend.data_sources import db_step_service as service

    monkeypatch.setattr(service, "load_main_config_snapshot", lambda _: {})
    monkeypatch.setattr(service, "load_platform_settings_from_env", lambda: object())

    @contextmanager
    def broken_session(_settings):
        raise RuntimeError("password=secret")
        yield

    monkeypatch.setattr(service, "platform_session", broken_session)
    result = service.run_db_step("cfg.yaml", SOURCE_ID, False)
    assert result["success"] is False
    assert "平台库" in result["message"]
    assert "secret" not in result["message"]


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
    monkeypatch.setattr(
        register,
        "insert_db_source",
        lambda _session, fields: seen.setdefault("fields", fields),
    )
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


def test_managed_db_connects_registered_source_not_config_or_platform(
    monkeypatch, tmp_path
):
    """doc 18.1 §5.1 双库边界全链路：三个不同目标，受管 `db` 只连登记业务库。

    平台库、登记业务库与旧 `DB_*`（主配置）各指向不同地址；execute_db 真
    跑（psycopg.connect 替身），断言最终连接参数取登记行，全程不含主配置
    的第三个目标。
    """
    from backend.data_sources import db_step_service as service
    from metaweave.core.metadata import db_step

    connects = []
    monkeypatch.setattr("metaweave.utils.file_utils.get_project_root", lambda: tmp_path)
    monkeypatch.setattr(
        service,
        "load_main_config_snapshot",
        lambda _path: {  # 旧 DB_* 展开后的第三个目标
            "database": {
                "host": "legacy-config.example.test",
                "port": 5999,
                "database": "legacy_db",
                "user": "legacy_user",
                "password": "legacy-pass",
            }
        },
    )
    from backend.persistence.platform_db import PlatformSettings

    platform_settings = PlatformSettings(
        host="platform.example.test",
        port=5433,
        name="metaweave",
        user="platform_admin",
        password="platform-secret",
    )
    monkeypatch.setattr(
        service, "load_platform_settings_from_env", lambda: platform_settings
    )

    received_settings = []

    @contextmanager
    def fake_session(settings):
        # 平台库目标也必须被锁定：会话收到的就是这份明确地址的设置
        received_settings.append(settings)
        yield object()

    monkeypatch.setattr(service, "platform_session", fake_session)
    monkeypatch.setattr(
        service,
        "get_db_source",
        lambda _session, _id: SimpleNamespace(
            db_type="postgresql",
            host="registered-source.example.test",
            port=5501,
            db_name="orders",
            username="owner",
            password_ciphertext="cipher",
            sslmode=None,
            selected_schemas=["public"],
            is_active=True,
        ),
    )
    monkeypatch.setattr(service, "decrypt_password", lambda _: "secret")
    monkeypatch.setattr(service, "start_step", lambda *_a: None)
    monkeypatch.setattr(service, "finish_step", lambda *_a: None)

    def fake_connect(**kwargs):
        connects.append(kwargs)
        return FakeConnection()

    monkeypatch.setattr(db_step.psycopg, "connect", fake_connect)

    result = service.run_db_step("cfg.yaml", SOURCE_ID, True)

    assert result["success"] is True, result
    # 平台库会话用的正是这份明确地址的设置（查登记与 job_info 各进入一次）
    assert received_settings
    assert all(s is platform_settings for s in received_settings)
    assert platform_settings.host == "platform.example.test"
    assert len(connects) == 1  # 只有登记业务库一次连接，无平台库/主配置库连接
    assert connects[0]["host"] == "registered-source.example.test"
    assert connects[0]["port"] == 5501
    assert connects[0]["dbname"] == "orders"
    assert connects[0]["user"] == "owner"
    assert connects[0]["password"] == "secret"
    assert "legacy-config.example.test" not in str(connects)
    assert "legacy_db" not in str(connects)
