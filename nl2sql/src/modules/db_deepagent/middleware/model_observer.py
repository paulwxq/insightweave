"""ModelCallObserver：主模型与摘要的统一调用观测（docs/v4/5 §11.2.1）

公开 BaseCallbackHandler 回调，经 Runner 的 config.callbacks 注入；内部摘要
与框架重试经 Runnable 上下文继承同一回调（K22）。运行归属只取观测器实例
（构造绑定 request_id／run_id），用途取回调 metadata 的 dbda_call_kind；
不得把回调事件参数 run_id（模型尝试 ID）当成业务 run_id。
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from langchain_core.callbacks import BaseCallbackHandler

from src.utils.logger import get_module_logger

logger = get_module_logger("nl2sql.db_deepagent.middleware.model_observer")

_VALID_CALL_KINDS = ("agent", "summarization")


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
    def not_started(cls, request_id: str, run_id: str) -> "ModelObservationSnapshot":
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
        self._start(str(run_id), metadata)

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
        self._end(str(run_id), response, ok=True)

    def on_llm_error(
        self, error, *, run_id, parent_run_id=None, tags=None, metadata=None, **kwargs
    ) -> None:
        self._end(str(run_id), None, ok=False)

    # ------------------------------------------------------------ 内部

    def _start(self, attempt_id: str, metadata: dict[str, Any] | None) -> None:
        call_kind = None
        if isinstance(metadata, dict):
            value = metadata.get("dbda_call_kind")
            if isinstance(value, str):
                call_kind = value
        with self._lock:
            if self._finalized:
                return
            if attempt_id in self._attempts:
                return  # on_chat_model_start 与 on_llm_start 同一尝试只计一次
            if call_kind not in _VALID_CALL_KINDS:
                # 用途标记缺失或非法：不能默认归为主模型（§11.2.1）
                self._anomalies.append(
                    f"attempt {attempt_id} 的 dbda_call_kind={call_kind!r} 缺失或非法"
                )
            self._attempts[attempt_id] = _CallAttempt(
                attempt_id=attempt_id,
                call_kind=call_kind if call_kind in _VALID_CALL_KINDS else None,
                started_at=time.monotonic(),
            )

    def _end(self, attempt_id: str, response, *, ok: bool) -> None:
        input_tokens: int | None = None
        output_tokens: int | None = None
        if ok and response is not None:
            input_tokens, output_tokens = _extract_usage(response)
        with self._lock:
            if self._finalized:
                return
            attempt = self._attempts.get(attempt_id)
            if attempt is None or attempt.ended_at is not None:
                return  # 未见开始（外部回调）或重复结束：不计
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
