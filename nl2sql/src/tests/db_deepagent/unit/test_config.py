"""配置模型与校验测试（docs/v4/5 §4.2；§19 步骤 1）"""

from __future__ import annotations

import pytest

from src.modules.db_deepagent.config import (
    DbAgentConfig,
    compute_validation_fingerprint,
    run_config_checks,
)
from src.modules.db_deepagent.errors import DbAgentConfigError


def _validated(config_dict: dict) -> DbAgentConfig:
    return DbAgentConfig.model_validate(config_dict)


def test_config_loads_and_checks_pass(db_agent_config: DbAgentConfig):
    """真实 §4.1 配置通过全部检查；两个业务库标识为 orders/lg_persist。"""
    assert db_agent_config.business_db.database == "orders"
    assert db_agent_config.platform_db.database == "lg_persist"
    assert db_agent_config.platform_db.schema_name == "db_deepagent"
    assert db_agent_config.persistence.mode in ("postgres", "memory")


def test_validation_fingerprint_stable_and_sensitive(db_agent_config: DbAgentConfig):
    """指纹 = sha256(canonical_json(validation 段) + GUARD_RULES_VERSION)，随校验配置变化。"""
    fp = compute_validation_fingerprint(db_agent_config)
    assert len(fp) == 64
    assert fp == compute_validation_fingerprint(db_agent_config)  # 稳定

    changed = db_agent_config.model_dump(mode="json")
    changed["validation"]["guard"]["enabled"] = False
    assert compute_validation_fingerprint(DbAgentConfig.model_validate(changed)) != fp

    # 与 validation 段无关的配置不影响指纹
    other = db_agent_config.model_dump(mode="json")
    other["budget"]["max_tool_calls_per_run"] += 1
    assert compute_validation_fingerprint(DbAgentConfig.model_validate(other)) == fp


def test_business_target_must_be_orders(config_dict: dict):
    config_dict["business_db"]["expected_database"] = "other"
    cfg = _validated(config_dict)
    with pytest.raises(DbAgentConfigError, match="orders"):
        run_config_checks(cfg, check_embedding_profile=False)


def test_platform_target_and_schema_rules(config_dict: dict):
    config_dict["platform_db"]["expected_database"] = "wrong"
    with pytest.raises(DbAgentConfigError, match="lg_persist"):
        run_config_checks(_validated(config_dict), check_embedding_profile=False)

    config_dict["platform_db"]["expected_database"] = "lg_persist"
    # model_dump 输出 python 字段名 schema_name（别名 schema 仅用于 yaml 载入）
    config_dict["platform_db"]["schema_name"] = "public"
    with pytest.raises(DbAgentConfigError, match="public"):
        run_config_checks(_validated(config_dict), check_embedding_profile=False)

    config_dict["platform_db"]["schema_name"] = "bad schema name; --"
    with pytest.raises(DbAgentConfigError, match="标识符"):
        run_config_checks(_validated(config_dict), check_embedding_profile=False)


def test_summarization_threshold_ordering(config_dict: dict):
    config_dict["summarization"]["recent_context_tokens"] = 100000
    with pytest.raises(DbAgentConfigError, match="recent_context_tokens"):
        run_config_checks(_validated(config_dict), check_embedding_profile=False)


def test_trim_tokens_to_summarize_positive_or_none(config_dict: dict):
    config_dict["summarization"]["trim_tokens_to_summarize"] = 0
    with pytest.raises(Exception, match="trim_tokens_to_summarize"):
        DbAgentConfig.model_validate(config_dict)


def test_summarization_independent_profile_loaded(db_agent_config: DbAgentConfig):
    """doc 5.1 §3：主模型仍 qwen3.8-max；摘要独立 Flash profile + 120s 超时，阈值不变。"""
    assert db_agent_config.model.provider == "dashscope_openai"
    assert db_agent_config.model.llm_name == "qwen3.8-max"
    s = db_agent_config.summarization
    assert s.provider == "dashscope_openai"
    assert s.llm_name == "qwen3.8-flash"
    assert s.params == {}
    assert s.request_timeout_seconds == 120
    # 原阈值与 trim=None 保留（doc 5.1 Global Constraints）
    assert s.summary_trigger_tokens == 96000
    assert s.recent_context_tokens == 24000
    assert s.trim_tokens_to_summarize is None


