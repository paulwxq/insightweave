"""发布编排（doc 6.2 §6.3、§8）：集中预检、创建运行与后台执行。

- 创建运行前对本次所选步骤集中预检：任一步骤的采集 marker、必要文件、
  内容或配置不合格，整单 400（`PublishPrecheckError`），不生成运行 UUID、
  不写任何业务记录；全部通过才创建 run_info 与步骤行。
- 执行期（后台任务）逐步骤：实际启动前复查，不合格记 `blocked`；实际执行
  出错记 `failed`；成功步骤的 PostgreSQL 内容写入与步骤成功状态在同一事务
  提交（doc 6.2 §8.3）。
- 不生成发布 marker；不比较采集步骤完成时间（doc 6 §4.3）。
"""

from __future__ import annotations

import logging
from copy import deepcopy
from pathlib import Path
from uuid import UUID

from sqlalchemy import delete
from sqlalchemy.exc import SQLAlchemyError

from backend.data_sources.ddl_step_service import build_managed_output_snapshot
from backend.jobs.job_params import (
    PUBLISH_STEP_PARAMS,
    PUBLISH_STEPS,
    StepConfigError,
)
from backend.jobs.job_repository import PUBLISH_JOB, get_job, get_revision
from backend.jobs.manifests import build_input_manifest, build_target_manifest
from backend.jobs.run_repository import (
    PUBLISH_RUN,
    STATUS_BLOCKED,
    STATUS_FAILED,
    STATUS_SUCCESS,
    create_run,
    finish_step,
    get_run_step,
    list_run_steps,
    mark_run_running,
    new_run_id,
)
from backend.jobs.run_request import resolve_run_request
from backend.persistence.models import (
    PublishJobInfo,
    PublishRunInfo,
    TableDefinition,
    TableDomain,
    TableProfile,
    WorkspaceInfo,
)
from backend.persistence.platform_db import PlatformSettings, platform_session
from backend.profile_loading.db_loader import (
    MetaLoadSourceDisabledError,
    MetaLoadSourceNotFoundError,
    ProfileInputError,
    read_db_profile_input,
    replace_db_profile,
    require_workspace_source,
)
from backend.profile_loading.ddl_loader import (
    DDLInputError,
    DEFINE_TYPE_DDL,
    read_ddl_definitions,
)
from backend.profile_loading.dim_value_loader import build_dim_value_loader
from backend.profile_loading.domain_loader import (
    DomainPublishInputError,
    domain_rows_from_config,
)
from backend.profile_loading.json_loader import (
    JsonPublishInputError,
    build_profile_rows,
    read_managed_json_documents,
    read_registration_snapshot,
)
from backend.publishing.precheck import (
    PUBLISH_STEP_ORDER,
    PublishPrecheckError,
    check_step,
    collect_input_dir_times,
    normalize_publish_steps,
    precheck_steps,
)
from backend.profile_loading.sql_loader import build_sql_example_loader
from backend.profile_loading.table_schema_loader import build_table_schema_loader
from backend.publishing.scheduling import (
    STATE_UNKNOWN,
    admission_lock_key,
    build_execution_plan,
    execute_parallel,
    max_active_publish_runs,
    publish_lock_timeout_ms,
)
from backend.publishing.target_lock import (
    acquire_target_lock,
    ensure_target_lock_alive,
    neo4j_target_guard_key,
    neo4j_target_lock_key,
    release_target_guard,
    release_target_lock,
    reserve_target_guard,
)
from backend.workspace_files.backups import backup_directory_before_update
from backend.workspaces.repository import get_workspace
from metaweave.core.clean_targets import (
    resolve_cql_output_dir,
    resolve_domain_output_dir,
)
from metaweave.core.cql_generator.generator import CQLGenerator
from metaweave.core.domains import DomainResolver
from metaweave.core.loaders.cql_loader import CQLLoader, CQLScriptValidationError
from metaweave.core.metadata.execution import load_main_config_snapshot
from metaweave.utils.file_utils import resolve_project_path
from metaweave.utils.source_id import normalize_publish_job_id

logger = logging.getLogger("backend.publishing")

METADATA_CONFIG = "configs/metadata_config.yaml"
INPUT_SUMMARY = "发布输入不可用或校验失败"
WRITE_SUMMARY = "平台库写入失败"

_PG_TARGETS = {
    "db": [("postgresql", "metaweave", "public", "db_profile")],
    "ddl": [("postgresql", "metaweave", "public", "table_definition")],
    "json": [
        ("postgresql", "metaweave", "public", "table_profile"),
        ("postgresql", "metaweave", "public", "table_column_profile"),
    ],
    "domain": [("postgresql", "metaweave", "public", "table_domain")],
}


class PublishRunError(ValueError):
    """发布运行创建的受控错误（作业/版本/来源/参数不满足）。"""


class UnsupportedPublicationStepError(ValueError):
    """空选择或不支持的发布步骤。"""


def _workspace_context(session, publish_job: PublishJobInfo):
    workspace = get_workspace(session, publish_job.workspace_id)
    if workspace is None:
        raise PublishRunError("工作区不存在")
    return workspace


# ---------------------------------------------------------------------
# publish-check（固定返回全部 8 个步骤，doc 6.2 §8.2.2）
# ---------------------------------------------------------------------


def check_publication(settings: PlatformSettings, workspace_id: str) -> dict:
    """预检展示：固定 8 步骤可用性 + 各输入目录采集时间；不创建运行。"""
    from metaweave.utils.source_id import normalize_workspace_id

    workspace_id = normalize_workspace_id(workspace_id)
    try:
        with platform_session(settings) as session:
            workspace = get_workspace(session, workspace_id)
            if workspace is None:
                raise MetaLoadSourceNotFoundError("工作区不存在")
            from backend.data_sources.repository import get_db_source

            source = get_db_source(session, workspace.source_id)
            if source is None:
                raise MetaLoadSourceNotFoundError("未找到指定 source_id 的数据源")
            if not source.is_active:
                raise MetaLoadSourceDisabledError("指定数据源已禁用，不能发布画像")
            db_name = source.db_name
            source_id = workspace.source_id
            steps = []
            for step in PUBLISH_STEP_ORDER:
                result = check_step(
                    session,
                    source_id,
                    workspace_id,
                    step,
                    db_name=db_name,
                )
                steps.append(
                    {
                        "step_name": step,
                        "ready": result.ready,
                        "reason": result.reason,
                    }
                )
            dir_times = collect_input_dir_times(source_id, workspace_id)
    except (MetaLoadSourceNotFoundError, MetaLoadSourceDisabledError):
        raise
    except SQLAlchemyError:
        from backend.data_sources.registration_service import PlatformUnavailableError

        raise PlatformUnavailableError("平台库暂不可用") from None
    return {
        "workspace_id": workspace_id,
        "source_id": source_id,
        "steps": steps,
        "input_directories": [dir_times[name] for name in sorted(dir_times)],
    }


# ---------------------------------------------------------------------
# 运行创建（集中预检 → 创建 run 行，doc 6.2 §6.3）
# ---------------------------------------------------------------------


