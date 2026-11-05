"""采集运行编排（doc 6 §3、6.2 §6.3、§7.1）。

一次点击（一次请求）= 一个 `collection_run_id`：创建运行时把本次所选步骤
整组交给现有拓扑排序（`normalize_collection_steps`，算法未改写，仅
`dim_config` 标识更名为 `dim`），同一事务写入 pending run_info 与每个已选
步骤的 pending run_step；后台任务只执行本次请求新建的那一个运行，逐步骤
写状态、采集 marker 与 manifest，最后按 §6.4 汇总整体终态。
"""

import logging
from uuid import UUID

from sqlalchemy.exc import SQLAlchemyError

from backend.data_sources.db_step_service import run_db_step
from backend.data_sources.ddl_step_service import run_ddl_step
from backend.data_sources.dim_config_step_service import run_dim_config_step
from backend.data_sources.domain_step_service import run_domain_step
from backend.data_sources.json_step_service import run_json_step
from backend.data_sources.md_step_service import run_md_step
from backend.data_sources.rel_step_service import run_rel_step
from backend.data_sources.sql_step_service import run_sql_step
from backend.data_sources.step_context import (
    DbStepConfigError,
    SourceDisabledError,
    StepOutcome,
    load_step_context,
)
from backend.jobs.job_params import (
    COLLECTION_STEP_PARAMS,
    COLLECTION_STEPS,
    StepConfigError,
)
from backend.jobs.job_repository import COLLECTION_JOB, get_job, get_revision
from backend.jobs.manifests import build_input_manifest, build_output_manifest
from backend.jobs.run_repository import (
    COLLECTION_RUN,
    STATUS_BLOCKED,
    STATUS_FAILED,
    STATUS_SUCCESS,
    create_run,
    finalize_run,
    finish_step,
    list_run_steps,
    mark_run_running,
    mark_step_running,
    new_run_id,
)
from backend.jobs.run_request import resolve_run_request
from backend.persistence.platform_db import PlatformSettings, platform_session
from backend.workspace_files.markers import write_collection_marker
from backend.workspaces.repository import get_workspace
from metaweave.core.metadata.execution import set_operation_step

logger = logging.getLogger("backend.jobs")

DB_STEP = "db"
DDL_STEP = "ddl"
JSON_STEP = "json"
MD_STEP = "md"
DOMAIN_STEP = "domain"
DIM_STEP = "dim"
REL_STEP = "rel"
SQL_STEP = "sql"
_SUPPORTED_STEPS = COLLECTION_STEPS
_SUPPORTED_STEPS_TEXT = "db、ddl、json、md、domain、dim、rel、sql"


class UnsupportedCollectionStepError(ValueError):
    """请求里包含当前还不能执行的步骤，或一个步骤都没选。"""


class CollectionRunError(ValueError):
    """运行创建的受控错误（作业/版本/来源/工作区不满足）。"""


def normalize_collection_steps(steps: list[str]) -> list[str]:
    if not steps:
        raise UnsupportedCollectionStepError("请至少选择一个步骤")
    chosen: list[str] = []
    for step in steps:
        if step not in _SUPPORTED_STEPS:
            raise UnsupportedCollectionStepError(
                f"当前仅支持 {_SUPPORTED_STEPS_TEXT} 步骤"
            )
        if step not in chosen:
            chosen.append(step)
    return _ensure_managed_step_order(chosen)


# 受管步骤依赖边（doc 6 §3）：与既有拓扑排序锚点关系一一对应，
# dim_config 标识随断代改造更名为 dim。只保留两端都在本次选择内的边。
_STEP_DEPS = {
    JSON_STEP: (DDL_STEP,),
    MD_STEP: (DDL_STEP, JSON_STEP),
    DOMAIN_STEP: (MD_STEP,),
    DIM_STEP: (JSON_STEP,),
    REL_STEP: (JSON_STEP, DOMAIN_STEP),
    SQL_STEP: (MD_STEP, DOMAIN_STEP, REL_STEP),
}


