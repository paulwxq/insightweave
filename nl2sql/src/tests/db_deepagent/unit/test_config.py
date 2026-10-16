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


def test_error_messages_leak_no_password(
    config_dict: dict, db_agent_config: DbAgentConfig
):
    """§4.2：错误消息只含字段名，不含密码。"""
    config_dict["business_db"]["expected_database"] = "other"
    secret = db_agent_config.business_db.password
    with pytest.raises(DbAgentConfigError) as exc_info:
        run_config_checks(_validated(config_dict), check_embedding_profile=False)
    assert secret not in str(exc_info.value)
