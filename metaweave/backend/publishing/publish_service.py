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
    revision_no: int,
    mode: str,
    steps: object,
    step_config: object,
) -> dict:
    """校验 + 集中预检 + 创建发布运行；预检失败整单 400（无业务记录）。"""
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
                {"step": step, "reason": result.reason, **({"code": result.code} if result.code else {})}
                for step, result in results.items()
                if not result.ready
            ]
            if failures:
                raise PublishPrecheckError(failures)
            run_id = new_run_id()
            create_run(
                PUBLISH_RUN,
                session,
                run_id=run_id,
                job_id=publish_job_id,
                revision_no=revision_no,
                steps=ordered,
                step_params=resolved.effective_step_params,
                trigger_type="manual",
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
    """工作区行锁下原子认领发布步骤；其他运行的 running 同名步骤 → 阻断。"""
    from sqlalchemy import select

    from backend.persistence.models import (
        PublishJobInfo,
        PublishRunInfo,
        PublishRunStep,
    )

    with platform_session(settings) as session:
        workspace = session.get(WorkspaceInfo, workspace_id, with_for_update=True)
        if workspace is None:
            return "工作区不存在"
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

        mark_step_running(PUBLISH_RUN, session, run_id, step_name)
        session.commit()
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


def execute_publish_run(settings: PlatformSettings, run_id: UUID) -> None:
    """后台执行本次请求新建的发布运行（固定顺序；blocked/failed 语义）。"""
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
        if not mark_run_running(PUBLISH_RUN, session, run_id):
            logger.warning("发布运行无法置 running run_id=%s", run_id)
            return
        session.commit()

    results: list[dict] = []
    for step_name in steps:
        outcome = _execute_step(settings, run_id, workspace_id, step_name)
        results.append(outcome)

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


def _execute_step(
    settings: PlatformSettings,
    run_id: UUID,
    workspace_id: str,
    step_name: str,
) -> dict:
    """执行单个发布步骤：执行期复查 → 认领 → 实际执行 → 状态与清单。"""
    from metaweave.utils.logger import set_current_step

    set_current_step(step_name)
    with platform_session(settings) as session:
        workspace = get_workspace(session, workspace_id)
        if workspace is None:
            _record_terminal(
                settings, run_id, step_name, STATUS_BLOCKED, "工作区不存在"
            )
            return {"step": step_name, "status": STATUS_BLOCKED}
        source_id = workspace.source_id
        # 执行期复查（doc 6.2 §6.4）：marker/文件/内容不合格记 blocked
        from backend.data_sources.repository import get_db_source

        source = get_db_source(session, source_id)
        if source is None:
            _record_terminal(
                settings, run_id, step_name, STATUS_BLOCKED, "数据源不存在"
            )
            return {"step": step_name, "status": STATUS_BLOCKED}
        if not source.is_active:
            _record_terminal(
                settings, run_id, step_name, STATUS_BLOCKED, "数据源已禁用，不能发布"
            )
            return {"step": step_name, "status": STATUS_BLOCKED}
        # 执行期复查（doc 6.2 §6.4 + doc 7.2 §5.3）：dim_value/domain 使用
        # 运行步骤冻结的有效配置（创建时固化的完整清单），复查外部条件但
        # 不重新读取共享 YAML 或最新作业版本。
        if step_name in ("dim_value", "domain"):
            step_row = get_run_step(PUBLISH_RUN, session, run_id, step_name)
            frozen = step_row.step_config if step_row is not None else None
            effective_params = {
                step_name: frozen if isinstance(frozen, dict) else {}
            }
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
            _record_terminal(
                settings, run_id, step_name, STATUS_BLOCKED, result.reason or "未就绪"
            )
            return {"step": step_name, "status": STATUS_BLOCKED}
        input_manifest = _input_manifest_from(result.input_files)
        payload = result.payload

    blocked = _claim_publish_step(settings, workspace_id, step_name, run_id)
    if blocked is not None:
        _record_terminal(settings, run_id, step_name, STATUS_BLOCKED, blocked)
        return {"step": step_name, "status": STATUS_BLOCKED}

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
        _record_terminal(settings, run_id, step_name, STATUS_BLOCKED, str(exc))
        return {"step": step_name, "status": STATUS_BLOCKED}
    except (
        ProfileInputError,
        DDLInputError,
        JsonPublishInputError,
        DomainPublishInputError,
    ) as exc:
        # 发布输入契约失败（doc 6.2 §8.2）：受控失败，保留具体原因
        _record_terminal(
            settings,
            run_id,
            step_name,
            STATUS_FAILED,
            str(exc) or INPUT_SUMMARY,
            input_manifest=input_manifest,
        )
        return {"step": step_name, "status": STATUS_FAILED}
    except _StepFailed as exc:
        _record_terminal(
            settings,
            run_id,
            step_name,
            STATUS_FAILED,
            str(exc),
            input_manifest=input_manifest,
        )
        return {"step": step_name, "status": STATUS_FAILED}
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "发布步骤非受控失败 run_id=%s step=%s type=%s",
            run_id,
            step_name,
            type(exc).__name__,
        )
        _record_terminal(
            settings,
            run_id,
            step_name,
            STATUS_FAILED,
            f"发布执行失败（{type(exc).__name__}）",
            input_manifest=input_manifest,
        )
        return {"step": step_name, "status": STATUS_FAILED}
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
) -> None:
    with platform_session(settings) as session:
        try:
            finish_step(
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
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "发布步骤状态写回失败 run_id=%s step=%s type=%s",
                run_id,
                step_name,
                type(exc).__name__,
            )