def create_publish_run(
    settings: PlatformSettings,
    publish_job_id: str,
    *,
    run_id: UUID | None = None,
    revision_no: int,
    mode: str,
    steps: object,
    step_config: object,
) -> dict:
    """校验 + 集中预检 + 创建发布运行；预检失败整单 400（无业务记录）。

    ``run_id`` 由创建路由在注册执行者后传入（doc 11 §4.2 提交前注册）；
    缺省时内部生成，行为不变。
    """
    publish_job_id = normalize_publish_job_id(publish_job_id)
    from backend.data_sources.registration_service import PlatformUnavailableError

    try:
        with platform_session(settings) as session:
            job = get_job(PUBLISH_JOB, session, publish_job_id)
            if job is None:
                raise PublishRunError("发布作业不存在")
            revision = get_revision(PUBLISH_JOB, session, publish_job_id, revision_no)
            if revision is None:
                raise PublishRunError("引用的作业版本不存在")
            workspace = _workspace_context(session, job)
            from backend.data_sources.repository import get_db_source

            source = get_db_source(session, workspace.source_id)
            if source is None:
                raise MetaLoadSourceNotFoundError("未找到指定 source_id 的数据源")
            if not source.is_active:
                raise MetaLoadSourceDisabledError("指定数据源已禁用，不能发布")
            # doc 10.2 §5.3：全局准入 transaction advisory lock 必须先于所有
            # 源行锁取得；只用于短期串行化创建（集中预检在本事务内、行锁
            # 之后执行，含文件读取），不延伸至运行执行期的外部库 I/O，
            # 也不在本事务等待目标锁。
            from sqlalchemy import text as _sa_text

            session.execute(
                _sa_text("SELECT pg_advisory_xact_lock(:key)").bindparams(
                    key=admission_lock_key()
                )
            )
            # doc 8.1 §5.1.1：源 → 工作区 → 发布作业加锁，锁内经完整层级守卫复查
            from backend.access_guards import require_job
            from backend.deletion.locks import (
                lock_job_row,
                lock_source_row,
                lock_workspace_row,
            )

            source = lock_source_row(session, workspace.source_id)
            workspace_row = lock_workspace_row(session, job.workspace_id)
            job = lock_job_row(session, "publish", publish_job_id)
            require_job(session, "publish", publish_job_id, for_write=True)
            if job is None or workspace_row is None or source is None:
                raise PublishRunError("发布作业、工作区或数据源不存在")
            if not source.is_active:
                raise MetaLoadSourceDisabledError("指定数据源已禁用，不能发布")
            # doc 10.2 §5.1–§5.3：源级互斥 / 工作区双向保留 / 全局名额，
            # 在准入锁与行锁内原子检查；冲突整单 409，不排队、不留额外运行。
            from backend.publishing.repository import (
                PublishCapacityExhaustedError,
                SourcePublishActiveError,
                WorkspaceCollectionActiveError,
                active_collection_run_for_workspace,
                active_publish_runs_for_source,
                count_active_publish_runs,
            )

            blocking_runs = active_publish_runs_for_source(
                session, workspace.source_id
            )
            if blocking_runs:
                raise SourcePublishActiveError(blocking_runs[0])
            collection_active = active_collection_run_for_workspace(
                session, job.workspace_id
            )
            if collection_active is not None:
                raise WorkspaceCollectionActiveError(collection_active)
            active_limit = max_active_publish_runs()
            if count_active_publish_runs(session) >= active_limit:
                raise PublishCapacityExhaustedError(active_limit)

            resolved = resolve_run_request(
                mode=mode,
                revision_steps=list(revision.steps),
                revision_step_config=revision.step_config,
                request_steps=steps,
                request_step_config=step_config,
                whitelist=PUBLISH_STEPS,
                params_table=PUBLISH_STEP_PARAMS,
            )
            ordered = normalize_publish_steps(list(resolved.steps))
            # 集中预检（doc 6.2 §6.3.2 + doc 7.2 §5.1/§5.2）：按解析后的有效
            # 参数校验（dim_value/domain 清单来自版本默认值与本次覆盖，不读
            # 共享 YAML）；任一步骤不合格整单 400，不生成运行 UUID、不写
            # run/step/manifest 等业务记录。
            results = precheck_steps(
                session,
                workspace.source_id,
                workspace.workspace_id,
                ordered,
                db_name=source.db_name,
                effective_params=resolved.effective_step_params,
            )
            failures = [
                {
                    "step": step,
                    "reason": result.reason,
                    **({"code": result.code} if result.code else {}),
                }
                for step, result in results.items()
                if not result.ready
            ]
            if failures:
                raise PublishPrecheckError(failures)
            if run_id is None:
                run_id = new_run_id()
            # doc 10.2 §7.3/§8（2026-11-06 裁定移除调度开关，并行唯一）：
            # 计划在创建时冻结进 execution_plan，后台执行按冻结计划走
            # 三通道并行协调器。计划不放用户 step_config。
            execution_plan = build_execution_plan(ordered)
            create_run(
                PUBLISH_RUN,
                session,
                run_id=run_id,
                job_id=publish_job_id,
                revision_no=revision_no,
                steps=ordered,
                step_params=resolved.effective_step_params,
                trigger_type="manual",
                execution_plan=execution_plan,
            )
            session.commit()
    except SQLAlchemyError:
        raise PlatformUnavailableError("平台库暂不可用") from None
    except StepConfigError as exc:
        raise PublishRunError(str(exc)) from None
    return {
        "publish_run_id": str(run_id),
        "publish_job_id": publish_job_id,
        "revision_no": revision_no,
        "steps": ordered,
        "status": "pending",
    }


# ---------------------------------------------------------------------
# 作业级预检（doc 7.2 §5.2）：与创建运行同一解析与校验规则，不创建运行
# ---------------------------------------------------------------------


def precheck_publish_job(
    settings: PlatformSettings,
    publish_job_id: str,
    *,
    revision_no: int,
    mode: str,
    steps: object,
    step_config: object,
) -> dict:
    """按"引用版本默认值 + 本次允许的覆盖"解析有效参数并逐步骤校验。

    供执行页在应用草稿后重新校验；不创建运行、不写任何业务记录。
    """
    publish_job_id = normalize_publish_job_id(publish_job_id)
    from backend.data_sources.registration_service import PlatformUnavailableError

    try:
        with platform_session(settings) as session:
            job = get_job(PUBLISH_JOB, session, publish_job_id)
            if job is None:
                raise PublishRunError("发布作业不存在")
            revision = get_revision(PUBLISH_JOB, session, publish_job_id, revision_no)
            if revision is None:
                raise PublishRunError("引用的作业版本不存在")
            workspace = _workspace_context(session, job)
            from backend.data_sources.repository import get_db_source

            source = get_db_source(session, workspace.source_id)
            if source is None:
                raise MetaLoadSourceNotFoundError("未找到指定 source_id 的数据源")
            if not source.is_active:
                raise MetaLoadSourceDisabledError("指定数据源已禁用，不能发布")
            resolved = resolve_run_request(
                mode=mode,
                revision_steps=list(revision.steps),
                revision_step_config=revision.step_config,
                request_steps=steps,
                request_step_config=step_config,
                whitelist=PUBLISH_STEPS,
                params_table=PUBLISH_STEP_PARAMS,
            )
            ordered = normalize_publish_steps(list(resolved.steps))
            results = precheck_steps(
                session,
                workspace.source_id,
                workspace.workspace_id,
                ordered,
                db_name=source.db_name,
                effective_params=resolved.effective_step_params,
            )
    except SQLAlchemyError:
        raise PlatformUnavailableError("平台库暂不可用") from None
    except StepConfigError as exc:
        raise PublishRunError(str(exc)) from None
    return {
        "publish_job_id": publish_job_id,
        "revision_no": revision_no,
        "mode": mode,
        "steps": [
            {
                "step_name": step,
                "ready": result.ready,
                "reason": result.reason,
                "code": result.code,
            }
            for step, result in results.items()
        ],
    }


