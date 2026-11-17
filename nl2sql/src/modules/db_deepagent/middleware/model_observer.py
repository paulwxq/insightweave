"""ModelCallObserver：主模型与摘要的统一调用观测（docs/v4/5 §11.2.1）

公开 BaseCallbackHandler 回调，经 Runner 的 config.callbacks 注入；内部摘要
与框架重试经 Runnable 上下文继承同一回调（K22）。运行归属只取观测器实例
（构造绑定 request_id／run_id），用途取回调 metadata 的 dbda_call_kind；
不得把回调事件参数 run_id（模型尝试 ID）当成业务 run_id。

doc 13：主 Agent 调用在 DEBUG 级记录完整上下文（输入/输出/失败标记），
排版前检查 logger 有效级别；记录的是回调实参（含 _format_for_tracing 的
转换结果），不承诺与供应商 HTTP 请求逐字段相同。
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from langchain_core.callbacks import BaseCallbackHandler

from src.utils.logger import get_module_logger

logger = get_module_logger("nl2sql.db_deepagent.middleware.model_observer")

_VALID_CALL_KINDS = ("agent", "summarization", "time_resolution")

_ROLE_LABELS = {
    "SystemMessage": "System",
    "HumanMessage": "Human",
    "AIMessage": "AI",
    "ToolMessage": "Tool",
}


@dataclass
class _CallAttempt:
    """一次可观察的模型调用尝试"""

    attempt_id: str
    call_kind: str | None
    started_at: float
    ended_at: float | None = None
    ok: bool | None = None
    elapsed_ms: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None


@dataclass
class ModelObservationSnapshot:
    """Runner 封存的观测快照（§11.2.1）"""

    request_id: str
    run_id: str
    started: bool = False
    summary_model_calls: int = 0
    total_model_calls: int = 0
    input_tokens: int | None = None
    output_tokens: int | None = None
    token_usage_complete: bool = True
    anomalies: list[str] = field(default_factory=list)

    @classmethod
    def not_started(cls, request_id: str, run_id: str) -> ModelObservationSnapshot:
        """P0 前初始化：无调用时计数与 Token 为 0、完整性为 True（§14.3）。"""
        return cls(request_id=request_id, run_id=run_id, started=False)


class ModelCallObserver(BaseCallbackHandler):
    """每次 Runner 运行创建独立实例；业务归属不可变（§11.2.1）"""

    def __init__(self, request_id: str, run_id: str, audit_path: Path | None = None):
        self._request_id = request_id
        self._run_id = run_id
        self._audit_path = Path(audit_path) if audit_path is not None else None
        self._lock = threading.Lock()
        self._attempts: dict[str, _CallAttempt] = {}  # 以回调 run_id（尝试 ID）去重
        self._anomalies: list[str] = []
        self._finalized = False

    # ------------------------------------------------------------ 回调钩子

    def on_chat_model_start(
        self,
        serialized,
        messages,
        *,
        run_id,
        parent_run_id=None,
        tags=None,
        metadata=None,
        **kwargs,
    ) -> None:
        attempt = self._start(str(run_id), metadata)
        if attempt is not None and attempt.call_kind == "agent":
            self._log_model_input(attempt, messages, kwargs)

    def on_llm_start(
        self,
        serialized,
        prompts,
        *,
        run_id,
        parent_run_id=None,
        tags=None,
        metadata=None,
        **kwargs,
    ) -> None:
        self._start(str(run_id), metadata)

    def on_llm_end(
        self,
        response,
        *,
        run_id,
        parent_run_id=None,
        tags=None,
        metadata=None,
        **kwargs,
    ) -> None:
        attempt = self._end(str(run_id), response, ok=True)
        if attempt is not None and attempt.call_kind == "agent":
            self._log_model_output(attempt, response)

    def on_llm_error(
        self, error, *, run_id, parent_run_id=None, tags=None, metadata=None, **kwargs
    ) -> None:
        attempt = self._end(str(run_id), None, ok=False)
        if attempt is not None and attempt.call_kind == "agent":
            self._log_model_failure(attempt, error)

    # ------------------------------------------------------------ 内部

    def _start(
        self, attempt_id: str, metadata: dict[str, Any] | None
    ) -> _CallAttempt | None:
        call_kind = None
        if isinstance(metadata, dict):
            value = metadata.get("dbda_call_kind")
            if isinstance(value, str):
                call_kind = value
        with self._lock:
            if self._finalized:
                return None
            existing = self._attempts.get(attempt_id)
            if existing is not None:
                return existing  # on_chat_model_start 与 on_llm_start 同一尝试只计一次
            if call_kind not in _VALID_CALL_KINDS:
                # 用途标记缺失或非法：不能默认归为主模型（§11.2.1）
                self._anomalies.append(
                    f"attempt {attempt_id} 的 dbda_call_kind={call_kind!r} 缺失或非法"
                )
            attempt = _CallAttempt(
                attempt_id=attempt_id,
                call_kind=call_kind if call_kind in _VALID_CALL_KINDS else None,
                started_at=time.monotonic(),
            )
            self._attempts[attempt_id] = attempt
            return attempt

    def _end(self, attempt_id: str, response, *, ok: bool) -> _CallAttempt | None:
        input_tokens: int | None = None
        output_tokens: int | None = None
        if ok and response is not None:
            input_tokens, output_tokens = _extract_usage(response)
        with self._lock:
            if self._finalized:
                return None
            attempt = self._attempts.get(attempt_id)
            if attempt is None or attempt.ended_at is not None:
                return None  # 未见开始（外部回调）或重复结束：不计
            attempt.ended_at = time.monotonic()
            attempt.ok = ok
            attempt.elapsed_ms = int((attempt.ended_at - attempt.started_at) * 1000)
            attempt.input_tokens = input_tokens
            attempt.output_tokens = output_tokens
            self._append_audit(
                {
                    "request_id": self._request_id,
                    "run_id": self._run_id,
                    "event": "model_attempt",
                    "attempt_id": attempt.attempt_id,
                    "call_kind": attempt.call_kind,
                    "ok": attempt.ok,
                    "elapsed_ms": attempt.elapsed_ms,
                    "input_tokens": attempt.input_tokens,
                    "output_tokens": attempt.output_tokens,
                }
            )
            return attempt

    def _append_audit(self, line: dict[str, Any]) -> None:
        """审计事件走会话 audit.jsonl（与 §11.2 同一路径）；失败只记日志。"""
        if self._audit_path is None:
            return
        try:
            self._audit_path.parent.mkdir(parents=True, exist_ok=True)
            with self._audit_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(line, ensure_ascii=False) + "\n")
        except OSError as exc:
            logger.warning("模型观测审计写入失败: %s", exc)

    # ------------------------------------------------------------ 完整上下文日志（doc 13）

    def _log_model_input(
        self, attempt: _CallAttempt, messages, kwargs: dict[str, Any]
    ) -> None:
        """主 Agent 调用的完整输入块；排版前检查 DEBUG（doc 13 §3.1/§4）。"""
        if not logger.isEnabledFor(logging.DEBUG):
            return
        try:
            params = kwargs.get("invocation_params")
            params = params if isinstance(params, dict) else {}
            options = kwargs.get("options")
            options = options if isinstance(options, dict) else {}
            lines = [
                _block_marker("MODEL_INPUT_BEGIN", self, attempt, start=True),
                f"模型：{_model_name(params)}",
                "",
            ]
            lines.extend(_format_context_messages(messages))
            tool_lines = []
            for key in ("tools", "tool_choice", "response_format"):
                if key in params:
                    tool_lines.append(f"{key}={_dump(params[key])}")
            ls_format = options.get("ls_structured_output_format")
            if ls_format is not None:
                tool_lines.append(f"ls_structured_output_format={_dump(ls_format)}")
            if tool_lines:
                lines.append("")
                lines.append("[Tools]")
                lines.extend(tool_lines)
            lines.append(_block_marker("MODEL_INPUT_END", self, attempt, start=False))
            logger.debug("\n".join(lines))
        except Exception as exc:  # noqa: BLE001
            logger.warning("模型上下文输入日志排版失败: %s", exc)

    def _log_model_output(self, attempt: _CallAttempt, response) -> None:
        """主 Agent 调用的输出块；与输入块共享 attempt_id（doc 13 §3.2）。"""
        if not logger.isEnabledFor(logging.DEBUG):
            return
        try:
            generations = getattr(response, "generations", None) or []
            lines = [_block_marker("MODEL_OUTPUT_BEGIN", self, attempt, start=True)]
            index = 1
            for batch in generations:
                for generation in batch or []:
                    message = getattr(generation, "message", None)
                    if message is None:
                        continue
                    lines.extend(_format_message(message, index))
                    index += 1
            lines.append(_block_marker("MODEL_OUTPUT_END", self, attempt, start=False))
            logger.debug("\n".join(lines))
        except Exception as exc:  # noqa: BLE001
            logger.warning("模型上下文输出日志排版失败: %s", exc)

    def _log_model_failure(self, attempt: _CallAttempt, error: BaseException) -> None:
        """失败尝试的标记块：同 attempt_id、错误类型，保留已写出的输入块。"""
        if not logger.isEnabledFor(logging.DEBUG):
            return
        try:
            lines = [
                _block_marker("MODEL_OUTPUT_BEGIN", self, attempt, start=True),
                f"[失败] error={type(error).__name__}: {error}",
                _block_marker("MODEL_OUTPUT_END", self, attempt, start=False),
            ]
            logger.debug("\n".join(lines))
        except Exception as exc:  # noqa: BLE001
            logger.warning("模型上下文失败标记日志排版失败: %s", exc)

    # ------------------------------------------------------------ 封存

    def finalize(self) -> ModelObservationSnapshot:
        """封存观测快照；成功或异常后由 Runner 调用（幂等，之后忽略回调）。"""
        with self._lock:
            if self._finalized:
                return self._snapshot_locked()
            self._finalized = True
            return self._snapshot_locked()

    def _snapshot_locked(self) -> ModelObservationSnapshot:
        attempts = list(self._attempts.values())
        summary_calls = sum(1 for a in attempts if a.call_kind == "summarization")
        total = len(attempts)
        known_input = [a.input_tokens for a in attempts if a.input_tokens is not None]
        known_output = [
            a.output_tokens for a in attempts if a.output_tokens is not None
        ]
        usage_complete = len(known_input) == total and len(known_output) == total
        snapshot = ModelObservationSnapshot(
            request_id=self._request_id,
            run_id=self._run_id,
            started=True,
            summary_model_calls=summary_calls,
            total_model_calls=total,
            input_tokens=sum(known_input) if usage_complete else None,
            output_tokens=sum(known_output) if usage_complete else None,
            token_usage_complete=usage_complete and not self._anomalies,
            anomalies=list(self._anomalies),
        )
        logger.info(
            "[request_id=%s run_id=%s] 模型观测：total=%d summary=%d tokens=%s/%s complete=%s",
            self._request_id,
            self._run_id,
            snapshot.total_model_calls,
            snapshot.summary_model_calls,
            snapshot.input_tokens,
            snapshot.output_tokens,
            snapshot.token_usage_complete,
        )
        return snapshot


def _extract_usage(response: Any) -> tuple[int | None, int | None]:
    """优先 usage_metadata；同一次响应不再累计 llm_output（§11.2.1）。"""
    generations = getattr(response, "generations", None) or []
    for batch in generations:
        for generation in batch:
            message = getattr(generation, "message", None)
            usage = getattr(message, "usage_metadata", None)
            if isinstance(usage, dict):
                input_tokens = usage.get("input_tokens")
                output_tokens = usage.get("output_tokens")
                if isinstance(input_tokens, int) and isinstance(output_tokens, int):
                    return input_tokens, output_tokens
    llm_output = getattr(response, "llm_output", None) or {}
    token_usage = (
        llm_output.get("token_usage") if isinstance(llm_output, dict) else None
    )
    if isinstance(token_usage, dict):
        prompt = token_usage.get("prompt_tokens")
        completion = token_usage.get("completion_tokens")
        if isinstance(prompt, int) and isinstance(completion, int):
            return prompt, completion
    return None, None


# ------------------------------------------------------------ 完整上下文排版（doc 13 §3）


def _dump(obj: Any) -> str:
    """结构化内容的可读 JSON；不可序列化字段降级为 str，不抛异常。"""
    return json.dumps(obj, ensure_ascii=False, default=str)


def _model_name(params: dict[str, Any]) -> str:
    """从调用参数取模型名；缺失时明确写"未知"，不推造。"""
    for key in ("model", "model_name"):
        value = params.get(key)
        if isinstance(value, str) and value:
            return value
    return "未知"


def _block_marker(
    name: str, observer: ModelCallObserver, attempt: _CallAttempt, *, start: bool
) -> str:
    del start  # 起止标记格式一致
    return (
        f"===== {name} request_id={observer._request_id} "
        f"run_id={observer._run_id} attempt_id={attempt.attempt_id} ====="
    )


def _format_content(content: Any) -> str:
    """正文按原样或 JSON 保留；结构化内容块不降级为字符串摘要（§3.1）。"""
    if isinstance(content, str):
        return content
    if content is None:
        return ""
    if isinstance(content, (list, tuple)):
        return _dump(list(content))
    return _dump(content)


def _format_tool_call(tool_call: Any) -> list[str]:
    args = (
        tool_call.get("args")
        if isinstance(tool_call, dict)
        else getattr(tool_call, "args", {})
    )
    call_id = (
        tool_call.get("id")
        if isinstance(tool_call, dict)
        else getattr(tool_call, "id", "")
    )
    name = (
        tool_call.get("name")
        if isinstance(tool_call, dict)
        else getattr(tool_call, "name", "")
    )
    return [
        f"tool_call_id={call_id}",
        f"name={name}",
        f"args={args if isinstance(args, str) else _dump(args)}",
    ]


def _format_message(message: Any, index: int) -> list[str]:
    """单条消息 → 标题行 + 正文/关联 ID/工具调用（§3.3 示例格式）。"""
    role = _ROLE_LABELS.get(type(message).__name__, type(message).__name__)
    tool_calls = getattr(message, "tool_calls", None) or []
    header = f"[消息 {index} / {role}" + (" / 工具调用]" if tool_calls else "]")
    lines = [header]
    body = _format_content(getattr(message, "content", ""))
    if body:
        lines.append(body)
    tool_call_id = getattr(message, "tool_call_id", None)
    if tool_call_id:
        lines.append(f"tool_call_id={tool_call_id}")
    for tool_call in tool_calls:
        lines.extend(_format_tool_call(tool_call))
    return lines


def _format_context_messages(messages: Any) -> list[str]:
    """按"批次→消息"两层遍历；多批次时批次边界可辨（§3.1.2）。

    回调 messages 为 list[list[BaseMessage]]；对裸列表（手工构造等）归一为
    单批次，不把内层消息列表当成一条消息。
    """
    batches: list[Any]
    if (
        isinstance(messages, list)
        and messages
        and isinstance(messages[0], (list, tuple))
    ):
        batches = list(messages)
    elif isinstance(messages, list):
        batches = [messages]
    else:
        batches = [[messages]] if messages is not None else []
    multi_batch = len(batches) > 1
    lines: list[str] = []
    index = 1
    for batch_no, batch in enumerate(batches, start=1):
        if multi_batch:
            lines.append(f"[批次 {batch_no}]")
        for message in batch or []:
            lines.extend(_format_message(message, index))
            index += 1
    return lines
