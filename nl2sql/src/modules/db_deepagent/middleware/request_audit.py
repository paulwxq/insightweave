"""RequestAuditMiddleware：模型请求审计与工具集合核对（docs/v4/5 §11.2）

wrap_model_call 记录请求轨迹（工具名、消息数、估算 Token、用量、耗时）到
审计日志与会话 audit.jsonl；工具名集合与期望集合（§10.6 同一常量）不一致时
记 ERROR、停止运行（tool_set_mismatch）。不记录 API key、连接串与提示词正文。

doc 20：wrap_model_call 在估算与模型调用之前，向每次主 Agent 请求的系统
消息注入本轮北京时间背景（ctx.now → Asia/Shanghai）。主 Agent 由此知道
"今天"；自然语言时间范围解析仍由 resolve_time_range 负责。注入块以保留
标题识别、替换式幂等，不写 state.messages，不改动原请求对象。
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from langchain.agents.middleware import AgentMiddleware, ModelRequest
from langchain_core.messages import AIMessage, SystemMessage
from langchain_core.messages.utils import count_tokens_approximately

from src.modules.db_deepagent.context import DbAgentContext
from src.modules.db_deepagent.services.run_budget import RunBudgetLedger
from src.modules.db_deepagent.state import DbDeepAgentState
from src.utils.logger import get_module_logger

logger = get_module_logger("nl2sql.db_deepagent.middleware.request_audit")
audit_logger = get_module_logger("nl2sql.db_deepagent.audit")

# doc 20 §4.1：本方案注入块的保留标题（识别时对块文本去前导空白后前缀匹配）
_REFERENCE_TIME_HEADER = "## 本轮北京时间背景\n"

_BEIJING = ZoneInfo("Asia/Shanghai")


def _with_reference_time(
    request: ModelRequest[DbAgentContext],
) -> ModelRequest[DbAgentContext]:
    """向请求系统消息追加/刷新本轮北京时间背景（doc 20 §4.1）。

    - 时钟：ctx.now（与 resolve_time_range 同源）转 Asia/Shanghai，跨午夜
      不中途变基准；
    - 幂等：以保留标题识别既有时间块并整体移除后追加当前块——重复处理
      同一原请求或已注入请求都只保留一块，且使用当前 ctx.now 刷新；
    - 纯函数：经 request.override() 创建新请求，不改动原请求与原系统消息。
    """
    reference_now = request.runtime.context.now.astimezone(_BEIJING)
    clock_text = (
        f"\n\n{_REFERENCE_TIME_HEADER}"
        f"本轮参考日期：{reference_now.date().isoformat()}\n"
        f"本轮参考时间：{reference_now.isoformat()}\n"
        "时区：Asia/Shanghai（北京时间，UTC+08:00）。\n"
        "以上是本轮请求开始时的参考时间；本轮“今天”以此为准。\n"
        "历史对话中的参考时间不代表本轮日期。\n"
        "自然语言时间范围解析、SQL 时间边界生成及时间计算结论复核"
        "仍使用 resolve_time_range。"
    )
    clock_block = {"type": "text", "text": clock_text}
    original = request.system_message
    if original is None:
        system_message = SystemMessage(content=[clock_block])
    else:
        blocks = (
            [{"type": "text", "text": original.content}]
            if isinstance(original.content, str)
            else list(original.content)
        )
        # 仅移除以保留标题开头的独立文本块；句中出现标题的其他块不动。
        blocks = [
            block
            for block in blocks
            if not (
                isinstance(block, dict)
                and block.get("type") == "text"
                and isinstance(block.get("text"), str)
                and block["text"].lstrip().startswith(_REFERENCE_TIME_HEADER)
            )
        ]
        system_message = original.model_copy(update={"content": [*blocks, clock_block]})
    return request.override(system_message=system_message)


def _tool_names(tools: list[Any]) -> list[str]:
    names = []
    for t in tools or []:
        name = getattr(t, "name", None)
        if name is None and isinstance(t, dict):
            name = (t.get("function") or {}).get("name") or t.get("name")
        names.append(str(name))
    return names


class RequestAuditMiddleware(AgentMiddleware[DbDeepAgentState, DbAgentContext]):
    """每次模型请求的审计轨迹 + 工具集合运行期核对（§11.2）"""

    def __init__(
        self,
        *,
        expected_tool_names: Callable[[], set[str]],
        budget: RunBudgetLedger,
        workspace_root: Path,
    ):
        # 期望集合经回调取值（按 features 计算的阶段清单常量，§10.6）
        self._expected_tool_names = expected_tool_names
        self._budget = budget
        self._workspace = Path(workspace_root)
        self._lock = threading.Lock()
        self._seq: dict[str, int] = {}

    def _next_seq(self, run_id: str) -> int:
        with self._lock:
            self._seq[run_id] = self._seq.get(run_id, 0) + 1
            return self._seq[run_id]

    def _append_audit(self, session_id: str, record: dict[str, Any]) -> None:
        """追加到 <workspace>/sessions/<session_id>/audit.jsonl；失败只记日志。"""
        try:
            path = self._workspace / "sessions" / session_id
            path.mkdir(parents=True, exist_ok=True)
            with (path / "audit.jsonl").open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError as exc:
            logger.warning("audit.jsonl 写入失败: %s", exc)

    def wrap_model_call(self, request, handler) -> Any:
        ctx: DbAgentContext = request.runtime.context
        # doc 20 §4：注入发生在序号生成、Token 估算与工具核对之前，
        # 估算与 handler 使用同一份注入后的请求。
        request = _with_reference_time(request)
        seq = self._next_seq(ctx.run_id)
        names = _tool_names(request.tools)
        approx_tokens = count_tokens_approximately(
            [request.system_message, *request.messages]
            if request.system_message is not None
            else request.messages
        )
        expected = self._expected_tool_names()
        if set(names) != expected:
            missing = sorted(expected - set(names))
            extra = sorted(set(names) - expected)
            audit_logger.error(
                "[request_id=%s run_id=%s seq=%d] 工具集合异常 missing=%s extra=%s",
                ctx.request_id,
                ctx.run_id,
                seq,
                missing,
                extra,
            )
            self._append_audit(
                ctx.session_id,
                {
                    "request_id": ctx.request_id,
                    "run_id": ctx.run_id,
                    "seq": seq,
                    "event": "tool_set_mismatch",
                    "missing": missing,
                    "extra": extra,
                },
            )
            self._budget.stop(ctx.run_id, "tool_set_mismatch")
            return AIMessage(content="工具集合异常，运行终止。")
        started = time.monotonic()
        response = handler(request)
        elapsed_ms = int((time.monotonic() - started) * 1000)
        usage: dict[str, Any] | None = None
        result = getattr(response, "result", None) or []
        for msg in result:
            usage = getattr(msg, "usage_metadata", None)
            if usage:
                break
        record = {
            "request_id": ctx.request_id,
            "run_id": ctx.run_id,
            "seq": seq,
            "event": "model_request",
            "tools": names,
            "input_messages": len(request.messages),
            "approx_input_tokens": approx_tokens,
            "elapsed_ms": elapsed_ms,
            "usage": usage,
        }
        audit_logger.info(
            "[request_id=%s run_id=%s seq=%d] tools=%d messages=%d approx_tokens=%d elapsed_ms=%d",
            ctx.request_id,
            ctx.run_id,
            seq,
            len(names),
            len(request.messages),
            approx_tokens,
            elapsed_ms,
        )
        self._append_audit(ctx.session_id, record)
        return response
