"""按 ``llm_providers.<provider>.models.<llm_name>`` 创建聊天模型。"""

import importlib
import math
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from langchain_core.language_models import BaseChatModel

from src.services.config_loader import get_config
from src.utils.logger import get_module_logger

logger = get_module_logger("llm_factory")

_ADAPTERS = {
    "dashscope_native": ("langchain_community.chat_models", "ChatTongyi"),
    "openai_chat_completions": ("langchain_openai", "ChatOpenAI"),
}
_ALLOWED_PARAMS = frozenset(
    {
        "temperature",
        "timeout",
        "max_tokens",
        "top_p",
        "enable_thinking",
        "enable_search",
        "search_options",
        "response_format",
        "stream",
        "thinking",
        "max_retries",
        "reasoning_effort",
    }
)
_DASHSCOPE_EXTRA_PARAMS = frozenset(
    {"enable_thinking", "enable_search", "search_options"}
)
_EXTRA_BODY_MAX_TOKEN_MODELS = frozenset(
    {
        ("deepseek", "deepseek-flash"),
        ("glm_coding", "glm-5.4"),
        ("glm_coding", "glm-5.3"),
        ("glm_coding", "glm-5.3-flash"),
    }
)


@dataclass
class LLMWithMeta:
    """LLM 实例及其元信息（用于日志、异常与可观测性）。"""

    llm: BaseChatModel
    provider: str
    model: str


def _default_param_path(key: str) -> str:
    return f"params.{key}"


def _validate_timeout_value(value: Any, path: str) -> None:
    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
        raise ValueError(f"{path}: 必须是有限且大于 0 的数")


def resolve_llm_config(provider: str, llm_name: str) -> tuple[dict, dict]:
    """精确解析连接与模型注册项，不校验未选供应商的密钥。"""
    providers = get_config().get("llm_providers")
    if not isinstance(providers, dict):
        raise ValueError(  # noqa: TRY004 - 配置错误统一使用 ValueError
            "llm_providers: 必须是映射，请检查 src/configs/config.yaml"
        )
    if not isinstance(provider, str) or not provider.strip():
        raise ValueError("provider: 必须是非空字符串")
    if not isinstance(llm_name, str) or not llm_name.strip():
        raise ValueError(f"llm_providers.{provider}.models: llm_name 必须是非空字符串")
    provider_config = providers.get(provider)
    if not isinstance(provider_config, dict):
        raise ValueError(  # noqa: TRY004 - 配置错误统一使用 ValueError
            f"llm_providers.{provider}: provider 不存在或配置不是映射"
        )
    models = provider_config.get("models")
    if not isinstance(models, dict):
        raise ValueError(  # noqa: TRY004 - 配置错误统一使用 ValueError
            f"llm_providers.{provider}.models: 必须是映射"
        )
    if llm_name not in models:
        raise ValueError(f"llm_providers.{provider}.models.{llm_name}: 模型未注册")
    model_config = models[llm_name]
    if not isinstance(model_config, dict):
        raise ValueError(  # noqa: TRY004 - 配置错误统一使用 ValueError
            f"llm_providers.{provider}.models.{llm_name}: 配置必须是映射"
        )
    params = model_config.get("params", {})
    if not isinstance(params, dict):
        raise ValueError(  # noqa: TRY004 - 配置错误统一使用 ValueError
            f"llm_providers.{provider}.models.{llm_name}.params: 必须是映射"
        )
    for key in params:
        if key not in _ALLOWED_PARAMS:
            raise ValueError(
                f"llm_providers.{provider}.models.{llm_name}.params.{key}: "
                "不支持的聊天模型参数"
            )
    adapter = provider_config.get("adapter")
    if adapter not in _ADAPTERS:
        raise ValueError(
            f"llm_providers.{provider}.adapter: 不支持 {adapter!r}，"
            f"可选 {sorted(_ADAPTERS)}"
        )
    return provider_config, model_config


