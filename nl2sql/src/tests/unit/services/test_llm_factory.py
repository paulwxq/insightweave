"""provider + llm_name 注册解析、参数映射与本地 HTTP 请求测试。"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest
from langchain_core.messages import HumanMessage
from langchain_openai import ChatOpenAI

from src.services.llm_factory import (
    LLMWithMeta,
    _build_params,
    extract_llm_content,
    get_llm,
    resolve_llm_config,
)


def _providers():
    return {
        "dashscope": {
            "adapter": "dashscope_native",
            "api_key": "ds-key",
            "models": {"qwen-plus": {}, "qwen-turbo": {}},
        },
        "dashscope_openai": {
            "adapter": "openai_chat_completions",
            "api_key": "ds-key",
            "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "models": {
                "qwen3.8-max": {"params": {"enable_thinking": False}},
                "qwen3.8-flash": {
                    "params": {
                        "temperature": 0,
                        "max_tokens": 8192,
                        "enable_thinking": False,
                    }
                },
                "qwen4-max": {},
            },
        },
        "deepseek": {
            "adapter": "openai_chat_completions",
            "api_key": "deepseek-key",
            "base_url": "https://api.deepseek.com",
            "models": {
                "deepseek-flash": {},
                "deepseek-chat": {
                    "params": {"temperature": 0, "max_tokens": 8192, "timeout": 60}
                },
                "deepseek-reasoner": {"params": {"max_tokens": 32768}},
            },
        },
        "glm_coding": {
            "adapter": "openai_chat_completions",
            "api_key": "glm-key",
            "base_url": "https://open.bigmodel.cn/api/coding/paas/v4",
            "models": {"glm-5.4": {}, "glm-5.3": {}, "glm-5.3-flash": {}},
        },
        "kimi_code": {
            "adapter": "openai_chat_completions",
            "api_key": "kimi-key",
            "base_url": "https://api.kimi.com/coding/v1",
            "models": {"k3.1-pro": {}},
        },
        "minimax": {
            "adapter": "openai_chat_completions",
            "api_key": "minimax-key",
            "base_url": "https://api.minimaxi.com/v1",
            "models": {"MiniMax-M2.1": {"params": {"temperature": 1.0}}},
        },
    }


def _mock_config(providers=None):
    store = {"llm_providers": _providers() if providers is None else providers}
    config = MagicMock()
    config.get = lambda key, default=None: store.get(key, default)
    return config


def _chat_response(model="test-model"):
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 1,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "ok"},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


class TestResolveAndMerge:
    def test_dotted_model_id_and_provider_connection(self):
        with patch("src.services.llm_factory.get_config", return_value=_mock_config()):
            provider, model = resolve_llm_config("dashscope_openai", "qwen3.8-flash")
        assert provider["base_url"].endswith("/v1")
        assert model["params"]["max_tokens"] == 8192

    def test_same_model_name_uses_selected_provider(self):
        providers = _providers()
        providers["dashscope"]["models"]["shared"] = {}
        providers["deepseek"]["models"]["shared"] = {}
        with patch(
            "src.services.llm_factory.get_config", return_value=_mock_config(providers)
        ):
            provider, _ = resolve_llm_config("deepseek", "shared")
        assert provider["base_url"] == "https://api.deepseek.com"

    def test_empty_params_inherit_and_override_without_mutation(self):
        providers = _providers()
        providers["dashscope_openai"]["models"]["qwen3.8-flash"]["params"][
            "response_format"
        ] = {"type": "json_object", "schema": {"old": True}}
        original = _mock_config(providers)
        with (
            patch("src.services.llm_factory.get_config", return_value=original),
            patch("src.services.llm_factory._import_class") as importer,
        ):
            client = MagicMock()
            importer.return_value = client
            get_llm(
                "dashscope_openai",
                "qwen3.8-flash",
                params={
                    "response_format": {"type": "text"},
                    "max_retries": 0,
                    "stream": False,
                },
                request_timeout_seconds=30,
            )
            get_llm(
                "dashscope_openai",
                "qwen3.8-flash",
                params={},
                request_timeout_seconds=30,
            )
        first = client.call_args_list[0].kwargs
        assert first["temperature"] == 0
        assert first["max_retries"] == 0
        assert first["streaming"] is False
        assert first["response_format"] == {"type": "text"}
        assert first["max_tokens"] == 8192
        assert first["extra_body"] == {"enable_thinking": False}
        assert client.call_args_list[1].kwargs["response_format"] == {
            "type": "json_object",
            "schema": {"old": True},
        }
        assert providers["dashscope_openai"]["models"]["qwen3.8-flash"]["params"][
            "response_format"
        ] == {"type": "json_object", "schema": {"old": True}}

    @pytest.mark.parametrize(
        ("provider", "model", "path"),
        [
            ("missing", "qwen3.8-max", "llm_providers.missing"),
            ("dashscope_openai", "qwen3-max", "models.qwen3-max"),
        ],
    )
    def test_unknown_reference_has_path(self, provider, model, path):
        with (
            patch("src.services.llm_factory.get_config", return_value=_mock_config()),
            pytest.raises(ValueError, match=path),
        ):
            resolve_llm_config(provider, model)

    def test_params_must_be_mapping(self):
        with (
            patch("src.services.llm_factory.get_config", return_value=_mock_config()),
            pytest.raises(ValueError, match="params"),
        ):
            get_llm("dashscope_openai", "qwen3.8-max", params=[])

    def test_unselected_provider_missing_key_does_not_block(self):
        providers = _providers()
        providers["glm_coding"]["api_key"] = None
        providers["kimi_code"]["api_key"] = None
        with (
            patch(
                "src.services.llm_factory.get_config",
                return_value=_mock_config(providers),
            ),
            patch("src.services.llm_factory._import_class") as importer,
        ):
            importer.return_value = MagicMock()
            result = get_llm(
                "dashscope_openai", "qwen3.8-max", request_timeout_seconds=30
            )
        assert isinstance(result, LLMWithMeta)

    def test_openai_timeout_is_required_when_all_sources_are_missing(self):
        with (
            patch("src.services.llm_factory.get_config", return_value=_mock_config()),
            patch("src.services.llm_factory._import_class") as importer,
            pytest.raises(ValueError) as exc_info,
        ):
            get_llm("dashscope_openai", "qwen3.8-max")

        message = str(exc_info.value)
        assert "request_timeout_seconds" in message
        assert "params.timeout" in message
        assert (
            "llm_providers.dashscope_openai.models.qwen3.8-max.params.timeout"
            in message
        )
        importer.assert_not_called()

    def test_openai_timeout_accepts_all_three_sources(self):
        with (
            patch("src.services.llm_factory.get_config", return_value=_mock_config()),
            patch("src.services.llm_factory._import_class") as importer,
        ):
            importer.return_value = MagicMock()
            get_llm("dashscope_openai", "qwen3.8-max", request_timeout_seconds=30)
            get_llm("dashscope_openai", "qwen3.8-max", params={"timeout": 20})
            get_llm("deepseek", "deepseek-chat")

        timeouts = [
            call.kwargs["request_timeout"]
            for call in importer.return_value.call_args_list
        ]
        assert timeouts == [30, 20, 60]

    @pytest.mark.parametrize("source", ["role", "business", "registry"])
    @pytest.mark.parametrize(
        "value", [0, -5, "30", [5, 30], True, float("nan"), float("inf")]
    )
    def test_timeout_must_be_finite_positive_number(self, source, value):
        providers = _providers()
        kwargs = {}
        if source == "role":
            kwargs["request_timeout_seconds"] = value
            expected_path = "request_timeout_seconds"
        elif source == "business":
            kwargs["params"] = {"timeout": value}
            expected_path = "params.timeout"
        else:
            providers["dashscope_openai"]["models"]["qwen3.8-max"]["params"][
                "timeout"
            ] = value
            expected_path = (
                "llm_providers.dashscope_openai.models.qwen3.8-max.params.timeout"
            )

        with (
            patch(
                "src.services.llm_factory.get_config",
                return_value=_mock_config(providers),
            ),
            patch("src.services.llm_factory._import_class") as importer,
            pytest.raises(ValueError) as exc_info,
        ):
            get_llm("dashscope_openai", "qwen3.8-max", **kwargs)

        assert expected_path in str(exc_info.value)
        assert "有限且大于 0" in str(exc_info.value)
        importer.assert_not_called()

    def test_inherited_invalid_value_has_registry_path(self):
        providers = _providers()
        providers["glm_coding"]["models"]["glm-5.4"]["params"] = {
            "thinking": {"type": "disabled"},
            "timeout": 60,
        }
        with (
            patch(
                "src.services.llm_factory.get_config",
                return_value=_mock_config(providers),
            ),
            patch("src.services.llm_factory._import_class") as importer,
            pytest.raises(ValueError) as exc_info,
        ):
            get_llm("glm_coding", "glm-5.4")

        assert "llm_providers.glm_coding.models.glm-5.4.params.thinking.type" in str(
            exc_info.value
        )
        importer.assert_not_called()

    def test_selected_provider_missing_key_has_field_path(self):
        providers = _providers()
        providers["glm_coding"]["api_key"] = None
        with (
            patch(
                "src.services.llm_factory.get_config",
                return_value=_mock_config(providers),
            ),
            pytest.raises(ValueError, match="llm_providers.glm_coding.api_key"),
        ):
            get_llm("glm_coding", "glm-5.4")

    def test_unknown_model_rejected_before_client_import(self):
        with (
            patch("src.services.llm_factory.get_config", return_value=_mock_config()),
            patch("src.services.llm_factory._import_class") as importer,
            pytest.raises(ValueError, match="qwen3-max"),
        ):
            get_llm("dashscope_openai", "qwen3-max")
        importer.assert_not_called()

    def test_deepseek_default_url_and_explicit_empty_rejected(self):
        providers = _providers()
        del providers["deepseek"]["base_url"]
        with (
            patch(
                "src.services.llm_factory.get_config",
                return_value=_mock_config(providers),
            ),
            patch("src.services.llm_factory._import_class") as importer,
        ):
            importer.return_value = MagicMock()
            get_llm("deepseek", "deepseek-flash", request_timeout_seconds=30)
        assert (
            importer.return_value.call_args.kwargs["base_url"]
            == "https://api.deepseek.com"
        )
        providers["deepseek"]["base_url"] = ""
        with (
            patch(
                "src.services.llm_factory.get_config",
                return_value=_mock_config(providers),
            ),
            pytest.raises(ValueError, match="llm_providers.deepseek.base_url"),
        ):
            get_llm("deepseek", "deepseek-flash", request_timeout_seconds=30)


class TestBuildParams:
    def test_native_dashscope_model_kwargs_and_timeout_rejected(self):
        params = _build_params(
            "dashscope",
            _providers()["dashscope"],
            "qwen-plus",
            {"temperature": 0, "enable_thinking": False},
            adapter="dashscope_native",
        )
        assert params["model"] == "qwen-plus"
        assert params["dashscope_api_key"] == "ds-key"
        assert params["model_kwargs"] == {
            "temperature": 0,
            "enable_thinking": False,
        }
        with pytest.raises(ValueError, match="request_timeout_seconds"):
            _build_params(
                "dashscope",
                _providers()["dashscope"],
                "qwen-plus",
                {},
                adapter="dashscope_native",
                request_timeout_seconds=15,
            )

    def test_native_legacy_parameter_mapping_preserved(self):
        params = _build_params(
            "dashscope",
            _providers()["dashscope"],
            "qwen-plus",
            {
                "max_tokens": 1500,
                "top_p": 0.9,
                "enable_search": True,
                "search_options": {"forced_search": True},
                "response_format": {"type": "json_object"},
                "stream": True,
            },
            adapter="dashscope_native",
        )
        assert params["streaming"] is True
        assert params["model_kwargs"] == {
            "max_tokens": 1500,
            "top_p": 0.9,
            "enable_search": True,
            "search_options": {"forced_search": True},
            "response_format": {"type": "json_object"},
        }

    def test_openai_direct_and_extra_body_fields(self):
        params = _build_params(
            "dashscope_openai",
            _providers()["dashscope_openai"],
            "qwen3.8-flash",
            {
                "temperature": 0,
                "max_tokens": 100,
                "enable_thinking": False,
                "response_format": {"type": "json_object"},
                "stream": False,
                "max_retries": 2,
            },
            adapter="openai_chat_completions",
            request_timeout_seconds=15,
        )
        assert params["request_timeout"] == 15
        assert params["streaming"] is False
        assert params["response_format"] == {"type": "json_object"}
        assert params["max_retries"] == 2
        assert params["max_tokens"] == 100
        assert params["extra_body"] == {"enable_thinking": False}
        assert params["use_responses_api"] is False

    @pytest.mark.parametrize("model", ["deepseek-chat", "deepseek-reasoner"])
    def test_deepseek_existing_max_tokens_mapping(self, model):
        params = _build_params(
            "deepseek",
            _providers()["deepseek"],
            model,
            {"max_tokens": 100},
            adapter="openai_chat_completions",
        )
        assert params["max_tokens"] == 100
        assert "extra_body" not in params

    @pytest.mark.parametrize(
        "model", ["deepseek-flash", "glm-5.4", "glm-5.3", "glm-5.3-flash"]
    )
    def test_four_models_max_tokens_uses_extra_body(self, model):
        provider = "deepseek" if model == "deepseek-flash" else "glm_coding"
        params = _build_params(
            provider,
            _providers()[provider],
            model,
            {"max_tokens": 100},
            adapter="openai_chat_completions",
        )
        assert params["extra_body"] == {"max_tokens": 100}
        assert "max_tokens" not in params

    def test_deepseek_flash_thinking_and_max_tokens_share_extra_body(self):
        params = _build_params(
            "deepseek",
            _providers()["deepseek"],
            "deepseek-flash",
            {"thinking": {"type": "disabled"}, "max_tokens": 100},
            adapter="openai_chat_completions",
        )
        assert params["extra_body"] == {
            "thinking": {"type": "disabled"},
            "max_tokens": 100,
        }
        assert "max_tokens" not in params

    @pytest.mark.parametrize(
        "thinking", ["disabled", None, {}, {"type": "unknown"}, {"type": []}]
    )
    def test_deepseek_thinking_requires_supported_type(self, thinking):
        with pytest.raises(ValueError, match="params.thinking.type"):
            _build_params(
                "deepseek",
                _providers()["deepseek"],
                "deepseek-flash",
                {"thinking": thinking},
                adapter="openai_chat_completions",
            )

    @pytest.mark.parametrize("thinking_type", ["enabled", "disabled"])
    def test_deepseek_thinking_accepts_supported_types(self, thinking_type):
        params = _build_params(
            "deepseek",
            _providers()["deepseek"],
            "deepseek-flash",
            {"thinking": {"type": thinking_type}},
            adapter="openai_chat_completions",
        )
        assert params["extra_body"] == {"thinking": {"type": thinking_type}}

    def test_kimi_defaults_and_reasoning_effort(self):
        params = _build_params(
            "kimi_code",
            _providers()["kimi_code"],
            "k3.1-pro",
            {"reasoning_effort": "high"},
            adapter="openai_chat_completions",
        )
        assert params["reasoning_effort"] == "high"
        assert "temperature" not in params
        assert "extra_body" not in params
        with pytest.raises(ValueError, match="reasoning_effort"):
            _build_params(
                "kimi_code",
                _providers()["kimi_code"],
                "k3.1-pro",
                {"reasoning_effort": "none"},
                adapter="openai_chat_completions",
            )

    @pytest.mark.parametrize("temperature", [0, 0.6, 2, True, "1", None])
    def test_kimi_unsupported_temperature_is_rejected(self, temperature):
        with pytest.raises(ValueError, match="params.temperature"):
            _build_params(
                "kimi_code",
                _providers()["kimi_code"],
                "k3.1-pro",
                {"temperature": temperature},
                adapter="openai_chat_completions",
            )

    @pytest.mark.parametrize("temperature", [1, 1.0])
    def test_kimi_thinking_mode_accepts_temperature_one(self, temperature):
        params = _build_params(
            "kimi_code",
            _providers()["kimi_code"],
            "k3.1-pro",
            {"temperature": temperature},
            adapter="openai_chat_completions",
        )
        assert params["temperature"] == 1

    @pytest.mark.parametrize("model", ["glm-5.4", "glm-5.3", "glm-5.3-flash"])
    def test_glm_disabled_thinking_is_rejected(self, model):
        with pytest.raises(ValueError, match="params.thinking.type"):
            _build_params(
                "glm_coding",
                _providers()["glm_coding"],
                model,
                {"thinking": {"type": "disabled"}},
                adapter="openai_chat_completions",
            )

    def test_glm_enabled_thinking_is_allowed(self):
        params = _build_params(
            "glm_coding",
            _providers()["glm_coding"],
            "glm-5.4",
            {"thinking": {"type": "enabled"}},
            adapter="openai_chat_completions",
        )
        assert params["extra_body"] == {"thinking": {"type": "enabled"}}

    @pytest.mark.parametrize(
        ("provider", "model", "value"),
        [
            ("kimi_code", "k3.1-pro", "low"),
            ("kimi_code", "k3.1-pro", "max"),
            ("dashscope_openai", "qwen4-max", "none"),
        ],
    )
    def test_reasoning_effort_verified_values(self, provider, model, value):
        params = _build_params(
            provider,
            _providers()[provider],
            model,
            {"reasoning_effort": value},
            adapter="openai_chat_completions",
        )
        assert params["reasoning_effort"] == value

    def test_reasoning_effort_container_has_config_error(self):
        with pytest.raises(ValueError, match="params.reasoning_effort"):
            _build_params(
                "kimi_code",
                _providers()["kimi_code"],
                "k3.1-pro",
                {"reasoning_effort": []},
                adapter="openai_chat_completions",
            )

    @pytest.mark.parametrize("stream", [0, 1, "false", None, []])
    def test_stream_must_be_boolean(self, stream):
        with pytest.raises(ValueError, match="params.stream"):
            _build_params(
                "dashscope_openai",
                _providers()["dashscope_openai"],
                "qwen3.8-max",
                {"stream": stream},
                adapter="openai_chat_completions",
            )

    def test_invalid_param_has_field_path(self):
        with pytest.raises(ValueError, match="params.unknown"):
            _build_params(
                "dashscope_openai",
                _providers()["dashscope_openai"],
                "qwen3.8-max",
                {"unknown": 1},
                adapter="openai_chat_completions",
            )

    def test_registry_timeout_does_not_conflict_with_role_timeout(self):
        with (
            patch("src.services.llm_factory.get_config", return_value=_mock_config()),
            patch("src.services.llm_factory._import_class") as importer,
        ):
            importer.return_value = MagicMock()
            get_llm("deepseek", "deepseek-chat", request_timeout_seconds=15)
        assert importer.return_value.call_args.kwargs["request_timeout"] == 15

    def test_timeout_conflict_has_paths(self):
        with pytest.raises(ValueError, match="params.timeout"):
            _build_params(
                "dashscope_openai",
                _providers()["dashscope_openai"],
                "qwen3.8-max",
                {"timeout": 10},
                adapter="openai_chat_completions",
                request_timeout_seconds=15,
            )

    @pytest.mark.parametrize("value", [-1, None, True, "0", 1.5])
    def test_max_retries_must_be_nonnegative_int(self, value):
        with pytest.raises(ValueError, match="max_retries"):
            _build_params(
                "dashscope_openai",
                _providers()["dashscope_openai"],
                "qwen3.8-max",
                {"max_retries": value},
                adapter="openai_chat_completions",
            )


class TestActualHttpRequests:
    def _invoke(self, provider, model, params, expected_url, providers=None):
        captured = []

        def handler(request):
            captured.append(request)
            return httpx.Response(200, json=_chat_response(model))

        transport = httpx.MockTransport(handler)
        real_class = ChatOpenAI

        def client_factory(**kwargs):
            kwargs["http_client"] = httpx.Client(transport=transport)
            return real_class(**kwargs)

        with (
            patch(
                "src.services.llm_factory.get_config",
                return_value=_mock_config(providers),
            ),
            patch(
                "src.services.llm_factory._import_class", return_value=client_factory
            ),
        ):
            llm = get_llm(
                provider, model, params=params, request_timeout_seconds=15
            ).llm
            llm.invoke([HumanMessage(content="ping")])
        assert len(captured) == 1
        request = captured[0]
        assert str(request.url) == expected_url
        import json

        return json.loads(request.content)

    def test_deepseek_flash_actual_uri_and_json(self):
        body = self._invoke(
            "deepseek",
            "deepseek-flash",
            {"thinking": {"type": "disabled"}, "max_tokens": 100},
            "https://api.deepseek.com/chat/completions",
        )
        assert body["model"] == "deepseek-flash"
        assert body["thinking"] == {"type": "disabled"}
        assert body["max_tokens"] == 100
        assert "max_completion_tokens" not in body

    def test_glm_max_tokens_and_omission(self):
        url = "https://open.bigmodel.cn/api/coding/paas/v4/chat/completions"
        body = self._invoke("glm_coding", "glm-5.4", {"max_tokens": 100}, url)
        assert body["max_tokens"] == 100
        assert "max_completion_tokens" not in body
        assert "thinking" not in body and "reasoning_effort" not in body
        body = self._invoke("glm_coding", "glm-5.4", {}, url)
        assert "max_tokens" not in body and "max_completion_tokens" not in body

    def test_max_tokens_mapping_is_scoped_to_verified_provider(self):
        providers = _providers()
        providers["generic"] = {
            "adapter": "openai_chat_completions",
            "api_key": "generic-key",
            "base_url": "https://generic.example/v1",
            "models": {"glm-5.4": {}},
        }
        verified_body = self._invoke(
            "glm_coding",
            "glm-5.4",
            {"max_tokens": 100},
            "https://open.bigmodel.cn/api/coding/paas/v4/chat/completions",
            providers=providers,
        )
        generic_body = self._invoke(
            "generic",
            "glm-5.4",
            {"max_tokens": 100},
            "https://generic.example/v1/chat/completions",
            providers=providers,
        )

        assert verified_body["max_tokens"] == 100
        assert "max_completion_tokens" not in verified_body
        assert generic_body["max_completion_tokens"] == 100
        assert "max_tokens" not in generic_body

    def test_kimi_request_omits_qwen_temperature_and_thinking(self):
        body = self._invoke(
            "kimi_code",
            "k3.1-pro",
            {},
            "https://api.kimi.com/coding/v1/chat/completions",
        )
        assert body["model"] == "k3.1-pro"
        assert "temperature" not in body
        assert "thinking" not in body and "reasoning_effort" not in body
        assert "enable_thinking" not in body

    def test_qwen_compatible_defaults_reach_http(self):
        body = self._invoke(
            "dashscope_openai",
            "qwen3.8-flash",
            {},
            "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
        )
        assert body["model"] == "qwen3.8-flash"
        assert body["temperature"] == 0
        assert body["max_completion_tokens"] == 8192
        assert body["enable_thinking"] is False


def test_extract_llm_content_preserved():
    response = MagicMock()
    response.content = "<think>draft</think>\nSELECT 1"
    assert extract_llm_content(response) == "SELECT 1"
    response.content = "<THINK>one</THINK>middle<Think>two</Think>final"
    assert extract_llm_content(response) == "middlefinal"
    response.content = None
    assert extract_llm_content(response) == ""
    response.content = '<think>draft</think>{"rewritten_query":"月销售额"}'
    import json

    assert json.loads(extract_llm_content(response)) == {"rewritten_query": "月销售额"}