# ---------------------------------------------------------------------
# 后台执行（doc 6.2 §6.3.5–6）
# ---------------------------------------------------------------------


def _claim_publish_step(
    settings: PlatformSettings,
    workspace_id: str,
    step_name: str,
    run_id: UUID,
) -> str | None:
    """工作区行锁下原子认领发布步骤；其他运行的 running 同名步骤 → 阻断。

    doc 10.2 §4.1/§5.3：短事务 ``SET LOCAL lock_timeout``——复查/认领在
    实际内容写入前超时回滚记 blocked（原因 ``publish_lock_timeout``）；
    条件 UPDATE 认领失败（不再 pending）不得继续执行 publisher。
    """
    from sqlalchemy import select

    from backend.persistence.models import (
        PublishJobInfo,
        PublishRunInfo,
        PublishRunStep,
    )

    try:
        with platform_session(settings) as session:
            _set_local_lock_timeout(session)
            # doc 8.1 §5.1.1：普通查询只用于定位父级；锁序为源 → 工作区
            from backend.deletion.locks import lock_source_row, lock_workspace_row

            probe = session.get(WorkspaceInfo, workspace_id)
            if probe is None:
                return "工作区不存在"
            source_row = lock_source_row(session, probe.source_id)
            workspace = lock_workspace_row(session, workspace_id)
            if source_row is None or workspace is None:
                return "数据源或工作区不存在"
            if workspace.source_id != source_row.source_id:
                return "工作区归属已变化，不能认领发布步骤"
            if getattr(source_row, "deletion_state", "active") != "active":
                return "数据源正在删除或已删除，不能认领发布步骤"
            if getattr(workspace, "deletion_state", "active") != "active":
                return "工作区正在删除，不能认领发布步骤"
            if step_name == "rel":
                # doc 8.1 §5.1.1：反向检查——json/domain/rel 采集先开始时，
                # Rel 发布不得读取正在改写的输入
                from backend.publishing.repository import (
                    collection_inputs_in_progress,
                )

                if (
                    collection_inputs_in_progress(session, workspace_id)
                    is not None
                ):
                    return (
                        "该工作区 JSON/Domain/Rel 采集正在执行，"
                        "不能发布 Rel，请稍后重试"
                    )
            conflict = session.execute(
                select(PublishRunInfo.publish_run_id)
                .join(
                    PublishRunStep,
                    PublishRunStep.publish_run_id == PublishRunInfo.publish_run_id,
                )
                .join(
                    PublishJobInfo,
                    PublishJobInfo.publish_job_id == PublishRunInfo.publish_job_id,
                )
                .where(
                    PublishJobInfo.workspace_id == workspace_id,
                    PublishRunStep.step_name == step_name,
                    PublishRunStep.status == "running",
                    PublishRunInfo.status == "running",
                    PublishRunInfo.publish_run_id != run_id,
                )
                .limit(1)
            ).scalar_one_or_none()
            if conflict is not None:
                session.rollback()
                return "该工作区此发布步骤可能正在执行（其他运行占用），请稍后重试"
            from backend.jobs.run_repository import mark_step_running

            if not mark_step_running(PUBLISH_RUN, session, run_id, step_name):
                # doc 10.2 §5.3/§9.4：条件认领失败——步骤已被其他执行者认领
                # 或已终结。未取得认领权不得写回任何终态（避免覆盖原执行者
                # 的 running/终态）；交由协调器按写回未知停发处理。
                session.rollback()
                return _CLAIM_SKIPPED
            session.commit()
    except SQLAlchemyError as exc:
        if _is_lock_timeout(exc):
            return (
                "publish_lock_timeout：等待源/工作区行锁超时，"
                "未认领本次步骤（可重试）"
            )
        raise
    return None


def _input_manifest_from(result_files) -> dict | None:
    if not result_files:
        return None
    return build_input_manifest(list(result_files))


def _pg_target_manifest(step_name: str) -> dict | None:
    targets = _PG_TARGETS.get(step_name)
    if not targets:
        return None
    return build_target_manifest(
        [
            {"type": kind, "database": db, "schema": schema, "table": table}
            for kind, db, schema, table in targets
        ]
    )


def _set_local_lock_timeout(session) -> None:
    """事务局部 ``lock_timeout``（doc 10.2 §4.1，初始 30 秒可配置）。

    ``set_config(..., true)`` 等价 ``SET LOCAL`` 且支持参数绑定（SET 语法
    不接受占位符）；事务结束时自动还原。复查/claim/prepare 超时回滚记
    blocked，已进入 PG 内容写入事务的超时回滚记 failed。
    """
    from sqlalchemy import text

    session.execute(
        text("SELECT set_config('lock_timeout', :value, true)").bindparams(
            value=f"{publish_lock_timeout_ms()}ms"
        )
    )


def _is_lock_timeout(exc: BaseException) -> bool:
    """锁等待超时：55P03 lock_not_available 或 57014 lock timeout 取消。

    只认 sqlstate 与 PostgreSQL 标准消息——SQL 文本本身含 "lock_timeout"
    字样，不能作为依据（曾把 SET 语法错误误判为锁超时）。
    """
    orig = getattr(exc, "orig", None) or exc
    if getattr(orig, "sqlstate", None) in ("55P03", "57014"):
        return True
    return "due to lock timeout" in str(orig).lower()


class _StateWriteFailed(RuntimeError):
    """步骤状态写回失败：外部结果可能已发生，但持久状态未知（§6.1）。"""

    def __init__(self, step_name: str, intended_status: str):
        super().__init__(f"步骤状态写回失败：{step_name} → {intended_status}")
        self.step_name = step_name
        self.intended_status = intended_status


# 认领哨兵：步骤已被其他执行者持有或已终结，本次执行者不拥有写回权。
_CLAIM_SKIPPED = "__claim_skipped__"


def _control_fault_wrapup(
    settings: PlatformSettings,
    run_id: UUID,
    steps,
    *,
    skip_steps: set[str] | None = None,
) -> None:
    """控制面故障收尾（doc 10.2 §6.1）：停发且在途 join 后尽力记录。

    running→failed、pending→blocked 的尽力写回；不无条件 finalize——运行
    保持 running、占用保留，由 Web/CLI 收尾（`--finalize-only`）处理。
    库不可用则留待恢复（§6.1）。``skip_steps`` 为未取得认领权的步骤
    （其他执行者持有）：本执行者不拥有写回权，不得改写其状态——等待
    本地 worker 结束不能证明对方执行者已停止（Codex R2 意见 2）。
    """
    skip = skip_steps or set()
    logger.error(
        "发布运行控制面故障：已停止派发并在途 join，尽力记录终态 run_id=%s "
        "跳过非本执行者步骤=%s",
        run_id,
        sorted(skip) or "无",
    )
    try:
        with platform_session(settings) as session:
            rows = {
                row.step_name: row
                for row in list_run_steps(PUBLISH_RUN, session, run_id)
            }
            for step_name in steps:
                if step_name in skip:
                    continue
                row = rows.get(step_name)
                if row is None or row.status in ("success", "failed", "blocked"):
                    continue
                target = (
                    STATUS_FAILED if row.status == "running" else STATUS_BLOCKED
                )
                try:
                    finish_step(
                        PUBLISH_RUN,
                        session,
                        run_id,
                        step_name,
                        target,
                        "控制面故障：状态写回失败，执行停止后尽力收尾；"
                        "外部写入结果未核验",
                    )
                except (ValueError, RuntimeError, LookupError) as exc:
                    logger.warning(
                        "控制面故障收尾被状态机拒绝 run_id=%s step=%s：%s",
                        run_id,
                        step_name,
                        exc,
                    )
            session.commit()
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "控制面故障收尾写回失败，留待恢复 run_id=%s type=%s",
            run_id,
            type(exc).__name__,
        )


