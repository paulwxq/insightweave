"""发布执行计划与三通道调度（doc 10.2 §4）。

- 计划结构：``step、data_dependencies、order_predecessors、resource_key、
  priority``（§4.2）。第一阶段 ``data_dependencies`` 恒为空；通道顺序由
  已选集合过滤原全局顺序后建立相邻 ``order_predecessors``，容量 1。
- 箭头表示"前驱终结后继续"的顺序约束，不是前驱必须成功的数据依赖：
  前驱 failed/blocked 仍放行后继（§4.1）。
- 协调器：单线程维护 pending/in-flight/terminal，有限线程池执行完整
  ``_execute_step``；Future 完成后立即检查可启动节点，不按整批屏障推进。
  状态写回未知（控制面故障）时停发新任务、join 全部在途（§6.1）。
"""

from __future__ import annotations

import logging
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait

from backend.publishing.precheck import PUBLISH_STEP_ORDER
from services.logging_manager import submit_with_context

logger = logging.getLogger("backend.publishing")

PLAN_VERSION = 1

PG_CHANNEL = "pg"
MILVUS_CHANNEL = "milvus"
NEO4J_CHANNEL = "neo4j"

# 第一阶段固定策略（§4.1）：三通道内保留原全局顺序，容量均为 1。
CHANNEL_STEPS: dict[str, list[str]] = {
    PG_CHANNEL: ["db", "ddl", "json", "domain"],
    MILVUS_CHANNEL: ["table_schema", "dim_value", "sql"],
    NEO4J_CHANNEL: ["rel"],
}
STEP_CHANNEL: dict[str, str] = {
    step: channel for channel, steps in CHANNEL_STEPS.items() for step in steps
}

# 每运行总步骤并发上限（§4.1）；调低时按全局固定顺序选择就绪节点。
RUN_CONCURRENCY = 3

# 步骤结果中"状态写回未知"标记：不能把异常返回当终态（§4.1/§6.1）。
STATE_UNKNOWN = "unknown"

# doc 10.2 §5.3：全局活动发布运行名额。同源/工作区互斥之上的全局资源
# 护栏；单 worker 部署（doc 11 前提）下取 1，扩容时改此常量
# （2026-11-06 用户裁定：不做部署开关/环境变量，默认值即唯一值）。
MAX_ACTIVE_PUBLISH_RUNS = 1

# claim/prepare/复查等短事务的 SET LOCAL lock_timeout（§4.1，毫秒）；
# 本地部署元数据短事务的宽松常量，需要调整时改此行。
PUBLISH_LOCK_TIMEOUT_MS = 30000


def max_active_publish_runs() -> int:
    """全局活动发布运行名额（§5.3）：常量 ``MAX_ACTIVE_PUBLISH_RUNS``。"""
    return MAX_ACTIVE_PUBLISH_RUNS


def publish_lock_timeout_ms() -> int:
    """复查/claim/prepare 短事务的 ``SET LOCAL lock_timeout``（§4.1，毫秒）。"""
    return PUBLISH_LOCK_TIMEOUT_MS


def admission_lock_key() -> int:
    """全局准入 transaction advisory lock 键（§5.3）。

    独立稳定命名空间，SHA-256 前 8 字节有符号 64 位（不用 Python hash()，
    跨进程稳定）。必须先于所有源行锁取得。
    """
    from hashlib import sha256

    payload = "metaweave:publish_admission:v1".encode("ascii")
    return int.from_bytes(sha256(payload).digest()[:8], "big", signed=True)


def build_execution_plan(selected_steps: list[str]) -> dict:
    """按已选步骤构造冻结计划（§4.2）。

    ``selected_steps`` 须已 ``normalize_publish_steps`` 固定排序；通道顺序
    边只在已选集合内建立相邻前驱，不同通道不连接。图构建验证未知节点。
    并行是唯一调度模式（2026-11-06 用户裁定移除部署开关）。
    """
    unknown = [step for step in selected_steps if step not in STEP_CHANNEL]
    if unknown:
        raise ValueError(f"执行计划包含未知步骤: {unknown!r}")
    predecessors: dict[str, list[str]] = {}
    for chain in CHANNEL_STEPS.values():
        previous: str | None = None
        for step in chain:
            if step not in selected_steps:
                continue
            predecessors[step] = [previous] if previous is not None else []
            previous = step
    return {
        "plan_version": PLAN_VERSION,
        "selected_steps": [
            step for step in PUBLISH_STEP_ORDER if step in selected_steps
        ],
        "data_dependencies": {},
        "order_predecessors": predecessors,
        "channel_limits": {channel: 1 for channel in CHANNEL_STEPS},
        "run_concurrency": RUN_CONCURRENCY,
    }


