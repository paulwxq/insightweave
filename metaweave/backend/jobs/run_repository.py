"""采集/发布运行与运行步骤表的状态机与查询（doc 6.1 §4–§5、§8–§9）。

创建运行：同一事务写一条 pending run_info 与全部已选步骤的 pending 行
（未选步骤不建行）。状态推进由编排层按步骤调用；整体终态按 §6.4 汇总：
全部 success → success；至少一个 success 且有 failed/blocked →
partial_success；结束且无 success → failed。事务边界由调用方决定。
"""

import uuid
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.persistence.models import (
    CollectionJobInfo,
    CollectionRunInfo,
    CollectionRunStep,
    PublishJobInfo,
    PublishRunInfo,
    PublishRunStep,
)

STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_SUCCESS = "success"
STATUS_FAILED = "failed"
STATUS_BLOCKED = "blocked"
STATUS_PARTIAL = "partial_success"

_RUN_TERMINAL = (STATUS_SUCCESS, STATUS_FAILED, STATUS_BLOCKED)


@dataclass(frozen=True)
class RunSpec:
    info_table: type
    step_table: type
    run_id_column: str
    job_id_column: str
    job_table: type


COLLECTION_RUN = RunSpec(
    info_table=CollectionRunInfo,
    step_table=CollectionRunStep,
    run_id_column="collection_run_id",
    job_id_column="collection_job_id",
    job_table=CollectionJobInfo,
)
PUBLISH_RUN = RunSpec(
    info_table=PublishRunInfo,
    step_table=PublishRunStep,
    run_id_column="publish_run_id",
    job_id_column="publish_job_id",
    job_table=PublishJobInfo,
)


def new_run_id() -> uuid.UUID:
    return uuid.uuid4()


def get_run(spec: RunSpec, session: Session, run_id: uuid.UUID):
    return session.get(spec.info_table, run_id)


def get_run_step(spec: RunSpec, session: Session, run_id: uuid.UUID, step_name: str):
    column = getattr(spec.step_table, spec.run_id_column)
    return session.execute(
        select(spec.step_table).where(
            column == run_id, spec.step_table.step_name == step_name
        )
    ).scalar_one_or_none()


def list_run_steps(spec: RunSpec, session: Session, run_id: uuid.UUID) -> list:
    """按启动时间查询步骤供展示；返回顺序不代表尚未开始的执行计划。"""
    column = getattr(spec.step_table, spec.run_id_column)
    stmt = (
        select(spec.step_table)
        .where(column == run_id)
        .order_by(spec.step_table.started_at, spec.step_table.step_name)
    )
    return list(session.scalars(stmt))


def create_run(
    spec: RunSpec,
    session: Session,
    *,
    run_id: uuid.UUID,
    job_id: str,
    revision_no: int,
    steps: list[str],
    step_params: dict[str, dict],
    trigger_type: str = "manual",
    execution_plan: dict | None = None,
) -> object:
    """同一事务创建 pending 运行与全部已选步骤行（doc 6.2 §6.3.3）。

    ``execution_plan`` 仅发布运行使用（doc 10.2 §8）：创建时冻结的调度
    计划审计快照；采集运行不传。
    """
    info_kwargs: dict = {
        **{
            spec.run_id_column: run_id,
            spec.job_id_column: job_id,
        },
        "revision_no": revision_no,
        "trigger_type": trigger_type,
        "status": STATUS_PENDING,
        "run_by_user_id": None,
    }
    if execution_plan is not None and hasattr(spec.info_table, "execution_plan"):
        info_kwargs["execution_plan"] = execution_plan
    info = spec.info_table(**info_kwargs)
    session.add(info)
    session.flush()
    run_fk = {spec.run_id_column: run_id}
    for step_name in steps:
        step_fields = {
            **run_fk,
            "step_name": step_name,
            "status": STATUS_PENDING,
        }
        params = step_params.get(step_name)
        if params:
            # psycopg3 None→JSON null；省略无参数列保持 SQL NULL
            step_fields["step_config"] = params
        session.add(spec.step_table(**step_fields))
    session.flush()
    return info