def get_llm(
    provider: str,
    llm_name: str,
    *,
    params: dict[str, Any] | None = None,
    request_timeout_seconds: float | None = None,
) -> LLMWithMeta:
    """合并模型默认值与业务 ``params``，只构造选中的客户端。

    OpenAI 兼容调用必须从角色超时、业务 ``params.timeout`` 或注册表默认
    ``params.timeout`` 获得有限且大于 0 的请求超时，避免无超时或非法客户端配置。
    """
    provider_config, model_config = resolve_llm_config(provider, llm_name)
    if params is not None and not isinstance(params, dict):
        raise ValueError("params: 必须是映射")
    business_params = params or {}
    effective = {
        **(model_config.get("params") or {}),
        **business_params,
    }
    registry_params_path = f"llm_providers.{provider}.models.{llm_name}.params"

    def param_path(key: str) -> str:
        if key in business_params:
            return f"params.{key}"
        return f"{registry_params_path}.{key}"

    if request_timeout_seconds is not None and params and "timeout" in params:
        raise ValueError(
            "params.timeout 与 request_timeout_seconds 冲突；请只保留业务超时字段"
        )
    adapter = provider_config["adapter"]
    if not provider_config.get("api_key"):
        raise ValueError(f"llm_providers.{provider}.api_key: 缺少密钥或值为空")
    if adapter == "openai_chat_completions":
        base_url = provider_config.get("base_url")
        if base_url is None and provider == "deepseek":
            base_url = "https://api.deepseek.com"
        if not isinstance(base_url, str) or not base_url.strip():
            raise ValueError(f"llm_providers.{provider}.base_url: 兼容接口地址不能为空")
        provider_config = {**provider_config, "base_url": base_url}
        if request_timeout_seconds is None and effective.get("timeout") is None:
            raise ValueError(
                "request_timeout_seconds 或 params.timeout: OpenAI 兼容调用必须"
                "配置请求超时；也可在 "
                f"llm_providers.{provider}.models.{llm_name}.params.timeout 设置默认值"
            )
    constructor_params = _build_params(
        provider,
        provider_config,
        llm_name,
        effective,
        adapter=adapter,
        request_timeout_seconds=request_timeout_seconds,
        timeout_is_explicit=bool(params and "timeout" in params),
        param_path=param_path,
    )
    module_path, class_name = _ADAPTERS[adapter]
    try:
        cls = _import_class(module_path, class_name)
    except (ImportError, AttributeError) as exc:
        raise ValueError(
            f"llm_providers.{provider}.adapter: 无法导入 {module_path}.{class_name}: {exc}"
        ) from exc
    llm = cls(**constructor_params)
    logger.debug(
        "LLM 实例已创建: provider=%s, model=%s, adapter=%s",
        provider,
        llm_name,
        adapter,
    )
    return LLMWithMeta(llm=llm, provider=provider, model=llm_name)


def _build_params(
    provider_name: str,
    provider_config: dict,
    model_name: str,
    overrides: dict,
    *,
    adapter: str,
    request_timeout_seconds: float | None = None,
    timeout_is_explicit: bool = True,
    param_path: Callable[[str], str] = _default_param_path,
) -> dict:
    """按注册适配器及具体模型映射 LangChain 构造参数。"""
    invalid_keys = set(overrides) - _ALLOWED_PARAMS
    if invalid_keys:
        key = min(invalid_keys)
        raise ValueError(f"{param_path(key)}: 不支持的聊天模型参数")
    if "stream" in overrides and type(overrides["stream"]) is not bool:
        raise ValueError(f"{param_path('stream')}: 必须是布尔值")
    if request_timeout_seconds is not None:
        _validate_timeout_value(request_timeout_seconds, "request_timeout_seconds")
    elif "timeout" in overrides:
        _validate_timeout_value(overrides["timeout"], param_path("timeout"))
    if (
        request_timeout_seconds is not None
        and timeout_is_explicit
        and "timeout" in overrides
    ):
        raise ValueError(
            "params.timeout 与 request_timeout_seconds 冲突；请只保留业务超时字段"
        )
    if adapter == "dashscope_native" and request_timeout_seconds is not None:
        raise ValueError("request_timeout_seconds: 原生 DashScope 不支持该请求超时")
    if "max_retries" in overrides:
        retries = overrides["max_retries"]
        if type(retries) is not int or retries < 0:
            raise ValueError(f"{param_path('max_retries')}: 必须是非负整数")
        if adapter == "dashscope_native":
            raise ValueError(f"{param_path('max_retries')}: 原生 DashScope 路径不支持")
    if "reasoning_effort" in overrides:
        effort = overrides["reasoning_effort"]
        allowed = (
            {"none"}
            if model_name == "qwen4-max"
            else (
                {"low", "high", "max"}
                if provider_name == "kimi_code" and model_name == "k3.1-pro"
                else set()
            )
        )
        if not isinstance(effort, str) or effort not in allowed:
            raise ValueError(
                f"{param_path('reasoning_effort')}: {model_name} 不支持值 {effort!r}"
            )
    if adapter == "dashscope_native":
        return _build_native_params(
            provider_config, model_name, overrides, param_path=param_path
        )
    return _build_openai_params(
        provider_name,
        provider_config,
        model_name,
        overrides,
        request_timeout_seconds=request_timeout_seconds,
        param_path=param_path,
    )


