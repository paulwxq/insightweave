"""ResultAnalysisService 单元测试（docs/v4/5 §7.10.1＝doc 7、§18.3）

覆盖：六操作本地语义（NULL/空集/精度）、路由决策（含数据源优先级与
单路径规则）、类型白名单（文本族仅下推、NaN）、下推 SQL 形态。
"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest

from src.modules.db_deepagent.services.result_analysis import (
    AggregateParams,
    AnalysisError,
    CompareParams,
    DescribeParams,
    DistinctValuesParams,
    InputBundle,
    MetricSpec,
    ResultAnalysisService,
    ShareParams,
    TopNParams,
    exact_sum,
)
from src.modules.db_deepagent.services.result_store import ResultMeta

pytestmark = pytest.mark.unit


def _meta(cols, *, ds="ds1", trunc=False, sql="SELECT 1 AS k, 2 AS x"):
    return ResultMeta(
        result_ref="res_" + "1" * 32,
        session_id="s",
        thread_id="t",
        request_id="r",
        run_id="run",
        purpose="query",
        datasource_id=ds,
        sql_template=sql,
        executed_sql=sql,
        executed_sql_sha256="h",
        columns=cols,
        saved_row_count=0,
        source_truncated=trunc,
        created_at="2026-10-01T00:00:00+08:00",
        rows_sha256="x",
        derived_from=[],
        guard_evidence={"time": [], "dim": []},
    )


@pytest.fixture()
def svc():
    return ResultAnalysisService(None, results=None)


# R2-09：内部分析服务入口要求有效运行持锁凭据；本文件直调 svc.analyze，
# 用独立会话的进程内 guard 提供真实凭据（不与 db_agent_context 的 s-test
# 冲突）。其余私有方法单测仍按原语义直接调用。
from src.modules.db_deepagent.persistence.session_guard import (  # noqa: E402
    acquire_memory_lease,
)

_ANALYSIS_TEST_LEASE = acquire_memory_lease("s-result-analysis-svc-tests")
CTX = SimpleNamespace(
    datasource_id="ds1",
    session_id="s-result-analysis-svc-tests",
    session_guard_lease=_ANALYSIS_TEST_LEASE,
)


def _no_pushdown(*_a, **_k):
    raise AssertionError("不应触发下推")


class TestPrecision:
    def test_exponent_span_sum(self):
        got = exact_sum([Decimal("1E+28"), Decimal("1E-28")])
        assert got == Decimal(
            "10000000000000000000000000000.0000000000000000000000000001"
        )

    def test_carry_margin(self):
        assert exact_sum([Decimal(10**28), Decimal(1)]) == Decimal(
            "10000000000000000000000000001"
        )

    def test_round_half_up_direction(self):
        """doc 7 §1.3：2.5→3、-2.5→-3（默认 HALF_EVEN 会得 2/-2）。"""
        from src.modules.db_deepagent.services.result_analysis import _round_half_up

        assert _round_half_up(Decimal("2.5"), 0) == 3
        assert _round_half_up(Decimal("-2.5"), 0) == -3
        assert _round_half_up(Decimal("1.005"), 2) == Decimal("1.01")
        assert _round_half_up(Decimal("-1.005"), 2) == Decimal("-1.01")

    def test_round_half_up_big_value_no_overflow(self):
        """大数在局部精度中量化，不抛 InvalidOperation。"""
        from src.modules.db_deepagent.services.result_analysis import _round_half_up

        big = Decimal("12345678901234567890123456789012345.6789")
        assert _round_half_up(big, 2) == Decimal(
            "12345678901234567890123456789012345.68"
        )

    def test_exact_sub_keeps_low_digit(self):
        """减法在局部精度中不丢末位（默认 28 位会把 10^28+1 舍成 10^28）。"""
        from src.modules.db_deepagent.services.result_analysis import exact_sub

        # 注意用字符串构造：Decimal(10**28) + 1 本身就在默认精度下被舍入
        big = Decimal("10000000000000000000000000001")
        assert exact_sub(big, Decimal(0)) == big
        assert exact_sub(big, Decimal(1)) == Decimal(10**28)

    def test_exact_sub_exponent_span(self):
        """减法复用指数跨度上界：1E28 − 1E-28 的小数部分不丢。"""
        from src.modules.db_deepagent.services.result_analysis import exact_sub

        got = exact_sub(Decimal("1E+28"), Decimal("1E-28"))
        assert got == Decimal(
            "9999999999999999999999999999.9999999999999999999999999999"
        )

    def test_div_no_premature_rounding_near_boundary(self):
        """略小于 0.005 的值 ÷1：除法不得先舍成 0.005（量化应得 0.00）。"""
        from src.modules.db_deepagent.services.result_analysis import (
            _round_half_up,
            divide,
        )

        just_below = Decimal("0.0049999999999999999999999999992")
        q = divide(just_below, Decimal(1), 2)
        assert _round_half_up(q, 2) == Decimal("0.00")
        just_above = Decimal("0.0050000000000000000000000000008")
        q2 = divide(just_above, Decimal(1), 2)
        assert _round_half_up(q2, 2) == Decimal("0.01")

    def test_div_precision_covers_quotient_integer_digits(self):
        """1 / 3E-28 商约 3.3E+27：精度须覆盖商的整数位＋目标小数位。"""
        from src.modules.db_deepagent.services.result_analysis import (
            _round_half_up,
            divide,
        )

        q = divide(Decimal(1), Decimal("3E-28"), 2)
        assert str(_round_half_up(q, 2)).endswith(".33")

    def test_compare_big_values_no_precision_loss(self, svc):
        """compare 差值/环比在大数下不丢精度（diff 用 exact_sub）。"""
        cols = [{"name": "v", "pg_type": "numeric"}]
        big = Decimal("10000000000000000000000000001")  # 字符串构造避免测试自身舍入
        a = InputBundle(meta=_meta(cols), rows=[[big]])
        b = InputBundle(meta=_meta(cols), rows=[[Decimal(1)]])
        out = svc._compare(
            CompareParams(
                a={"ref": "r1", "column": "v"},
                b={"ref": "r2", "column": "v"},
                round_digits=2,
            ),
            ["r1", "r2"],
            load_bundle=lambda r: a if r == "r1" else b,
        )
        assert out.rows[0][0] == Decimal(10**28)  # (10^28+1) - 1 不丢位
        assert out.rows[0][1] is not None

    def test_compare_inputs_must_match_refs(self, svc):
        """inputs 与 a.ref/b.ref 不一致 → invalid_argument（登记来源防伪）。"""
        cols = [{"name": "k", "pg_type": "int4"}, {"name": "x", "pg_type": "numeric"}]
        a = InputBundle(meta=_meta(cols), rows=[[1, Decimal("1")]])
        b = InputBundle(meta=_meta(cols), rows=[[1, Decimal("2")]])
        with pytest.raises(AnalysisError) as e:
            svc._compare(
                CompareParams(
                    a={"ref": "r1", "column": "x"}, b={"ref": "r2", "column": "x"}
                ),
                ["r1", "res_bogus"],  # 与实际使用的引用不一致
                load_bundle=lambda r: a if r == "r1" else b,
            )
        assert e.value.error_type == "invalid_argument"
        assert "一致" in e.value.message


class TestAggregateSemantics:
    COLS = [{"name": "k", "pg_type": "int4"}, {"name": "x", "pg_type": "numeric"}]

    def test_no_group_empty_input_one_row(self, svc):
        b = InputBundle(meta=_meta(self.COLS), rows=[])
        out = svc._aggregate(
            AggregateParams(
                group_by=[],
                metrics=[
                    MetricSpec(column="x", func="count"),
                    MetricSpec(column="x", func="sum"),
                ],
            ),
            [b],
            CTX,
            allow_pushdown=True,
            run_pushdown=_no_pushdown,
        )
        assert out.rows == [[0, None]]  # PG：无分组键空输入 1 行，SUM=NULL 非 0

    def test_group_all_null_metric_is_null(self, svc):
        b = InputBundle(meta=_meta(self.COLS), rows=[[1, None], [1, None]])
        out = svc._aggregate(
            AggregateParams(
                group_by=["k"], metrics=[MetricSpec(column="x", func="sum")]
            ),
            [b],
            CTX,
            allow_pushdown=True,
            run_pushdown=_no_pushdown,
        )
        assert out.rows == [[1, None]]

    def test_group_key_null_group(self, svc):
        rows = [[None, Decimal("2")]]
        b = InputBundle(meta=_meta(self.COLS), rows=rows)
        out = svc._aggregate(
            AggregateParams(
                group_by=["k"], metrics=[MetricSpec(column="x", func="sum")]
            ),
            [b],
            CTX,
            allow_pushdown=True,
            run_pushdown=_no_pushdown,
        )
        assert out.rows == [[None, Decimal("2")]]

    def test_count_distinct_equal_decimals_one(self, svc):
        """等值 Decimal（1.0/1.00）按值判等只计一次（序列化判等会得 2）。"""
        rows = [[1, Decimal("1.0")], [1, Decimal("1.00")]]
        b = InputBundle(meta=_meta(self.COLS), rows=rows)
        out = svc._aggregate(
            AggregateParams(
                group_by=["k"],
                metrics=[MetricSpec(column="x", func="count_distinct")],
            ),
            [b],
            CTX,
            allow_pushdown=True,
            run_pushdown=_no_pushdown,
        )
        assert out.rows == [[1, 1]]

    def test_output_types_preserve_input(self, svc):
        """数值分组键与 min/max 输出保留输入类型，不再标 text。"""
        cols = [
            {"name": "k", "pg_type": "int4"},
            {"name": "x", "pg_type": "numeric"},
            {"name": "d", "pg_type": "date"},
        ]
        b = InputBundle(meta=_meta(cols), rows=[[1, Decimal("1"), None]])
        out = svc._aggregate(
            AggregateParams(
                group_by=["k"],
                metrics=[
                    MetricSpec(column="x", func="min"),
                    MetricSpec(column="d", func="max"),
                ],
            ),
            [b],
            CTX,
            allow_pushdown=True,
            run_pushdown=_no_pushdown,
        )
        types = {c["name"]: c["pg_type"] for c in out.columns}
        assert types["k"] == "int4"
        assert types["min_x"] == "numeric"
        assert types["max_d"] == "date"

    def test_share_group_type_preserved(self, svc):
        cols = [{"name": "k", "pg_type": "int4"}, {"name": "x", "pg_type": "numeric"}]
        b = InputBundle(meta=_meta(cols), rows=[[1, Decimal("1")]])
        out = svc._share(
            ShareParams(value_column="x", group_by=["k"]),
            [b],
            CTX,
            allow_pushdown=True,
            run_pushdown=_no_pushdown,
        )
        types = {c["name"]: c["pg_type"] for c in out.columns}
        assert types["k"] == "int4" and types["share"] == "numeric"


class TestRouting:
    COLS = [{"name": "k", "pg_type": "int4"}, {"name": "x", "pg_type": "numeric"}]

    def test_step0_input_datasource_mismatch_first(self, svc):
        # 经公共入口：第 0 步先于操作分派与参数校验
        b1 = InputBundle(meta=_meta(self.COLS, ds="ds1"), rows=[])
        b2 = InputBundle(meta=_meta(self.COLS, ds="ds2"), rows=[])
        with pytest.raises(AnalysisError) as e:
            svc.analyze(
                operation="aggregate",
                params={"group_by": [], "metrics": [{"column": "x", "func": "sum"}]},
                input_refs=["r1", "r2"],
                ctx=CTX,
                load_bundle=lambda r: b1 if r == "r1" else b2,
                run_pushdown=_no_pushdown,
            )
        assert e.value.error_type == "result_datasource_mismatch"

    def test_truncated_no_pushdown_incomplete_source(self, svc):
        b = InputBundle(meta=_meta(self.COLS, trunc=True), rows=[[1, Decimal("1")]])
        with pytest.raises(AnalysisError) as e:
            svc._aggregate(
                AggregateParams(
                    group_by=[], metrics=[MetricSpec(column="x", func="sum")]
                ),
                [b],
                CTX,
                allow_pushdown=False,
                run_pushdown=_no_pushdown,
            )
        assert e.value.error_type == "incomplete_source"

    def test_local_unsupported_no_pushdown_invalid_argument(self, svc):
        cols = [{"name": "x", "pg_type": "float8"}]
        b = InputBundle(meta=_meta(cols), rows=[[1.5]])
        with pytest.raises(AnalysisError) as e:
            svc._aggregate(
                AggregateParams(
                    group_by=[], metrics=[MetricSpec(column="x", func="sum")]
                ),
                [b],
                CTX,
                allow_pushdown=False,
                run_pushdown=_no_pushdown,
            )
        assert e.value.error_type == "invalid_argument"
        assert "仅支持下推" in e.value.message

    def test_text_family_pushdown_only_even_eq(self, svc):
        cols = [{"name": "g", "pg_type": "varchar"}, {"name": "x", "pg_type": "int4"}]
        b = InputBundle(meta=_meta(cols), rows=[["a", 1]])
        with pytest.raises(AnalysisError) as e:
            svc._aggregate(
                AggregateParams(
                    group_by=["g"], metrics=[MetricSpec(column="x", func="count")]
                ),
                [b],
                CTX,
                allow_pushdown=False,
                run_pushdown=_no_pushdown,
            )
        assert e.value.error_type == "invalid_argument"

    def test_truncated_pushdown_invoked_with_warning(self, svc):
        b = InputBundle(meta=_meta(self.COLS, trunc=True), rows=[[1, Decimal("1")]])
        calls = []

        def fake_pushdown(sql, metas):
            calls.append(sql)
            outcome = SimpleNamespace(
                ok=True,
                error_type=None,
                message=None,
                hint=None,
                meta=SimpleNamespace(result_ref="res_" + "9" * 32),
            )
            update = SimpleNamespace(
                executions=[{"evidence_id": "E-push"}],
                as_state_update=lambda: {"db_executions": []},
            )
            return outcome, update

        out = svc._aggregate(
            AggregateParams(group_by=[], metrics=[MetricSpec(column="x", func="sum")]),
            [b],
            CTX,
            allow_pushdown=True,
            run_pushdown=fake_pushdown,
        )
        assert out.executed_sql and 'SUM(src."x"::numeric)' in out.executed_sql
        assert out.pushdown_result_ref == "res_" + "9" * 32
        assert any("非原结果快照" in w for w in out.warnings)


class TestSinglePath:
    COLS = [{"name": "k", "pg_type": "int4"}, {"name": "x", "pg_type": "numeric"}]

    def test_describe_truncated_still_local(self, svc):
        b = InputBundle(
            meta=_meta(self.COLS, trunc=True), rows=[[1, Decimal("2")], [1, None]]
        )
        out = svc._describe(
            DescribeParams(columns=["x"]), ["r1"], load_bundle=lambda r: b
        )
        assert out.ok and out.rows[0][:4] == ["x", 2, 1, 1]
        assert any("已保存行" in w for w in out.warnings)

    def test_describe_nonfinite_skips_stats(self, svc):
        b = InputBundle(meta=_meta(self.COLS), rows=[[1, Decimal("NaN")]])
        out = svc._describe(
            DescribeParams(columns=["x"]), ["r1"], load_bundle=lambda r: b
        )
        assert out.rows[0][4] is None and out.rows[0][6] is None
        assert any("非有限值" in w for w in out.warnings)

    def test_describe_mixed_finite_nonfinite_omits_stats(self, svc):
        """[1, NaN] 混合：min/max/均值真正省略（不是剔除 NaN 后照算 1）。"""
        b = InputBundle(
            meta=_meta([{"name": "x", "pg_type": "float8"}]),
            rows=[[1.5], [float("nan")]],
        )
        out = svc._describe(
            DescribeParams(columns=["x"]), ["r1"], load_bundle=lambda r: b
        )
        row = out.rows[0]
        assert row[:4] == ["x", 2, 2, 0]  # count/non_null/null_count 仍完整
        assert row[4] is None and row[5] is None and row[6] is None
        assert any("非有限值" in w for w in out.warnings)

    def test_compare_truncated_incomplete_source(self, svc):
        a = InputBundle(meta=_meta(self.COLS, trunc=True), rows=[[1, Decimal("1")]])
        b = InputBundle(meta=_meta(self.COLS), rows=[[1, Decimal("1")]])
        with pytest.raises(AnalysisError) as e:
            svc._compare(
                CompareParams(
                    a={"ref": "r1", "column": "x"}, b={"ref": "r2", "column": "x"}
                ),
                ["r1", "r2"],
                load_bundle=lambda r: a if r == "r1" else b,
            )
        assert e.value.error_type == "incomplete_source"

    def test_compare_row_missing_invalid_argument(self, svc):
        a = InputBundle(meta=_meta(self.COLS), rows=[])
        b = InputBundle(meta=_meta(self.COLS), rows=[[1, Decimal("1")]])
        with pytest.raises(AnalysisError) as e:
            svc._compare(
                CompareParams(
                    a={"ref": "r1", "column": "x"}, b={"ref": "r2", "column": "x"}
                ),
                ["r1", "r2"],
                load_bundle=lambda r: a if r == "r1" else b,
            )
        assert e.value.error_type == "invalid_argument"


class TestTopNAndDistinct:
    COLS = [{"name": "k", "pg_type": "int4"}, {"name": "x", "pg_type": "numeric"}]

    def test_desc_nulls_first_strict_count(self, svc):
        b = InputBundle(
            meta=_meta([{"name": "v", "pg_type": "int4"}]),
            rows=[[3], [None], [1], [2]],
        )
        out = svc._top_n(
            TopNParams(order_by="v", descending=True, n=2, ties="strict"),
            [b],
            CTX,
            allow_pushdown=True,
            run_pushdown=_no_pushdown,
        )
        assert out.rows == [[None], [3]]

    def test_tie_key_null_no_crash_pg_order(self, svc):
        """主键并列时次键含 NULL 不崩溃；次键升序按 PG NULLS LAST。"""
        cols = [{"name": "a", "pg_type": "int4"}, {"name": "b", "pg_type": "int4"}]
        b = InputBundle(meta=_meta(cols), rows=[[5, None], [5, 3], [5, 1]])
        out = svc._top_n(
            TopNParams(order_by="a", descending=True, n=3, ties="strict"),
            [b],
            CTX,
            allow_pushdown=True,
            run_pushdown=_no_pushdown,
        )
        # 次键 ASC、NULLS LAST：b=1, 3, NULL
        assert out.rows == [[5, 1], [5, 3], [5, None]]

    def test_include_keeps_boundary_ties(self, svc):
        b = InputBundle(
            meta=_meta([{"name": "v", "pg_type": "int4"}]),
            rows=[[5], [3], [3], [3], [1]],
        )
        out = svc._top_n(
            TopNParams(order_by="v", descending=True, n=2, ties="include"),
            [b],
            CTX,
            allow_pushdown=True,
            run_pushdown=_no_pushdown,
        )
        values = [r[0] for r in out.rows]
        assert values == [5, 3, 3, 3]  # 边界并列全部保留

    def test_distinct_truncation_ordered(self, svc):
        b = InputBundle(
            meta=_meta([{"name": "v", "pg_type": "int4"}]), rows=[[2], [1], [3]]
        )
        out = svc._distinct_values(
            DistinctValuesParams(column="v", max_values=2),
            [b],
            CTX,
            allow_pushdown=True,
            run_pushdown=_no_pushdown,
        )
        assert out.rows == [[1], [2]]
        assert any("limit" in w for w in out.warnings)

    def test_distinct_truncation_flag_carried(self, svc):
        """超 max_values 的去重结果必须带 source_truncated=True。"""
        b = InputBundle(
            meta=_meta([{"name": "v", "pg_type": "int4"}]), rows=[[2], [1], [3]]
        )
        out = svc._distinct_values(
            DistinctValuesParams(column="v", max_values=2),
            [b],
            CTX,
            allow_pushdown=True,
            run_pushdown=_no_pushdown,
        )
        assert out.source_truncated is True

    def test_distinct_no_truncation_flag_false(self, svc):
        b = InputBundle(meta=_meta([{"name": "v", "pg_type": "int4"}]), rows=[[2], [1]])
        out = svc._distinct_values(
            DistinctValuesParams(column="v", max_values=5),
            [b],
            CTX,
            allow_pushdown=True,
            run_pushdown=_no_pushdown,
        )
        assert out.source_truncated is False
        assert out.rows == [[1], [2]]

    def test_distinct_equal_decimals_once(self, svc):
        b = InputBundle(
            meta=_meta([{"name": "v", "pg_type": "numeric"}]),
            rows=[[Decimal("1.0")], [Decimal("1.00")], [Decimal("2")]],
        )
        out = svc._distinct_values(
            DistinctValuesParams(column="v", max_values=10),
            [b],
            CTX,
            allow_pushdown=True,
            run_pushdown=_no_pushdown,
        )
        assert out.rows == [[Decimal("1.0")], [Decimal("2")]]

    def test_top_n_ties_include_equal_decimals(self, svc):
        """等值 Decimal 并列（1.0 与 1.00）在 include 模式须同入。"""
        b = InputBundle(
            meta=_meta([{"name": "v", "pg_type": "numeric"}]),
            rows=[[Decimal("1.0")], [Decimal("1.00")], [Decimal("2")]],
        )
        out = svc._top_n(
            TopNParams(order_by="v", descending=False, n=1, ties="include"),
            [b],
            CTX,
            allow_pushdown=True,
            run_pushdown=_no_pushdown,
        )
        # 边界值 1.0 与 1.00 数值相等：并列行都保留（序列化判等会漏并）
        assert out.rows == [[Decimal("1.0")], [Decimal("1.00")]]

    def test_distinct_pushdown_passes_row_cap(self, svc):
        """文本列本地不可排序 → 下推，且 row_cap=max_values（存上限行+截断标记）。"""
        cols = [{"name": "v", "pg_type": "varchar"}]
        b = InputBundle(meta=_meta(cols, trunc=False), rows=[["a"]])
        captured: dict = {}

        def fake_pushdown(sql, metas, row_cap=None):
            captured["sql"] = sql
            captured["row_cap"] = row_cap
            outcome = SimpleNamespace(
                ok=True,
                error_type=None,
                message=None,
                hint=None,
                meta=SimpleNamespace(result_ref="res_" + "8" * 32),
            )
            update = SimpleNamespace(
                executions=[
                    {
                        "evidence_id": "E-push",
                        "source_truncated": True,
                    }
                ],
                as_state_update=lambda: {"db_executions": []},
            )
            return outcome, update

        out = svc._distinct_values(
            DistinctValuesParams(column="v", max_values=7),
            [b],
            CTX,
            allow_pushdown=True,
            run_pushdown=fake_pushdown,
        )
        assert "LIMIT 8" in captured["sql"]  # max_values + 1 哨兵
        assert captured["row_cap"] == 7
        assert out.pushdown_result_ref == "res_" + "8" * 32
        assert out.source_truncated is True  # 来自执行记录

    def test_pushdown_outcome_source_truncated_from_record(self, svc):
        """下推结果outcome的截断标记取自 ExecutionRecord（非固定 False）。"""
        b = InputBundle(meta=_meta(self.COLS, trunc=True), rows=[[1, Decimal("1")]])

        def fake_pushdown(sql, metas, row_cap=None):
            outcome = SimpleNamespace(
                ok=True,
                error_type=None,
                message=None,
                hint=None,
                meta=SimpleNamespace(result_ref="res_" + "7" * 32),
            )
            update = SimpleNamespace(
                executions=[{"evidence_id": "E-p", "source_truncated": True}],
                as_state_update=lambda: {},
            )
            return outcome, update

        out = svc._aggregate(
            AggregateParams(group_by=[], metrics=[MetricSpec(column="x", func="sum")]),
            [b],
            CTX,
            allow_pushdown=True,
            run_pushdown=fake_pushdown,
        )
        assert out.source_truncated is True

    def test_local_outcome_datasource_from_input(self, svc):
        """本地输出沿用输入数据源（跨数据源输入不改写为当前值）。"""
        b = InputBundle(meta=_meta(self.COLS, ds="ds-other"), rows=[[1, Decimal("2")]])
        out = svc._aggregate(
            AggregateParams(group_by=[], metrics=[MetricSpec(column="x", func="sum")]),
            [b],
            CTX,
            allow_pushdown=True,
            run_pushdown=_no_pushdown,
        )
        assert out.datasource_id == "ds-other"

    def test_describe_outcome_datasource_from_input(self, svc):
        b = InputBundle(meta=_meta(self.COLS, ds="ds-other"), rows=[[1, Decimal("2")]])
        out = svc._describe(
            DescribeParams(columns=["x"]), ["r1"], load_bundle=lambda r: b
        )
        assert out.datasource_id == "ds-other"


class TestShare:
    def test_zero_denominator_all_null_with_warning(self, svc):
        cols = [{"name": "k", "pg_type": "int4"}, {"name": "x", "pg_type": "numeric"}]
        b = InputBundle(meta=_meta(cols), rows=[[1, Decimal("0")], [1, Decimal("0")]])
        out = svc._share(
            ShareParams(value_column="x"),
            [b],
            CTX,
            allow_pushdown=True,
            run_pushdown=_no_pushdown,
        )
        assert out.rows == [[None], [None]]
        assert any("分母" in w for w in out.warnings)
