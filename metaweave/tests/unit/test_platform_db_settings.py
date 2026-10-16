"""平台库 sslmode 复用同一校验函数（doc 18.1 §4.1/§5.2）。

`PLATFORM_DB_SSLMODE` 经 `normalize_sslmode` 规范化：非法值在加载设置时
（建连前）报配置错误；空串与未配置等价；合法值规范化为小写后由
`platform_session()` 作为单独参数传入。
"""

import pytest

from backend.persistence import platform_db
from backend.persistence.platform_db import load_platform_settings_from_env

BASE_ENV = {
    "PLATFORM_DB_HOST": "platform.example.test",
    "PLATFORM_DB_USER": "platform",
    "PLATFORM_DB_PASSWORD": "secret",
}


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for key, value in BASE_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("PLATFORM_DB_SSLMODE", raising=False)


def test_unset_sslmode_stays_none():
    settings = load_platform_settings_from_env()
    assert settings.sslmode is None


def test_blank_sslmode_stays_none(monkeypatch):
    monkeypatch.setenv("PLATFORM_DB_SSLMODE", "   ")
    assert load_platform_settings_from_env().sslmode is None


def test_valid_sslmode_normalized_to_lowercase(monkeypatch):
    monkeypatch.setenv("PLATFORM_DB_SSLMODE", " Verify-Full ")
    assert load_platform_settings_from_env().sslmode == "verify-full"


@pytest.mark.parametrize("bad", ["reqiure", "require host=other"])
def test_invalid_platform_sslmode_fails_before_engine(monkeypatch, bad):
    monkeypatch.setenv("PLATFORM_DB_SSLMODE", bad)

    def fail_engine(*_args, **_kwargs):  # pragma: no cover - 到这里即失败
        raise AssertionError("非法 PLATFORM_DB_SSLMODE 不应创建引擎")

    monkeypatch.setattr(platform_db, "create_engine", fail_engine)
    with pytest.raises(ValueError, match="sslmode"):
        load_platform_settings_from_env()


def test_platform_session_passes_normalized_sslmode(monkeypatch):
    """settings.sslmode 已规范化；session 只在有值时传该单独参数。"""
    captured = {}

    class _FakeSession:
        def __init__(self, engine):
            captured["engine"] = engine

        def __enter__(self):
            return object()

        def __exit__(self, *exc):
            return False

    class _FakeEngine:
        def dispose(self):
            captured["disposed"] = True

    monkeypatch.setattr(
        platform_db,
        "create_engine",
        lambda _url, connect_args=None: (
            captured.update(connect_args=connect_args) or _FakeEngine()
        ),
    )
    monkeypatch.setattr(platform_db, "Session", _FakeSession)
    settings = platform_db.PlatformSettings(
        host="h", port=5432, name="metaweave", user="u", password="p", sslmode="require"
    )
    with platform_db.platform_session(settings):
        pass
    assert captured["connect_args"] == {"connect_timeout": 10, "sslmode": "require"}
    assert captured["disposed"] is True


def test_session_rejects_invalid_sslmode_from_directly_built_settings(monkeypatch):
    """直接构造的 PlatformSettings 也逃不过校验（doc 18.1 §4.1：入口再校验）。"""
    from backend.persistence.platform_db import PlatformSettings, platform_session

    def fail_engine(*_args, **_kwargs):  # pragma: no cover - 到这里即失败
        raise AssertionError("非法 sslmode 不应创建引擎")

    monkeypatch.setattr(platform_db, "create_engine", fail_engine)
    settings = PlatformSettings(
        host="h",
        port=5432,
        name="n",
        user="u",
        password="p",
        sslmode="require host=other",
    )
    with pytest.raises(ValueError, match="sslmode"):
        with platform_session(settings):
            pass