def execute_publish_run(settings: PlatformSettings, run_id: UUID) -> None:
    """后台执行发布运行：按冻结计划三通道并行调度（doc 10.2 §4）。

    并行是唯一调度模式（2026-11-06 用户裁定移除部署开关）；计划缺失
    （历史行/防御分支）时按已选步骤即时重建。协调器等全部 worker join
    且终态（或控制面故障收尾）提交后才返回——doc 11 ``active_runs``
    注册存活期因此覆盖全部外部写入窗口（§6.3）。
    """
    with platform_session(settings) as session:
        run = session.get(PublishRunInfo, run_id)
        if run is None or run.status != "pending":
            logger.warning("发布运行不存在或已不是 pending，跳过执行 run_id=%s", run_id)
            return
        job = session.get(PublishJobInfo, run.publish_job_id)
        if job is None:
            logger.error("发布作业不存在 run_id=%s", run_id)
            return
        workspace_id = job.workspace_id
        step_rows = list_run_steps(PUBLISH_RUN, session, run_id)
        # 新步骤的 started_at 均为空；查询顺序不能替代发布固定执行顺序。
        steps = normalize_publish_steps([row.step_name for row in step_rows])
        plan = getattr(run, "execution_plan", None)
        plan = plan if isinstance(plan, dict) else None
        if plan is None:
            plan = build_execution_plan(steps)
        if not mark_run_running(PUBLISH_RUN, session, run_id):
            logger.warning("发布运行无法置 running run_id=%s", run_id)
            return
        session.commit()

    def _run_step(step_name: str) -> dict:
        return _execute_step_safely(settings, run_id, workspace_id, step_name)

    terminal, control_fault, skipped_steps = execute_parallel(steps, plan, _run_step)
    results = [{"step": step, "status": st} for step, st in terminal.items()]

    if control_fault:
        _control_fault_wrapup(settings, run_id, steps, skip_steps=skipped_steps)
        return

    with platform_session(settings) as session:
        try:
            from backend.jobs.run_repository import finalize_run

            final = finalize_run(PUBLISH_RUN, session, run_id)
            session.commit()
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "发布运行汇总失败 run_id=%s type=%s", run_id, type(exc).__name__
            )
            return
    logger.info("发布运行结束 run_id=%s status=%s steps=%s", run_id, final, results)


def _execute_step_safely(
    settings: PlatformSettings,
    run_id: UUID,
    workspace_id: str,
    step_name: str,
) -> dict:
    """协调器入口：复查/认领等现有 try 外的异常也必须被收集（§6.1）。"""
    try:
        return _execute_step(settings, run_id, workspace_id, step_name)
    except BaseException as exc:  # noqa: BLE001 —— 收集一切未捕获异常
        logger.error(
            "发布步骤执行框架异常 run_id=%s step=%s type=%s",
            run_id,
            step_name,
            type(exc).__name__,
        )
        try:
            recorded = _record_terminal(
                settings,
                run_id,
                step_name,
                STATUS_FAILED,
                f"发布执行失败（{type(exc).__name__}）",
            )
        except _StateWriteFailed:
            return {"step": step_name, "status": STATE_UNKNOWN}
        return {"step": step_name, "status": recorded}


def _execute_step(
    settings: PlatformSettings,
    run_id: UUID,
    workspace_id: str,
    step_name: str,
) -> dict:
    """执行单个发布步骤：日志上下文隔离 + 状态写回未知传播（doc 10.2 §7.5）。

    ContextVar token 在 finally 恢复（复用 doc 10.1 已实施机制）；
    ``_StateWriteFailed`` 转为 ``STATE_UNKNOWN`` 结果——外部结果可能已
    发生而持久状态未知，协调器据此停发（§6.1）。
    """
    from metaweave.utils.logger import reset_current_step, set_current_step

    token = set_current_step(step_name)
    try:
        return _execute_step_inner(settings, run_id, workspace_id, step_name)
    except _StateWriteFailed:
        return {"step": step_name, "status": STATE_UNKNOWN}
    finally:
        reset_current_step(token)


def _execute_step_inner(
    settings: PlatformSettings,
    run_id: UUID,
    workspace_id: str,
    step_name: str,
) -> dict:
    """执行单个发布步骤：执行期复查 → 认领 → 实际执行 → 状态与清单。"""
    with platform_session(settings) as session:
        workspace = get_workspace(session, workspace_id)
        if workspace is None:
            recorded = _record_terminal(
                settings, run_id, step_name, STATUS_BLOCKED, "工作区不存在"
            )
            return {"step": step_name, "status": recorded}
        source_id = workspace.source_id
        # 执行期复查（doc 6.2 §6.4）：marker/文件/内容不合格记 blocked
        from backend.data_sources.repository import get_db_source

        source = get_db_source(session, source_id)
        if source is None:
            recorded = _record_terminal(
                settings, run_id, step_name, STATUS_BLOCKED, "数据源不存在"
            )
            return {"step": step_name, "status": recorded}
        if not source.is_active:
            recorded = _record_terminal(
                settings, run_id, step_name, STATUS_BLOCKED, "数据源已禁用，不能发布"
            )
            return {"step": step_name, "status": recorded}
        # 执行期复查（doc 6.2 §6.4 + doc 7.2 §5.3）：dim_value/domain 使用
        # 运行步骤冻结的有效配置（创建时固化的完整清单），复查外部条件但
        # 不重新读取共享 YAML 或最新作业版本。
        if step_name in ("dim_value", "domain"):
            step_row = get_run_step(PUBLISH_RUN, session, run_id, step_name)
            frozen = step_row.step_config if step_row is not None else None
            effective_params = {step_name: frozen if isinstance(frozen, dict) else {}}
        else:
            effective_params = None
        result = check_step(
            session,
            source_id,
            workspace_id,
            step_name,
            db_name=source.db_name,
            effective_params=effective_params,
        )
        if not result.ready:
            recorded = _record_terminal(
                settings, run_id, step_name, STATUS_BLOCKED, result.reason or "未就绪"
            )
            return {"step": step_name, "status": recorded}
        input_manifest = _input_manifest_from(result.input_files)
        payload = result.payload

    blocked = _claim_publish_step(settings, workspace_id, step_name, run_id)
    if blocked == _CLAIM_SKIPPED:
        # 未取得认领权：不写回任何终态（§9.4 终态不能被旧结果覆盖）；
        # 按写回未知处理 → 协调器停发，控制面收尾只处理本运行未终结行。
        logger.warning(
            "发布步骤认领被其他执行者持有或已终结，跳过写回 run_id=%s step=%s",
            run_id,
            step_name,
        )
        return {"step": step_name, "status": STATE_UNKNOWN, "claim_skipped": True}
    if blocked is not None:
        recorded = _record_terminal(
            settings, run_id, step_name, STATUS_BLOCKED, blocked
        )
        return {"step": step_name, "status": recorded}

    try:
        _PUBLISHERS[step_name](
            settings,
            run_id,
            workspace_id,
            input_manifest=input_manifest,
            payload=payload,
        )
    except (
        MetaLoadSourceNotFoundError,
        MetaLoadSourceDisabledError,
        _StepBlocked,
    ) as exc:
        recorded = _record_terminal(
            settings, run_id, step_name, STATUS_BLOCKED, str(exc)
        )
        return {"step": step_name, "status": recorded}
    except (
        ProfileInputError,
        DDLInputError,
        JsonPublishInputError,
        DomainPublishInputError,
    ) as exc:
        # 发布输入契约失败（doc 6.2 §8.2）：受控失败，保留具体原因
        recorded = _record_terminal(
            settings,
            run_id,
            step_name,
            STATUS_FAILED,
            str(exc) or INPUT_SUMMARY,
            input_manifest=input_manifest,
        )
        return {"step": step_name, "status": recorded}
    except _StepFailed as exc:
        recorded = _record_terminal(
            settings,
            run_id,
            step_name,
            STATUS_FAILED,
            str(exc),
            input_manifest=input_manifest,
        )
        return {"step": step_name, "status": recorded}
    except _StateWriteFailed:
        # publisher 内部的状态写回失败（如外部库步骤记 success 时库异常）
        # 必须先于通用分支传播：不能落入"再记一次 failed"的假报路径（§6.1）
        raise
    except SQLAlchemyError as exc:
        # doc 10.2 §4.1：已进入内容/状态事务的 lock_timeout 回滚记 failed。
        if _is_lock_timeout(exc):
            recorded = _record_terminal(
                settings,
                run_id,
                step_name,
                STATUS_FAILED,
                "publish_lock_timeout：发布事务等待行锁超时，本次事务已回滚",
                input_manifest=input_manifest,
            )
            return {"step": step_name, "status": recorded}
        logger.error(
            "发布步骤非受控失败 run_id=%s step=%s type=%s",
            run_id,
            step_name,
            type(exc).__name__,
        )
        recorded = _record_terminal(
            settings,
            run_id,
            step_name,
            STATUS_FAILED,
            f"发布执行失败（{type(exc).__name__}）",
            input_manifest=input_manifest,
        )
        return {"step": step_name, "status": recorded}
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "发布步骤非受控失败 run_id=%s step=%s type=%s",
            run_id,
            step_name,
            type(exc).__name__,
        )
        recorded = _record_terminal(
            settings,
            run_id,
            step_name,
            STATUS_FAILED,
            f"发布执行失败（{type(exc).__name__}）",
            input_manifest=input_manifest,
        )
        return {"step": step_name, "status": recorded}
    return {"step": step_name, "status": STATUS_SUCCESS}


