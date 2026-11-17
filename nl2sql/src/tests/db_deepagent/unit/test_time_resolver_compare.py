"""新旧时间解析器同题对比（doc 16 §9.1 固定时钟题集 / §9.3 对照输出）。

响应来源：旧版=真实规则词表实测；新版=mock flash（脚本按 §4/§9.1 口径
人工核算）。mock 结果仅验证接线/解析/登记，**不作为 flash 语义正确性的
验收证据**——语义验收走真实查询（doc 16 §9.3 第二条）。

固定时钟：conftest.db_agent_context 的 2026-10-13 12:00+08:00（周二）；
期望区间按 reference_now=2026-10-13 核算（doc §9.1 以 2026-11-15 出题，
此处同口径平移）。
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import date

import pytest

from src.modules.db_deepagent.context import DbAgentContext
from src.modules.db_deepagent.services.time_resolver import TimeResolver
from src.modules.db_deepagent.services.time_resolver_old import TimeResolverOld
from src.tests.db_deepagent.conftest import ScriptedTimeModel

pytestmark = pytest.mark.unit


def _ok(start, end, grain, interp="mock 解析"):
    return json.dumps(
        {
            "status": "ok",
            "start": start,
            "end": end,
            "grain": grain,
            "interpretation": interp,
            "clarification_question": None,
        },
        ensure_ascii=False,
    )


def _clarify(question):
    return json.dumps(
        {
            "status": "needs_clarification",
            "start": None,
            "end": None,
            "grain": None,
            "interpretation": "无法确定",
            "clarification_question": question,
        },
        ensure_ascii=False,
    )


# (原文, context, 旧版 kwargs, 旧版预期 ok|error_code, mock 响应,
#  新版预期 ok|error_code, 新版预期区间)
_CASES = [
    (
        "前天",
        None,
        {"kind": "relative", "relative": "yesterday"},
        "time_params_mismatch",
        _ok("2026-10-11", "2026-10-12", "day"),
        "ok",
        ("2026-10-11", "2026-10-12"),
    ),
    (
        "大前天",
        None,
        {"kind": "relative", "relative": "yesterday"},
        "time_params_mismatch",
        _ok("2026-10-10", "2026-10-11", "day"),
        "ok",
        ("2026-10-10", "2026-10-11"),
    ),
    (
        "上个月",
        None,
        {"kind": "relative", "relative": "last_month"},
        "ok",
        _ok("2026-09-01", "2026-10-01", "month"),
        "ok",
        ("2026-09-01", "2026-10-01"),
    ),
    (
        "最近三周",
        None,
        {"kind": "relative", "relative": "last_n_days", "n": 21},
        "time_params_mismatch",
        _ok("2026-09-23", "2026-10-14", "range"),
        "ok",
        ("2026-09-23", "2026-10-14"),
    ),
    (
        "过去72小时",
        None,
        {"kind": "relative", "relative": "last_n_days", "n": 3},
        "time_params_mismatch",
        _ok("2026-10-10T12:00:00", "2026-10-13T12:00:00", "hour"),
        "ok",
        ("2026-10-10T12:00:00", "2026-10-13T12:00:00"),
    ),
    (
        "截至今天中午12点",
        None,
        {"kind": "open_range", "boundary_date": "2026-10-13", "relation": "through"},
        "time_params_mismatch",
        _ok(None, "2026-10-13T12:00:00", "hour"),
        "ok",
        (None, "2026-10-13T12:00:00"),
    ),
    (
        "从大前天起",
        None,
        {"kind": "open_range", "boundary_date": "2026-10-10", "relation": "from"},
        "time_params_mismatch",
        _ok("2026-10-10", None, "day"),
        "ok",
        ("2026-10-10", None),
    ),
    (
        "上周一到周五",
        None,
        {
            "kind": "explicit_range",
            "start_date": "2026-10-05",
            "end_date_inclusive": "2026-10-09",
        },
        "time_params_mismatch",
        _ok("2026-10-05", "2026-10-10", "range"),
        "ok",
        ("2026-10-05", "2026-10-10"),
    ),
    (
        "9月份",
        "前文明确是 2025 年",
        {"kind": "calendar", "year": 2025, "month": 9},
        # 旧版无 context 通道，year 数字不在原文即被逐字匹配规则拒
        "time_params_mismatch",
        _ok("2025-09-01", "2025-10-01", "month"),
        "ok",
        ("2025-09-01", "2025-10-01"),
    ),
    (
        "9月份",
        None,
        {"kind": "calendar", "year": 2026, "month": 9},
        "time_params_mismatch",  # 同上：裸月无年份可传
        _ok("2026-09-01", "2026-10-01", "month"),
        "ok",
        ("2026-09-01", "2026-10-01"),
    ),
    (
        "2026年9月份",
        None,
        {"kind": "calendar", "year": 2026, "month": 9},
        "ok",
        _ok("2026-09-01", "2026-10-01", "month"),
        "ok",
        ("2026-09-01", "2026-10-01"),
    ),
    (
        "2025年2月29日",
        None,
        {"kind": "calendar", "year": 2025, "month": 2, "day": 29},
        "time_params_mismatch",  # 旧版同样拒（日期有效性校验）
        _ok("2025-02-29", "2025-03-01", "day"),
        "time_parse_invalid_output",  # 新版 fromisoformat 拒无效日期
        None,
    ),
    (
        "最近一段时间",
        None,
        {"kind": "relative", "relative": "last_n_days", "n": 7},
        "time_params_mismatch",
        _clarify("最近一段时间具体指多少天？"),
        "ambiguous_time_reference",
        None,
    ),
    (
        "2025年和2026年",
        None,
        {
            "kind": "explicit_range",
            "start_date": "2025-01-01",
            "end_date_inclusive": "2026-12-31",
        },
        "time_params_mismatch",
        _clarify("并列时间请分别询问，一次只解析一个连续区间"),
        "ambiguous_time_reference",
        None,
    ),
]


@pytest.mark.parametrize(
    "source_text,context,old_kwargs,old_expect,mock_resp,new_expect,new_bounds",
    _CASES,
    ids=[c[0] + (f"[{c[1]}]" if c[1] else "") for c in _CASES],
)
def test_old_vs_new(
    source_text,
    context,
    old_kwargs,
    old_expect,
    mock_resp,
    new_expect,
    new_bounds,
    db_agent_context,
):
    ctx: DbAgentContext = replace(
        db_agent_context,
        user_texts=(f"{source_text}的订单情况",),
    )

    # 旧版（规则词表）：ok 或按 error_code 拒绝
    old = TimeResolverOld().resolve(ctx, source_text=source_text, **old_kwargs)
    if old_expect == "ok":
        assert old.ok, f"旧版应成功: {old.error_code} {old.message}"
    else:
        assert not old.ok
        assert old.error_code == old_expect

    # 新版（mock flash）
    new = TimeResolver(
        ScriptedTimeModel(script={source_text: mock_resp}), "TEST"
    ).resolve(ctx, source_text=source_text, context=context)
    if new_expect == "ok":
        assert new.ok, f"新版应成功: {new.error_code} {new.message}"
        assert (new.evidence["start"], new.evidence["end"]) == new_bounds
        if source_text == "最近三周":
            start = date.fromisoformat(new_bounds[0])
            end = date.fromisoformat(new_bounds[1])
            assert (end - start).days == 21  # 日历窗口含今天共 21 天
    else:
        assert not new.ok
        assert new.error_code == new_expect

    # 对照行（-s 可见；来源：旧=规则词表实测，新=mock flash）
    old_part = "ok" if old.ok else (old.error_code or "error")
    new_part = f"ok {new_bounds}" if new_expect == "ok" else new_expect
    ctx_note = f" ctx={context!r}" if context else ""
    print(f"[对照] {source_text!r}{ctx_note}: 旧={old_part} | 新(mock)={new_part}")
