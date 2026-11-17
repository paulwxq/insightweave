"""NL2SQL_LOG_LEVEL 环境变量覆盖测试（doc 13 补充需求）。

规则：
- 未设置 → 沿用 YAML 的 DEBUG（本地开发行为不变）
- 设为 INFO → nl2sql 业务 logger 和 nl2sql_file handler 均降为 INFO
- 设为 DEBUG → 保持 DEBUG
- 非法值 → 启动时明确报错
- nl2sql.api、console 始终不受影响
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from src.utils.logger import setup_logging_from_yaml

YAML_PATH = Path(__file__).resolve().parents[3] / "configs" / "logging.yaml"


def _setup(monkeypatch, env_value=None):
    if env_value is None:
        monkeypatch.delenv("NL2SQL_LOG_LEVEL", raising=False)
    else:
        monkeypatch.setenv("NL2SQL_LOG_LEVEL", env_value)
    setup_logging_from_yaml(str(YAML_PATH))


def _business_level() -> int:
    return logging.getLogger("nl2sql").getEffectiveLevel()


def _file_handler_level() -> int:
    for h in logging.getLogger("nl2sql").handlers:
        if h.name == "nl2sql_file":
            return h.level
    raise AssertionError("nl2sql_file handler 未找到")


def _api_level() -> int:
    return logging.getLogger("nl2sql.api").getEffectiveLevel()


def _console_level() -> int:
    for h in logging.getLogger("nl2sql").handlers:
        if h.name == "console":
            return h.level
    raise AssertionError("console handler 未找到")


class TestLogLevelEnv:
    def test_unset_keeps_yaml_debug(self, monkeypatch):
        _setup(monkeypatch, None)
        assert _business_level() == logging.DEBUG
        assert _file_handler_level() == logging.DEBUG

    def test_info_overrides_business_and_file(self, monkeypatch):
        _setup(monkeypatch, "INFO")
        assert _business_level() == logging.INFO
        assert _file_handler_level() == logging.INFO

    def test_debug_explicit_same_as_default(self, monkeypatch):
        _setup(monkeypatch, "DEBUG")
        assert _business_level() == logging.DEBUG
        assert _file_handler_level() == logging.DEBUG

    def test_api_and_console_unaffected(self, monkeypatch):
        _setup(monkeypatch, "INFO")
        assert _api_level() == logging.INFO  # 本来就是 INFO
        assert _console_level() == logging.INFO

    def test_invalid_value_raises(self, monkeypatch):
        monkeypatch.setenv("NL2SQL_LOG_LEVEL", "VERBOSE")
        with pytest.raises(ValueError, match="NL2SQL_LOG_LEVEL"):
            setup_logging_from_yaml(str(YAML_PATH))

    def test_info_suppresses_debug_prompt_output(self, monkeypatch):
        """INFO 下 isEnabledFor(DEBUG) 为 False → 完整提示词不输出。"""
        _setup(monkeypatch, "INFO")
        logger = logging.getLogger("nl2sql.test")
        assert not logger.isEnabledFor(logging.DEBUG)

    def test_debug_enables_prompt_output(self, monkeypatch):
        _setup(monkeypatch, "DEBUG")
        logger = logging.getLogger("nl2sql.test")
        assert logger.isEnabledFor(logging.DEBUG)