class _StepFailed(RuntimeError):
    """发布步骤受控失败（消息对外展示）。"""


def _record_terminal(
    settings: PlatformSettings,
    run_id: UUID,
    step_name: str,
    status: str,
    message: str,
    *,
    input_manifest: dict | None = None,
    target_manifest: dict | None = None,
) -> str:
    """写回步骤终态并返回最终生效状态（doc 10.2 §6.1）。

    - 条件 UPDATE 生效 → 返回 ``status``；
    - rowcount=0 且行已是同一终态 → 幂等，返回该状态；
    - 行处于其他状态（如已被其他执行者终结）→ 不覆盖，返回行上实际
      状态，调用方据此对齐数据库事实，不假报已按本意图终结；
    - 数据库异常/行缺失/未知终态 → 抛 ``_StateWriteFailed``（写回未
      确认），协调层转入控制面故障收尾，不得虚报。
    """
    with platform_session(settings) as session:
        try:
            recorded = finish_step(
                PUBLISH_RUN,
                session,
                run_id,
                step_name,
                status,
                message,
                input_manifest=input_manifest,
                target_manifest=target_manifest,
            )
            session.commit()
            if recorded not in ("success", "failed", "blocked"):
                # 写回未生效且行仍非终态（如认领前框架异常给 pending 行记
                # failed）：按写回未知处理，不得当作已完成结果（Codex R2 #3）
                raise _StateWriteFailed(step_name, status)
            if recorded != status:
                logger.warning(
                    "发布步骤终态写回未生效（行已处于 %s）run_id=%s step=%s 意图=%s",
                    recorded,
                    run_id,
                    step_name,
                    status,
                )
            return recorded
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "发布步骤状态写回失败 run_id=%s step=%s type=%s",
                run_id,
                step_name,
                type(exc).__name__,
            )
            raise _StateWriteFailed(step_name, status) from exc


def _finish_success(
    session,
    run_id: UUID,
    step_name: str,
    message: str,
    *,
    input_manifest: dict | None,
    target_manifest: dict | None,
) -> None:
    recorded = finish_step(
        PUBLISH_RUN,
        session,
        run_id,
        step_name,
        STATUS_SUCCESS,
        message,
        input_manifest=input_manifest,
        target_manifest=target_manifest,
    )
    if recorded != STATUS_SUCCESS:
        # 步骤行已被并发终结（协议外场景，如 CLI 误收尾）：raise 使内容
        # 写入随同一事务回滚，不产生"内容已写而状态未变"（Kimi R1 意见 9）
        raise RuntimeError(
            f"步骤状态写回未生效（行已处于 {recorded}），内容写入回滚"
            f"（{run_id}/{step_name}）"
        )


# ---- PostgreSQL 内容步骤：内容写入与成功状态同一事务（doc 6.2 §8.3） ----


def _publish_db(settings, run_id, workspace_id, *, input_manifest, payload) -> None:
    source_id = _source_id(settings, workspace_id)
    fields, _dir = read_db_profile_input(source_id, workspace_id)
    with platform_session(settings) as session:
        _set_local_lock_timeout(session)
        # doc 8.1 §5.1.1：先锁源再锁工作区（原仅工作区 FOR UPDATE）
        from backend.deletion.locks import lock_source_row

        lock_source_row(session, source_id)
        session.get(WorkspaceInfo, workspace_id, with_for_update=True)
        _reverify_source(session, source_id)
        replace_db_profile(session, run_id, fields)
        _finish_success(
            session,
            run_id,
            "db",
            "数据库画像发布完成",
            input_manifest=input_manifest,
            target_manifest=_pg_target_manifest("db"),
        )
        session.commit()


def _publish_ddl(settings, run_id, workspace_id, *, input_manifest, payload) -> None:
    source_id = _source_id(settings, workspace_id)
    rows, _dir = read_ddl_definitions(source_id, workspace_id)
    with platform_session(settings) as session:
        _set_local_lock_timeout(session)
        # doc 8.1 §5.1.1：先锁源再锁工作区（原仅工作区 FOR UPDATE）
        from backend.deletion.locks import lock_source_row

        lock_source_row(session, source_id)
        session.get(WorkspaceInfo, workspace_id, with_for_update=True)
        _reverify_source(session, source_id)
        session.execute(
            delete(TableDefinition).where(
                TableDefinition.source_id == source_id,
                TableDefinition.define_type == DEFINE_TYPE_DDL,
            )
        )
        session.add_all(TableDefinition(publish_run_id=run_id, **row) for row in rows)
        _finish_success(
            session,
            run_id,
            "ddl",
            f"表定义发布完成（{len(rows)} 个对象）",
            input_manifest=input_manifest,
            target_manifest=_pg_target_manifest("ddl"),
        )
        session.commit()


