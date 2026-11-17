"""统一返回包与 Command 构造工具函数（docs/v4/5 §7.1；doc 9 §4 步骤 2）

业务工具实际执行后的返回为 JSON 文本（ensure_ascii=False, indent=2），
结构统一为 {"status": "ok|empty|error", "data": {}, "warnings": [],
"error": {...}}。多行布局是 SDK 大输出按行回读的必要条件；超过
filesystem.tool_token_limit_before_evict 的正文由 FilesystemMiddleware
自动转存（doc 9 §2），本层不再做字符级截断或列表裁剪。
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


def serialize_tool_output(payload: dict[str, Any]) -> str:
    """完整序列化工具返回包（doc 9 §4 步骤 2 唯一序列化入口）。

    只负责 JSON 序列化与多行布局：不递归修复上游值类型、不做字符上限、
    不裁列/删行——超长正文由 SDK FilesystemMiddleware 按既有阈值转存，
    业务层的数据量（预览行数/分页大小/保存上限）在各自工具内决定。
    """
    return json.dumps(payload, ensure_ascii=False, indent=2)


def ok_envelope(
    data: dict[str, Any] | None = None,
    warnings: list[str] | None = None,
    *,
    empty: bool = False,
) -> str:
    """status=ok（或 empty）的返回包。"""
    return serialize_tool_output(
        {
            "status": "empty" if empty else "ok",
            "data": data or {},
            "warnings": warnings or [],
        }
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
    return serialize_tool_output(
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
        }
    )


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
