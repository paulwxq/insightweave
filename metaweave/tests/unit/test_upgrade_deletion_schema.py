"""结构更新脚本的目标库隔离与事务边界，不连接真实数据库。"""

from contextlib import contextmanager
import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "upgrade_deletion_schema.py"
spec = importlib.util.spec_from_file_location("upgrade_deletion_schema", SCRIPT)
upgrade = importlib.util.module_from_spec(spec)
spec.loader.exec_module(upgrade)


class FakeConnection:
    def __init__(self, database=upgrade.TARGET_DATABASE, ddl_error=None):
        self.database = database
        self.ddl_error = ddl_error
        self.commands = []
        self.committed = False
        self.rolled_back = False

    @contextmanager
    def transaction(self):
        try:
            yield
        except Exception:
            self.rolled_back = True
            raise
        else:
            self.committed = True

    def execute(self, sql, *args, **kwargs):
        self.commands.append(sql)
        if sql == "SELECT current_database()":
            return type("Result", (), {"fetchone": lambda _: (self.database,)})()
        if "ALTER TABLE" in sql and self.ddl_error:
            raise self.ddl_error


def test_preview_does_not_connect_or_load_credentials(monkeypatch, capsys):
    def forbidden(*args, **kwargs):
        raise AssertionError("预览不应连接数据库或读取凭据")

    monkeypatch.setattr(upgrade.psycopg, "connect", forbidden)
    monkeypatch.setattr(upgrade, "load_project_env", forbidden)
    assert upgrade.main([]) == 0
    text = capsys.readouterr().out
    assert "metaweave_rehearsal_final" in text
    assert "ALTER TABLE" in text


def test_wrong_database_rejected_before_any_ddl():
    conn = FakeConnection(database="metaweave")
    with pytest.raises(ValueError, match="目标必须"):
        upgrade.apply_update(conn)
    assert conn.rolled_back
    assert conn.commands == ["SELECT current_database()"]


def test_ddl_failure_rolls_back_without_success(monkeypatch):
    conn = FakeConnection(ddl_error=ValueError("existing rows violate constraint"))
    with pytest.raises(ValueError, match="violate constraint"):
        upgrade.apply_update(conn)
    assert conn.rolled_back
    assert not conn.committed


def test_failed_postcheck_remains_in_same_transaction(monkeypatch):
    conn = FakeConnection()
    monkeypatch.setattr(upgrade, "schema_problems", lambda _: ["missing task column"])
    with pytest.raises(ValueError, match="missing task column"):
        upgrade.apply_update(conn)
    assert conn.rolled_back
    assert not conn.committed


def test_success_commits_only_after_postcheck(monkeypatch):
    conn = FakeConnection()

    def checked(connection):
        assert not connection.committed
        assert any("ALTER TABLE" in sql for sql in connection.commands)
        return []

    monkeypatch.setattr(upgrade, "schema_problems", checked)
    upgrade.apply_update(conn)
    assert conn.committed
    assert not conn.rolled_back