def _ensure_managed_step_order(chosen: list[str]) -> list[str]:
    """同请求偏序拓扑排序（既有算法原样保留，doc 6.2 §2.1.1）。

    规则：每轮在当前可执行（有效入度为 0）的步骤中，优先选传入顺序最靠前
    （原始下标最小）者。
    """
    position = {step: index for index, step in enumerate(chosen)}
    pending = {
        step: {dep for dep in _STEP_DEPS.get(step, ()) if dep in position}
        for step in chosen
    }
    ordered: list[str] = []
    done: set[str] = set()
    while pending:
        available = [step for step in pending if pending[step] <= done]
        nxt = min(available, key=lambda step: position[step])
        ordered.append(nxt)
        done.add(nxt)
        del pending[nxt]
    return ordered


_BLOCK_MESSAGES = {
    (DDL_STEP, JSON_STEP): (
        "本次请求的 DDL 未成功，JSON 未启动；如需使用已有 DDL 产物，请单独重跑 JSON"
    ),
    (JSON_STEP, MD_STEP): (
        "本次请求的 JSON 未成功，MD 未启动；如需使用已有 JSON 产物，请单独重跑 MD"
    ),
    (DDL_STEP, MD_STEP): (
        "本次请求的 DDL 未成功，MD 未启动；如需使用已有 JSON 产物，请单独重跑 MD"
    ),
    (MD_STEP, DOMAIN_STEP): (
        "本次请求的 MD 未成功，Domain 未启动；如需使用已有 MD 产物，请单独重跑 Domain"
    ),
    (JSON_STEP, DIM_STEP): (
        "本次请求的 JSON 未成功，Dim 未启动；如需使用已有 JSON 产物，请单独重跑 Dim"
    ),
    (JSON_STEP, REL_STEP): (
        "本次请求的 JSON 未成功，Rel 未启动；如需使用已有 JSON 产物，请单独重跑 Rel"
    ),
    (DOMAIN_STEP, REL_STEP): (
        "本次请求的 Domain 未成功，Rel 未启动；如需使用已有 Domain 产物，请单独重跑 Rel"
    ),
    (MD_STEP, SQL_STEP): "本次请求的 MD 未成功，SQL 未启动；请修复 MD 后重跑 SQL",
    (DOMAIN_STEP, SQL_STEP): (
        "本次请求的 Domain 未成功，SQL 未启动；请修复 Domain 后重跑 SQL"
    ),
    (REL_STEP, SQL_STEP): "本次请求的 Rel 未成功，SQL 未启动；请修复 Rel 后重跑 SQL",
}


def _blocked_by_same_request_upstream(
    step: str, chosen: list[str], results: list[dict]
) -> str | None:
    """本次请求内上游失败或被阻断，则阻断后继步骤，不能复用旧产物。"""
    unsuccessful = {
        row["step"]
        for row in results
        if row["status"] in (STATUS_FAILED, STATUS_BLOCKED)
    }
    pairs = [pair for pair in _BLOCK_MESSAGES if pair[1] == step]
    if step == MD_STEP and JSON_STEP not in chosen:
        pairs = [(DDL_STEP, MD_STEP)]
    elif step == MD_STEP:
        pairs = [(JSON_STEP, MD_STEP)]
    for upstream, _downstream in pairs:
        if upstream in chosen and upstream in unsuccessful:
            return _BLOCK_MESSAGES[(upstream, step)]
    return None


_WORKERS = {
    DB_STEP: run_db_step,
    DDL_STEP: run_ddl_step,
    JSON_STEP: run_json_step,
    MD_STEP: run_md_step,
    DOMAIN_STEP: run_domain_step,
    DIM_STEP: run_dim_config_step,
    REL_STEP: run_rel_step,
    SQL_STEP: run_sql_step,
}

# 步骤产物目录名与步骤标识一致的映射（doc 6 §2）


def _step_output_dir(source_id: str, workspace_id: str, step_name: str):
    from metaweave.core.clean_targets import resolve_step_output_dir

    return resolve_step_output_dir(source_id, workspace_id, step_name)