def _assert_validation_error(config_dict: dict, *, loc: tuple, err_type: str) -> None:
    """断言校验失败于指定字段路径与具体错误类型（防 extra_forbidden 假阳性）。"""
    from pydantic import ValidationError

    with pytest.raises(ValidationError) as exc_info:
        DbAgentConfig.model_validate(config_dict)
    errors = {(tuple(e["loc"]), e["type"]) for e in exc_info.value.errors()}
    assert (loc, err_type) in errors, f"期望 {(loc, err_type)}，实际 {sorted(errors)}"


def test_summarization_profile_and_timeout_required(config_dict: dict):
    """llm_name 与 request_timeout_seconds 必填：缺失报 missing，路径含 summarization。"""
    import copy

    missing_profile = copy.deepcopy(config_dict)
    missing_profile["summarization"].pop("llm_name", None)
    _assert_validation_error(
        missing_profile,
        loc=("summarization", "llm_name"),
        err_type="missing",
    )

    missing_timeout = copy.deepcopy(config_dict)
    missing_timeout["summarization"].pop("request_timeout_seconds", None)
    _assert_validation_error(
        missing_timeout,
        loc=("summarization", "request_timeout_seconds"),
        err_type="missing",
    )


def test_summarization_profile_nonempty_and_timeout_positive(config_dict: dict):
    """空 profile 报 string_too_short；非正超时报 greater_than。"""
    import copy

    empty_profile = copy.deepcopy(config_dict)
    empty_profile["summarization"]["llm_name"] = ""
    _assert_validation_error(
        empty_profile,
        loc=("summarization", "llm_name"),
        err_type="string_too_short",
    )

    bad_timeout = copy.deepcopy(config_dict)
    bad_timeout["summarization"]["request_timeout_seconds"] = 0
    _assert_validation_error(
        bad_timeout,
        loc=("summarization", "request_timeout_seconds"),
        err_type="greater_than",
    )


def test_summarization_profile_must_exist_in_global_registry(config_dict: dict):
    """引用不存在的 profile → _check_summarization 明确报错并指向全局注册位置。"""
    config_dict["summarization"]["llm_name"] = "no_such_profile"
    cfg = _validated(config_dict)
    with pytest.raises(DbAgentConfigError, match="models.no_such_profile"):
        run_config_checks(cfg, check_embedding_profile=False)


def test_main_model_unknown_provider_has_role_path(config_dict: dict):
    config_dict["model"]["provider"] = "missing_provider"
    cfg = _validated(config_dict)
    with pytest.raises(DbAgentConfigError) as exc_info:
        run_config_checks(cfg, check_embedding_profile=False)
    assert str(exc_info.value).startswith("model: llm_providers.missing_provider")


def test_registry_unknown_param_fails_config_check_with_full_path(
    config_dict: dict, monkeypatch
):
    import copy

    from src.services.config_loader import get_config

    global_config = copy.deepcopy(get_config())
    global_config["llm_providers"]["dashscope_openai"]["models"]["qwen3.8-max"][
        "params"
    ]["temperautre"] = 0
    monkeypatch.setattr("src.services.llm_factory.get_config", lambda: global_config)

    with pytest.raises(DbAgentConfigError) as exc_info:
        run_config_checks(_validated(config_dict), check_embedding_profile=False)

    message = str(exc_info.value)
    assert message.startswith(
        "model: llm_providers.dashscope_openai.models.qwen3.8-max.params.temperautre"
    )
    assert "model.params: params.temperautre" not in message


@pytest.mark.parametrize("value", [None, {"type": "text"}, []])
def test_time_resolution_requires_json_object(config_dict: dict, value):
    if value is None:
        config_dict["time_resolution"]["params"].pop("response_format")
    else:
        config_dict["time_resolution"]["params"]["response_format"] = value

    with pytest.raises(
        DbAgentConfigError, match="time_resolution.params.response_format.type"
    ):
        run_config_checks(_validated(config_dict), check_embedding_profile=False)