def _publish_json(settings, run_id, workspace_id, *, input_manifest, payload) -> None:
    source_id = _source_id(settings, workspace_id)
    with platform_session(settings) as session:
        registration = read_registration_snapshot(session, source_id)
        documents = read_managed_json_documents(
            source_id, workspace_id, registration.db_name
        )
        table_rows, column_rows = build_profile_rows(
            source_id, run_id, documents, registration.selected_schemas
        )
        _set_local_lock_timeout(session)
        # doc 8.1 §5.1.1：先锁源再锁工作区（原仅工作区 FOR UPDATE）
        from backend.deletion.locks import lock_source_row

        lock_source_row(session, source_id)
        session.get(WorkspaceInfo, workspace_id, with_for_update=True)
        _reverify_source(session, source_id)
        session.execute(delete(TableProfile).where(TableProfile.source_id == source_id))
        session.add_all(TableProfile(**row) for row in table_rows)
        session.flush()
        from backend.persistence.models import TableColumnProfile

        session.add_all(TableColumnProfile(**row) for row in column_rows)
        _finish_success(
            session,
            run_id,
            "json",
            f"对象画像发布完成（{len(table_rows)} 个对象）",
            input_manifest=input_manifest,
            target_manifest=_pg_target_manifest("json"),
        )
        session.commit()


def _publish_domain(settings, run_id, workspace_id, *, input_manifest, payload) -> None:
    source_id = _source_id(settings, workspace_id)
    with platform_session(settings) as session:
        registration = read_registration_snapshot(session, source_id)
        _set_local_lock_timeout(session)
        # doc 8.1 §5.1.1：先锁源再锁工作区（原仅工作区 FOR UPDATE）
        from backend.deletion.locks import lock_source_row

        lock_source_row(session, source_id)
        session.get(WorkspaceInfo, workspace_id, with_for_update=True)
        _reverify_source(session, source_id)
        # doc 7.2 §7.4：payload 为预检/执行期复查通过的有效域清单（运行快照），
        # 锁下按同一映射规则重算发布行，不读取共享 YAML。
        domains = payload.get("domains") if isinstance(payload, dict) else None
        if not isinstance(domains, list):
            raise _StepBlocked(
                "运行快照缺少 domain.domains 配置，请编辑作业保存新版本后重新运行"
            )
        rows = domain_rows_from_config(domains, source_id, workspace_id, registration)
        if not rows:
            raise _StepFailed("Domain 配置没有任何有效表归属，请检查配置后重试")
        session.execute(delete(TableDomain).where(TableDomain.source_id == source_id))
        session.add_all(
            TableDomain(
                source_id=source_id,
                publish_run_id=run_id,
                **row,
            )
            for row in rows
        )
        _finish_success(
            session,
            run_id,
            "domain",
            f"Domain 映射发布完成（{len(rows)} 行）",
            input_manifest=input_manifest,
            target_manifest=_pg_target_manifest("domain"),
        )
        session.commit()


# ---- Milvus / Neo4j 步骤 ----


def _publish_table_schema(
    settings, run_id, workspace_id, *, input_manifest, payload
) -> None:
    source_id = _source_id(settings, workspace_id)
    loader = build_table_schema_loader(source_id, workspace_id, run_id)
    try:
        if not loader.validate():
            raise _StepFailed("表结构发布失败：Milvus 或 Embedding 服务不可用")
        result = loader.load()
    finally:
        loader.close()
    success = bool(result.get("success"))
    loaded = result.get("objects_loaded", 0) or 0
    if not success:
        raise _StepFailed(
            f"表结构未全部发布到 Milvus（成功 {loaded} 条 / "
            f"失败 {result.get('objects_skipped', 0) or 0} 条），请重新发布"
        )
    recorded = _record_terminal(
        settings,
        run_id,
        "table_schema",
        STATUS_SUCCESS,
        f"表结构已发布到 Milvus（{loaded} 条）",
        input_manifest=input_manifest,
        target_manifest=build_target_manifest(
            [
                {
                    "type": "milvus",
                    "collection": _milvus_collection("table_schema_loader"),
                }
            ]
        ),
    )
    if recorded != STATUS_SUCCESS:
        raise _StepFailed(
            f"发布步骤已被其他执行者终结为 {recorded}，本次结果不覆盖"
        )


def _publish_dim_value(
    settings, run_id, workspace_id, *, input_manifest, payload
) -> None:
    dim_tables = payload
    source_id = _source_id(settings, workspace_id)
    with platform_session(settings) as session:
        workspace, source = require_workspace_source(session, workspace_id, source_id)
        from backend.security.source_password import decrypt_password as _decrypt

        loader = build_dim_value_loader(
            source,
            _decrypt(source.password_ciphertext),
            workspace_id,
            run_id,
            dim_tables,
        )
        try:
            if not loader.validate():
                raise _StepFailed("维度值发布失败：Milvus、源库或 Embedding 服务不可用")
            result = loader.load()
        finally:
            loader.close()
    success = bool(result.get("success"))
    loaded = result.get("records_loaded", 0) or 0
    if not success:
        raise _StepFailed(_dim_value_failure_summary(result))
    recorded = _record_terminal(
        settings,
        run_id,
        "dim_value",
        STATUS_SUCCESS,
        f"维度值已发布到 Milvus（{loaded} 条）",
        input_manifest=input_manifest,
        target_manifest=build_target_manifest(
            [{"type": "milvus", "collection": _milvus_collection("dim_loader")}]
        ),
    )
    if recorded != STATUS_SUCCESS:
        raise _StepFailed(
            f"发布步骤已被其他执行者终结为 {recorded}，本次结果不覆盖"
        )


def _dim_value_failure_summary(result: dict) -> str:
    kind = result.get("error_kind")
    if kind == "schema_mismatch":
        return "dim_value_embeddings 集合结构为旧版本，请手动删除该集合后重新发布"
    loaded = result.get("records_loaded") or 0
    failed = result.get("records_failed") or 0
    unconfirmed = result.get("records_unconfirmed") or 0
    if unconfirmed:
        return (
            f"维度值未全部发布到 Milvus（成功 {loaded} 条 / 失败 {failed} 条，"
            f"另有 {unconfirmed} 条写入结果未确认），请重新发布"
        )
    if kind == "source":
        return (
            f"维度值发布失败：源库查询失败（{result.get('failed_table')}："
            f"{result.get('error_summary')}），请检查维表配置与源库"
        )
    if kind == "input":
        return "维度值发布输入不可用，请检查 dim 产物"
    return (
        f"维度值未全部发布到 Milvus（成功 {loaded} 条 / 失败 {failed} 条），请重新发布"
    )


