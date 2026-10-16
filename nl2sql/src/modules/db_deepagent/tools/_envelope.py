"""统一返回包与 Command 构造工具函数（docs/v4/5 §7.1）

业务工具实际执行后的返回为 JSON 文本（ensure_ascii=False），结构统一为
{"status": "ok|empty|error", "data": {}, "warnings": [], "error": {...}}。
框架层参数校验错误由 BaseTool.handle_validation_error = format_tool_validation_error
格式化为 "invalid_argument: ..." 文本（ToolMessage status="error"）。
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import ValidationError

from langchain_core.messages import ToolMessage
from langgraph.types import Command


def format_tool_validation_error(exc: ValidationError) -> str:
    """统一参数校验错误格式化：只输出公开字段路径与错误类型，不回显输入值。

    文本以 invalid_argument: 开头；BaseTool 在业务函数执行前调用，
    返回对应 tool_call_id 的 ToolMessage(status="error")。
    """
    parts = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err["loc"] if p != "runtime")
        parts.append(f"{loc}({err['type']})")
    return (
        "invalid_argument: " + "; ".join(parts)
        if parts
        else "invalid_argument: 参数校验失败"
    ) + "。请检查参数名与类型；隐藏字段 runtime 由系统注入，不能提供。"


def ok_envelope(
    data: dict[str, Any] | None = None,
    warnings: list[str] | None = None,
    *,
    empty: bool = False,
) -> str:
    """status=ok（或 empty）的返回包。"""
    return json.dumps(
        {
            "status": "empty" if empty else "ok",
            "data": data or {},
            "warnings": warnings or [],
        },
        ensure_ascii=False,
    )


def error_envelope(
    error_type: str,
    message: str,
    retryable: bool,
    hint: str | None = None,
    data: dict[str, Any] | None = None,
    warnings: list[str] | None = None,
) -> str:
    """status=error 的返回包（§7.1 错误结构）。"""
    return json.dumps(
        {
            "status": "error",
            "data": data or {},
            "warnings": warnings or [],
            "error": {
                "error_type": error_type,
                "message": message,
                "retryable": retryable,
                **({"hint": hint} if hint else {}),
            },
        },
        ensure_ascii=False,
    )


def finalize_output(
    payload: dict[str, Any],
    max_chars: int,
) -> tuple[str, list[str]]:
    """保证返回文本不超过 max_chars（§7.1：不触发 SDK 大结果转存，K9）。

    超长时先截短 data 中的长文本字段（card_text/sql/预览类），仍超长则整体截断
    文本并标注 warning。
    """
    warnings: list[str] = []

    def _dump() -> str:
        return json.dumps(payload, ensure_ascii=False)

    text = _dump()
    if len(text) <= max_chars:
        return text, warnings

    data = payload.get("data")
    if isinstance(data, dict):
        for key in ("card_text", "sql"):
            value = data.get(key)
            if isinstance(value, str) and len(value) > 200:
                data[key] = value[:200] + "…(截短)"
        for key in ("tables", "columns", "dim_values", "sql_examples", "results"):
            items = data.get(key)
            if isinstance(items, list) and len(items) > 5:
                data[key] = items[:5]
                warnings.append(f"{key} 结果过多，仅保留前 5 项")
        text = _dump()
        if len(text) <= max_chars:
            warnings.append("输出超长，已截短部分内容")
            return text, warnings

    warnings.append(f"输出超过 {max_chars} 字符上限，整体截断")
    return text[:max_chars], warnings


def tool_command(
    content: str,
    tool_call_id: str,
    updates: dict[str, Any] | None = None,
) -> Command:
    """构造工具返回 Command：账本更新 + 对应 tool_call_id 的 ToolMessage（§7.1）。"""
    return Command(
        update={
            **(updates or {}),
            "messages": [ToolMessage(content=content, tool_call_id=tool_call_id)],
        }
    )