def _finish_success(
    session,
    run_id: UUID,
    step_name: str,
    message: str,
    *,
    input_manifest: dict | None,
    target_manifest: dict | None,
) -> None:
    finish_step(
        PUBLISH_RUN,
        session,
        run_id,
        step_name,
        STATUS_SUCCESS,
        message,
        input_manifest=input_manifest,
        target_manifest=target_manifest,
    )


# ---- PostgreSQL 内容步骤：内容写入与成功状态同一事务（doc 6.2 §8.3） ----


def _publish_db(settings, run_id, workspace_id, *, input_manifest, payload) -> None:
    source_id = _source_id(settings, workspace_id)
    fields, _dir = read_db_profile_input(source_id, workspace_id)
    with platform_session(settings) as session:
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
    _record_terminal(
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
    _record_terminal(
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
    """rel 发布：锁内复查 → 整目录备份 cql → 生成 → validate → load Neo4j。"""
    from backend.profile_loading.rel_loader import (
        RelPrecheckError,
        capture_rel_precheck_snapshot,
    )

    source_id = _source_id(settings, workspace_id)
    with platform_session(settings) as session:
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
        cql_dir.mkdir(parents=True, exist_ok=True)
        backup_directory_before_update(cql_dir)
        managed_snapshot = build_managed_output_snapshot(
            snapshot.main_config,
            source_id,
            workspace_id,
            snapshot.registration.db_name,
            snapshot.registration.selected_schemas,
        )
        domain_yaml = (
            resolve_domain_output_dir(source_id, workspace_id) / "db_domains.yaml"
        )
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
            p.is_file()
            and not p.is_symlink()
            and p.resolve().parent == cql_dir.resolve()
            for p in outputs
        )
        if not gen_result.success or not outputs_ok:
            raise _StepFailed(
                "Rel 发布失败：CQL 生成或产物核验未通过（"
                + ((gen_result.errors or ["产物核验未通过"])[0])
                + "）"
            )
        loader_config = deepcopy(
            (snapshot.main_config.get("loaders") or {}).get("cql_loader") or {}
        )
        loader_config["input_file"] = str(
            cql_dir / f"import_all.{snapshot.registration.db_name}.cypher"
        )
        loader = CQLLoader({"cql_loader": loader_config}, source_id=source_id)
        try:
            if not loader.validate():
                raise _StepFailed("Rel 发布失败：Neo4j 服务不可用")
            try:
                result = loader.load()
            except CQLScriptValidationError as exc:
                raise _StepFailed(f"Rel 发布失败：{exc}") from None
        finally:
            loader.close()
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
                raise _StepFailed(
                    "Rel 发布失败（已回滚，旧图保留）：" + _first_rel_error(result)
                )
            raise _StepFailed("Rel 发布失败：Neo4j 图事务提交状态不可知，请重跑发布")
        tables, columns, relationships = gen_counts
        message = f"Rel 已发布到 Neo4j（{tables} 张表 / {columns} 列 / {relationships} 条关系）"
        if relationships == 0:
            message += "；关系为空，请检查 Rel 产物与阈值配置"
        _record_terminal(
            settings,
            run_id,
            "rel",
            STATUS_SUCCESS,
            message,
            input_manifest=input_manifest,
            target_manifest=build_target_manifest(
                [{"type": "neo4j", "database": _neo4j_database(snapshot.main_config)}]
            ),
        )


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
    _record_terminal(
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
