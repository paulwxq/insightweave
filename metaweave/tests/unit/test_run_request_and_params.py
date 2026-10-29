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


# ---- 发布 dim_value/domain 参数（doc 7.2 §3.1/§7.2） ----

from backend.jobs.job_params import PUBLISH_STEP_PARAMS, PUBLISH_STEPS

PUB_REV_STEPS = ["db", "domain", "dim_value"]
PUB_REV_CONFIG = {
    "dim_value": {
        "tables": [
            {"schema_name": "public", "object_name": "t1", "embedding_col": ["name"]}
        ]
    },
    "domain": {
        "domains": [{"name": "用户", "description": "用户域", "tables": ["d.public.t1"]}]
    },
}


def _resolve_publish(mode, steps=..., step_config=..., revision_config=None):
    return resolve_run_request(
        mode=mode,
        revision_steps=PUB_REV_STEPS,
        revision_step_config=(
            PUB_REV_CONFIG if revision_config is None else revision_config
        ),
        request_steps=None if steps is ... else steps,
        request_step_config=None if step_config is ... else step_config,
        whitelist=PUBLISH_STEPS,
        params_table=PUBLISH_STEP_PARAMS,
    )


def test_publish_dim_tables_multi_field_normalized():
    """人工多字段数组保留全部字段并按首次出现去重（§3.1.1，不限最多一个）。"""
    resolved = _resolve_publish(
        "custom",
        steps=["dim_value"],
        step_config={
            "dim_value": {
                "tables": [
                    {
                        "schema_name": "public",
                        "object_name": "t2",
                        "embedding_col": ["a", "b", "a"],
                    }
                ]
            }
        },
    )
    assert resolved.effective_step_params["dim_value"]["tables"] == [
        {"schema_name": "public", "object_name": "t2", "embedding_col": ["a", "b"]}
    ]


def test_publish_dim_tables_complete_replacement_not_merge():
    """完整替换（§3.2）：覆盖清单替换版本默认，删除的表不补回。"""
    resolved = _resolve_publish(
        "custom",
        steps=["dim_value"],
        step_config={"dim_value": {"tables": []}},
    )
    assert resolved.effective_step_params["dim_value"]["tables"] == []


def test_publish_dim_tables_inherited_when_not_overridden():
    resolved = _resolve_publish("custom", steps=["dim_value"], step_config=None)
    assert resolved.effective_step_params["dim_value"] == PUB_REV_CONFIG["dim_value"]


@pytest.mark.parametrize(
    "tables,needle",
    [
        ("x", "必须是数组"),
        ([{"schema_name": "public", "object_name": "t", "embedding_col": []}], "未指定"),
        (
            [
                {"schema_name": "public", "object_name": "t", "embedding_col": ["a"]},
                {"schema_name": "public", "object_name": "t", "embedding_col": ["b"]},
            ],
            "重复表",
        ),
        (
            [{"schema_name": "pu.blic", "object_name": "t", "embedding_col": ["a"]}],
            "含点",
        ),
        (
            [{"schema_name": "public", "object_name": "t", "embedding_col": "a,b"}],
            "字段名数组",
        ),
        (
            [{"schema_name": "public", "object_name": "t", "embedding_col": ["a", ""]}],
            "非法字段名",
        ),
    ],
    ids=[
        "not-list",
        "empty-fields",
        "duplicate-table",
        "dotted-schema",
        "comma-string",
        "empty-element",
    ],
)
def test_publish_dim_tables_structure_rejected(tables, needle):
    with pytest.raises(StepConfigError, match=needle):
        _resolve_publish(
            "custom", steps=["dim_value"], step_config={"dim_value": {"tables": tables}}
        )


def test_publish_domains_normalized_and_complete_replacement():
    """域名称/描述去空白；显式 [] 完整替换不继承。"""
    resolved = _resolve_publish(
        "custom",
        steps=["domain"],
        step_config={
            "domain": {
                "domains": [
                    {
                        "name": " 订单 ",
                        "description": " 订单域 ",
                        "tables": ["d.public.t1", "d.public.t2"],
                    }
                ]
            }
        },
    )
    assert resolved.effective_step_params["domain"]["domains"] == [
        {"name": "订单", "description": "订单域", "tables": ["d.public.t1", "d.public.t2"]}
    ]
    empty = _resolve_publish(
        "custom", steps=["domain"], step_config={"domain": {"domains": []}}
    )
    assert empty.effective_step_params["domain"]["domains"] == []


@pytest.mark.parametrize(
    "domains,needle",
    [
        ("x", "必须是数组"),
        ([{"name": "", "description": "d", "tables": []}], "名称"),
        ([{"name": "a", "description": " ", "tables": []}], "描述"),
        (
            [
                {"name": "a", "description": "x", "tables": []},
                {"name": " a ", "description": "y", "tables": []},
            ],
            "名称重复",
        ),
        ([{"name": "a", "description": "x", "tables": "t"}], "字符串数组"),
        (
            [{"name": "a", "description": "x", "tables": ["d.public.t", "d.public.t"]}],
            "重复表名",
        ),
    ],
    ids=[
        "not-list",
        "empty-name",
        "empty-desc",
        "duplicate-name",
        "tables-not-list",
        "duplicate-table-in-domain",
    ],
)
def test_publish_domains_structure_rejected(domains, needle):
    with pytest.raises(StepConfigError, match=needle):
        _resolve_publish(
            "custom", steps=["domain"], step_config={"domain": {"domains": domains}}
        )


def test_publish_override_only_selected_steps():
    """验收 24：覆盖未选步骤或对版本外步骤覆盖均 400。"""
    with pytest.raises(StepConfigError, match="不在本次选择的步骤内"):
        _resolve_publish(
            "custom",
            steps=["db"],
            step_config={"dim_value": {"tables": []}},
        )
    with pytest.raises(StepConfigError, match="超出引用版本允许的范围"):
        _resolve_publish(
            "custom",
            steps=["db", "rel"],
            step_config=None,
        )
