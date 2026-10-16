"""_assert_f08_counts 替身测试（结果文件按列核对，无需模型）

覆盖：正确数据通过；订单数全错（999）被拒；999＋rank 列 0/1/2 陷阱被拒
（修复前“同行任意位置含目标数字”会被排名列满足）；0 计数按缺席核对通过。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.modules.db_deepagent.context import DbAgentContext
from src.modules.db_deepagent.services.result_store import ResultStore
from src.tests.db_deepagent.integration.test_business_cases import (
    _assert_f08_counts,
)

pytestmark = pytest.mark.unit

AUG_SQL = (
    "SELECT user_id, count(*) AS order_count FROM orders o "
    "WHERE o.order_date >= '2026-08-01' AND o.order_date < '2026-09-01' "
    "AND o.user_id IN (SELECT user_id FROM top3) GROUP BY user_id"
)
TOP3_SQL = (
    "SELECT user_id, sum(amount) AS total_amount FROM orders "
    "WHERE order_date >= '2026-09-01' AND order_date < '2026-10-01' "
    "GROUP BY user_id ORDER BY total_amount DESC LIMIT 3"
)


def _ctx(session_id: str) -> DbAgentContext:
    from datetime import datetime
    from zoneinfo import ZoneInfo

    return DbAgentContext(
        session_id=session_id,
        thread_id="dbda:%s:1" % session_id,
        request_id="req-x",
        run_id="run-x",
        datasource_id="orders",
        validation_fingerprint="fp",
        now=datetime(2026, 10, 13, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
        timezone="Asia/Shanghai",
        user_texts=("q",),
        carried_evidence=(),
        deadline_monotonic=0.0,
    )


def _save(store: ResultStore, ctx: DbAgentContext, columns, rows, sql):
    return store.save(
        ctx=ctx,
        purpose="query",
        sql_template=sql,
        executed_sql=sql,
        columns=columns,
        rows=rows,
        source_truncated=False,
        derived_from=[],
        guard_evidence={"time": [], "dim": []},
    )


def _build(tmp_path, aug_rows, top3_rows, aug_columns=None, top3_columns=None):
    """构造 runtime/result 替身：top3 文件 + 八月计数文件。"""
    store = ResultStore(tmp_path)
    ctx = _ctx("s-f08")
    top3_columns = top3_columns or [
        {"name": "user_id", "pg_type": "int4"},
        {"name": "total_amount", "pg_type": "numeric"},
    ]
    aug_columns = aug_columns or [
        {"name": "user_id", "pg_type": "int4"},
        {"name": "order_count", "pg_type": "int8"},
    ]
    ref_top3 = _save(store, ctx, top3_columns, top3_rows, TOP3_SQL)
    ref_aug = _save(store, ctx, aug_columns, aug_rows, AUG_SQL)
    runtime = {"workspace": str(tmp_path)}
    result = SimpleNamespace(
        session_id="s-f08",
        evidence=[
            SimpleNamespace(result_ref=ref_top3.result_ref, sql=TOP3_SQL),
            SimpleNamespace(result_ref=ref_aug.result_ref, sql=AUG_SQL),
        ],
    )
    return runtime, result


class TestF08Counts:
    def test_correct_data_passes(self, tmp_path):
        runtime, result = _build(
            tmp_path,
            aug_rows=[[15, 1], [81, 2]],  # 60 的 0 计数缺席
            top3_rows=[[60, "465.00"], [15, "240.00"], [81, "224.00"]],
        )
        _assert_f08_counts(runtime, result)  # 不抛即通过

    def test_all_wrong_counts_rejected(self, tmp_path):
        runtime, result = _build(
            tmp_path,
            aug_rows=[[60, 999], [15, 999], [81, 999]],
            top3_rows=[[60, "465.00"], [15, "240.00"], [81, "224.00"]],
        )
        with pytest.raises(AssertionError, match="60"):
            _assert_f08_counts(runtime, result)

    def test_rank_column_trap_rejected(self, tmp_path):
        """订单数全错但 rank 列恰为 0/1/2：按列核对必须拒绝。"""
        aug_columns = [
            {"name": "user_id", "pg_type": "int4"},
            {"name": "aug_order_count", "pg_type": "int8"},
            {"name": "rank", "pg_type": "int4"},
        ]
        runtime, result = _build(
            tmp_path,
            aug_rows=[[60, 999, 0], [15, 999, 1], [81, 999, 2]],
            top3_rows=[[60, "465.00"], [15, "240.00"], [81, "224.00"]],
            aug_columns=aug_columns,
        )
        with pytest.raises(AssertionError):
            _assert_f08_counts(runtime, result)

    def test_explicit_zero_row_passes(self, tmp_path):
        """COALESCE 风格显式 0 行：按列配对直接通过。"""
        runtime, result = _build(
            tmp_path,
            aug_rows=[[60, 0], [15, 1], [81, 2]],
            top3_rows=[[60, "465.00"], [15, "240.00"], [81, "224.00"]],
        )
        _assert_f08_counts(runtime, result)

    def test_missing_customer_in_top3_rejected(self, tmp_path):
        """0 计数缺席核对要求 top3 行确实含该客户。"""
        runtime, result = _build(
            tmp_path,
            aug_rows=[[15, 1], [81, 2]],
            top3_rows=[[7, "465.00"], [15, "240.00"], [81, "224.00"]],  # 无 60
        )
        with pytest.raises(AssertionError, match="60"):
            _assert_f08_counts(runtime, result)

    def test_customer_name_column_not_mistaken_for_id(self, tmp_path):
        """列序 customer_name、user_id、order_count：不得把名称列当 ID 列。"""
        aug_columns = [
            {"name": "customer_name", "pg_type": "varchar"},
            {"name": "user_id", "pg_type": "int4"},
            {"name": "order_count", "pg_type": "int8"},
        ]
        aug_rows = [
            ["张三", 15, 1],
            ["李四", 81, 2],
        ]  # 60 的 0 计数缺席
        top3_columns = [
            {"name": "customer_name", "pg_type": "varchar"},
            {"name": "user_id", "pg_type": "int4"},
            {"name": "total_amount", "pg_type": "numeric"},
        ]
        top3_rows = [
            ["张三", 60, "465.00"],
            ["李四", 15, "240.00"],
            ["王五", 81, "224.00"],
        ]
        runtime, result = _build(
            tmp_path,
            aug_rows=aug_rows,
            top3_rows=top3_rows,
            aug_columns=aug_columns,
            top3_columns=top3_columns,
        )
        _assert_f08_counts(runtime, result)  # 精确匹配 user_id，不误中名称列

    def test_unidentifiable_customer_column_fails_closed(self, tmp_path):
        """无精确 ID 列名时 fail-closed（报列识别问题而非静默放行）。"""
        aug_columns = [
            {"name": "customer_name", "pg_type": "varchar"},  # 只有名称列
            {"name": "order_count", "pg_type": "int8"},
        ]
        runtime, result = _build(
            tmp_path,
            aug_rows=[["张三", 1]],
            top3_rows=[[60, "465.00"]],
            aug_columns=aug_columns,
        )
        with pytest.raises(AssertionError, match="无法定位"):
            _assert_f08_counts(runtime, result)
