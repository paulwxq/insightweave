"""TimeResolver 单元测试（docs/v4/5 §7.5、§18.2 R01–R03/F01）

固定时钟 2026-10-13 12:00:00+08:00（conftest.db_agent_context）。
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from src.modules.db_deepagent.context import DbAgentContext
from src.modules.db_deepagent.services.time_resolver import TimeResolver

pytestmark = pytest.mark.unit


@pytest.fixture()
def resolver() -> TimeResolver:
    return TimeResolver()


@pytest.fixture()
def ctx(db_agent_context: DbAgentContext) -> DbAgentContext:
    """user_texts 覆盖各类原文；其余字段沿用固定时钟上下文"""
    return replace(
        db_agent_context,
        user_texts=(
            "今天有多少订单",
            "本月销售额",
            "2026年9月的订单",
            "最近 7 天的退款",
            "最近三十天的订单",
            "上周订单量",
            "当前订单状态分布",
            "2026年10月1日之前，不含当天",
            "截至2026年10月1日，含当天",
            "从2026年10月1日起的订单",
            "2026年10月1日之后的订单，不含当天",
            "2026年9月1日到15日的订单",
        ),
    )


def resolve(resolver, ctx, **kwargs):
    return resolver.resolve(ctx, **kwargs)


class TestSubstringAndAmbiguity:
    def test_time_text_not_in_question(self, resolver, ctx):
        """source_text 不在原文中 → time_text_not_in_question（R01 相关）。"""
        r = resolve(
            resolver, ctx, source_text="上个月", kind="relative", relative="last_month"
        )
        assert not r.ok
        assert r.error_code == "time_text_not_in_question"

    def test_bare_current_rejected(self, resolver, ctx):
        """R01：裸“当前”不能解释为今天。"""
        r = resolve(
            resolver,
            ctx,
            source_text="当前订单状态分布",
            kind="relative",
            relative="today",
        )
        assert not r.ok
        assert r.error_code == "ambiguous_time_reference"
        assert r.hint

    def test_current_month_allowed(self, resolver, ctx):
        """“当前月”带时间单位证据，可映射。"""
        r = resolve(
            resolver,
            replace(ctx, user_texts=("当前月的订单",)),
            source_text="当前月",
            kind="relative",
            relative="this_month",
        )
        assert r.ok, (r.error_code, r.message)


class TestTimeUnitGate:
    def test_no_time_unit_in_text(self, resolver, ctx):
        r = resolve(
            resolver,
            replace(ctx, user_texts=("订单状态分布",)),
            source_text="订单状态分布",
            kind="explicit_range",
            start_date="2026-09-01",
            end_date_inclusive="2026-09-30",
        )
        assert not r.ok
        assert r.error_code == "time_params_mismatch"


class TestRelativeBoundaries:
    """边界值对齐 §7.5 固定时钟示例。"""

    def test_today(self, resolver, ctx):
        r = resolve(
            resolver, ctx, source_text="今天", kind="relative", relative="today"
        )
        assert r.ok
        assert (r.evidence["start"], r.evidence["end"]) == ("2026-10-13", "2026-10-14")
        assert r.evidence["grain"] == "day"

    def test_this_month(self, resolver, ctx):
        r = resolve(
            resolver, ctx, source_text="本月", kind="relative", relative="this_month"
        )
        assert r.ok
        assert (r.evidence["start"], r.evidence["end"]) == ("2026-10-01", "2026-11-01")

    def test_calendar_september(self, resolver, ctx):
        """F01：2026年9月。"""
        r = resolve(
            resolver, ctx, source_text="2026年9月", kind="calendar", year=2026, month=9
        )
        assert r.ok
        assert (r.evidence["start"], r.evidence["end"]) == ("2026-09-01", "2026-10-01")

    def test_last_n_days_with_arabic_digit(self, resolver, ctx):
        """最近 7 天 → [2026-10-07, 2026-10-14)。"""
        r = resolve(
            resolver,
            ctx,
            source_text="最近 7 天",
            kind="relative",
            relative="last_n_days",
            n=7,
        )
        assert r.ok
        assert (r.evidence["start"], r.evidence["end"]) == ("2026-10-07", "2026-10-14")

    def test_last_n_days_with_chinese_digit(self, resolver, ctx):
        r = resolve(
            resolver,
            ctx,
            source_text="最近三十天",
            kind="relative",
            relative="last_n_days",
            n=30,
        )
        assert r.ok
        assert (r.evidence["start"], r.evidence["end"]) == ("2026-09-14", "2026-10-14")

    def test_last_n_days_param_mismatch(self, resolver, ctx):
        """N 由代码提取；原文 7、参数 30 必须拒绝。"""
        r = resolve(
            resolver,
            ctx,
            source_text="最近 7 天",
            kind="relative",
            relative="last_n_days",
            n=30,
        )
        assert not r.ok
        assert r.error_code == "time_params_mismatch"
        assert "7" in (r.hint or "")

    def test_last_n_days_vague(self, resolver, ctx):
        """“最近几天”无 N → 澄清。"""
        r = resolve(
            resolver,
            replace(ctx, user_texts=("最近几天的订单",)),
            source_text="最近几天",
            kind="relative",
            relative="last_n_days",
            n=7,
        )
        assert not r.ok
        assert r.error_code == "time_params_mismatch"

    def test_last_n_weeks_not_mapped(self, resolver, ctx):
        """最近 N 周第一阶段不映射。"""
        r = resolve(
            resolver,
            replace(ctx, user_texts=("最近 2 周的订单",)),
            source_text="最近 2 周",
            kind="relative",
            relative="last_n_days",
            n=14,
        )
        assert not r.ok
        assert r.error_code == "time_params_mismatch"

    def test_relative_word_param_mismatch(self, resolver, ctx):
        """原文“上周”、参数 this_week → 拒绝。"""
        r = resolve(
            resolver, ctx, source_text="上周", kind="relative", relative="this_week"
        )
        assert not r.ok
        assert r.error_code == "time_params_mismatch"

    def test_calendar_digit_mismatch(self, resolver, ctx):
        """原文 9 月、参数 month=10 → 拒绝。"""
        r = resolve(
            resolver,
            ctx,
            source_text="2026年9月",
            kind="calendar",
            year=2026,
            month=10,
        )
        assert not r.ok
        assert r.error_code == "time_params_mismatch"


class TestExplicitRange:
    def test_component_inheritance(self, resolver, ctx):
        """9月1日到15日：第二端点继承年份与月份（缺省 2026）。"""
        r = resolve(
            resolver,
            ctx,
            source_text="2026年9月1日到15日",
            kind="explicit_range",
            start_date="2026-09-01",
            end_date_inclusive="2026-09-15",
        )
        assert r.ok, (r.error_code, r.message)
        assert (r.evidence["start"], r.evidence["end"]) == ("2026-09-01", "2026-09-16")

    def test_year_not_determinable_rejected(self, resolver, ctx):
        """原文全年份缺失且不可唯一确定 → 拒绝，不猜测边界。"""
        r = resolve(
            resolver,
            replace(ctx, user_texts=("9月1日到15日的订单",)),
            source_text="9月1日到15日",
            kind="explicit_range",
            start_date="2026-09-01",
            end_date_inclusive="2026-09-15",
        )
        assert not r.ok
        assert r.error_code == "time_params_mismatch"


class TestOpenRange:
    def test_before_exclusive(self, resolver, ctx):
        r = resolve(
            resolver,
            ctx,
            source_text="2026年10月1日之前，不含当天",
            kind="open_range",
            boundary_date="2026-10-01",
            relation="before",
        )
        assert r.ok, (r.error_code, r.message)
        assert (r.evidence["start"], r.evidence["end"]) == (None, "2026-10-01")

    def test_through_inclusive(self, resolver, ctx):
        r = resolve(
            resolver,
            ctx,
            source_text="截至2026年10月1日，含当天",
            kind="open_range",
            boundary_date="2026-10-01",
            relation="through",
        )
        assert r.ok
        assert (r.evidence["start"], r.evidence["end"]) == (None, "2026-10-02")

    def test_from_inclusive(self, resolver, ctx):
        r = resolve(
            resolver,
            ctx,
            source_text="从2026年10月1日起的订单",
            kind="open_range",
            boundary_date="2026-10-01",
            relation="from",
        )
        assert r.ok
        assert (r.evidence["start"], r.evidence["end"]) == ("2026-10-01", None)

    def test_after_exclusive(self, resolver, ctx):
        r = resolve(
            resolver,
            ctx,
            source_text="2026年10月1日之后的订单，不含当天",
            kind="open_range",
            boundary_date="2026-10-01",
            relation="after",
        )
        assert r.ok
        assert (r.evidence["start"], r.evidence["end"]) == ("2026-10-02", None)

    def test_boundary_date_mismatch(self, resolver, ctx):
        """boundary_date 与原文日期不一致 → 拒绝。"""
        r = resolve(
            resolver,
            ctx,
            source_text="2026年10月1日之前，不含当天",
            kind="open_range",
            boundary_date="2026-10-02",
            relation="before",
        )
        assert not r.ok
        assert r.error_code == "time_params_mismatch"


class TestContextYearInheritance:
    """§7.5：年份缺省时取 user_texts 全文唯一年份（F06/F12 验收根因）。"""

    def test_month_without_year_inherits_unique_context_year(self, resolver, ctx):
        """F12：上一轮“2026 年 9 月”，本轮“再看 8 月”——8 月继承 2026。"""
        r = resolve(
            resolver,
            replace(
                ctx,
                user_texts=(
                    "统计2026年9月销售额",
                    "再看 8 月，用相同口径计算两个月差额",
                ),
            ),
            source_text="8 月",
            kind="calendar",
            month=8,
        )
        assert r.ok, (r.error_code, r.message)
        assert (r.evidence["start"], r.evidence["end"]) == ("2026-08-01", "2026-09-01")

    def test_same_sentence_year_covers_bare_month(self, resolver, ctx):
        """F06：“2026 年 8 月与 9 月”中单独解析“9 月”。"""
        r = resolve(
            resolver,
            replace(ctx, user_texts=("对比2026年8月与9月销售额",)),
            source_text="9月",
            kind="calendar",
            month=9,
        )
        assert r.ok, (r.error_code, r.message)
        assert (r.evidence["start"], r.evidence["end"]) == ("2026-09-01", "2026-10-01")

    def test_multiple_distinct_years_still_rejected(self, resolver, ctx):
        """上下文出现多个不同年份 → 不可唯一确定，仍拒绝。"""
        r = resolve(
            resolver,
            replace(
                ctx, user_texts=("2025年12月的订单", "2026年1月的订单", "再看 8 月")
            ),
            source_text="8 月",
            kind="calendar",
            month=8,
        )
        assert not r.ok and r.error_code == "time_params_mismatch"

    def test_explicit_year_matching_context_allowed_in_substring(self, resolver, ctx):
        """参数 year=2026（原文子串无年份）＝上下文唯一年 → 放行（F06/F08 根因）。"""
        r = resolve(
            resolver,
            replace(ctx, user_texts=("对比2026年8月与9月销售额，计算环比。",)),
            source_text="9月",
            kind="calendar",
            year=2026,
            month=9,
        )
        assert r.ok, (r.error_code, r.message)
        assert (r.evidence["start"], r.evidence["end"]) == ("2026-09-01", "2026-10-01")

    def test_explicit_year_conflicting_context_rejected(self, resolver, ctx):
        """参数年≠上下文唯一年（或上下文多年份）→ 仍拒绝。"""
        r = resolve(
            resolver,
            replace(ctx, user_texts=("对比2026年8月与9月销售额，计算环比。",)),
            source_text="9月",
            kind="calendar",
            year=2025,
            month=9,
        )
        assert not r.ok and "year" in r.message
