"""步骤白名单与每步骤允许的参数（doc 6.2 §4.3.4、§6.2）。

保存作业版本与解析运行请求共用同一套白名单和参数表；未知步骤或未知
参数一律明确报错，不悄悄忽略。
"""

COLLECTION_STEPS = ("db", "ddl", "json", "md", "domain", "dim", "rel", "sql")
PUBLISH_STEPS = (
    "db",
    "ddl",
    "json",
    "domain",
    "table_schema",
    "dim_value",
    "rel",
    "sql",
)

# 发布步骤固定执行顺序（doc 6.2 §8.1，后端落实，不依赖页面）
PUBLISH_STEP_ORDER = (
    "db",
    "ddl",
    "json",
    "domain",
    "table_schema",
    "dim_value",
    "rel",
    "sql",
)


class StepConfigError(ValueError):
    """步骤或参数不合法；保存与运行入口共用。"""


def _validate_description(value: object) -> str:
    if not isinstance(value, str):
        raise StepConfigError("domain.description 必须是字符串")
    return value.strip()


# 每步骤允许的参数名及其校验器；空 dict 表示该步骤当前没有可配置参数
COLLECTION_STEP_PARAMS: dict[str, dict[str, object]] = {
    "db": {},
    "ddl": {},
    "json": {},
    "md": {},
    "domain": {"description": _validate_description},
    "dim": {},
    "rel": {},
    "sql": {},
}
PUBLISH_STEP_PARAMS: dict[str, dict[str, object]] = {step: {} for step in PUBLISH_STEPS}


def validate_steps(steps: object, whitelist: tuple[str, ...]) -> list[str]:
    """非空、白名单、去重（保持首次出现顺序）。"""
    if not isinstance(steps, list) or not steps:
        raise StepConfigError("steps 必须是非空数组")
    for item in steps:
        if not isinstance(item, str):
            raise StepConfigError("steps 的每一项必须是字符串")
    seen: list[str] = []
    for item in steps:
        if item not in whitelist:
            raise StepConfigError(
                f"未知步骤 {item!r}；允许的步骤：{'、'.join(whitelist)}"
            )
        if item not in seen:
            seen.append(item)
    return seen


def validate_step_config(
    step_config: object,
    chosen_steps: list[str],
    params_table: dict[str, dict[str, object]],
) -> dict | None:
    """校验并规范化 step_config：空对象归一为 None（doc 6.1 §step_config 约定）。

    - 键必须是已选步骤；
    - 每个步骤的参数对象只允许该步骤支持的参数，参数值经校验器归一。
    """
    if step_config is None:
        return None
    if not isinstance(step_config, dict):
        raise StepConfigError("step_config 必须是对象")
    normalized: dict[str, dict] = {}
    for step, params in step_config.items():
        if step not in chosen_steps:
            raise StepConfigError(f"step_config 中的步骤 {step!r} 不在本次选择的步骤内")
        allowed = params_table.get(step)
        if allowed is None:
            raise StepConfigError(f"未知步骤 {step!r}")
        if params is None:
            continue
        if not isinstance(params, dict):
            raise StepConfigError(f"步骤 {step!r} 的参数必须是对象")
        if not params:
            continue
        for key, value in params.items():
            if key not in allowed:
                raise StepConfigError(
                    f"步骤 {step!r} 不支持参数 {key!r}；"
                    f"支持：{'、'.join(allowed) or '（无）'}"
                )
            validator = allowed[key]
            normalized_value = validator(value) if callable(validator) else value
            normalized.setdefault(step, {})[key] = normalized_value
    return normalized or None