def _publish_rel(settings, run_id, workspace_id, *, input_manifest, payload) -> None:
    """rel 发布：短事务准备 → 事务外执行 → 短事务收尾（doc 8.1 §5.1.1）。

    准备事务按“源 → 工作区”加锁复查 marker/指纹并取输入快照，提交即释放
    行锁；备份/生成/validate/load Neo4j 均在会话关闭后执行，运行与步骤保持
    running；loader 关闭后另开短事务记录成功（失败经 _StepFailed 走既有
    收尾路径）。同工作区的 Rel 配置保存与重新采集由运行中检查互斥
    （write_rel_config / _claim_step），不依赖本函数持有的行锁。
    """
    from backend.profile_loading.rel_loader import (
        RelPrecheckError,
        capture_rel_precheck_snapshot,
    )

    source_id = _source_id(settings, workspace_id)

    # ---- 短事务准备：锁内复查 + 输入快照，提交释放行锁 ----
    # doc 10.2 §4.1：prepare 在实际内容写入前，行锁等待超时记 blocked。
    try:
        with platform_session(settings) as session:
            _set_local_lock_timeout(session)
            from backend.deletion.locks import lock_source_row

            lock_source_row(session, source_id)
            session.get(WorkspaceInfo, workspace_id, with_for_update=True)
            _reverify_source(session, source_id)
            # 锁下复查三目录 marker 与内容（预检后变化 → blocked）
            try:
                snapshot = capture_rel_precheck_snapshot(session, source_id, workspace_id)
            except RelPrecheckError as exc:
                raise _StepBlocked(str(exc)) from None
            # rel json 指纹复核（内容变更只能由指纹捕获）
            from backend.profile_loading.file_utils import read_regular_text
            from metaweave.core.clean_targets import resolve_rel_output_dir
            import hashlib

            rel_dir = resolve_rel_output_dir(source_id, workspace_id)
            json_path = (
                rel_dir / f"{snapshot.registration.db_name}.relationships_global.json"
            )
            status, content = read_regular_text(json_path, "rel json")
            fingerprint = (
                hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]
                if status == "ok"
                else None
            )
            if fingerprint != snapshot.rel_fingerprint:
                raise _StepBlocked("Rel 产物内容已变更（预检后有编辑保存），请重新发布")

            cql_dir = resolve_cql_output_dir(source_id, workspace_id)
            managed_snapshot = build_managed_output_snapshot(
                snapshot.main_config,
                source_id,
                workspace_id,
                snapshot.registration.db_name,
                snapshot.registration.selected_schemas,
            )
            loader_config = deepcopy(
                (snapshot.main_config.get("loaders") or {}).get("cql_loader") or {}
            )
            loader_config["input_file"] = str(
                cql_dir / f"import_all.{snapshot.registration.db_name}.cypher"
            )
            neo4j_target = {
                "type": "neo4j",
                "database": _neo4j_database(snapshot.main_config),
            }
            session.commit()  # 释放源/工作区行锁（doc 8.1：不跨外部 I/O 持锁）
    except SQLAlchemyError as exc:
        if _is_lock_timeout(exc):
            raise _StepBlocked(
                "publish_lock_timeout：Rel 准备事务等待源/工作区行锁超时，"
                "未执行（可重试）"
            ) from None
        raise

    # ---- 事务外执行：备份 → 生成 → validate → load（运行/步骤保持 running） ----
    cql_dir.mkdir(parents=True, exist_ok=True)
    backup_directory_before_update(cql_dir)
    domain_yaml = resolve_domain_output_dir(source_id, workspace_id) / "db_domains.yaml"
    generator = CQLGenerator(
        resolve_project_path(METADATA_CONFIG),
        domain_resolver=DomainResolver(domain_yaml),
        config=managed_snapshot,
        provenance=(source_id, str(run_id)),
    )
    gen_result = generator.generate()
    outputs = [Path(p) for p in (gen_result.output_files or [])]
    gen_counts = (
        gen_result.tables_count,
        gen_result.columns_count,
        gen_result.relationships_count,
    )
    outputs_ok = bool(outputs) and all(
        p.is_file() and not p.is_symlink() and p.resolve().parent == cql_dir.resolve()
        for p in outputs
    )
    if not gen_result.success or not outputs_ok:
        raise _StepFailed(
            "Rel 发布失败：CQL 生成或产物核验未通过（"
            + ((gen_result.errors or ["产物核验未通过"])[0])
            + "）"
        )
    # ---- Neo4j 目标互斥（doc 10.2 §5.1）：部署级 advisory lock + 持久保留 ----
    # CQL 生成/产物核验均为文件操作，不持目标锁；validate/load 的 schema
    # 与数据操作在锁内执行，loader 注入锁存活探测（每条 schema 语句与
    # 数据事务前后）。保留记录对任何重新进入的执行尝试一律生效（含同
    # owner_id 重试），只能人工核验后清除。清除时序：步骤终态**可靠提交
    # 之后**才允许清除；且仅限"无在途/结果已确认"的路径——
    # - validate 失败、脚本校验拒绝（图写入前，无任何目标写入）→ 可清；
    # - load 成功 → 可清；
    # - load 失败一律保留：_rolled_back 只证明数据事务未开始/已回滚，
    #   schema 阶段异常同样返回 _rolled_back=True，无法区分已发出的
    #   独立 schema 请求是否结束（§5.1，Codex R1 意见 5）；
    # - 失锁、状态写回失败、未知异常 → 保留。
    from backend.publishing.target_lock import (
        TargetLockLostError,
        TargetLockTimeoutError,
    )

    neo_uri = _neo4j_uri(snapshot.main_config)
    neo_db = _neo4j_database(snapshot.main_config)
    # 可读键与锁键同一规范化口径（主机+端口+database；协议/别名写法
    # 不区分实例，2026-11-06 堵别名坑）
    target_key = neo4j_target_guard_key(neo_uri, neo_db)
    target_lock_key = neo4j_target_lock_key(neo_uri, neo_db)

    def _clear_target_guard() -> None:
        try:
            with platform_session(settings) as gsession:
                release_target_guard(
                    gsession, target_key, "publish_run", str(run_id)
                )
                gsession.commit()
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "清除 Neo4j 目标保留记录失败（需人工核验清除）"
                "run_id=%s type=%s",
                run_id,
                type(exc).__name__,
            )

    def _fail_rel(message: str, *, clear_guard: bool) -> None:
        """先可靠提交 failed 终态，再按需清除保留记录，最后抛受控失败。

        ``_record_terminal`` 写回失败抛 ``_StateWriteFailed``（§6.1 控制面
        故障，保留记录不清）；写回生效且 ``clear_guard`` 才清除。外层
        ``_StepFailed`` 处理器对已终结行幂等。
        """
        recorded = _record_terminal(
            settings,
            run_id,
            "rel",
            STATUS_FAILED,
            message,
            input_manifest=input_manifest,
        )
        if clear_guard and recorded == STATUS_FAILED:
            _clear_target_guard()
        raise _StepFailed(message)

    loader = CQLLoader(
        {"cql_loader": loader_config},
        source_id=source_id,
    )
    try:
        target_conn = acquire_target_lock(
            settings, target_lock_key, wait_timeout_s=publish_lock_timeout_ms() / 1000
        )
    except TargetLockTimeoutError as exc:
        raise _StepBlocked(str(exc)) from None

    try:
        with platform_session(settings) as gsession:
            existing = reserve_target_guard(
                gsession, target_key, "publish_run", str(run_id)
            )
            gsession.commit()
        if existing is not None:
            raise _StepBlocked(
                "Neo4j 目标保留记录存在（"
                f"owner={existing['owner_kind']}:{existing['owner_id']}），"
                "上次执行中断且结果未核验，请人工核验清除后重试"
            )
        # 取得连接后挂锁探测回调（构造已在锁前完成）
        loader._lock_probe = lambda: ensure_target_lock_alive(target_conn)
        try:
            ensure_target_lock_alive(target_conn)
            if not loader.validate():
                _fail_rel("Rel 发布失败：Neo4j 服务不可用", clear_guard=True)
            try:
                ensure_target_lock_alive(target_conn)
                result = loader.load()
            except CQLScriptValidationError as exc:
                _fail_rel(f"Rel 发布失败：{exc}", clear_guard=True)
            ensure_target_lock_alive(target_conn)
        finally:
            loader.close()

        # ---- 成功/失败收尾全部在目标锁保护期内完成（DeepSeek R1 #1）：
        # success 终态可靠提交并清除保留记录之后才释放 advisory lock——
        # 与删除侧一致，消除"锁已放、guard 未清"窗口内其他执行者被保留
        # 记录误拒的竞态。平台库短事务在锁保护下执行，不持行锁。
        success = bool(result.get("success"))
        logger.info(
            "rel 发布图事务结束 source_id=%s publish_run_id=%s success=%s "
            "expected=(tables=%s columns=%s rel=%s) actual_counts=%s",
            source_id,
            run_id,
            success,
            gen_counts[0],
            gen_counts[1],
            gen_counts[2],
            result.get("counts"),
        )
        if not success:
            if result.get("_rolled_back"):
                # schema 阶段异常与数据事务回滚共用 _rolled_back=True，无法
                # 确认已发出的 schema 请求结束 → 保留记录（Codex R1 意见 5）
                _fail_rel(
                    "Rel 发布失败（图未完成替换，旧图保留）："
                    + _first_rel_error(result)
                    + "；目标保留记录已保留，重跑前须人工核验并清除",
                    clear_guard=False,
                )
            _fail_rel(
                "Rel 发布失败：Neo4j 图事务提交状态不可知；目标保留记录已保留，"
                "重跑前须人工核验 Neo4j 结果并清除保留记录",
                clear_guard=False,
            )
        tables, columns, relationships = gen_counts
        message = (
            f"Rel 已发布到 Neo4j（{tables} 张表 / {columns} 列 / "
            f"{relationships} 条关系）"
        )
        if relationships == 0:
            message += "；关系为空，请检查 Rel 产物与阈值配置"

        # 先可靠提交成功终态，再清除保留记录（§5.1 执行段粒度）
        recorded = _record_terminal(
            settings,
            run_id,
            "rel",
            STATUS_SUCCESS,
            message,
            input_manifest=input_manifest,
            target_manifest=build_target_manifest([neo4j_target]),
        )
        if recorded != STATUS_SUCCESS:
            # 行已被其他执行者终结：不覆盖、不清保留记录，按 DB 事实对齐
            raise _StepFailed(
                f"发布步骤已被其他执行者终结为 {recorded}，本次结果不覆盖"
            )
        _clear_target_guard()
    except TargetLockLostError as exc:
        # 失锁（§5.1）：停止后续操作、不宣告成功；保留记录不清。
        _fail_rel(
            f"Rel 发布失败：{exc}；目标保留记录已保留，请人工核验后清除再重试",
            clear_guard=False,
        )
    finally:
        try:
            release_target_lock(target_conn, target_lock_key)
        except Exception:  # noqa: BLE001 —— 释放失败不得遮蔽正在传播的主异常
            logger.exception("释放 Neo4j 目标锁失败（不遮蔽主异常）")