def test_time_resolution_inherited_json_error_has_registry_path(
    config_dict: dict, monkeypatch
):
    import copy

    from src.services.config_loader import get_config

    config_dict["time_resolution"]["params"].pop("response_format")
    global_config = copy.deepcopy(get_config())
    global_config["llm_providers"]["dashscope_openai"]["models"]["qwen3.8-flash"][
        "params"
    ]["response_format"] = {"type": "text"}
    monkeypatch.setattr("src.services.llm_factory.get_config", lambda: global_config)

    with pytest.raises(DbAgentConfigError) as exc_info:
        run_config_checks(_validated(config_dict), check_embedding_profile=False)

    assert (
        "llm_providers.dashscope_openai.models.qwen3.8-flash.params."
        "response_format.type" in str(exc_info.value)
    )


def test_time_resolution_inherits_json_object(config_dict: dict, monkeypatch):
    import copy

    from src.services.config_loader import get_config

    config_dict["time_resolution"]["params"].pop("response_format")
    global_config = copy.deepcopy(get_config())
    global_config["llm_providers"]["dashscope_openai"]["models"]["qwen3.8-flash"][
        "params"
    ]["response_format"] = {"type": "json_object"}
    monkeypatch.setattr("src.services.llm_factory.get_config", lambda: global_config)

    run_config_checks(_validated(config_dict), check_embedding_profile=False)


@pytest.mark.parametrize("role", ["model", "summarization", "time_resolution"])
@pytest.mark.parametrize("value", [True, "15"])
def test_yaml_role_timeout_rejects_bool_and_string_before_assembly(
    config_dict: dict, tmp_path, role, value
):
    import yaml

    from src.modules.db_deepagent.config import load_base_db_agent_config

    config_dict[role]["request_timeout_seconds"] = value
    config_path = tmp_path / "db_deepagent.yaml"
    config_path.write_text(
        yaml.safe_dump(config_dict, allow_unicode=True), encoding="utf-8"
    )

    with pytest.raises(DbAgentConfigError) as exc_info:
        load_base_db_agent_config(config_path)

    assert f"{role}.request_timeout_seconds" in str(exc_info.value)


def test_yaml_timeout_env_reference_reaches_model_assembly(
    config_dict: dict, tmp_path, monkeypatch
):
    from unittest.mock import MagicMock

    import yaml

    from src.modules.db_deepagent.agent import build_model
    from src.modules.db_deepagent.config import load_base_db_agent_config
    from src.services.llm_factory import LLMWithMeta

    config_dict["model"]["request_timeout_seconds"] = "${TEST_MODEL_TIMEOUT_SECONDS}"
    config_path = tmp_path / "db_deepagent.yaml"
    config_path.write_text(
        yaml.safe_dump(config_dict, allow_unicode=True), encoding="utf-8"
    )
    monkeypatch.setenv("TEST_MODEL_TIMEOUT_SECONDS", "20")
    captured = {}

    def fake_get_llm(provider, llm_name, *, params, request_timeout_seconds):
        captured["timeout"] = request_timeout_seconds
        return LLMWithMeta(MagicMock(), provider, llm_name)

    monkeypatch.setattr("src.services.llm_factory.get_llm", fake_get_llm)
    cfg = load_base_db_agent_config(config_path)
    cfg.model.retry.enabled = False
    build_model(cfg)

    assert captured["timeout"] == 20


def test_summarization_change_keeps_validation_fingerprint(config_dict: dict):
    """摘要配置不进入 SQL validation_fingerprint（doc 5.1 §4.1）。"""
    import copy

    fp = compute_validation_fingerprint(_validated(config_dict))
    changed = copy.deepcopy(config_dict)
    changed["summarization"]["llm_name"] = "qwen3.8-max"
    changed["summarization"]["request_timeout_seconds"] = 60
    assert compute_validation_fingerprint(_validated(changed)) == fp


def test_retrieval_scores_bounded(config_dict: dict):
    config_dict["retrieval"]["dim_value_min_score"] = 1.5
    with pytest.raises(Exception, match="dim_value_min_score"):
        DbAgentConfig.model_validate(config_dict)


def test_maintenance_lock_lt_statement_timeout(config_dict: dict):
    config_dict["maintenance"]["lock_timeout_ms"] = 60000
    with pytest.raises(DbAgentConfigError, match="lock_timeout_ms"):
        run_config_checks(_validated(config_dict), check_embedding_profile=False)


