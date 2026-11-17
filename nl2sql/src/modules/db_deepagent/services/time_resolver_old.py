"""【已退役 2026-11-15】规则词表版时间解析器（docs/v4/5 §7.5）。

被 LLM 版（services/time_resolver.py，flash 模型 + 半开区间约定，doc 16）取代。
保留仅供新旧同题对比跑分（test_time_resolver_compare.py），勿在新代码中引用。
原说明：有限区间统一 [start, end)；单边区间恰有一端 None。所有与原文的一致性
核对（相对词、显式数字、last_n_days 的 N、open_range 的方向与包含关系）由代码
完成，不信任模型参数。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional

from src.modules.db_deepagent.context import DbAgentContext
from src.modules.db_deepagent.schemas import TimeEvidence
from src.utils.logger import get_module_logger

logger = get_module_logger("nl2sql.db_deepagent.time_resolver")

# §7.5 第 2/3 步的时间单位证据正则（与文档逐字一致）
TIME_UNIT_EVIDENCE = re.compile(
    r"\d{4}-\d{1,2}-\d{1,2}|\d{4}\s*年|\d{1,2}\s*月|\d{1,2}\s*[日号]|季度|Q[1-4]"
    r"|[本上下这]\s*(?:个)?\s*(?:月|周|星期|季度|年)|今[天日年]|昨[天日]|前天"
    r"|最近|过去|近\s*\d+\s*[天日周月年]|当前\s*(?:月|周|季度|年)|当月|当年|当天|当日"
)

_BARE_CURRENT = re.compile(r"当前|目前|现在")

# 相对词 → relative 参数（"以此类推"补全；前天等无对应参数的词不入表）
_RELATIVE_WORDS: dict[str, str] = {
    "今天": "today",
    "当天": "today",
    "当日": "today",
    "昨天": "yesterday",
    "昨日": "yesterday",
    "本周": "this_week",
    "这周": "this_week",
    "本星期": "this_week",
    "这星期": "this_week",
    "这一周": "this_week",
    "当前周": "this_week",
    "上周": "last_week",
    "上星期": "last_week",
    "上一周": "last_week",
    "本月": "this_month",
    "这个月": "this_month",
    "当月": "this_month",
    "当前月": "this_month",
    "这月": "this_month",
    "上月": "last_month",
    "上个月": "last_month",
    "本季度": "this_quarter",
    "这个季度": "this_quarter",
    "当前季度": "this_quarter",
    "这季度": "this_quarter",
    "上季度": "last_quarter",
    "上个季度": "last_quarter",
    "今年": "this_year",
    "本年": "this_year",
    "当年": "this_year",
    "当前年": "this_year",
    "这一年": "this_year",
    "去年": "last_year",
    "上一年": "last_year",
    "上年": "last_year",
}

# last_n_days 的 N 提取（§7.5 第 4 步；N 由代码提取，不信任模型参数）
_LAST_N_DAYS = re.compile(
    r"(?:最近|过去|近)\s*(\d+|[零一二两三四五六七八九十百]+)\s*(?:个)?\s*(?:天|日)"
)
# “最近 N 周/个月/年”第一阶段不映射
_LAST_N_OTHER_UNIT = re.compile(
    r"(?:最近|过去|近)\s*(?:\d+|[零一二两三四五六七八九十百]+)\s*(?:个)?\s*(?:周|星期|个月|月|年)"
)
# 最近几天 / 最近一段时间：有天/日方向但无数词
_LAST_N_VAGUE = re.compile(r"(?:最近|过去)\s*(?:几|一段|多)")

_CN_DIGITS = {
    "零": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}

_YEAR_RE = re.compile(r"(\d{4})\s*年")
_MONTH_RE = re.compile(r"(\d{1,2})\s*月")
_DAY_RE = re.compile(r"(\d{1,2})\s*[日号]")
_QUARTER_RE = re.compile(
    r"(?:Q([1-4])|第\s*([1-4])\s*季度|([1-4])\s*季度)", re.IGNORECASE
)
# 日期 token（open_range / explicit_range 用）：YYYY年M月D日 / M月D日 / D日 / ISO
# 缺省分量（如“9 月 1 日到 15 日”的第二端）由前一个完整日期继承
_DATE_TOKEN = re.compile(
    r"(?:(\d{4})\s*年)?\s*(?:(\d{1,2})\s*月)?\s*(\d{1,2})\s*[日号]|(\d{4})-(\d{1,2})-(\d{1,2})"
)

# open_range 方向词（§7.5：包含关系必须显式，歧义即澄清）
_DIR_BEFORE = re.compile(r"之前|以前")
_DIR_AFTER = re.compile(r"之后|以后")
_DIR_THROUGH = re.compile(r"截至|截止")
_DIR_FROM = re.compile(r"起|开始")
_INCL_EXCL = re.compile(r"不含|不包括")
_INCL_INCL = re.compile(r"(?<!不)含|(?<!不)包括")


@dataclass(frozen=True)
class TimeResolveResult:
    """resolve 的结果：ok 时 evidence 非空；失败时 error_code 给规则码"""

    ok: bool
    error_code: Optional[str] = None
    message: str = ""
    hint: Optional[str] = None
    extracted_n: Optional[int] = None
    evidence: Optional[TimeEvidence] = None


_CONTEXT_YEAR_RE = None


def _context_year(ctx) -> int | None:
    """§7.5：年份缺省时，取 user_texts 全文中唯一出现的年份（出现多个不同年份则不可唯一确定）。"""
    import re

    global _CONTEXT_YEAR_RE
    if _CONTEXT_YEAR_RE is None:
        _CONTEXT_YEAR_RE = re.compile(r"(?:19|20)\d{2}")
    years: set[int] = set()
    for text in getattr(ctx, "user_texts", None) or ():
        years.update(int(m.group(0)) for m in _CONTEXT_YEAR_RE.finditer(text))
    return next(iter(years)) if len(years) == 1 else None


def _cn_to_int(text: str) -> int | None:
    """中文数字转整数（支持到三百六十六；无法解析返回 None）。"""
    if text.isdigit():
        return int(text)
    total, current = 0, 0
    for ch in text:
        if ch == "十":
            current = (current or 1) * 10
            total += current
            current = 0
        elif ch == "百":
            current = (current or 1) * 100
            total += current
            current = 0
        elif ch in _CN_DIGITS:
            current = _CN_DIGITS[ch]
        else:
            return None
    return total + current if 0 < total + current <= 366 else None


def _fmt(d: date | None) -> str | None:
    return d.isoformat() if d else None


class TimeResolverOld:
    """【退役】规则词表版时间解析：校验顺序与边界计算严格按 §7.5（doc 16 后仅对比用）"""

    def resolve(
        self,
        ctx: DbAgentContext,
        *,
        source_text: str,
        kind: str,
        relative: str | None = None,
        n: int | None = None,
        year: int | None = None,
        quarter: int | None = None,
        month: int | None = None,
        day: int | None = None,
        start_date: str | None = None,
        end_date_inclusive: str | None = None,
        boundary_date: str | None = None,
        relation: str | None = None,
    ) -> TimeResolveResult:
        # 1. 原文核对：source_text 必须是某条用户原文的子串
        text = source_text.strip()
        if not any(text in (ut or "") for ut in ctx.user_texts):
            return TimeResolveResult(
                False,
                "time_text_not_in_question",
                "source_text 不是用户原文中任何一句的子串",
                hint="逐字引用用户的时间表达；如需沿用前文口径，使用 reuse_evidence",
            )

        # 2. 裸“当前”判定
        if _BARE_CURRENT.search(text) and not TIME_UNIT_EVIDENCE.search(text):
            return TimeResolveResult(
                False,
                "ambiguous_time_reference",
                "裸“当前”不能解释为今天",
                hint="状态类问题不加时间过滤；确需时间范围时向用户澄清",
            )

        # 3. 必须含时间单位证据
        if not TIME_UNIT_EVIDENCE.search(text):
            return TimeResolveResult(
                False,
                "time_params_mismatch",
                "原文不含可识别的时间单位证据",
                hint="逐字引用用户的时间表达（如“2026 年 9 月”“本月”）",
            )

        # 4. 参数与原文一致性 + 5. 边界计算（按 kind 分派）
        now = ctx.now.astimezone(self._tz(ctx.timezone))
        if kind == "relative":
            return self._resolve_relative(ctx, text, relative, n, now)
        if kind == "calendar":
            return self._resolve_calendar(ctx, text, year, quarter, month, day)
        if kind == "explicit_range":
            return self._resolve_explicit_range(
                ctx, text, start_date, end_date_inclusive
            )
        if kind == "open_range":
            return self._resolve_open_range(ctx, text, boundary_date, relation)
        return TimeResolveResult(
            False, "time_params_mismatch", f"未知的 kind: {kind!r}", None
        )

    # ---------------------------------------------------------------- 相对

    def _resolve_relative(
        self,
        ctx: DbAgentContext,
        text: str,
        relative: str | None,
        n: int | None,
        now: datetime,
    ) -> TimeResolveResult:
        today = now.date()

        # last_n_days：N 由代码提取
        n_matches = _LAST_N_DAYS.findall(text)
        if _LAST_N_OTHER_UNIT.search(text):
            return TimeResolveResult(
                False,
                "time_params_mismatch",
                "“最近 N 周／个月／年”第一阶段不映射到 last_n_days",
                hint="周／月起止口径有多种理解，向用户确认具体日期范围",
            )
        if len(n_matches) == 1:
            extracted = _cn_to_int(n_matches[0])
            if extracted is None:
                return TimeResolveResult(
                    False,
                    "time_params_mismatch",
                    "无法解析原文中的天数",
                    hint="向用户确认具体天数",
                )
            if relative != "last_n_days":
                return TimeResolveResult(
                    False,
                    "time_params_mismatch",
                    f"原文为最近 {extracted} 天，relative 应为 last_n_days",
                    hint=f"提取到 N={extracted}；relative 参数改为 last_n_days",
                    extracted_n=extracted,
                )
            if n != extracted:
                return TimeResolveResult(
                    False,
                    "time_params_mismatch",
                    f"参数 n={n} 与原文提取的 N={extracted} 不一致",
                    hint=f"代码从原文提取到 N={extracted}，参数 n 必须等于该值",
                    extracted_n=extracted,
                )
            start = today.fromordinal(today.toordinal() - (extracted - 1))
            return self._finite(
                ctx, text, "day", start, today.fromordinal(today.toordinal() + 1)
            )
        if len(n_matches) > 1:
            return TimeResolveResult(
                False,
                "time_params_mismatch",
                "原文中出现多个“最近 N 天”表达",
                hint="向用户确认具体天数",
            )
        if _LAST_N_VAGUE.search(text):
            return TimeResolveResult(
                False,
                "time_params_mismatch",
                "“最近几天／最近一段时间”没有确定天数",
                hint="向用户确认具体天数",
            )

        # 普通相对词
        found = {param for word, param in _RELATIVE_WORDS.items() if word in text}
        if not found:
            return TimeResolveResult(
                False,
                "time_params_mismatch",
                "原文不含可映射的相对时间词（或该词无对应参数，如“前天”）",
                hint="改用 calendar／explicit_range 表达，或向用户澄清",
            )
        if relative not in found:
            return TimeResolveResult(
                False,
                "time_params_mismatch",
                f"relative={relative!r} 与原文相对词不符（原文支持 {sorted(found)}）",
                hint="relative 参数必须与原文相对词对应",
            )
        bounds = {
            "today": (today, _shift(today, 1)),
            "yesterday": (_shift(today, -1), today),
            "this_week": (self._week_start(today), _shift(self._week_start(today), 7)),
            "last_week": (
                _shift(self._week_start(today), -7),
                self._week_start(today),
            ),
            "this_month": self._month_bounds(today),
            "last_month": self._prev_month_bounds(today),
            "this_quarter": self._quarter_bounds(today),
            "last_quarter": self._prev_quarter_bounds(today),
            "this_year": (date(today.year, 1, 1), date(today.year + 1, 1, 1)),
            "last_year": (date(today.year - 1, 1, 1), date(today.year, 1, 1)),
        }
        grain = {
            "today": "day",
            "yesterday": "day",
            "this_week": "week",
            "last_week": "week",
            "this_month": "month",
            "last_month": "month",
            "this_quarter": "quarter",
            "last_quarter": "quarter",
            "this_year": "year",
            "last_year": "year",
        }[relative]
        start, end = bounds[relative]
        return self._finite(ctx, text, grain, start, end)

    # ---------------------------------------------------------------- 日历

    def _resolve_calendar(
        self,
        ctx: DbAgentContext,
        text: str,
        year: int | None,
        quarter: int | None,
        month: int | None,
        day: int | None,
    ) -> TimeResolveResult:
        text_year = _YEAR_RE.search(text)
        text_month = _MONTH_RE.search(text)
        text_day = _DAY_RE.search(text)
        text_quarter = _QUARTER_RE.search(text)
        if text_year and int(text_year.group(1)) != year:
            return self._digit_mismatch("year", text_year.group(1), year)
        if not text_year and year is not None:
            # §7.5 一致性细化：原文无年份时，参数年＝上下文唯一确定年份即放行
            # （如“…2026 年 8 月与 9 月”中单独引用“9 月”并传 year=2026）
            if year != _context_year(ctx):
                return self._not_in_text("year", year)
        if text_month and int(text_month.group(1)) != month:
            return self._digit_mismatch("month", text_month.group(1), month)
        if not text_month and month is not None:
            return self._not_in_text("month", month)
        if text_day and int(text_day.group(1)) != day:
            return self._digit_mismatch("day", text_day.group(1), day)
        if not text_day and day is not None:
            return self._not_in_text("day", day)
        q = None
        if text_quarter:
            q = int(next(g for g in text_quarter.groups() if g))
            if q != quarter:
                return self._digit_mismatch("quarter", q, quarter)
        elif quarter is not None:
            return self._not_in_text("quarter", quarter)
        if year is None:
            year = _context_year(
                ctx
            )  # §7.5：上下文唯一确定年份（如“2026 年 8 月与 9 月”的 9 月）
        if year is None:
            return TimeResolveResult(
                False,
                "time_params_mismatch",
                "calendar 表达缺少年份，无法唯一确定",
                hint="请用户提供年份",
            )
        try:
            if day is not None and month is not None:
                start = date(year, month, day)
                return self._finite(ctx, text, "day", start, _shift(start, 1))
            if month is not None:
                start = date(year, month, 1)
                end = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
                return self._finite(ctx, text, "month", start, end)
            if q is not None:
                start = date(year, 3 * (q - 1) + 1, 1)
                end = date(year + 1, 1, 1) if q == 4 else date(year, 3 * q + 1, 1)
                return self._finite(ctx, text, "quarter", start, end)
            return self._finite(
                ctx, text, "year", date(year, 1, 1), date(year + 1, 1, 1)
            )
        except ValueError as exc:
            return TimeResolveResult(
                False, "time_params_mismatch", f"日期无效: {exc}", None
            )

    # ---------------------------------------------------------------- 区间

    def _resolve_explicit_range(
        self,
        ctx: DbAgentContext,
        text: str,
        start_date: str | None,
        end_date_inclusive: str | None,
    ) -> TimeResolveResult:
        if not start_date or not end_date_inclusive:
            return TimeResolveResult(
                False,
                "time_params_mismatch",
                "explicit_range 需要 start_date 与 end_date_inclusive",
                None,
            )
        try:
            s = _parse_iso_date(start_date)
            e = _parse_iso_date(end_date_inclusive)
        except ValueError as exc:
            return TimeResolveResult(False, "time_params_mismatch", str(exc), None)
        tokens = _extract_dates(text)
        if len(tokens) != 2:
            return TimeResolveResult(
                False,
                "time_params_mismatch",
                "原文未给出区间两端的日期（各一个）",
                hint="区间两端必须逐字出现在原文中",
            )
        first, second = tokens[0], _inherit(tokens[0], tokens[1])
        if first is not None and first[0] is None:
            context_year = _context_year(ctx)
            if context_year is not None:
                first = (context_year, first[1], first[2])
                if second is not None and second[0] is None:
                    second = (context_year, second[1], second[2])
        if first is None or second is None or first[0] is None:
            return TimeResolveResult(
                False,
                "time_params_mismatch",
                "原文区间缺少年份，无法唯一确定",
                hint="请用户提供年份",
            )
        if first != (s.year, s.month, s.day) or second != (e.year, e.month, e.day):
            return TimeResolveResult(
                False,
                "time_params_mismatch",
                "原文日期与 start_date/end_date_inclusive 不一致",
                hint="区间两端必须与原文日期逐项一致",
            )
        if s > e:
            return TimeResolveResult(
                False,
                "time_params_mismatch",
                "start_date 晚于 end_date_inclusive",
                None,
            )
        return self._finite(ctx, text, "range", s, _shift(e, 1))

    def _resolve_open_range(
        self,
        ctx: DbAgentContext,
        text: str,
        boundary_date: str | None,
        relation: str | None,
    ) -> TimeResolveResult:
        if not boundary_date or not relation:
            return TimeResolveResult(
                False,
                "time_params_mismatch",
                "open_range 需要 boundary_date 与 relation",
                None,
            )
        try:
            d = _parse_iso_date(boundary_date)
        except ValueError as exc:
            return TimeResolveResult(False, "time_params_mismatch", str(exc), None)
        tokens = _extract_dates(text)
        if len(tokens) != 1 or tokens[0] != (d.year, d.month, d.day):
            return TimeResolveResult(
                False,
                "time_params_mismatch",
                "boundary_date 与原文日期不一致",
                hint="边界日期必须逐字对应原文中的唯一日期（含年份）",
            )
        derived = self._derive_relation(text)
        if derived is None:
            return TimeResolveResult(
                False,
                "time_params_mismatch",
                "无法从原文确定单边方向与是否包含当天（或有歧义）",
                hint="向用户澄清：是否包含边界当天、方向是之前还是之后",
            )
        if derived != relation:
            return TimeResolveResult(
                False,
                "time_params_mismatch",
                f"relation={relation!r} 与原文表达不符（原文支持 {derived!r}）",
                hint="relation 必须与原文的方向与包含关系一致",
            )
        if relation == "before":
            return self._open(ctx, text, None, d)
        if relation == "through":
            return self._open(ctx, text, None, _shift(d, 1))
        if relation == "from":
            return self._open(ctx, text, d, None)
        return self._open(ctx, text, _shift(d, 1), None)  # after

    @staticmethod
    def _derive_relation(text: str) -> str | None:
        """从原文推导 relation；含糊（只有方向词、无包含限定）返回 None。"""
        has_incl = bool(_INCL_INCL.search(text))
        has_excl = bool(_INCL_EXCL.search(text))
        if _DIR_THROUGH.search(text):
            return "through"  # “截至”天然含当天
        m = _DIR_FROM.search(text)
        if m and re.search(r"从", text):
            return "from"  # “从 X 起/开始”天然含当天
        if _DIR_BEFORE.search(text):
            if has_excl and not has_incl:
                return "before"
            if has_incl and not has_excl:
                return "through"
            return None
        if _DIR_AFTER.search(text):
            if has_excl and not has_incl:
                return "after"
            if has_incl and not has_excl:
                return "from"
            return None
        return None

    # ---------------------------------------------------------------- 内部

    @staticmethod
    def _tz(name: str):
        from zoneinfo import ZoneInfo

        return ZoneInfo(name)

    @staticmethod
    def _week_start(d: date) -> date:
        return _shift(d, -d.isoweekday() + 1)

    @staticmethod
    def _month_bounds(d: date) -> tuple[date, date]:
        start = date(d.year, d.month, 1)
        end = date(d.year + 1, 1, 1) if d.month == 12 else date(d.year, d.month + 1, 1)
        return start, end

    @staticmethod
    def _prev_month_bounds(d: date) -> tuple[date, date]:
        first = date(d.year, d.month, 1)
        prev_end = _shift(first, -1)
        return date(prev_end.year, prev_end.month, 1), first

    @staticmethod
    def _quarter_bounds(d: date) -> tuple[date, date]:
        q = (d.month - 1) // 3
        return date(d.year, 3 * q + 1, 1), (
            date(d.year + 1, 1, 1) if q == 3 else date(d.year, 3 * (q + 1) + 1, 1)
        )

    @staticmethod
    def _prev_quarter_bounds(d: date) -> tuple[date, date]:
        q = (d.month - 1) // 3
        start = date(d.year, 3 * q + 1, 1)
        prev_end = _shift(start, -1)
        pq = (prev_end.month - 1) // 3
        qs = date(prev_end.year, 3 * pq + 1, 1)
        return qs, start

    def _finite(
        self,
        ctx: DbAgentContext,
        text: str,
        grain: str,
        start: date,
        end: date,
    ) -> TimeResolveResult:
        if not (start and end and start < end):
            return TimeResolveResult(
                False,
                "time_params_mismatch",
                f"有限区间无效: [{start}, {end})",
                None,
            )
        return TimeResolveResult(
            True,
            evidence=self._evidence(
                ctx,
                text,
                grain,
                _fmt(start),
                _fmt(end),
            ),
        )

    def _open(
        self,
        ctx: DbAgentContext,
        text: str,
        start: date | None,
        end: date | None,
    ) -> TimeResolveResult:
        if (start is None) == (end is None):
            return TimeResolveResult(
                False,
                "time_params_mismatch",
                "open_range 必须恰有一端非空",
                None,
            )
        return TimeResolveResult(
            True, evidence=self._evidence(ctx, text, "range", _fmt(start), _fmt(end))
        )

    def _evidence(
        self,
        ctx: DbAgentContext,
        text: str,
        grain: str,
        start: str | None,
        end: str | None,
    ) -> TimeEvidence:
        evidence_id = (
            "T-"
            + hashlib.sha256(
                "\x1f".join([ctx.run_id, text, grain, start or "", end or ""]).encode()
            ).hexdigest()[:8]
        )
        return TimeEvidence(
            time_evidence_id=evidence_id,
            run_id=ctx.run_id,
            source_text=text,
            grain=grain,
            start=start,
            end=end,
            timezone=ctx.timezone,
            datasource_id=ctx.datasource_id,
            inherited_from=None,
        )

    @staticmethod
    def _digit_mismatch(field: str, text_value, param) -> TimeResolveResult:
        return TimeResolveResult(
            False,
            "time_params_mismatch",
            f"原文 {field}={text_value} 与参数 {field}={param} 不一致",
            hint="参数必须与原文中的显式数字相同",
        )

    @staticmethod
    def _not_in_text(field: str, param) -> TimeResolveResult:
        return TimeResolveResult(
            False,
            "time_params_mismatch",
            f"参数 {field}={param} 在原文中没有对应数字",
            hint="逐字引用原文；不能补原文没有的数字",
        )


def _shift(d: date, days: int) -> date:
    from datetime import timedelta

    return d + timedelta(days=days)


def _parse_iso_date(value: str) -> date:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value or ""):
        raise ValueError(f"日期必须为 YYYY-MM-DD: {value!r}")
    return date.fromisoformat(value)


def _extract_dates(text: str) -> list[tuple[int | None, int | None, int | None]]:
    """提取 (年, 月, 日) token；缺省分量为 None（由 _inherit 补齐）。"""
    out: list[tuple[int | None, int | None, int | None]] = []
    for m in _DATE_TOKEN.finditer(text):
        if m.group(4):
            out.append((int(m.group(4)), int(m.group(5)), int(m.group(6))))
        else:
            out.append(
                (
                    int(m.group(1)) if m.group(1) else None,
                    int(m.group(2)) if m.group(2) else None,
                    int(m.group(3)),
                )
            )
    return out


def _inherit(
    base: tuple[int | None, int | None, int | None],
    partial: tuple[int | None, int | None, int | None],
) -> tuple[int | None, int | None, int | None]:
    """“9 月 1 日到 15 日”的第二端继承首端的年月。"""
    y, m, d = (p if p is not None else b for b, p in zip(base, partial))
    return y, m, d
