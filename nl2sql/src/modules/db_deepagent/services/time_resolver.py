"""TimeResolver（LLM 版，doc 16）：flash 模型解析自然语言时间为半开区间。

服务端流程四步：拼输入（source_text + 当前问题 + context + 北京时间
reference_now）→ 单次调用 flash → 解析 JSON + 校验（日期有效、grain 枚举、
非单边时 start<end、至少一端非空——仅适用 status=ok）→ 登记时间证据。

时钟唯一来源：ctx.now 转北京时间（时区写死 Asia/Shanghai，doc 16 §3.3）；
模型永不提供当前日期。失败不回退旧词表；不缓存。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from src.modules.db_deepagent.context import DbAgentContext
from src.modules.db_deepagent.schemas import TimeEvidence
from src.services.llm_factory import resolve_llm_config
from src.utils.logger import get_module_logger

logger = get_module_logger("nl2sql.db_deepagent.services.time_resolver")

# doc 16 §3.2：grain 枚举（由 flash 返回，代码仅校验）
VALID_GRAINS = frozenset({"day", "week", "month", "quarter", "year", "hour", "range"})

# 时区写死北京时间（doc 16 §3.3；输出的不带偏移日期时间同口径解释）
_TIMEZONE = "Asia/Shanghai"

_CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"


@dataclass(frozen=True)
class TimeResolveResult:
    """resolve 的结果：ok 时 evidence 非空；失败时 error_code 给信封码"""

    ok: bool
    error_code: str | None = None
    message: str = ""
    hint: str | None = None
    evidence: TimeEvidence | None = None
    # flash 的一句话解释（doc 16 §3.2 返回字段；不写入 TimeEvidence，证据
    # schema 沿用现有字段不加新键）
    interpretation: str | None = None


class TimeParsePayload(BaseModel):
    """flash 输出的唯一结构校验入口（doc 16 §5）。"""

    status: str = Field(pattern="^(ok|needs_clarification)$")
    start: str | None = None
    end: str | None = None
    grain: str | None = None
    interpretation: str | None = None
    clarification_question: str | None = None


class TimeResolver:
    """LLM 版时间解析器：flash 理解语义，代码验形与记账。"""

    def __init__(self, model: Any, system_prompt: str):
        self._model = model
        self._system_prompt = system_prompt

    # ------------------------------------------------------------ 主入口

    def resolve(
        self,
        ctx: DbAgentContext,
        *,
        source_text: str,
        context: str | None = None,
        config: dict[str, Any] | None = None,
    ) -> TimeResolveResult:
        # 1. 原文核对：source_text 必须是某条用户原文的子串（沿用现行检查）
        text = source_text.strip()
        if not any(text in (ut or "") for ut in ctx.user_texts):
            return TimeResolveResult(
                False,
                "time_text_not_in_question",
                "source_text 不是用户原文中任何一句的子串",
                hint="逐字引用用户的时间表达；如需沿用前文口径，使用 reuse_evidence",
            )

        # 2. 拼输入：服务端当前时刻转北京时间（doc 16 §3.3，写死北京时区）
        reference_now = ctx.now.astimezone(ZoneInfo(_TIMEZONE))
        current_question = (ctx.user_texts or [""])[-1] if ctx.user_texts else ""
        human_payload = json.dumps(
            {
                "source_text": text,
                "current_question": current_question,
                "context": context,
                "reference_now": reference_now.isoformat(),
                "timezone": _TIMEZONE,
            },
            ensure_ascii=False,
        )
        messages = [
            SystemMessage(content=self._system_prompt),
            HumanMessage(content=human_payload),
        ]
        invoke_config: dict[str, Any] = {}
        if isinstance(config, dict) and config.get("callbacks") is not None:
            invoke_config["callbacks"] = config["callbacks"]

        # 3. 单次调用 flash（无应用层重试循环；SDK 传输层重试在模型客户端内）
        # doc 16 §7/§9.2.6：调用前记实际 System/Human 输入，调用后记响应；
        # 均在 DEBUG 门内，便于事后还原该次调用的实际指令。
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "[request_id=%s run_id=%s] 时间解析输入 system=%s human=%s",
                ctx.request_id,
                ctx.run_id,
                self._system_prompt,
                human_payload,
            )
        try:
            response = self._model.invoke(messages, config=invoke_config or None)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[request_id=%s run_id=%s] 时间解析模型调用失败: %s: %s",
                ctx.request_id,
                ctx.run_id,
                type(exc).__name__,
                exc,
            )
            return TimeResolveResult(
                False,
                "time_parse_model_error",
                f"日期解析未完成（模型调用失败：{type(exc).__name__}）",
                hint="可重试一次；仍失败时向用户澄清时间范围",
            )

        raw = _content_text(response)
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "[request_id=%s run_id=%s] 时间解析模型输出: %s",
                ctx.request_id,
                ctx.run_id,
                raw,
            )

        # 4. 解析 JSON + 校验（仅 status=ok 做边界校验；澄清直接返回）
        try:
            data = json.loads(raw)
            payload = TimeParsePayload.model_validate(data)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[request_id=%s run_id=%s] 时间解析输出不合法: %s: %s",
                ctx.request_id,
                ctx.run_id,
                type(exc).__name__,
                exc,
            )
            return TimeResolveResult(
                False,
                "time_parse_invalid_output",
                "日期解析输出不合法（JSON 结构/字段类型错误）",
                hint="可重试一次；仍失败时向用户澄清时间范围",
            )

        if payload.status == "needs_clarification":
            return TimeResolveResult(
                False,
                "ambiguous_time_reference",
                payload.clarification_question
                or payload.interpretation
                or "时间表达存在歧义，需要用户澄清",
                hint="向用户澄清时间范围后重新解析",
            )

        problem = _validate_bounds(payload)
        if problem is not None:
            logger.warning(
                "[request_id=%s run_id=%s] 时间解析边界校验失败: %s",
                ctx.request_id,
                ctx.run_id,
                problem,
            )
            return TimeResolveResult(
                False,
                "time_parse_invalid_output",
                f"日期解析输出不合法（{problem}）",
                hint="可重试一次；仍失败时向用户澄清时间范围",
            )

        evidence = self._evidence(
            ctx,
            text,
            payload.grain or "range",
            payload.start,
            payload.end,
        )
        logger.info(
            "[request_id=%s run_id=%s] resolve_time_range(LLM): %s -> [%s, %s) grain=%s",
            ctx.request_id,
            ctx.run_id,
            text,
            payload.start,
            payload.end,
            payload.grain,
        )
        return TimeResolveResult(
            True, evidence=evidence, interpretation=payload.interpretation
        )

    # ------------------------------------------------------------ 内部

    @staticmethod
    def _evidence(
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
            timezone=_TIMEZONE,
            datasource_id=ctx.datasource_id,
            inherited_from=None,
        )


def _content_text(response: Any) -> str:
    """提取模型回复文本（str 或内容块列表；只取文本不取其他块）。"""
    content = getattr(response, "content", response)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        return "".join(parts)
    return str(content)


_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DATETIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$")


def _validate_bounds(payload: TimeParsePayload) -> str | None:
    """status=ok 的边界校验；返回问题描述或 None。doc 16 §3.2/§3.3。

    格式白名单先行（C1-02）：仅接受无偏移、无小数秒的
    YYYY-MM-DD / YYYY-MM-DDTHH:MM:SS——fromisoformat 接受过宽（偏移、
    紧凑日期、Z 后缀等），且混用 offset-naive/aware 会在比较时抛
    TypeError 逃出解析器。白名单内再做真实日期与顺序校验。
    """
    if payload.start is None and payload.end is None:
        return "至少一端非空"
    if payload.grain is None or payload.grain not in VALID_GRAINS:
        return f"grain 非法: {payload.grain!r}"
    parsed: dict[str, datetime] = {}
    for name in ("start", "end"):
        value = getattr(payload, name)
        if value is None:
            continue
        if not (_DATE_RE.match(value) or _DATETIME_RE.match(value)):
            return f"{name} 格式非法（仅接受 YYYY-MM-DD 或 YYYY-MM-DDTHH:MM:SS）: {value!r}"
        try:
            parsed[name] = datetime.fromisoformat(value)
        except ValueError:
            return f"{name} 不是有效日期时间: {value!r}"
    if "start" in parsed and "end" in parsed and parsed["start"] >= parsed["end"]:
        return "有限范围要求 start < end"
    return None


# ------------------------------------------------------------ 装配


def build_time_resolver(cfg: Any) -> TimeResolver:
    """按 time_resolution 配置构造 LLM 版解析器（doc 16 §6/§8）。

    - 提示词来自 config/time_resolution_prompts.yaml（缺文件即配置错误）；
    - get_llm 接收 time_resolution.params 与 request_timeout_seconds；原生
      dashscope 不支持该业务超时映射，工厂会明确报错；
    - 模型 metadata 标记 dbda_call_kind=time_resolution（observer 识别用途）。
    """
    from src.services.llm_factory import get_llm

    tr = cfg.time_resolution
    try:
        resolve_llm_config(tr.provider, tr.llm_name)
    except ValueError as exc:
        raise ValueError(f"time_resolution: {exc}") from exc
    prompt_path = _CONFIG_DIR / "time_resolution_prompts.yaml"
    try:
        with open(prompt_path, encoding="utf-8") as fh:
            prompts = yaml.safe_load(fh) or {}
    except OSError as exc:
        raise ValueError(
            f"time_resolution: 日期提示词文件缺失或不可读: {prompt_path}（{exc}）"
        ) from exc
    system_prompt = prompts.get("system")
    if not isinstance(system_prompt, str) or not system_prompt.strip():
        raise ValueError("time_resolution_prompts.yaml 缺少非空 system 提示词")

    # timeout 无条件映射（C1-06）：request_timeout_seconds 必须生效；
    # 原生 dashscope provider 不支持 timeout，由工厂显式拒绝（报错指向
    # time_resolution 配置），不静默丢弃。
    try:
        model_with_meta = get_llm(
            tr.provider,
            tr.llm_name,
            params=dict(tr.params),
            request_timeout_seconds=tr.request_timeout_seconds,
        )
    except ValueError as exc:
        raise ValueError(f"time_resolution: {exc}") from exc
    model = model_with_meta.llm
    model.metadata = {
        **(getattr(model, "metadata", None) or {}),
        "dbda_call_kind": "time_resolution",
    }
    return TimeResolver(model, system_prompt)