def _claim_step(
    settings: PlatformSettings,
    workspace_id: str,
    step_name: str,
    run_id: UUID,
) -> str | None:
    """在工作区行锁下原子认领步骤：置 running 前核对同名步骤无其他运行。

    冲突只认 `running` 状态的其他运行——pending 记录可能属于尚未启动或
    已中断的后台任务，不得永久阻塞后续运行（doc 6.2 §6.3、A25）。工作区
    行锁使「查冲突 + 认领」对同一工作区的并发运行串行化。
    返回 None 表示认领成功，否则返回阻断原因。
    """
    from sqlalchemy import select

    from backend.persistence.models import (
        CollectionJobInfo,
        CollectionRunInfo,
        CollectionRunStep,
        WorkspaceInfo,
    )

    with platform_session(settings) as session:
        # doc 8.1 §5.1.1：普通查询只用于定位父级，不在此处加锁、不作准入结论
        from backend.deletion.locks import lock_source_row, lock_workspace_row

        probe = session.get(WorkspaceInfo, workspace_id)
        if probe is None:
            return "工作区不存在"
        source_row = lock_source_row(session, probe.source_id)
        workspace_row = lock_workspace_row(session, workspace_id)
        if source_row is None or workspace_row is None:
            return "数据源或工作区不存在"
        if workspace_row.source_id != source_row.source_id:
            return "工作区归属已变化，不能认领步骤"
        if getattr(source_row, "deletion_state", "active") != "active":
            return "数据源正在删除或已删除，不能认领步骤"
        if getattr(workspace_row, "deletion_state", "active") != "active":
            return "工作区正在删除，不能认领步骤"
        if step_name in ("json", "domain", "rel"):
            # doc 8.1 §5.1.1：Rel 发布执行期不再持工作区行锁；其输入
            # （json/domain/rel 产物）的采集以运行中检查互斥
            from backend.publishing.repository import rel_publish_in_progress

            if rel_publish_in_progress(session, workspace_id) is not None:
                return (
                    "该工作区 Rel 发布正在执行，不能采集 "
                    f"{step_name}（Rel 发布输入），请稍后重试"
                )
        conflict = session.execute(
            select(CollectionRunInfo.collection_run_id)
            .join(
                CollectionRunStep,
                CollectionRunStep.collection_run_id
                == CollectionRunInfo.collection_run_id,
            )
            .join(
                CollectionJobInfo,
                CollectionJobInfo.collection_job_id
                == CollectionRunInfo.collection_job_id,
            )
            .where(
                CollectionJobInfo.workspace_id == workspace_id,
                CollectionRunStep.step_name == step_name,
                CollectionRunStep.status == "running",
                CollectionRunInfo.status == "running",
                CollectionRunInfo.collection_run_id != run_id,
            )
            .limit(1)
        ).scalar_one_or_none()
        if conflict is not None:
            session.rollback()
            return "该工作区此步骤可能正在执行（其他运行占用），请稍后重试"
        mark_step_running(COLLECTION_RUN, session, run_id, step_name)
        session.commit()
    return None


