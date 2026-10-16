"""公共连接参数模块用例（doc 18.1 §4.1）。

sslmode 六模式值域、规范化（strip + 小写）与可选超时是 18.1 的核心规则：
- None / 空串 / 纯空白 = 未配置，省略该参数且不设应用默认；
- 非法值（拼错模式、`require host=other` 一类注入文本）在建连前报配置错误；
- 两个超时参数为 None 时不向 libpq 添加对应选项；
- 密码/用户名特殊字符原样作为单个参数传递。
"""

import pytest

from metaweave.db.postgres_options import (
    SSL_MODES,
    build_connect_kwargs,
    normalize_sslmode,
)

VALID_MODES = ("disable", "allow", "prefer", "require", "verify-ca", "verify-full")


class TestSslmodeNormalization:
    @pytest.mark.parametrize("mode", VALID_MODES)
    def test_accepts_all_six_libpq_modes(self, mode: str):
        assert normalize_sslmode(mode) == mode

    @pytest.mark.parametrize("blank", [None, "", "   ", "\t \n"])
    def test_blank_means_unconfigured(self, blank):
        assert normalize_sslmode(blank) is None

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("Require", "require"),
            (" require ", "require"),
            ("VERIFY-FULL", "verify-full"),
        ],
    )
    def test_strips_and_lowercases(self, raw, expected):
        assert normalize_sslmode(raw) == expected

    @pytest.mark.parametrize(
        "invalid",
        ["reqiure", "require host=other", "disable extra", "postgres://x"],
    )
    def test_rejects_invalid_values_before_connecting(self, invalid):
        with pytest.raises(ValueError, match="sslmode"):
            normalize_sslmode(invalid)

    def test_mode_set_exactly_matches_libpq_six_modes(self):
        assert SSL_MODES == frozenset(VALID_MODES)


class TestBuildConnectKwargs:
    BASE = {
        "host": "db.example.com",
        "port": 5432,
        "dbname": "orders",
        "user": "pg user",
        "password": "p@ss word:=x%",
    }

    def test_basic_fields_passed_verbatim_as_single_params(self):
        kwargs = build_connect_kwargs(**self.BASE)
        assert kwargs == {
            "host": "db.example.com",
            "port": 5432,
            "dbname": "orders",
            "user": "pg user",
            "password": "p@ss word:=x%",
        }

    def test_sslmode_unconfigured_omits_key(self):
        kwargs = build_connect_kwargs(**self.BASE, sslmode="   ")
        assert "sslmode" not in kwargs

    def test_sslmode_normalized(self):
        kwargs = build_connect_kwargs(**self.BASE, sslmode=" Require ")
        assert kwargs["sslmode"] == "require"

    def test_invalid_sslmode_raises_before_connecting(self):
        with pytest.raises(ValueError, match="sslmode"):
            build_connect_kwargs(**self.BASE, sslmode="require host=other")

    def test_no_timeouts_adds_no_options(self):
        kwargs = build_connect_kwargs(**self.BASE)
        assert "connect_timeout" not in kwargs
        assert "options" not in kwargs

    def test_connect_timeout_only(self):
        kwargs = build_connect_kwargs(**self.BASE, connect_timeout_seconds=10)
        assert kwargs["connect_timeout"] == 10
        assert "options" not in kwargs

    def test_statement_timeout_uses_options(self):
        kwargs = build_connect_kwargs(**self.BASE, statement_timeout_ms=60000)
        assert kwargs["options"] == "-c statement_timeout=60000"
        assert "connect_timeout" not in kwargs

    def test_both_timeouts_like_db_step(self):
        kwargs = build_connect_kwargs(
            **self.BASE, connect_timeout_seconds=10, statement_timeout_ms=60000
        )
        assert kwargs["connect_timeout"] == 10
        assert kwargs["options"] == "-c statement_timeout=60000"
