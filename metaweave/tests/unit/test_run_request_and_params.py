"""运行请求 default/custom 解析与步骤参数校验（doc 6.2 §6.2、A05/A23）。"""

import pytest

from backend.jobs.job_params import (
    COLLECTION_STEPS,
    StepConfigError,
    validate_step_config,
    validate_steps,
)
from backend.jobs.run_request import resolve_run_request

REV_STEPS = ["ddl", "json", "md", "domain", "rel"]
REV_CONFIG = {"domain": {"description": "订单库"}}


def _resolve(
    mode, steps=..., step_config=..., revision_steps=None, revision_config=None
):
    return resolve_run_request(
        mode=mode,
        revision_steps=revision_steps if revision_steps is not None else REV_STEPS,
        revision_step_config=(
            revision_config if revision_config is not None else REV_CONFIG
        ),
        request_steps=None if steps is ... else steps,
        request_step_config=None if step_config is ... else step_config,
        whitelist=COLLECTION_STEPS,
        params_table={
            "db": {},
            "ddl": {},
            "json": {},
            "md": {},
            "domain": {"description": lambda v: v.strip() if isinstance(v, str) else v},
            "dim": {},
            "rel": {},
            "sql": {},
        },
    )


# ---- default 模式 ----


def test_default_omitted_null_empty_steps_execute_all_revision_steps():
    for steps in (None, []):
        resolved = _resolve("default", steps=steps)
        assert resolved.is_default_mode
        assert resolved.steps == REV_STEPS
        assert resolved.effective_step_params == {"domain": {"description": "订单库"}}


def test_default_full_match_steps_confirm_range_order_insensitive():
    resolved = _resolve("default", steps=["rel", "domain", "md", "json", "ddl"])
    assert resolved.steps == REV_STEPS  # 执行顺序取版本，不取请求数组


@pytest.mark.parametrize(
    "steps",
    [
        ["ddl", "json"],  # 缺步骤
        ["ddl", "json", "md", "domain", "rel", "sql"],  # 多步骤
        ["ddl", "json", "md", "domain", "cql"],  # 未知步骤（白名单先拒绝）
    ],
)
def test_default_mismatched_steps_rejected(steps):
    with pytest.raises(StepConfigError) as excinfo:
        _resolve("default", steps=steps)
    assert str(excinfo.value)


def test_default_nonempty_step_config_rejected_even_if_equal_to_defaults():
    with pytest.raises(StepConfigError) as excinfo:
        _resolve("default", step_config={"domain": {"description": "订单库"}})
    assert "custom" in str(excinfo.value)


def test_default_empty_object_step_config_means_no_override():
    resolved = _resolve("default", step_config={})
    assert resolved.effective_step_params == {"domain": {"description": "订单库"}}


# ---- custom 模式 ----


def test_custom_subset_with_override_merges_over_defaults():
    resolved = _resolve(
        "custom",
        steps=["domain"],
        step_config={"domain": {"description": "临时描述"}},
    )
    assert not resolved.is_default_mode
    assert resolved.steps == ["domain"]
    assert resolved.effective_step_params == {"domain": {"description": "临时描述"}}


def test_custom_full_set_without_override_uses_defaults():
    resolved = _resolve("custom", steps=REV_STEPS, step_config=None)
    assert resolved.steps == REV_STEPS
    assert resolved.effective_step_params == {"domain": {"description": "订单库"}}


def test_custom_out_of_revision_steps_rejected():
    with pytest.raises(StepConfigError) as excinfo:
        _resolve("custom", steps=["sql"])
    assert "超出" in str(excinfo.value)


def test_custom_empty_steps_rejected():
    with pytest.raises(StepConfigError):
        _resolve("custom", steps=[])


def test_custom_unknown_param_rejected():
    with pytest.raises(StepConfigError) as excinfo:
        _resolve("custom", steps=["rel"], step_config={"rel": {"threshold": 0.9}})
    assert "不支持参数" in str(excinfo.value)


def test_custom_override_for_unselected_step_rejected():
    with pytest.raises(StepConfigError):
        _resolve("custom", steps=["rel"], step_config={"domain": {"description": "x"}})


def test_unknown_mode_rejected():
    with pytest.raises(StepConfigError):
        _resolve("scheduled")


# ---- 步骤参数校验 ----


def test_validate_steps_dedup_and_whitelist():
    assert validate_steps(["md", "ddl", "md"], COLLECTION_STEPS) == ["md", "ddl"]
    with pytest.raises(StepConfigError):
        validate_steps(["dim_config"], COLLECTION_STEPS)


def test_validate_step_config_normalizes_empty_object_to_none():
    assert validate_step_config({}, ["db"], {"db": {}}) is None
    assert validate_step_config(None, ["db"], {"db": {}}) is None