def create_collection_run(
    settings: PlatformSettings,
    job_id: str,
    *,
    revision_no: int,
    mode: str,
    steps: object,
    step_config: object,
) -> dict:
    """校验并创建一次采集运行（doc 6.2 §6.3 步骤 1–4）；不执行。

    返回 202 载荷与后台执行计划。请求不合法、作业/版本/工作区/来源不满足
    时不创建运行、不写业务记录。
    """
    from metaweave.utils.source_id import normalize_collection_job_id

    job_id = normalize_collection_job_id(job_id)
    try:
        with platform_session(settings) as session:
            job = get_job(COLLECTION_JOB, session, job_id)
            if job is None:
                raise CollectionRunError("采集作业不存在")
            revision = get_revision(COLLECTION_JOB, session, job_id, revision_no)
            if revision is None:
                raise CollectionRunError("引用的作业版本不存在")
            workspace = get_workspace(session, job.workspace_id)
            if workspace is None:
                raise CollectionRunError("工作区不存在")
            from backend.data_sources.repository import get_db_source

            source = get_db_source(session, workspace.source_id)
            if source is None:
                raise CollectionRunError("数据源不存在")
            if not source.is_active:
                raise CollectionRunError("数据源已禁用，不能发起采集")
            # doc 8.1 §5.1.1：源 → 工作区 → 作业加锁，锁内经完整层级守卫复查
            # （作业/工作区/源已删 410、删除中 409、知识清理冻结 409）
            from backend.access_guards import require_job
            from backend.deletion.locks import (
                lock_job_row,
                lock_source_row,
                lock_workspace_row,
            )

            source = lock_source_row(session, workspace.source_id)
            workspace = lock_workspace_row(session, job.workspace_id)
            job = lock_job_row(session, "collection", job_id)
            require_job(session, "collection", job_id, for_write=True)
            if job is None or workspace is None or source is None:
                raise CollectionRunError("作业、工作区或数据源不存在")
            if not source.is_active:
                raise CollectionRunError("数据源已禁用，不能发起采集")
            resolved = resolve_run_request(
                mode=mode,
                revision_steps=list(revision.steps),
                revision_step_config=revision.step_config,
                request_steps=steps,
                request_step_config=step_config,
                whitelist=COLLECTION_STEPS,
                params_table=COLLECTION_STEP_PARAMS,
            )
            ordered = normalize_collection_steps(list(resolved.steps))
            run_id = new_run_id()
            create_run(
                COLLECTION_RUN,
                session,
                run_id=run_id,
                job_id=job_id,
                revision_no=revision_no,
                steps=ordered,
                step_params=resolved.effective_step_params,
                trigger_type="manual",
            )
            session.commit()
    except SQLAlchemyError:
        from backend.data_sources.registration_service import PlatformUnavailableError

        raise PlatformUnavailableError("平台库暂不可用") from None
    except StepConfigError as exc:
        raise CollectionRunError(str(exc)) from None
    return {
        "collection_run_id": str(run_id),
        "collection_job_id": job_id,
        "revision_no": revision_no,
        "steps": ordered,
        "status": "pending",
    }


def _manifest_or_none(builder, entries):
    if not entries:
        return None
    return builder(entries)