def mark_run_running(spec: RunSpec, session: Session, run_id: uuid.UUID) -> bool:
    """条件 UPDATE：pending → running（doc 10.2 §5.3 原子认领）。

    检查 affected rows——先读再改在并发下可能双执行；认领失败返回 False。
    """
    from sqlalchemy import update

    run_column = getattr(spec.info_table, spec.run_id_column)
    result = session.execute(
        update(spec.info_table)
        .where(run_column == run_id, spec.info_table.status == STATUS_PENDING)
        .values(status=STATUS_RUNNING, started_at=func.now())
        .execution_options(synchronize_session="fetch")
    )
    return bool(result.rowcount)


def mark_step_running(
    spec: RunSpec, session: Session, run_id: uuid.UUID, step_name: str
) -> bool:
    """条件 UPDATE：步骤 pending → running（doc 10.2 §5.3 原子认领）。"""
    from sqlalchemy import update

    column = getattr(spec.step_table, spec.run_id_column)
    result = session.execute(
        update(spec.step_table)
        .where(
            column == run_id,
            spec.step_table.step_name == step_name,
            spec.step_table.status == STATUS_PENDING,
        )
        .values(status=STATUS_RUNNING, started_at=func.now())
        .execution_options(synchronize_session="fetch")
    )
    return bool(result.rowcount)


# finish_step 的合法来源状态（doc 10.2 §5.3：限定来源，防终态覆盖）。
# blocked 允许来自 pending（复查不就绪/认领前超时未认领）与 running；
# success/failed 只允许来自 running（认领后执行）。
_FINISH_SOURCES = {
    STATUS_BLOCKED: (STATUS_PENDING, STATUS_RUNNING),
    STATUS_SUCCESS: (STATUS_RUNNING,),
    STATUS_FAILED: (STATUS_RUNNING,),
}


