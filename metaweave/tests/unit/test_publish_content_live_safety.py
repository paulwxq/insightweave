"""发布实库测试的启用与初始化失败清理；本文件不连接实库。"""

from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from backend.persistence.platform_db import PlatformSettings
from tests.unit import test_publish_content_live as live


@pytest.mark.parametrize(
    "enabled, database", [(None, None), ("1", None), (None, "test_db")]
)
def test_live_test_requires_both_explicit_options(monkeypatch, enabled, database):
    monkeypatch.delenv("METAWEAVE_RUN_LIVE_TESTS", raising=False)
    monkeypatch.delenv("METAWEAVE_TEST_DB_NAME", raising=False)
    if enabled:
        monkeypatch.setenv("METAWEAVE_RUN_LIVE_TESTS", enabled)
    if database:
        monkeypatch.setenv("METAWEAVE_TEST_DB_NAME", database)

    def forbidden():
        raise AssertionError("未显式启用不得加载实库环境")

    monkeypatch.setattr(live, "load_project_env", forbidden)
    assert live._settings() is None


def test_live_database_is_separate_from_application(monkeypatch):
    settings = PlatformSettings(
        host="h", port=5432, name="application", user="u", password="p"
    )
    monkeypatch.setenv("METAWEAVE_RUN_LIVE_TESTS", "1")
    monkeypatch.setenv("METAWEAVE_TEST_DB_NAME", "isolated_test")
    monkeypatch.setattr(live, "load_project_env", lambda: None)
    monkeypatch.setattr(live, "load_platform_settings_from_env", lambda: settings)
    assert live._settings().name == "isolated_test"
    assert settings.name == "application"
    monkeypatch.setenv("METAWEAVE_TEST_DB_NAME", "application")
    with pytest.raises(ValueError, match="必须与应用"):
        live._settings()


@pytest.mark.parametrize("failure_at", [1, 2, 3])
def test_cleanup_is_registered_before_workspace_setup(
    monkeypatch, tmp_path, failure_at
):
    callbacks, cleaned, workspaces = [], [], []

    @contextmanager
    def scope(_settings):
        yield SimpleNamespace(
            execute=lambda *args, **kwargs: SimpleNamespace(
                scalar_one=lambda: "db_src_1"
            ),
            commit=lambda: None,
        )

    def create(*args):
        assert len(callbacks) == 1
        workspaces.append(args)
        if len(workspaces) == failure_at:
            raise RuntimeError("模拟工作区创建失败")
        return {"workspace_id": f"ws_{len(workspaces)}"}

    monkeypatch.setattr(live, "platform_session", scope)
    monkeypatch.setattr(live, "create_workspace", create)
    monkeypatch.setattr(
        live,
        "_cleanup_live_source",
        lambda settings, source_id: cleaned.append(source_id),
    )
    request = SimpleNamespace(addfinalizer=callbacks.append)
    try:
        with pytest.raises((RuntimeError, AssertionError)):
            live.live_source_two_workspaces.__wrapped__(request, tmp_path, monkeypatch)
            raise AssertionError("模拟测试断言失败")
    finally:
        for cleanup in callbacks:
            cleanup()
    assert cleaned == ["db_src_1"]


def test_file_cleanup_failure_is_visible_and_database_cleanup_still_attempted(
    monkeypatch,
):
    cleaned = []

    def fail(_path):
        raise PermissionError("模拟目录清理失败")

    monkeypatch.setattr(live.shutil, "rmtree", fail)
    monkeypatch.setattr(
        live,
        "_cleanup_live_source_rows",
        lambda settings, source_id: cleaned.append(source_id),
    )
    with pytest.raises(PermissionError):
        live._cleanup_live_source(None, "db_src_1")
    assert cleaned == ["db_src_1"]