def execute_parallel(
    steps: list[str],
    plan: dict,
    execute_step,
    *,
    pool: ThreadPoolExecutor | None = None,
):
    """事件驱动三通道调度（§4.1）；返回 ``(terminal, control_fault, skipped)``。

    ``execute_step(step_name) -> {"step", "status"}``；status 为
    ``STATE_UNKNOWN`` 表示状态写回未知——立即停发、等待全部在途结束后
    返回 ``control_fault=True``，由调用方做尽力记录且不无条件汇总。
    outcome 携带 ``claim_skipped=True`` 表示该步骤未取得认领权（他人
    持有），收集进 ``skipped``：统一收尾不得改写这些步骤（Codex R2
    意见 2）。worker 未捕获异常按 failed 收集（§6.1），不中断其余节点。
    """
    predecessors = {
        step: list(plan.get("order_predecessors", {}).get(step, []))
        for step in steps
    }
    run_concurrency = max(1, int(plan.get("run_concurrency") or 1))
    channel_limits = {
        channel: max(1, int(limit))
        for channel, limit in (plan.get("channel_limits") or {}).items()
    }
    priority = {step: index for index, step in enumerate(PUBLISH_STEP_ORDER)}

    pending: list[str] = list(steps)
    terminal: dict[str, str] = {}
    in_flight: dict[Future, str] = {}
    control_fault = False
    skipped: set[str] = set()

    own_pool = pool is None
    executor = pool or ThreadPoolExecutor(
        max_workers=min(run_concurrency, max(1, len(steps)))
    )
    try:
        while pending or in_flight:
            if not control_fault:
                # §4.2/§8：通道容量以冻结计划为准（当前 plan_version=1 恒为
                # 1；未来版本放开时调度器自动遵从，Kimi R1 意见 6）
                channel_usage: dict[str, int] = {}
                for running_step in in_flight.values():
                    channel_usage[STEP_CHANNEL[running_step]] = (
                        channel_usage.get(STEP_CHANNEL[running_step], 0) + 1
                    )
                ready = [
                    step
                    for step in pending
                    if all(pred in terminal for pred in predecessors[step])
                    and channel_usage.get(STEP_CHANNEL[step], 0)
                    < channel_limits.get(STEP_CHANNEL[step], 1)
                ]
                ready.sort(key=priority.__getitem__)
                submitted = True
                budget = run_concurrency - len(in_flight)
                submitted_count = 0
                for step in ready:
                    if submitted_count >= budget:
                        break
                    channel = STEP_CHANNEL[step]
                    # 派发循环内增量扣减：同通道容量已满时跳过该步骤、
                    # 继续尝试后续其他通道的 ready，且容量跳过不消耗
                    # 并发预算（DeepSeek R1 #3）
                    if channel_usage.get(channel, 0) >= channel_limits.get(
                        channel, 1
                    ):
                        continue
                    try:
                        future = submit_with_context(executor, execute_step, step)
                    except BaseException as exc:  # noqa: BLE001 —— 提交失败
                        # Codex R3 P2：提交异常也走停发+join+统一收尾路径，
                        # 不直接抛出绕过 _control_fault_wrapup（安全底线：
                        # 运行保持 running、不汇总、不释放占用）
                        logger.error(
                            "发布步骤提交失败，转入停发 step=%s type=%s",
                            step,
                            type(exc).__name__,
                        )
                        control_fault = True
                        submitted = False
                        break
                    in_flight[future] = step
                    pending.remove(step)
                    channel_usage[channel] = channel_usage.get(channel, 0) + 1
                    submitted_count += 1
                if not submitted:
                    continue
            if not in_flight:
                # 停发后无在途（控制面故障），或无可启动节点且无在途
                # （前驱未全部终态却无等待中的 Future——防御分支，不应出现）。
                break
            done, _ = wait(set(in_flight), return_when=FIRST_COMPLETED)
            for future in done:
                step = in_flight.pop(future)
                try:
                    outcome = future.result()
                except BaseException as exc:  # noqa: BLE001 —— 协调器收集一切异常
                    logger.error(
                        "发布步骤 worker 未捕获异常 step=%s type=%s",
                        step,
                        type(exc).__name__,
                    )
                    outcome = {
                        "step": step,
                        "status": "failed",
                        "message": f"执行异常（{type(exc).__name__}）",
                    }
                status = outcome.get("status")
                if status == STATE_UNKNOWN:
                    # 写回未知：不能把异常返回当终态（§4.1）→ 控制面故障。
                    control_fault = True
                    terminal[step] = STATE_UNKNOWN
                    if outcome.get("claim_skipped"):
                        skipped.add(step)
                    logger.error(
                        "发布步骤状态写回未知，停发新任务 step=%s", step
                    )
                elif status in ("success", "failed", "blocked"):
                    terminal[step] = status
                else:
                    # 非终态返回（如写回未生效被 _record_terminal 拒绝）：
                    # 与写回未知同语义——控制面故障停发（Codex R2 意见 3）
                    control_fault = True
                    terminal[step] = STATE_UNKNOWN
                    if outcome.get("claim_skipped"):
                        skipped.add(step)
                    logger.error(
                        "发布步骤返回未知结果状态 step=%s outcome=%s", step, outcome
                    )
    finally:
        if own_pool:
            executor.shutdown(wait=True)
    return terminal, control_fault, skipped