def _build_native_params(
    provider_config: dict,
    model_name: str,
    overrides: dict,
    *,
    param_path: Callable[[str], str] = _default_param_path,
) -> dict:
    if "timeout" in overrides:
        raise ValueError(
            f"{param_path('timeout')}: 原生 DashScope 不支持该请求超时参数"
        )
    unsupported = {"thinking", "reasoning_effort"} & set(overrides)
    if unsupported:
        key = min(unsupported)
        raise ValueError(f"{param_path(key)}: 原生 DashScope 不支持")
    model_kwargs = {
        key: overrides[key]
        for key in (
            "temperature",
            "max_tokens",
            "top_p",
            "enable_thinking",
            "enable_search",
            "search_options",
            "response_format",
        )
        if key in overrides
    }
    params = {
        "model": model_name,
        "dashscope_api_key": provider_config["api_key"],
    }
    if "stream" in overrides:
        params["streaming"] = overrides["stream"]
    if model_kwargs:
        params["model_kwargs"] = model_kwargs
    return params


def _build_openai_params(
    provider_name: str,
    provider_config: dict,
    model_name: str,
    overrides: dict,
    *,
    request_timeout_seconds: float | None,
    param_path: Callable[[str], str] = _default_param_path,
) -> dict:
    ds_only = _DASHSCOPE_EXTRA_PARAMS & set(overrides)
    if ds_only and provider_name != "dashscope_openai":
        key = min(ds_only)
        raise ValueError(f"{param_path(key)}: 仅适用于 dashscope_openai")
    if provider_name == "kimi_code" and "temperature" in overrides:
        temperature = overrides["temperature"]
        if type(temperature) not in (int, float) or temperature != 1:
            raise ValueError(
                f"{param_path('temperature')}: "
                "Kimi k3.1-pro 思考模式仅支持 temperature=1"
            )
    if "thinking" in overrides:
        if provider_name == "kimi_code":
            raise ValueError(f"{param_path('thinking')}: Kimi 请使用 reasoning_effort")
        if provider_name not in {"deepseek", "glm_coding"}:
            raise ValueError(f"{param_path('thinking')}: 当前模型不支持")
        thinking = overrides["thinking"]
        if provider_name == "deepseek" and (
            not isinstance(thinking, dict)
            or thinking.get("type") not in ("enabled", "disabled")
        ):
            raise ValueError(
                f"{param_path('thinking')}.type: DeepSeek 仅支持 enabled 或 disabled"
            )
        if provider_name == "glm_coding" and (
            not isinstance(thinking, dict) or thinking.get("type") != "enabled"
        ):
            raise ValueError(
                f"{param_path('thinking')}.type: GLM Coding 仅支持 enabled；"
                "disabled 实测仍会产生推理，请省略 thinking"
            )
    use_extra_body_max_tokens = (
        provider_name,
        model_name,
    ) in _EXTRA_BODY_MAX_TOKEN_MODELS
    extra_body = {}
    if "thinking" in overrides:
        extra_body["thinking"] = overrides["thinking"]
    for key in _DASHSCOPE_EXTRA_PARAMS:
        if key in overrides:
            extra_body[key] = overrides[key]
    if "max_tokens" in overrides and use_extra_body_max_tokens:
        extra_body["max_tokens"] = overrides["max_tokens"]
    params = {
        "model": model_name,
        "api_key": provider_config["api_key"],
        "base_url": provider_config["base_url"],
        "use_responses_api": False,
    }
    for key in ("temperature", "top_p"):
        if key in overrides:
            params[key] = overrides[key]
    if "max_tokens" in overrides and not use_extra_body_max_tokens:
        params["max_tokens"] = overrides["max_tokens"]
    timeout = (
        request_timeout_seconds
        if request_timeout_seconds is not None
        else overrides.get("timeout")
    )
    if timeout is not None:
        params["request_timeout"] = timeout
    if "stream" in overrides:
        params["streaming"] = overrides["stream"]
    if "response_format" in overrides:
        params["response_format"] = overrides["response_format"]
    if "max_retries" in overrides:
        params["max_retries"] = overrides["max_retries"]
    if "reasoning_effort" in overrides:
        params["reasoning_effort"] = overrides["reasoning_effort"]
    if extra_body:
        params["extra_body"] = extra_body
    return params


def _import_class(module_path: str, class_name: str) -> type:
    """动态导入指定 LangChain 类。"""
    module = importlib.import_module(module_path)
    return getattr(module, class_name)


_THINK_TAG_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


def extract_llm_content(response) -> str:
    """提取最终文本，剥离兼容适配器混入正文的 think 标签。"""
    content = (response.content or "").strip()
    return _THINK_TAG_RE.sub("", content).strip()
