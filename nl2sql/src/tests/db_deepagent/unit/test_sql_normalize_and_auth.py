"""normalize_sql 与 auth_key 单元测试（docs/v4/5 §5.4、§8.1、§18.2 P02–P06）"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime

import pytest

from src.modules.db_deepagent.context import DbAgentContext
from src.modules.db_deepagent.services.sql_pipeline import auth_key, normalize_sql

pytestmark = pytest.mark.unit


class TestNormalizeSql:
    def test_strips_leading_trailing_ascii_whitespace(self):
        assert normalize_sql("  SELECT 1\t\n") == "SELECT 1"

    def test_preserves_inner_whitespace(self):
        """内部空白（含缩进、换行）原样保留。"""
        sql = "SELECT\n  a,\n  b\nFROM t\n WHERE x = 1"
        assert normalize_sql(sql) == sql

    def test_fullwidth_space_is_not_ascii_whitespace(self):
        """全角空格（U+3000）不是 ASCII 空白，首尾也保留。"""
        sql = "　SELECT 1　"
        assert normalize_sql(sql) == sql

    def test_empty_and_whitespace_only(self):
        assert normalize_sql("") == ""
        assert normalize_sql(" \t\r\n ") == ""


class TestAuthKey:
    SQL = "SELECT 1 FROM public.orders"

    def _ctx(self, **overrides) -> DbAgentContext:
        base = DbAgentContext(
            session_id="s1",
            thread_id="t1",
            request_id="r1",
            run_id="run-1",
            datasource_id="ds1",
            validation_fingerprint="fp1",
            now=datetime(2026, 10, 13, 12, 0),
            timezone="Asia/Shanghai",
            user_texts=("q",),
            carried_evidence=(),
            deadline_monotonic=0.0,
        )
        return replace(base, **overrides) if overrides else base

    def test_deterministic(self):
        assert auth_key(self._ctx(), self.SQL) == auth_key(self._ctx(), self.SQL)

    def test_scope_fields_all_change_key(self):
        """任一作用域字段变化都得到新键（§5.4）。"""
        base = auth_key(self._ctx(), self.SQL)
        variants = {
            "thread_id": "t2",
            "request_id": "r2",
            "run_id": "run-2",
            "datasource_id": "ds2",
            "validation_fingerprint": "fp2",
        }
        for field, value in variants.items():
            assert auth_key(self._ctx(**{field: value}), self.SQL) != base, field

    def test_normalized_text_change_changes_key(self):
        base = auth_key(self._ctx(), "SELECT 1")
        # 调用方先规范化：首尾空白剥离后键相同；内部空白不同则不同
        assert auth_key(self._ctx(), normalize_sql("  SELECT 1 \n")) == base
        assert auth_key(self._ctx(), "SELECT  1") != base
