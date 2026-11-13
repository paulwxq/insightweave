"""服务端错误事件工厂（doc 6.3 §5.1/§10）

make_task_error_record/task_error_command：SQL 校验/执行、分析（含下推）
和 E/A 沿用的可恢复错误统一写 db_tool_errors 并在错误包中返回 failure_ref。

- error_id 由服务端生成（"ERR-" + uuid4 全量十六进制），在
  (session_id, thread_id, run_id) 内唯一；Command 重放沿用原记录原 ID。
- 同一错误的 Command(update) 重放携带原 error_id 和原记录（dedup reducer
  去重）；真实重试的新错误事件使用新 ID，即使 message 相同也不合并。
- source_task_ref 只在服务端核验目标身份后填入；参数层未通过、未知/旧版/
  不合法目标与 probe 的错误不伪造业务归属（保留 None）。
- 不可恢复异常不在此截获：仍遵守 Runner 的异常优先级，不为留错误记录
  掩盖持久化故障。
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any

from langgraph.types import Command

from src.modules.db_deepagent.context import DbAgentContext
from src.modules.db_deepagent.schemas import ToolErrorRecord
from src.modules.db_deepagent.tools._envelope import tool_command


def make_error_id() -> str:
    """ERR- + uuid4 完整十六进制（不使用短截断哈希或模型填写值）。"""
    return "ERR-" + uuid.uuid4().hex


def make_task_error_record(
    *,
    ctx: DbAgentContext,
    tool: str,
    error_type: str,
    message: str,
    retryable: bool,
    tool_call_id: str,
    source_task_ref: Mapping[str, Any] | None = None,
    error_id: str | None = None,
) -> ToolErrorRecord:
    """构造一条服务端错误记录（doc 6.3 §5.1）。

    error_id 显式传入时沿用（Command 重放）；否则新生成。
    source_task_ref 必须是服务端核验过的完整任务版本身份。
    """
    return ToolErrorRecord(
        error_id=error_id or make_error_id(),
        session_id=ctx.session_id,
        thread_id=ctx.thread_id,
        request_id=ctx.request_id,
        run_id=ctx.run_id,
        tool_call_id=tool_call_id or "",
        tool=tool,
        error_type=error_type,
        message=message,
        retryable=retryable,
        source_task_ref=dict(source_task_ref) if source_task_ref else None,
    )


def failure_ref_of(record: ToolErrorRecord) -> dict[str, Any]:
    """错误包中返回的完整 failure_ref（含完整 error_ref）。"""
    return {
        "error_ref": {
            "session_id": record["session_id"],
            "thread_id": record["thread_id"],
            "run_id": record["run_id"],
            "error_id": record["error_id"],
        },
        "tool_call_id": record["tool_call_id"],
        "error_type": record["error_type"],
        "source_task_ref": record["source_task_ref"],
    }


def task_error_command(
    record: ToolErrorRecord,
    content: str,
    tool_call_id: str,
    updates: dict[str, Any] | None = None,
) -> Command:
    """错误消息与账本更新一并经 Command 返回（§5.1，不改 runtime.state）。"""
    merged: dict[str, Any] = dict(updates or {})
    errors = list(merged.get("db_tool_errors") or [])
    errors.append(record)
    merged["db_tool_errors"] = errors
    return tool_command(content, tool_call_id, updates=merged)


def source_task_ref_of(
    ctx: DbAgentContext,
    sub_task_id: str | None,
    sub_task_revision: int | None,
) -> dict[str, Any] | None:
    """构造已核验目标的完整任务引用；目标缺失时返回 None（不伪造归属）。"""
    if not sub_task_id or sub_task_revision is None:
        return None
    return {
        "session_id": ctx.session_id,
        "thread_id": ctx.thread_id,
        "run_id": ctx.run_id,
        "sub_task_id": sub_task_id,
        "revision": sub_task_revision,
    }
