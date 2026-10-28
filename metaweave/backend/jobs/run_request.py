"""运行请求的 default/custom 解析（doc 6.2 §6.2、A05/A23）。

- `mode = default`：执行引用版本的全部步骤与默认参数。`steps` 允许省略、
  `null`、`[]`；非空时必须与版本完整步骤集合一致（视为确认，不作为覆盖，
  顺序不限）。`step_config` 允许省略、`null`、`{}`；非空一律 400，需要
  临时参数时使用 custom（不做与默认参数相等的深比较放行）。
- `mode = custom`：步骤非空且为引用版本子集（允许等于全集）；未覆盖的
  参数继承版本默认值，覆盖仅作用于所选步骤允许的参数；临时配置不写回
  作业版本。

最终产出每步实际生效参数（写入 run_step.step_config 的解析值）。
"""

from dataclasses import dataclass, field

from backend.jobs.job_params import (
    StepConfigError,
    validate_step_config,
    validate_steps,
)


@dataclass(frozen=True)
class ResolvedRunRequest:
    """解析后的执行计划输入（排序由采集/发布编排层完成）。"""

    steps: list[str]
    effective_step_params: dict[str, dict] = field(default_factory=dict)
    is_default_mode: bool = True


def _no_override_config(step_config: object) -> bool:
    return step_config is None or (isinstance(step_config, dict) and not step_config)


def _revision_defaults(
    revision_step_config: dict | None, steps: list[str]
) -> dict[str, dict]:
    defaults: dict[str, dict] = {}
    if not revision_step_config:
        return defaults
    for step in steps:
        params = revision_step_config.get(step)
        if isinstance(params, dict) and params:
            defaults[step] = dict(params)
    return defaults


def resolve_run_request(
    *,
    mode: str,
    revision_steps: list[str],
    revision_step_config: dict | None,
    request_steps: object,
    request_step_config: object,
    whitelist: tuple[str, ...],
    params_table: dict[str, dict[str, object]],
) -> ResolvedRunRequest:
    if mode not in ("default", "custom"):
        raise StepConfigError("mode 必须是 default 或 custom")

    if mode == "default":
        if request_step_config is not None and not _no_override_config(
            request_step_config
        ):
            raise StepConfigError(
                "default 模式不接受非空 step_config；需要临时参数请使用 custom 模式"
            )
        if request_steps is None or (
            isinstance(request_steps, list) and not request_steps
        ):
            chosen = list(revision_steps)
        elif isinstance(request_steps, list):
            candidate = validate_steps(request_steps, whitelist)
            missing = [step for step in revision_steps if step not in candidate]
            extra = [step for step in candidate if step not in revision_steps]
            if missing or extra:
                parts = []
                if missing:
                    parts.append("缺少步骤: " + "、".join(missing))
                if extra:
                    parts.append("多出步骤: " + "、".join(extra))
                raise StepConfigError(
                    "default 模式的 steps 必须与引用版本的全部步骤一致（"
                    + "; ".join(parts)
                    + "）；不传 steps 即执行版本全部步骤"
                )
            chosen = list(revision_steps)
        else:
            raise StepConfigError("steps 必须是数组")
        defaults = _revision_defaults(revision_step_config, chosen)
        return ResolvedRunRequest(
            steps=chosen,
            effective_step_params=defaults,
            is_default_mode=True,
        )

    # custom 模式
    if not isinstance(request_steps, list) or not request_steps:
        raise StepConfigError("custom 模式必须提供非空 steps 子集")
    candidate = validate_steps(request_steps, whitelist)
    out_of_range = [step for step in candidate if step not in revision_steps]
    if out_of_range:
        raise StepConfigError(
            "所选步骤超出引用版本允许的范围: " + "、".join(out_of_range)
        )
    overrides = None
    if not _no_override_config(request_step_config):
        overrides = validate_step_config(request_step_config, candidate, params_table)
    effective = _revision_defaults(revision_step_config, candidate)
    if overrides:
        for step, params in overrides.items():
            effective.setdefault(step, {}).update(params)
    return ResolvedRunRequest(
        steps=candidate,
        effective_step_params=effective,
        is_default_mode=False,
    )