def finish_step(
    spec: RunSpec,
    session: Session,
    run_id: uuid.UUID,
    step_name: str,
    status: str,
    message: str,
    *,
    input_manifest: dict | None = None,
    output_manifest: dict | None = None,
    target_manifest: dict | None = None,
) -> str:
    """条件 UPDATE 写回步骤终态，返回最终生效的状态（doc 10.2 §5.3）。

    原子性：UPDATE 带 ``status IN (合法来源)`` 条件并检查 rowcount——
    读-改-写在并发下可能双写覆盖。结果语义：
    - rowcount=1：本次提交生效，返回 ``status``；
    - rowcount=0：重读当前行——同一终态为幂等重放，返回该状态；
      其他状态（含其他终态）说明本执行者未取得写回权（可能已被其他
      执行者终结），**不覆盖**，返回行上实际状态供调用方对齐数据库
      事实，不再当作已按本意图终结。
    """
    if status not in _RUN_TERMINAL:
        raise ValueError(f"未知的步骤终态: {status!r}")
    from sqlalchemy import update

    allowed = _FINISH_SOURCES.get(status, ())
    values: dict = {"status": status, "message": message, "finished_at": func.now()}
    if input_manifest is not None:
        values["input_manifest"] = input_manifest
    if output_manifest is not None and status == STATUS_SUCCESS:
        values["output_manifest"] = output_manifest
    if target_manifest is not None:
        values["target_manifest"] = target_manifest
    column = getattr(spec.step_table, spec.run_id_column)
    result = session.execute(
        update(spec.step_table)
        .where(
            column == run_id,
            spec.step_table.step_name == step_name,
            spec.step_table.status.in_(allowed),
        )
        .values(**values)
        .execution_options(synchronize_session="fetch")
    )
    if result.rowcount:
        return status
    # rowcount=0：绕过 identity map 强制读库（doc 10.2 §6.1——数据库事实
    # 优先；缓存对象可能仍是 UPDATE 前的旧状态）
    from sqlalchemy import select as _select

    column = getattr(spec.step_table, spec.run_id_column)
    step = session.execute(
        _select(spec.step_table)
        .where(
            column == run_id, spec.step_table.step_name == step_name
        )
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if step is None:
        raise LookupError(f"步骤行不存在: {run_id}/{step_name}")
    return step.status


def rollup_run_status(statuses: list[str]) -> str:
    """整体终态汇总（doc 6.2 §6.4）。"""
    if STATUS_SUCCESS in statuses:
        if all(item == STATUS_SUCCESS for item in statuses):
            return STATUS_SUCCESS
        return STATUS_PARTIAL
    return STATUS_FAILED


def finalize_run(spec: RunSpec, session: Session, run_id: uuid.UUID) -> str:
    """全部步骤到终态后汇总整体状态并写 finished_at（doc 6.2 §6.3.6）。"""
    steps = list_run_steps(spec, session, run_id)
    statuses = [step.status for step in steps]
    if any(item not in _RUN_TERMINAL for item in statuses):
        raise ValueError("仍有步骤未到终态，不能汇总运行结果")
    final = rollup_run_status(statuses)
    info = get_run(spec, session, run_id)
    if info is None:
        raise LookupError(f"运行不存在: {run_id}")
    info.status = final
    info.finished_at = func.now()
    return final


def list_runs_for_job(
    spec: RunSpec,
    session: Session,
    job_id: str,
    *,
    limit: int,
    offset: int,
) -> tuple[list, int]:
    """按运行 created_at DESC、运行 UUID DESC 分页（doc 6.2 §6.1）。"""
    job_column = getattr(spec.info_table, spec.job_id_column)
    run_column = getattr(spec.info_table, spec.run_id_column)
    total = int(
        session.execute(
            select(func.count())
            .select_from(spec.info_table)
            .where(job_column == job_id)
        ).scalar_one()
    )
    stmt = (
        select(spec.info_table)
        .where(job_column == job_id)
        .order_by(spec.info_table.created_at.desc(), run_column.desc())
        .limit(limit)
        .offset(offset)
    )
    return list(session.scalars(stmt)), total


def list_runs_for_workspace(
    spec: RunSpec,
    session: Session,
    workspace_id: str,
    *,
    limit: int,
    offset: int,
) -> tuple[list, int]:
    """跨作业的工作区运行列表：run → job → workspace 关联限定（A26）。"""
    job_column = getattr(spec.info_table, spec.job_id_column)
    run_column = getattr(spec.info_table, spec.run_id_column)
    job_run_column = getattr(spec.job_table, spec.job_id_column)
    join_cond = job_run_column == job_column
    base = (
        select(spec.info_table)
        .join(spec.job_table, join_cond)
        .where(spec.job_table.workspace_id == workspace_id)
    )
    total = int(
        session.execute(
            select(func.count())
            .select_from(spec.info_table)
            .join(spec.job_table, join_cond)
            .where(spec.job_table.workspace_id == workspace_id)
        ).scalar_one()
    )
    stmt = (
        base.order_by(spec.info_table.created_at.desc(), run_column.desc())
        .limit(limit)
        .offset(offset)
    )
    return list(session.scalars(stmt)), total


def job_of_run(spec: RunSpec, session: Session, run_id: uuid.UUID):
    info = get_run(spec, session, run_id)
    if info is None:
        return None, None
    job = session.get(spec.job_table, getattr(info, spec.job_id_column))
    return info, job


def count_runs_for_jobs(spec: RunSpec, session: Session, job_ids: list[str]) -> dict:
    """批量统计各作业的运行总数；无运行的作业不在结果中。"""
    if not job_ids:
        return {}
    column = getattr(spec.info_table, spec.job_id_column)
    stmt = (
        select(column, func.count())
        .where(column.in_(job_ids))
        .group_by(column)
    )
    return {row[0]: int(row[1]) for row in session.execute(stmt)}
