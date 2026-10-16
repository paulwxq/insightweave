"""SqlEvidenceGuard 单元测试（docs/v4/5 §9、§18.2 §9.9 反例集）

覆盖：时间规则（半开算子/边界一致/空区间/链式证据/OR 分支/CASE/FILTER 合并/
NOT 翻转/实时时钟）、单边证据、维度规则（归属/取值/模式/空串豁免/谓词形态）、
来源追踪（CTE 字面量/拼接/VALUES/unnest/UNION/MAX 数据派生）、generate_series
受控来源、used_evidence 快照。
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from src.modules.db_deepagent.config import GuardCheckConfig
from src.modules.db_deepagent.context import DbAgentContext
from src.modules.db_deepagent.schemas import DimEvidence, TimeEvidence
from src.modules.db_deepagent.services.dimension_probe import DimensionProbeBuilder
from src.modules.db_deepagent.services.sql_guard import SqlEvidenceGuard

pytestmark = pytest.mark.unit


class FakeGuardDb:
    """§9.9 反例集用列结构：sales.orders / sales.refunds / sales.x"""

    _TABLES = {
        ("sales", "orders"): [
            "id",
            "status",
            "created_at",
            "channel",
            "customer_id",
            "name",
            "amount",
            "d",
        ],
        ("sales", "refunds"): [
            "id",
            "status",
            "created_at",
            "channel",
            "customer_id",
            "name",
            "amount",
            "d",
        ],
        ("sales", "x"): ["id", "status", "max_status"],
    }

    def get_columns(self, schema: str, table: str):
        names = self._TABLES.get((schema, table), [])
        return [type("C", (), {"column_name": n})() for n in names]

    def column_type(self, schema: str, table: str, column: str) -> str | None:
        if column == "name":
            return "text"
        if column in ("status", "channel"):
            return "varchar"
        return "integer"


def _ctx() -> DbAgentContext:
    return DbAgentContext(
        session_id="s",
        thread_id="t",
        request_id="r",
        run_id="run-1",
        datasource_id="ds",
        validation_fingerprint="fp",
        now=datetime(2026, 10, 13, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
        timezone="Asia/Shanghai",
        user_texts=("q",),
        carried_evidence=(),
        deadline_monotonic=0.0,
    )


def _te(evidence_id: str, start: str | None, end: str | None) -> TimeEvidence:
    return TimeEvidence(
        time_evidence_id=evidence_id,
        run_id="run-1",
        source_text="x",
        grain="month",
        start=start,
        end=end,
        timezone="Asia/Shanghai",
        datasource_id="ds",
        inherited_from=None,
    )


def _de(
    table: str,
    column: str,
    values: list[str],
    *,
    source: str = "milvus_dim_hit",
    patterns: list[str] | None = None,
    ds: str = "ds",
) -> DimEvidence:
    return DimEvidence(
        dim_evidence_id=f"d-{table}.{column}",
        run_id="run-1",
        source=source,  # type: ignore[typeddict-item]
        table=table,
        column=column,
        values=values,
        patterns=patterns or [],
        query_text=None,
        ref=None,
        datasource_id=ds,
        inherited_from=None,
    )


AUG = _te("T-aug", "2026-08-01", "2026-09-01")
SEP = _te("T-sep", "2026-09-01", "2026-10-01")
UPPER = _te("T-up", None, "2026-10-01")
LOWER = _te("T-lo", "2026-10-01", None)
DIM = _de("sales.orders", "status", ["已支付"])


@pytest.fixture()
def guard() -> SqlEvidenceGuard:
    cfg = GuardCheckConfig(
        enabled=True,
        forbid_realtime_clock_in_filters=True,
        require_time_evidence=True,
        require_dimension_evidence=True,
    )
    return SqlEvidenceGuard(cfg, FakeGuardDb(), DimensionProbeBuilder())


def check(guard, sql, t_ev=(), d_ev=()):
    return guard.check(_ctx(), sql, time_evidence=list(t_ev), dim_evidence=list(d_ev))


def expect_pass(guard, sql, t_ev=(), d_ev=()):
    r = check(guard, sql, t_ev, d_ev)
    assert r.ok, f"应通过: {r.error_codes}: {r.errors}"


def expect_code(guard, code, sql, t_ev=(), d_ev=()):
    r = check(guard, sql, t_ev, d_ev)
    assert (
        not r.ok and code in r.error_codes
    ), f"应报 {code}，实际 {r.error_codes}: {r.errors}"
    return r


# ============================================================================
# 时间规则（§9.4）
# ============================================================================


class TestTimeRules:
    def test_or_branches_each_evidenced(self, guard):
        """OR 两个分支各自成链：各分支独立通过（§9.4 分组拆分）。"""
        expect_pass(
            guard,
            "SELECT 1 FROM sales.orders WHERE (d >= '2026-08-01' AND d < '2026-09-01') "
            "OR (d >= '2026-09-01' AND d < '2026-10-01')",
            [AUG, SEP],
        )

    def test_empty_range_same_owner(self, guard):
        expect_code(
            guard,
            "empty_time_range",
            "SELECT 1 FROM sales.orders WHERE d >= '2026-09-01' AND d < '2026-09-01'",
            [AUG, SEP],
        )

    def test_aug_match(self, guard):
        expect_pass(
            guard,
            "SELECT 1 FROM sales.orders WHERE d >= '2026-08-01' AND d < '2026-09-01'",
            [AUG, SEP],
        )

    def test_sep_match(self, guard):
        expect_pass(
            guard,
            "SELECT 1 FROM sales.orders WHERE d >= '2026-09-01' AND d < '2026-10-01'",
            [AUG, SEP],
        )

    def test_lower_bound_mismatch_plus_literal(self, guard):
        """下界与证据不符：mismatch 且字面量无证据支撑。"""
        r = expect_code(
            guard,
            "time_lower_bound_mismatch",
            "SELECT 1 FROM sales.orders WHERE d >= '2026-09-01' AND d < '2026-09-15'",
            [AUG],
        )
        assert "unsupported_time_literal" in r.error_codes

    @pytest.mark.parametrize("op", [">", "<=", "="])
    def test_non_half_open_operators(self, guard, op):
        expect_code(
            guard,
            "time_operator_not_half_open",
            f"SELECT 1 FROM sales.orders WHERE d {op} '2026-09-01'",
            [SEP],
        )

    def test_literal_left_reversed_always_false_rejected(self, guard):
        """字面量在左的恒假反向区间不得通过（'v' >= col ⇔ col <= v）。"""
        expect_code(
            guard,
            "time_operator_not_half_open",
            "SELECT 1 FROM sales.orders "
            "WHERE '2026-09-01' >= d AND '2026-10-01' < d",
            [SEP],
        )

    def test_literal_left_equivalent_half_open_passes(self, guard):
        """合法等价反向形式：'v1' <= col AND 'v2' > col ⇔ col >= v1 AND col < v2。"""
        expect_pass(
            guard,
            "SELECT 1 FROM sales.orders "
            "WHERE '2026-09-01' <= d AND '2026-10-01' > d",
            [SEP],
        )

    def test_literal_left_single_sided_upper_passes(self, guard):
        """'v' > col ⇔ col < v：单边上界证据通过。"""
        expect_pass(
            guard,
            "SELECT 1 FROM sales.orders WHERE '2026-10-01' > d",
            [UPPER],
        )

    def test_between_time(self, guard):
        expect_code(
            guard,
            "time_operator_not_half_open",
            "SELECT 1 FROM sales.orders WHERE d BETWEEN '2026-09-01' AND '2026-09-30'",
            [SEP],
        )

    def test_union_two_months(self, guard):
        """[08-01,10-01) 由 AUG+SEP 两条证据拼链通过。"""
        expect_pass(
            guard,
            "SELECT 1 FROM sales.orders WHERE d >= '2026-08-01' AND d < '2026-10-01'",
            [AUG, SEP],
        )

    def test_union_missing_month(self, guard):
        expect_code(
            guard,
            "unsupported_time_literal",
            "SELECT 1 FROM sales.orders WHERE d >= '2026-08-01' AND d < '2026-10-01'",
            [SEP],
        )

    def test_case_lower_only(self, guard):
        expect_code(
            guard,
            "time_range_not_evidenced",
            "SELECT CASE WHEN d >= '2026-09-01' THEN 1 ELSE 0 END AS f "
            "FROM sales.orders",
            [SEP],
        )

    def test_filter_where_union(self, guard):
        """FILTER 条件与 WHERE 必要合取合并成链。"""
        expect_pass(
            guard,
            "SELECT COUNT(*) FILTER (WHERE d < '2026-09-01') FROM sales.orders "
            "WHERE d >= '2026-08-01' AND d < '2026-10-01'",
            [AUG, SEP],
        )

    def test_time_under_not(self, guard):
        expect_code(
            guard,
            "time_predicate_under_not",
            "SELECT 1 FROM sales.orders WHERE NOT (d >= '2026-09-01')",
            [SEP],
        )

    @pytest.mark.parametrize(
        "expr",
        [
            "CURRENT_DATE",
            "now()",
            "clock_timestamp()",
            "LOCALTIMESTAMP",
        ],
    )
    def test_realtime_clock(self, guard, expr):
        expect_code(
            guard,
            "realtime_clock_in_filter",
            f"SELECT 1 FROM sales.orders WHERE d >= {expr}",
        )


# ============================================================================
# 单边证据（§9.4）
# ============================================================================


class TestSingleSideRules:
    def test_upper_only_evidence(self, guard):
        expect_pass(guard, "SELECT 1 FROM sales.orders WHERE d < '2026-10-01'", [UPPER])

    def test_upper_evidence_wrong_side(self, guard):
        expect_code(
            guard,
            "time_lower_bound_mismatch",
            "SELECT 1 FROM sales.orders WHERE d >= '2026-10-01'",
            [UPPER],
        )

    def test_lower_only_evidence(self, guard):
        expect_pass(
            guard, "SELECT 1 FROM sales.orders WHERE d >= '2026-10-01'", [LOWER]
        )

    def test_finite_evidence_not_upper(self, guard):
        """有限区间证据不能当单边上界用。"""
        expect_code(
            guard,
            "time_range_not_evidenced",
            "SELECT 1 FROM sales.orders WHERE d < '2026-10-01'",
            [SEP],
        )

    def test_two_one_side_not_chainable(self, guard):
        """两个单边证据不能拼成有限区间。"""
        expect_code(
            guard,
            "time_range_not_evidenced",
            "SELECT 1 FROM sales.orders WHERE d >= '2026-10-01' AND d < '2026-11-01'",
            [LOWER, UPPER],
        )


# ============================================================================
# 维度规则（§9.5）
# ============================================================================


class TestDimRules:
    def test_unqualified_column_join(self, guard):
        expect_code(
            guard,
            "column_owner_unresolved",
            "SELECT 1 FROM sales.orders o JOIN sales.refunds r ON o.id = r.id "
            "WHERE status = '已支付'",
            [AUG],
            [DIM],
        )

    def test_wrong_table_value(self, guard):
        expect_code(
            guard,
            "unsupported_dimension_value",
            "SELECT 1 FROM sales.orders o JOIN sales.refunds r ON o.id = r.id "
            "WHERE r.status = '已支付'",
            [AUG],
            [DIM],
        )

    def test_evidenced_value(self, guard):
        expect_pass(
            guard,
            "SELECT 1 FROM sales.orders o WHERE o.status = '已支付'",
            d_ev=[DIM],
        )

    def test_case_dim(self, guard):
        expect_code(
            guard,
            "unsupported_dimension_value",
            "SELECT CASE WHEN o.status = '未登记值' THEN 1 END FROM sales.orders o",
            d_ev=[DIM],
        )

    def test_filter_dim(self, guard):
        expect_code(
            guard,
            "unsupported_dimension_value",
            "SELECT COUNT(*) FILTER (WHERE o.channel = '无证据值') "
            "FROM sales.orders o",
            d_ev=[DIM],
        )

    def test_pattern_without_probe_evidence(self, guard):
        expect_code(
            guard,
            "unsupported_pattern_filter",
            "SELECT 1 FROM sales.orders o WHERE o.status LIKE '%系统%'",
            d_ev=[DIM],
        )

    def test_pattern_with_probe_evidence(self, guard):
        expect_pass(
            guard,
            "SELECT 1 FROM sales.orders o WHERE o.status LIKE '%系统%'",
            d_ev=[
                _de(
                    "sales.orders", "status", [], source="db_probe", patterns=["%系统%"]
                )
            ],
        )

    def test_date_trunc_predicate(self, guard):
        expect_code(
            guard,
            "unsupported_predicate_form",
            "SELECT 1 FROM sales.orders o "
            "WHERE date_trunc('month', o.created_at) = '2026-09-01'",
        )

    def test_any_array_unqualified(self, guard):
        expect_code(
            guard,
            "unsupported_predicate_form",
            "SELECT 1 FROM sales.orders o WHERE x = ANY(ARRAY['a'])",
        )

    def test_any_array_qualified(self, guard):
        expect_code(
            guard,
            "unsupported_predicate_form",
            "SELECT 1 FROM sales.orders o WHERE o.channel = ANY(ARRAY['a'])",
        )

    @pytest.mark.parametrize("op", ["=", "<>", "!="])
    def test_empty_string_exemption(self, guard, op):
        """文本列空串比较豁免（name 为 text）。"""
        expect_pass(guard, f"SELECT 1 FROM sales.orders o WHERE o.name {op} ''")

    @pytest.mark.parametrize("form", ["IS NULL", "IS NOT NULL"])
    def test_null_predicates_allowed(self, guard, form):
        expect_pass(guard, f"SELECT 1 FROM sales.orders o WHERE o.name {form}")

    def test_space_is_not_empty_string(self, guard):
        expect_code(
            guard,
            "unsupported_dimension_value",
            "SELECT 1 FROM sales.orders o WHERE o.name <> ' '",
        )

    def test_like_empty_pattern(self, guard):
        expect_code(
            guard,
            "unsupported_pattern_filter",
            "SELECT 1 FROM sales.orders o WHERE o.name LIKE ''",
        )

    def test_in_empty_string_not_exempt(self, guard):
        expect_code(
            guard,
            "unsupported_dimension_value",
            "SELECT 1 FROM sales.orders o WHERE o.name IN ('')",
        )

    def test_trim_wrapped(self, guard):
        expect_code(
            guard,
            "unsupported_predicate_form",
            "SELECT 1 FROM sales.orders o WHERE TRIM(o.name) <> ''",
        )

    def test_coalesce_wrapped(self, guard):
        expect_code(
            guard,
            "unsupported_predicate_form",
            "SELECT 1 FROM sales.orders o WHERE COALESCE(o.name, '') = ''",
        )

    def test_not_in_evidenced(self, guard):
        expect_pass(
            guard,
            "SELECT 1 FROM sales.orders o WHERE o.status NOT IN ('已支付')",
            d_ev=[DIM],
        )


# ============================================================================
# 来源追踪（§9.3、§9.6）
# ============================================================================


class TestSourceTracing:
    def test_cte_literal_uncovered(self, guard):
        expect_code(
            guard,
            "unsupported_dimension_value",
            "WITH f AS (SELECT '系统'::text AS v) SELECT COUNT(*) FROM sales.orders o "
            "JOIN f ON o.status = f.v",
        )

    def test_cte_literal_evidenced(self, guard):
        expect_pass(
            guard,
            "WITH f AS (SELECT '已支付'::text AS v) SELECT COUNT(*) FROM sales.orders o "
            "JOIN f ON o.status = f.v",
            d_ev=[DIM],
        )

    def test_concat_projection(self, guard):
        expect_code(
            guard,
            "constant_source_unsupported",
            "SELECT * FROM (SELECT '系' || '统' AS v) f "
            "JOIN sales.orders o ON o.status = f.v",
        )

    def test_values_source(self, guard):
        expect_code(
            guard,
            "constant_source_unsupported",
            "SELECT * FROM (VALUES ('系统')) AS f(v) "
            "JOIN sales.orders o ON o.status = f.v",
        )

    def test_unnest_source(self, guard):
        expect_code(
            guard,
            "constant_source_unsupported",
            "SELECT * FROM unnest(ARRAY['系统']) AS t(v) "
            "JOIN sales.orders o ON o.status = t.v",
        )

    def test_in_subquery_literal(self, guard):
        expect_code(
            guard,
            "unsupported_dimension_value",
            "SELECT 1 FROM sales.orders o WHERE o.status IN (SELECT '系统')",
        )

    def test_scalar_subquery_literal(self, guard):
        expect_code(
            guard,
            "unsupported_dimension_value",
            "SELECT 1 FROM sales.orders o WHERE o.status = (SELECT '系统')",
        )

    def test_union_literal_branch(self, guard):
        expect_code(
            guard,
            "unsupported_dimension_value",
            "WITH u AS (SELECT status FROM sales.x UNION SELECT '系统' AS status) "
            "SELECT * FROM sales.orders o JOIN u ON o.status = u.status",
        )

    def test_max_aggregate_is_data(self, guard):
        """MAX 聚合结果是数据派生，不按字面量检查。"""
        expect_pass(
            guard,
            "WITH f AS (SELECT MAX(status) AS max_status FROM sales.x) "
            "SELECT * FROM sales.orders o JOIN f ON o.status = f.max_status",
        )

    def test_expr_projection_owner_unresolved(self, guard):
        expect_code(
            guard,
            "column_owner_unresolved",
            "SELECT 1 FROM (SELECT upper(status) AS s FROM sales.orders) t "
            "WHERE t.s = 'x'",
        )


# ============================================================================
# generate_series 受控来源（§9.8）
# ============================================================================

_F03 = (
    "SELECT d.day FROM generate_series('2026-09-01'::date, '2026-09-30', "
    "interval '1 day') AS d(day) LEFT JOIN sales.orders o "
    "ON o.created_at >= d.day AND o.created_at < d.day + interval '1 day'"
)


class TestGenerateSeries:
    def test_f03_controlled(self, guard):
        expect_pass(guard, _F03, [SEP])

    def test_wrong_end_arg(self, guard):
        expect_code(
            guard,
            "constant_source_unsupported",
            _F03.replace("'2026-09-30'", "'2026-10-15'"),
            [SEP],
        )

    def test_no_evidence(self, guard):
        expect_code(guard, "constant_source_unsupported", _F03)

    def test_wrong_step(self, guard):
        bad = _F03.replace(
            "interval '1 day') AS d(day)", "interval '2 days') AS d(day)"
        )
        expect_code(guard, "constant_source_unsupported", bad, [SEP])

    def test_derived_column_series(self, guard):
        """投影列里的 series 不在受控形态内（须 FROM 表函数）。"""
        expect_code(
            guard,
            "constant_source_unsupported",
            "SELECT * FROM (SELECT generate_series('2026-09-01'::date, '2026-09-30', "
            "interval '1 day') AS day) d WHERE d.day < '2026-09-02'",
            [SEP],
        )


# ============================================================================
# 其他谓词形态与快照
# ============================================================================


class TestMisc:
    def test_column_vs_column(self, guard):
        expect_pass(
            guard,
            "SELECT 1 FROM sales.orders o JOIN sales.refunds r ON o.id = r.id "
            "WHERE o.status = r.status",
        )

    def test_in_subquery_column(self, guard):
        expect_pass(
            guard,
            "SELECT 1 FROM sales.orders o WHERE o.id IN (SELECT customer_id FROM sales.orders)",
        )

    def test_exists_correlated(self, guard):
        expect_pass(
            guard,
            "SELECT 1 FROM sales.orders o "
            "WHERE EXISTS (SELECT 1 FROM sales.refunds r WHERE r.id = o.id)",
        )

    def test_case_in_order_by(self, guard):
        expect_pass(
            guard,
            "SELECT o.status FROM sales.orders o "
            "ORDER BY CASE WHEN o.status = '已支付' THEN 1 ELSE 2 END",
            d_ev=[DIM],
        )

    def test_used_evidence_snapshot(self, guard):
        r = check(
            guard,
            "SELECT o.status FROM sales.orders o WHERE o.status = '已支付'",
            d_ev=[DIM],
        )
        assert r.ok
        assert r.used_evidence["dim"][0]["dim_evidence_id"] == "d-sales.orders.status"
        assert r.used_evidence["time"] == []
