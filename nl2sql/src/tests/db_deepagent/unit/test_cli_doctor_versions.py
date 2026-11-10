"""doctor 锁定版本与实际安装一致性单元测试（doc 1 §4.2）

升级 sqlglot 30.22.0 → 30.30.0 时，`_LOCKED_VERSIONS` 常量必须与最终安装
版本同步；不一致时 doctor 会把 `30.30.0（要求 30.22.0）` 记为失败。本测试
直接断言常量与 importlib 实测版本一致，防止只改 pyproject 漏改常量。
"""

from __future__ import annotations

import importlib.metadata

import pytest

from src.modules.db_deepagent.cli import _LOCKED_VERSIONS, _collect_dependency_report

pytestmark = pytest.mark.unit


def test_locked_sqlglot_matches_installed():
    installed = importlib.metadata.version("sqlglot")
    assert (
        _LOCKED_VERSIONS["sqlglot"] == installed
    ), "cli._LOCKED_VERSIONS 与实际安装的 sqlglot 不一致，doctor 将误报失败"


def test_dependency_report_all_ok():
    """_collect_dependency_report 实际输出：锁定项必须全部通过。"""
    report = _collect_dependency_report()
    failed = [(name, detail) for name, ok, detail in report if not ok]
    assert failed == [], f"依赖核对失败项: {failed}"
    locked_rows = [
        (name, detail) for name, ok, detail in report if name == "依赖 sqlglot"
    ]
    assert locked_rows and locked_rows[0][1] == _LOCKED_VERSIONS["sqlglot"]
