"""DatabaseConnector 经公共参数模块构造连接（doc 18.1 §4.1/§5.2）。

连接器不再自行拼装源库连接参数：字段交给 metaweave.db.postgres_options
构造 kwargs，再 make_conninfo(**kwargs) 序列化；sslmode 从配置读取并
规范化，非法值在建连（连接池创建）前失败。DDL/JSON/REL 既有路径不传
超时——连接参数中不应出现 connect_timeout 或 options。
"""

import logging

import pytest
from psycopg.conninfo import conninfo_to_dict

from metaweave.core.metadata import connector as connector_module
from metaweave.core.metadata.connector import DatabaseConnector

BASE_CONFIG = {
    "host": "localhost",
    "port": 5432,
    "database": "demo",
    "user": "reader",
    "password": "p a=s;word",
}


@pytest.fixture()
def captured_pool(monkeypatch):
    """记录传给连接池的 conninfo，不真正建连。"""
    captured = {}

    def fake_pool(conninfo, **_kwargs):
        captured["conninfo"] = conninfo
        return object()

    monkeypatch.setattr(connector_module, "ConnectionPool", fake_pool)
    return captured


def test_connector_normalizes_config_sslmode_into_conninfo(captured_pool):
    DatabaseConnector({**BASE_CONFIG, "sslmode": " Require "})

    params = conninfo_to_dict(captured_pool["conninfo"])
    assert params["sslmode"] == "require"
    assert params["password"] == "p a=s;word"


def test_connector_omits_sslmode_when_config_has_none(captured_pool):
    connector = DatabaseConnector(dict(BASE_CONFIG))

    params = conninfo_to_dict(captured_pool["conninfo"])
    assert "sslmode" not in params
    assert connector.database == "demo"


def test_connector_adds_no_timeout_options_for_legacy_paths(captured_pool):
    DatabaseConnector(dict(BASE_CONFIG))

    params = conninfo_to_dict(captured_pool["conninfo"])
    assert "connect_timeout" not in params
    assert "options" not in params


def test_connector_rejects_invalid_sslmode_before_connecting(monkeypatch):
    def fail_pool(*_args, **_kwargs):  # pragma: no cover - 到这里即失败
        raise AssertionError("非法 sslmode 不应触发建连")

    monkeypatch.setattr(connector_module, "ConnectionPool", fail_pool)

    with pytest.raises(ValueError, match="sslmode"):
        DatabaseConnector({**BASE_CONFIG, "sslmode": "reqiure"})


def test_connector_logs_keep_non_secret_fields_without_password(monkeypatch, caplog):
    """日志口径（doc 18.1 §4.1）：host/port/database/user 可见，密码不可见。"""
    monkeypatch.setattr(connector_module, "ConnectionPool", lambda *_a, **_k: object())
    caplog.set_level(logging.INFO, logger="metaweave.connector")

    DatabaseConnector(dict(BASE_CONFIG))

    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert "demo" in messages and "reader" in messages
    assert "p a=s;word" not in messages