class _StepBlocked(RuntimeError):
    """执行期复查不合格：记 blocked、未实际执行。"""


def _first_rel_error(result: dict) -> str:
    errors = result.get("errors")
    if isinstance(errors, list) and errors:
        first = errors[0]
        return first.get("error_message") if isinstance(first, dict) else str(first)
    return "请检查 Neo4j 与产物后重试"


def _publish_sql(settings, run_id, workspace_id, *, input_manifest, payload) -> None:
    source_id = _source_id(settings, workspace_id)
    loader = build_sql_example_loader(source_id, workspace_id, run_id)
    try:
        if not loader.validate():
            raise _StepFailed("SQL 样例发布失败：Milvus 或 Embedding 服务不可用")
        result = loader.load()
    finally:
        loader.close()
    success = bool(result.get("success"))
    loaded = result.get("records_loaded", 0) or 0
    if not success:
        raise _StepFailed(_sql_failure_summary(result))
    recorded = _record_terminal(
        settings,
        run_id,
        "sql",
        STATUS_SUCCESS,
        f"SQL 样例已发布到 Milvus（共 {loaded} 条）",
        input_manifest=input_manifest,
        target_manifest=build_target_manifest(
            [{"type": "milvus", "collection": _milvus_collection("sql_loader")}]
        ),
    )
    if recorded != STATUS_SUCCESS:
        raise _StepFailed(
            f"发布步骤已被其他执行者终结为 {recorded}，本次结果不覆盖"
        )


def _sql_failure_summary(result: dict) -> str:
    error_kind = result.get("error_kind")
    if error_kind == "schema_mismatch":
        return "sql_example_embeddings 集合结构为旧版本，请手动删除该集合后重新发布"
    if error_kind == "input":
        error_file = result.get("error_file")
        if error_file:
            return f"SQL 样例产物不可读或结构非法（{error_file}），请重跑 sql 采集步骤"
        return "SQL 样例产物不可读或结构非法，请重跑 sql 采集步骤"
    loaded = result.get("records_loaded", 0)
    failed = result.get("records_failed", 0)
    unconfirmed = result.get("records_unconfirmed", 0)
    if unconfirmed > 0:
        summary = (
            f"SQL 样例未全部发布到 Milvus（成功 {loaded} 条 / 失败 {failed} 条，"
            f"另有 {unconfirmed} 条写入结果未确认），请重新发布"
        )
    elif failed > 0:
        summary = (
            f"SQL 样例未全部发布到 Milvus（成功 {loaded} 条 / 失败 {failed} 条），"
            "请重新发布"
        )
    elif error_kind == "cleanup":
        summary = (
            f"SQL 样例已写入 Milvus（共 {loaded} 条），但本源旧记录未清理，请重新发布"
        )
    else:
        return f"SQL 样例发布失败（{result.get('error_summary')}），请重新发布"
    if result.get("error_summary"):
        summary = f"{summary}；原因：{result['error_summary']}"
    return summary


def _source_id(settings: PlatformSettings, workspace_id: str) -> str:
    with platform_session(settings) as session:
        workspace = get_workspace(session, workspace_id)
        if workspace is None:
            raise _StepBlocked("工作区不存在")
        return workspace.source_id


def _reverify_source(session, source_id: str) -> None:
    from backend.data_sources.repository import get_db_source

    source = get_db_source(session, source_id)
    if source is None:
        raise MetaLoadSourceNotFoundError("未找到指定 source_id 的数据源")
    if not source.is_active:
        raise MetaLoadSourceDisabledError("指定数据源已禁用，不能发布")


def _milvus_collection(loader_key: str) -> str:
    config = load_main_config_snapshot(resolve_project_path(METADATA_CONFIG))
    loaders = config.get("loaders") or {}
    node = loaders.get(loader_key) or {}
    return str(node.get("collection_name", "")).strip() or loader_key


def _neo4j_database(main_config: dict) -> str:
    cql_loader = (main_config.get("loaders") or {}).get("cql_loader") or {}
    neo = cql_loader.get("neo4j") or {}
    return str(neo.get("database") or "neo4j").strip() or "neo4j"


def _neo4j_uri(main_config: dict) -> str:
    """Neo4j 目标 URI（doc 10.2 §5.1 目标锁键组成；缺省空串保持稳定）。

    与删除侧 ``resolve_deletion_targets`` 使用同一 ``normalize_neo4j_uri``
    规范化，保证两侧锁键一致（否则互斥失效）。
    """
    from metaweave.core.clean_targets import normalize_neo4j_uri

    cql_loader = (main_config.get("loaders") or {}).get("cql_loader") or {}
    neo = cql_loader.get("neo4j") or {}
    raw = str(neo.get("uri") or "").strip()
    return normalize_neo4j_uri(raw) if raw else ""


_PUBLISHERS = {
    "db": _publish_db,
    "ddl": _publish_ddl,
    "json": _publish_json,
    "domain": _publish_domain,
    "table_schema": _publish_table_schema,
    "dim_value": _publish_dim_value,
    "rel": _publish_rel,
    "sql": _publish_sql,
}