def test_semantic_explain_analyze_forbidden(config_dict: dict):
    config_dict["validation"]["semantic"]["explain_analyze"] = True
    with pytest.raises(DbAgentConfigError, match="explain_analyze"):
        run_config_checks(_validated(config_dict), check_embedding_profile=False)


def test_fixed_now_requires_timezone(config_dict: dict):
    config_dict["clock"]["fixed_now"] = "2026-10-13T12:00:00"
    with pytest.raises(DbAgentConfigError, match="fixed_now"):
        run_config_checks(_validated(config_dict), check_embedding_profile=False)
    config_dict["clock"]["fixed_now"] = "2026-10-13T12:00:00+08:00"
    run_config_checks(
        _validated(config_dict), check_embedding_profile=False
    )  # 带时区则通过


def test_embedding_profile_must_be_concrete_key(config_dict: dict):
    config_dict["embedding"]["profile"] = "active"
    with pytest.raises(DbAgentConfigError, match="active"):
        run_config_checks(_validated(config_dict))
    config_dict["embedding"]["profile"] = "no_such_profile"
    with pytest.raises(DbAgentConfigError, match="no_such_profile"):
        run_config_checks(_validated(config_dict))


def test_unknown_keys_rejected(config_dict: dict):
    config_dict["model"]["unknown_key"] = 1
    with pytest.raises(Exception, match="unknown_key"):
        DbAgentConfig.model_validate(config_dict)


def test_results_no_max_tool_output_chars(config_dict: dict):
    """doc 9：results.max_tool_output_chars 已删除；真实 YAML 与模型一致。"""
    from src.modules.db_deepagent.config import ResultsConfig

    assert "max_tool_output_chars" not in ResultsConfig.model_fields
    assert "max_tool_output_chars" not in config_dict["results"]
    # 其余业务行数限制保留（doc 9 §2：业务层继续决定数据量）
    assert "preview_rows" in ResultsConfig.model_fields
    assert "read_max_rows" in ResultsConfig.model_fields


def test_readded_max_tool_output_chars_rejected(config_dict: dict):
    """严格配置模型：重新加回旧字段报 extra_forbidden，不再是软上限。"""
    config_dict["results"]["max_tool_output_chars"] = 12000
    with pytest.raises(Exception, match="max_tool_output_chars"):
        DbAgentConfig.model_validate(config_dict)


def test_error_messages_leak_no_password(
    config_dict: dict, db_agent_config: DbAgentConfig
):
    """§4.2：错误消息只含字段名，不含密码。"""
    config_dict["business_db"]["expected_database"] = "other"
    secret = db_agent_config.business_db.password
    with pytest.raises(DbAgentConfigError) as exc_info:
        run_config_checks(_validated(config_dict), check_embedding_profile=False)
    assert secret not in str(exc_info.value)


# ----------------------------------------------------------------------------
# doc 6.2 §7.5：主模型有限重试配置
# ----------------------------------------------------------------------------


class TestModelRetryConfig:
    def test_code_default_disabled(self):
        from src.modules.db_deepagent.config import ModelRetryConfig

        cfg = ModelRetryConfig()
        assert cfg.enabled is False  # 代码默认关闭：旧配置不改变启动行为
        assert cfg.max_retries == 2

    def test_standard_yaml_enables_retry(self, db_agent_config: DbAgentConfig):
        assert db_agent_config.model.retry.enabled is True
        assert db_agent_config.model.retry.max_retries == 2

    @pytest.mark.parametrize("bad", [-1, 3], ids=["below", "above"])
    def test_max_retries_bounds(self, config_dict: dict, bad):
        from pydantic import ValidationError

        config_dict["model"]["retry"] = {"enabled": True, "max_retries": bad}
        with pytest.raises(ValidationError):
            DbAgentConfig.model_validate(config_dict)

    def test_unknown_retry_keys_rejected(self, config_dict: dict):
        from pydantic import ValidationError

        config_dict["model"]["retry"] = {"enabled": True, "backoff": 1}
        with pytest.raises(ValidationError):
            DbAgentConfig.model_validate(config_dict)

    def test_model_retry_defaults_when_absent(self, config_dict: dict):
        del config_dict["model"]["retry"]
        cfg = DbAgentConfig.model_validate(config_dict)
        assert cfg.model.retry.enabled is False  # 未声明 → 默认关闭