def execute_collection_run(
    settings: PlatformSettings, run_id: UUID, execution_steps: list[str]
) -> None:
    """后台执行本次请求新建的采集运行（doc 6.2 §6.3 步骤 5–6）。

    只执行传入的那一个运行：仍为 pending 才置 running；各步骤执行前写
    running、后写终态与 manifest；成功/失败写采集 marker，blocked 不写。
    不扫描、不接管其他 pending/running 记录。
    execution_steps 由创建运行的请求传入，保留本次选择的顺序；数据库步骤行
    没有顺序字段，不能将查询结果的展示顺序当作执行计划。
    """

    from backend.persistence.models import CollectionJobInfo, CollectionRunInfo

    with platform_session(settings) as session:
        run = session.get(CollectionRunInfo, run_id)
        if run is None:
            logger.error("采集运行不存在 run_id=%s", run_id)
            return
        if run.status != "pending":
            logger.warning(
                "采集运行已不是 pending，跳过执行 run_id=%s status=%s",
                run_id,
                run.status,
            )
            return
        job = session.get(CollectionJobInfo, run.collection_job_id)
        if job is None:
            logger.error(
                "采集作业不存在 run_id=%s job_id=%s", run_id, run.collection_job_id
            )
            return
        workspace = get_workspace(session, job.workspace_id)
        if workspace is None:
            logger.error(
                "工作区不存在 run_id=%s workspace_id=%s", run_id, job.workspace_id
            )
            return
        source_id = workspace.source_id
        workspace_id = workspace.workspace_id
        step_rows = list_run_steps(COLLECTION_RUN, session, run_id)
        # 保留请求顺序并再次确保依赖序，不能依赖 started_at 或名称排序。
        steps = normalize_collection_steps(execution_steps)
        if set(steps) != {row.step_name for row in step_rows}:
            raise CollectionRunError("执行计划与本次运行记录的步骤不一致")
        params_by_step = {row.step_name: row.step_config for row in step_rows}
        # 未到终态的步骤从这里推进；已终态（如前次中断）不再执行
        if not mark_run_running(COLLECTION_RUN, session, run_id):
            logger.warning("采集运行无法置 running run_id=%s", run_id)
            return
        session.commit()

    results: list[dict] = []
    for step_name in steps:
        try:
            # 日志设置、认领及上下文装配也可能失败，须纳入步骤异常处理，
            # 避免后台任务退出后遗留 pending 步骤和 running 整体状态。
            set_operation_step(step_name)
            blocked_message = _blocked_by_same_request_upstream(
                step_name, steps, results
            )
            if blocked_message is None:
                blocked_message = _claim_step(settings, workspace_id, step_name, run_id)
            if blocked_message is not None:
                _record_terminal(
                    settings,
                    run_id,
                    step_name,
                    STATUS_BLOCKED,
                    blocked_message,
                )
                results.append({"step": step_name, "status": STATUS_BLOCKED})
                continue

            from backend.data_sources.step_context import METADATA_CONFIG

            ctx = load_step_context(
                settings,
                source_id=source_id,
                workspace_id=workspace_id,
                run_id=run_id,
                step_name=step_name,
                step_params=params_by_step.get(step_name),
                config_path=METADATA_CONFIG,
            )
            # 认领即置 running（工作区行锁下原子完成）
            outcome: StepOutcome = _WORKERS[step_name](ctx)
        except (DbStepConfigError, SourceDisabledError) as exc:
            # 步骤前置条件不满足（上游产物缺失、配置非法）：blocked 不写 marker
            _record_terminal(settings, run_id, step_name, STATUS_BLOCKED, str(exc))
            results.append({"step": step_name, "status": STATUS_BLOCKED})
            continue
        except Exception as exc:  # noqa: BLE001 —— 非受控故障按步骤失败计
            logger.exception(
                "采集步骤非受控失败 run_id=%s step=%s type=%s",
                run_id,
                step_name,
                type(exc).__name__,
            )
            _write_marker_for_step(
                source_id, workspace_id, step_name, run_id, failed=True
            )
            _record_terminal(
                settings,
                run_id,
                step_name,
                STATUS_FAILED,
                f"步骤执行失败（{type(exc).__name__}）",
            )
            results.append({"step": step_name, "status": STATUS_FAILED})
            continue

        input_manifest = _manifest_or_none(build_input_manifest, outcome.input_files)
        output_manifest = (
            _manifest_or_none(build_output_manifest, outcome.output_files)
            if outcome.success
            else None
        )
        _write_marker_for_step(
            source_id,
            workspace_id,
            step_name,
            run_id,
            failed=not outcome.success,
        )
        status = STATUS_SUCCESS if outcome.success else STATUS_FAILED
        _record_terminal(
            settings,
            run_id,
            step_name,
            status,
            outcome.message,
            input_manifest=input_manifest,
            output_manifest=output_manifest,
        )
        results.append({"step": step_name, "status": status})

    with platform_session(settings) as session:
        try:
            final = finalize_run(COLLECTION_RUN, session, run_id)
            session.commit()
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "采集运行汇总失败 run_id=%s type=%s", run_id, type(exc).__name__
            )
            return
    logger.info(
        "采集运行结束 run_id=%s status=%s steps=%s",
        run_id,
        final,
        results,
    )


def _write_marker_for_step(
    source_id: str,
    workspace_id: str,
    step_name: str,
    run_id: UUID,
    *,
    failed: bool,
) -> None:
    """成功/失败写采集 marker（doc 6 §6）；blocked/pending 不写。"""
    try:
        directory = _step_output_dir(source_id, workspace_id, step_name)
        write_collection_marker(directory, run_id, "failed" if failed else "success")
    except (OSError, ValueError) as exc:
        logger.error(
            "采集 marker 写入失败 run_id=%s step=%s type=%s",
            run_id,
            step_name,
            type(exc).__name__,
        )


def _mark_step_running(
    settings: PlatformSettings, run_id: UUID, step_name: str
) -> None:
    with platform_session(settings) as session:
        mark_step_running(COLLECTION_RUN, session, run_id, step_name)
        session.commit()


def _record_terminal(
    settings: PlatformSettings,
    run_id: UUID,
    step_name: str,
    status: str,
    message: str,
    *,
    input_manifest: dict | None = None,
    output_manifest: dict | None = None,
) -> None:
    with platform_session(settings) as session:
        try:
            finish_step(
                COLLECTION_RUN,
                session,
                run_id,
                step_name,
                status,
                message,
                input_manifest=input_manifest,
                output_manifest=output_manifest,
            )
            session.commit()
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "采集步骤状态写回失败 run_id=%s step=%s type=%s",
                run_id,
                step_name,
                type(exc).__name__,
            )
