"""步骤白名单与每步骤允许的参数（doc 6.2 §4.3.4、§6.2；doc 7.2 §3.1/§7.2）。

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


def _validate_dim_tables_param(value: object) -> list[dict]:
    """发布 dim_value.tables：完整表清单（doc 7.2 §3.1/§3.2），纯结构校验。

    - 每项 {schema_name, object_name, embedding_col: [字段名, ...]}；
      embedding_col 仅允许非空字符串数组（元素非空、不含逗号），按首次
      出现顺序去重；人工允许多字段，不做"最多一个"限制；
    - 每表字段数组为空按未配置处理（报配置错误，列出表名），与整个
      tables: [] 的零表语义分开（零表留给发布预检的 dim_value_empty_tables）；
    - 表名重复、schema 含点（yaml 键格式限制）均报错；
    - 工作区候选/字段真实性核对属于预检职责，这里不做。
    """
    if not isinstance(value, list):
        raise StepConfigError("dim_value.tables 必须是数组")
    seen: set[str] = set()
    missing: list[str] = []
    normalized: list[dict] = []
    for item in value:
        if not isinstance(item, dict):
            raise StepConfigError("dim_value.tables 的每一项必须是对象")
        schema_name = item.get("schema_name")
        object_name = item.get("object_name")
        if not isinstance(schema_name, str) or not schema_name.strip():
            raise StepConfigError("dim_value.tables 的 schema_name 必须是非空字符串")
        if "." in schema_name:
            raise StepConfigError(f"dim_value.tables 的 schema 名不支持含点: {schema_name}")
        if not isinstance(object_name, str) or not object_name.strip():
            raise StepConfigError("dim_value.tables 的 object_name 必须是非空字符串")
        full_name = f"{schema_name}.{object_name}"
        if full_name in seen:
            raise StepConfigError(f"dim_value.tables 存在重复表: {full_name}")
        seen.add(full_name)
        embedding_col = item.get("embedding_col")
        columns: list[str] = []
        if isinstance(embedding_col, list):
            for column in embedding_col:
                if (
                    not isinstance(column, str)
                    or not column.strip()
                    or "," in column
                ):
                    raise StepConfigError(
                        f"dim_value.tables 的 {full_name} 存在非法字段名；"
                        "字段必须是不含逗号的非空字符串（多字段请使用数组）"
                    )
                text = column.strip()
                if text not in columns:
                    columns.append(text)
        elif embedding_col is not None:
            raise StepConfigError(
                f"dim_value.tables 的 {full_name} 的 embedding_col 必须是字段名数组"
            )
        if not columns:
            missing.append(full_name)
            continue
        normalized.append(
            {
                "schema_name": schema_name.strip(),
                "object_name": object_name.strip(),
                "embedding_col": columns,
            }
        )
    if missing:
        raise StepConfigError(
            "以下维度表未指定 embedding_col 字段: " + "、".join(sorted(missing))
        )
    return normalized


def _validate_domains_param(value: object) -> list[dict]:
    """发布 domain.domains：完整业务域清单（doc 7.2 §7.2），纯结构校验。

    - 每项 {name, description, tables: ["db.schema.table", ...]}；
      名称/描述去空白后非空，名称唯一（去空白后判定），域内表名去重；
    - tables 允许空列表（零行归属留给发布准入的既有规则处理）；
    - MD 清单、来源边界等候选核对属于预检职责，这里不做。
    """
    if not isinstance(value, list):
        raise StepConfigError("domain.domains 必须是数组")
    seen_names: set[str] = set()
    normalized: list[dict] = []
    for item in value:
        if not isinstance(item, dict):
            raise StepConfigError("domain.domains 的每一项必须是对象")
        name = item.get("name")
        description = item.get("description")
        if not isinstance(name, str) or not name.strip():
            raise StepConfigError("domain.domains 的名称必须是非空字符串")
        if not isinstance(description, str) or not description.strip():
            raise StepConfigError("domain.domains 的描述必须是非空字符串")
        stripped_name = name.strip()
        if stripped_name in seen_names:
            raise StepConfigError(f"domain.domains 名称重复: {stripped_name}")
        seen_names.add(stripped_name)
        tables = item.get("tables")
        if not isinstance(tables, list) or any(
            not isinstance(table, str) for table in tables
        ):
            raise StepConfigError(
                f"domain.domains 的 {stripped_name} 的 tables 必须是字符串数组"
            )
        # doc 7.2 §7.4：域内表名不重复沿用校验口径（拒绝），不套用 dim 字段
        # 去重规则
        seen_tables: set[str] = set()
        for table in tables:
            if table in seen_tables:
                raise StepConfigError(
                    f"domain.domains 的 {stripped_name} 的 tables 存在重复表名: {table}"
                )
            seen_tables.add(table)
        normalized.append(
            {
                "name": stripped_name,
                "description": description.strip(),
                "tables": list(tables),
            }
        )
    return normalized


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
PUBLISH_STEP_PARAMS: dict[str, dict[str, object]] = {
    **{step: {} for step in PUBLISH_STEPS},
    # doc 7.2：发布 domain/dim_value 支持完整清单参数（版本默认值 + custom 覆盖）
    "domain": {"domains": _validate_domains_param},
    "dim_value": {"tables": _validate_dim_tables_param},
}


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
