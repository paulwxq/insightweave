"""Web 发布编排：预检、单步事务和失败记录。不依赖 HTTP。"""

import hashlib
import json
import logging
from copy import deepcopy
from pathlib import Path
from datetime import datetime, timezone
from uuid import UUID, uuid4

from sqlalchemy import delete
from sqlalchemy.exc import IntegrityError

from backend.data_sources.ddl_step_service import build_managed_output_snapshot
from backend.data_sources.registration_service import PlatformUnavailableError
from backend.jobs.repository import (
    STATUS_FAILED,
    STATUS_RUNNING,
    STATUS_SUCCESS,
    WorkspaceMissingError,
    WorkspaceSourceMismatchError,
    assert_job_publishable,
    assert_workspace_owner,
    get_job_status,
    get_step_row,
)
from backend.persistence.models import (
    TableColumnProfile,
    TableDefinition,
    TableDomain,
    TableProfile,
)
from backend.persistence.platform_db import PlatformSettings, platform_session
from backend.profile_loading.db_loader import (
    MetaLoadSourceDisabledError,
    MetaLoadSourceNotFoundError,
    ProfileInputError,
    commit_db_current_state,
    inspect_db_profile_context,
    read_db_profile_input,
    resolve_db_profile_input_path,
)
from backend.profile_loading.ddl_loader import (
    DEFINE_TYPE_DDL,
    DDLInputError,
    check_ddl_output_ready,
    inspect_ddl_definition_context,
    read_ddl_definitions,
)
from backend.profile_loading.domain_loader import (
    DomainPublishInputError,
    check_domain_output_ready,
    inspect_domain_context,
    read_domain_rows,
)
from backend.profile_loading import dim_value_loader as dv_web
from backend.profile_loading.dim_value_config_api import (
    DimValueConfigInvalidError,
    DimValueEmptyTablesError,
    DimValueNotConfiguredError,
    classify_dim_tables_yaml,
)
from backend.profile_loading.dim_value_loader import (
    DIM_VALUE_STEP,
    check_dim_value_output_ready,
    inspect_dim_value_context,
)
from backend.profile_loading.json_loader import (
    JSON_STEP,
    JsonPublishInputError,
    build_profile_rows,
    check_json_output_ready,
    inspect_json_profile_context,
    read_managed_json_documents,
    read_registration_snapshot,
)
from backend.profile_loading.file_utils import read_regular_text
from backend.profile_loading.rel_loader import (
    METADATA_CONFIG,
    REL_STEP,
    assert_rel_publish_strict,
    capture_rel_precheck_snapshot,
    check_rel_output_ready,
    inspect_rel_context,
)
from backend.publishing.errors import (
    JobNotPublishableError,
    PublicationStepNotReadyError,
)
from backend.profile_loading.sql_loader import (
    SQL_STEP,
    build_sql_example_loader,
    check_sql_output_ready,
    inspect_sql_context,
)
from backend.profile_loading.table_schema_loader import (
    TABLE_SCHEMA_STEP,
    inspect_table_schema_context,
    check_table_schema_output_ready,
    build_table_schema_loader,
)
from backend.publishing.repository import (
    clear_current,
    insert_publish_row,
    lock_db_source_row,
)
from metaweave.core.clean_targets import (
    resolve_cql_output_dir,
    resolve_dim_config_output_dir,
    resolve_domain_output_dir,
    resolve_rel_output_dir,
)
from metaweave.core.cql_generator.generator import CQLGenerator
from metaweave.core.domains import DomainResolver
from metaweave.core.loaders.cql_loader import CQLLoader, CQLScriptValidationError
from metaweave.utils.time_utils import utc_isoformat
from metaweave.utils.logger import set_current_step
from metaweave.utils.source_id import normalize_source_id
from metaweave.utils.file_utils import clean_top_level_files, resolve_project_path

logger = logging.getLogger("backend.publishing")

DB_STEP = "db"
DDL_STEP = "ddl"
DOMAIN_STEP = "domain"
INPUT_SUMMARY = "画像输入不可用或校验失败"
WRITE_SUMMARY = "平台库写入失败"
OUTPUT_REASON = "产物不存在或不可用"
NOT_READY_MESSAGE = "步骤尚未采集成功"
JOB_NOT_PUBLISHABLE_MESSAGE = "采集作业存在失败或运行中的步骤，不能发布"
CONFLICT_MESSAGE = "发布发生并发冲突，请重新发布"
INTERNAL_MESSAGE = "服务暂时无法处理请求"
SUCCESS_MESSAGE = "数据库画像发布完成"
DDL_SUCCESS_MESSAGE = "表定义发布完成"
JSON_SUCCESS_MESSAGE = "对象画像发布完成"
DOMAIN_SUCCESS_MESSAGE = "Domain 映射发布完成"
DOMAIN_EMPTY_MESSAGE = "Domain 产物没有表映射，请检查产物后重试"
REGISTRATION_CHANGED_MESSAGE = "数据源登记信息已变化，请重新发布"
_JOB_REASONS = {
    STATUS_FAILED: "最近一次采集失败",
    STATUS_RUNNING: "正在执行",
    None: "没有执行记录",
}
_SUCCESS_PHASE = "成功事务"
_FAILURE_PHASE = "独立失败记录事务"


class UnsupportedPublicationStepError(ValueError):
    """空选择或本阶段不支持的步骤。"""


class PublicationConflictError(RuntimeError):
    """publish_info_current_idx 唯一冲突；本次事务已回滚。"""


class RegistrationChangedError(RuntimeError):
    """源行锁下发现登记版本与预检时不同；按本步骤受控失败处理（doc 19.2 §7.4）。"""


def is_current_index_conflict(exc: BaseException) -> bool:
    """仅当唯一冲突落在 current 部分索引上时为真。"""
    if not isinstance(exc, IntegrityError):
        return False
    return _constraint(exc) == ("23505", "publish_info_current_idx")


def is_primary_key_conflict(exc: BaseException) -> bool:
    """发布记录或画像主键的唯一冲突。"""
    if not isinstance(exc, IntegrityError):
        return False
    sqlstate, name = _constraint(exc)
    return sqlstate == "23505" and name in {
        "publish_info_pkey",
        "db_profile_pkey",
        "table_definition_pkey",
        "table_profile_pkey",
        "table_column_profile_pkey",
        "table_column_profile_object_order_idx",
    }


def _constraint(exc: IntegrityError) -> tuple[str | None, str | None]:
    orig = getattr(exc, "orig", None)
    diag = getattr(orig, "diag", None) if orig is not None else None
    sqlstate = getattr(orig, "sqlstate", None) if orig is not None else None
    name = getattr(diag, "constraint_name", None) if diag is not None else None
    return sqlstate, name


def _log_constraint(
    exc: IntegrityError,
    *,
    source_id: UUID,
    job_id: UUID,
    publish_id: UUID,
    step_name: str,
    phase: str,
) -> None:
    sqlstate, name = _constraint(exc)
    logger.error(
        "发布约束错误 source_id=%s job_id=%s publish_id=%s step_name=%s phase=%s type=%s sqlstate=%s constraint=%s",
        source_id,
        job_id,
        publish_id,
        step_name,
        phase,
        type(exc).__name__,
        sqlstate or "未知",
        name or "未知",
    )


def normalize_publish_steps(steps: list[str]) -> list[str]:
    """允许平台画像、domain 映射与 table_schema 发布，并按首次出现去重。"""
    if not steps:
        raise UnsupportedPublicationStepError("请至少选择一个步骤")
    chosen: list[str] = []
    for step in steps:
        if step not in (
            DB_STEP,
            DDL_STEP,
            JSON_STEP,
            DOMAIN_STEP,
            TABLE_SCHEMA_STEP,
            DIM_VALUE_STEP,
            REL_STEP,
            SQL_STEP,
        ):
            raise UnsupportedPublicationStepError(
                "当前仅支持 db、ddl、json、domain、table_schema、dim_value、rel、sql 步骤"
            )
        if step not in chosen:
            chosen.append(step)
    return chosen


def check_db_output_ready(source_id: UUID, job_id: UUID) -> tuple[bool, str | None]:
    """主文件是否就绪。失败原因对页面固定，具体原因只写 WARNING 日志。"""
    try:
        resolve_db_profile_input_path(source_id, job_id)
    except ProfileInputError as exc:
        logger.warning(
            "发布预检产物不可用 source_id=%s job_id=%s reason=%s",
            source_id,
            job_id,
            str(exc),
        )
        return False, OUTPUT_REASON
    return True, None


def _identifiers(source_id, job_id) -> tuple[UUID, UUID]:
    try:
        return normalize_source_id(source_id), normalize_source_id(job_id)
    except ValueError:
        raise UnsupportedPublicationStepError(
            "source_id 或 job_id 不是合法 UUID"
        ) from None


def check_publication(settings: PlatformSettings, source_id, job_id) -> dict:
    """预检 db、ddl 与 json 步骤。不生成发布 ID，不写发布记录。"""
    source_uuid, job_uuid = _identifiers(source_id, job_id)
    steps = []
    job_publishable = None
    for step_name in (
        DB_STEP,
        DDL_STEP,
        JSON_STEP,
        DOMAIN_STEP,
        REL_STEP,
        TABLE_SCHEMA_STEP,
        DIM_VALUE_STEP,
        SQL_STEP,
    ):
        # 按步骤切换日志分流：ddl/json 步骤的预检日志落各自 log（doc 15）
        set_current_step(step_name)
        inspector = _INSPECTORS[step_name]
        try:
            with platform_session(settings) as session:
                job_status, _run_marker = inspector(session, source_uuid, job_uuid)
                database_name = None
                json_marker = None
                if step_name == TABLE_SCHEMA_STEP and job_status == STATUS_SUCCESS:
                    database_name = read_registration_snapshot(
                        session, source_uuid
                    ).db_name
                    json_marker = get_step_row(session, job_uuid, JSON_STEP).updated_at
                if step_name == JSON_STEP and job_status == STATUS_SUCCESS:
                    # json 产物核对需要登记库名（混库校验，doc 19.2 §7.2）；
                    # 未成功时无需核对产物，不读登记快照
                    database_name = read_registration_snapshot(
                        session, source_uuid
                    ).db_name
        except (
            MetaLoadSourceNotFoundError,
            MetaLoadSourceDisabledError,
            WorkspaceMissingError,
            WorkspaceSourceMismatchError,
        ):
            raise
        except Exception:
            logger.error(
                "发布预检读取平台库失败 source_id=%s job_id=%s step_name=%s",
                source_uuid,
                job_uuid,
                step_name,
            )
            raise PlatformUnavailableError("平台库暂不可用") from None

        if step_name == DB_STEP:
            output_ready, output_reason = check_db_output_ready(source_uuid, job_uuid)
        elif step_name == DDL_STEP:
            output_ready = check_ddl_output_ready(source_uuid, job_uuid)
            output_reason = None if output_ready else OUTPUT_REASON
        elif step_name == TABLE_SCHEMA_STEP:
            output_ready = check_table_schema_output_ready(
                source_uuid, job_uuid, json_marker, database_name
            )
            output_reason = (
                None if output_ready else "JSON、MD 产物缺失或不匹配，请重新采集"
            )
        elif step_name == DOMAIN_STEP:
            # 只判存在与普通文件，不数表映射行数（doc 21.2 §4）：零行仍可勾选
            output_ready = check_domain_output_ready(source_uuid, job_uuid)
            output_reason = None if output_ready else OUTPUT_REASON
        elif step_name == DIM_VALUE_STEP:
            # 显式分支（doc 22.2 §6.1 审核修订 34）：不得落进 json 兜底的 else
            output_ready = check_dim_value_output_ready(source_uuid, job_uuid)
            output_reason = None if output_ready else OUTPUT_REASON
        elif step_name == REL_STEP:
            # 显式分支（doc 23.2 §6.2）：不得落进 json 兜底的 else——
            # 宽松产物就绪：目录顶层非忽略普通文件（忽略规则与执行预检共用）
            output_ready = check_rel_output_ready(source_uuid, job_uuid)
            output_reason = None if output_ready else "该作业没有 Rel 产物"
        elif step_name == SQL_STEP:
            # 显式分支（doc 25.2 §5，22.2 修订 34 同款）：sql-rag 顶层 ≥1 个
            # 非忽略 *.json 普通文件（需求 4 的落点，共享枚举 helper 同口径）
            output_ready = check_sql_output_ready(source_uuid, job_uuid)
            output_reason = None if output_ready else OUTPUT_REASON
        else:
            output_ready = check_json_output_ready(source_uuid, job_uuid, database_name)
            output_reason = None if output_ready else OUTPUT_REASON
        if job_publishable is None:
            job_publishable = _job_is_publishable(settings, source_uuid, job_uuid)
        publishable = job_status == STATUS_SUCCESS and output_ready and job_publishable
        if job_status != STATUS_SUCCESS:
            reason = _JOB_REASONS.get(job_status, _JOB_REASONS[None])
        elif not job_publishable:
            # 本步骤自身成功，但同一 Job 的其他步骤失败或仍在运行
            reason = JOB_NOT_PUBLISHABLE_MESSAGE
        elif not output_ready:
            reason = output_reason
        else:
            reason = None
        steps.append(
            {
                "step_name": step_name,
                "job_status": job_status,
                "output_ready": output_ready,
                "publishable": publishable,
                "reason": reason,
            }
        )
    return {
        "source_id": str(source_uuid),
        "job_id": str(job_uuid),
        "steps": steps,
    }


def _finished_at() -> str:
    return utc_isoformat(datetime.now(timezone.utc))


def _result(step_name: str, status: str, message: str) -> dict:
    return {
        "step": step_name,
        "status": status,
        "message": message,
        "finished_at": _finished_at(),
    }


def _raise_known_conflict(
    exc: IntegrityError,
    *,
    source_id: UUID,
    job_id: UUID,
    publish_id: UUID,
    step_name: str,
    phase: str,
) -> None:
    _log_constraint(
        exc,
        source_id=source_id,
        job_id=job_id,
        publish_id=publish_id,
        step_name=step_name,
        phase=phase,
    )
    if is_current_index_conflict(exc):
        raise PublicationConflictError(CONFLICT_MESSAGE) from None
    if is_primary_key_conflict(exc):
        raise RuntimeError(INTERNAL_MESSAGE) from None


def _store_failure(
    settings: PlatformSettings,
    source_id: UUID,
    job_id: UUID,
    publish_id: UUID,
    step_name: str,
    summary: str,
    revoke_current: bool = False,
) -> dict:
    """失败尝试只插 is_current=False 的行；默认不清 current、不动画像（doc 16 §1）。

    ``revoke_current=True`` 仅用于 table_schema 已开始替换 Milvus 之后的失败：此时
    旧 current 已不能代表 Milvus 现状，须在源行锁下撤销（doc 20.1 §10.3）。

    写入前在源行锁下重查登记（doc 16 §5/§7）：已删除或已禁用时不新增
    失败历史行，直接抛 MetaLoadSourceNotFoundError / MetaLoadSourceDisabledError，
    与成功提交的前置检查对外行为一致（404 not_found / 409 source_disabled）。
    """
    try:
        with platform_session(settings) as session:
            source = lock_db_source_row(session, source_id)
            if source is None:
                raise MetaLoadSourceNotFoundError("未找到指定 source_id 的数据源")
            if not source.is_active:
                raise MetaLoadSourceDisabledError("指定数据源已禁用，不能加载画像")
            if revoke_current:
                clear_current(session, source_id, step_name)
            insert_publish_row(
                session,
                publish_id,
                source_id,
                job_id,
                step_name,
                STATUS_FAILED,
                error_summary=summary,
                is_current=False,
            )
            session.commit()
    except (MetaLoadSourceNotFoundError, MetaLoadSourceDisabledError):
        raise
    except IntegrityError as exc:
        _raise_known_conflict(
            exc,
            source_id=source_id,
            job_id=job_id,
            publish_id=publish_id,
            step_name=step_name,
            phase=_FAILURE_PHASE,
        )
        logger.error(
            "失败记录提交遇到其他约束错误 source_id=%s job_id=%s publish_id=%s",
            source_id,
            job_id,
            publish_id,
        )
        raise PlatformUnavailableError("平台库暂不可用") from None
    except Exception:
        logger.error(
            "失败记录未能提交 source_id=%s job_id=%s publish_id=%s",
            source_id,
            job_id,
            publish_id,
        )
        raise PlatformUnavailableError("平台库暂不可用") from None
    return _result(step_name, STATUS_FAILED, summary)


def _publish_db(
    settings: PlatformSettings,
    source_id: UUID,
    job_id: UUID,
    publish_id: UUID,
    expected_run_marker: datetime,
) -> dict:
    try:
        fields, _directory = read_db_profile_input(source_id, job_id)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        _log_input_failure(exc, source_id, job_id, publish_id)
        return _store_failure(
            settings, source_id, job_id, publish_id, DB_STEP, INPUT_SUMMARY
        )

    try:
        commit_db_current_state(
            settings,
            source_id,
            job_id,
            publish_id,
            fields,
            expected_run_marker,
            verify_job=True,
        )
    except (MetaLoadSourceNotFoundError, MetaLoadSourceDisabledError):
        # 锁下重查发现源已删除或禁用：404/409 直接上抛，不写失败记录（doc 16 §5）。
        raise
    except PublicationStepNotReadyError:
        # 读取输入期间该作业步骤被重跑：输入已不代表当前态，上抛给
        # publish_steps 统一处理（计入结果并写失败尝试历史行）。
        raise
    except IntegrityError as exc:
        _raise_known_conflict(
            exc,
            source_id=source_id,
            job_id=job_id,
            publish_id=publish_id,
            step_name=DB_STEP,
            phase=_SUCCESS_PHASE,
        )
        return _store_failure(
            settings, source_id, job_id, publish_id, DB_STEP, WRITE_SUMMARY
        )
    except Exception:
        logger.error(
            "画像入库失败 source_id=%s job_id=%s publish_id=%s",
            source_id,
            job_id,
            publish_id,
        )
        return _store_failure(
            settings, source_id, job_id, publish_id, DB_STEP, WRITE_SUMMARY
        )
    return _result(DB_STEP, STATUS_SUCCESS, SUCCESS_MESSAGE)


def _commit_ddl_current_state(
    settings: PlatformSettings,
    source_id: UUID,
    job_id: UUID,
    publish_id: UUID,
    rows: list[dict],
    expected_run_marker: datetime,
) -> None:
    """成功提交 DDL 当前态（doc 16 §5）。

    事务：锁源 → 锁下重查登记（不存在/禁用直接抛错，不写失败记录）
    → 锁下重查步骤状态与运行标识（读取输入后步骤被重跑则拒绝提交，
    start_step 与本事务共用源行锁，天然串行化）→ 翻旧 DDL current
    → 删除该源 define_type='ddl' 全部旧行（不动 MD 行，也不动 db_profile）
    → 插本次全部行与成功 current 发布行 → 提交。
    """
    with platform_session(settings) as session:
        source = lock_db_source_row(session, source_id)
        if source is None:
            raise MetaLoadSourceNotFoundError("未找到指定 source_id 的数据源")
        if not source.is_active:
            raise MetaLoadSourceDisabledError("指定数据源已禁用，不能加载画像")
        _verify_step_current_under_lock(session, job_id, DDL_STEP, expected_run_marker)
        clear_current(session, source_id, DDL_STEP)
        session.execute(
            delete(TableDefinition).where(
                TableDefinition.source_id == source_id,
                TableDefinition.define_type == DEFINE_TYPE_DDL,
            )
        )
        session.add_all(TableDefinition(publish_id=publish_id, **row) for row in rows)
        insert_publish_row(
            session,
            publish_id,
            source_id,
            job_id,
            DDL_STEP,
            STATUS_SUCCESS,
            is_current=True,
        )
        session.commit()


def _verify_step_current_under_lock(
    session,
    job_id: UUID,
    step_name: str,
    expected_run_marker: datetime,
) -> None:
    """源行锁下重查步骤：仍为 success 且运行标识未变，否则拒绝提交。

    运行标识是步骤行的 updated_at：start_step/finish_step 每次状态写入
    都会刷新它。读取输入后步骤被重跑（哪怕已重跑完成）都会使标识变化，
    此时读到的输入不再代表当前态，应让调用方重新发布而不是照常提交。
    """
    step = get_step_row(session, job_id, step_name)
    if (
        step is None
        or step.last_status != STATUS_SUCCESS
        or step.updated_at != expected_run_marker
    ):
        raise PublicationStepNotReadyError(NOT_READY_MESSAGE)
    # 逐项取得源行锁后再次核对整个 Job：期间若其他步骤变为 failed/running，
    # 本项不执行数据替换（doc 20.1 §11.2）。
    assert_job_publishable(session, job_id)


def _job_is_publishable(
    settings: PlatformSettings, source_uuid: UUID, job_uuid: UUID
) -> bool:
    """整个 Job 的现有步骤行是否全部 success（作业级准入，doc 20.1 §11.2）。"""
    try:
        with platform_session(settings) as session:
            # 工作区存在与归属已由各步骤的检查器核对，这里只汇总状态
            return get_job_status(session, job_uuid) in (None, STATUS_SUCCESS)
    except Exception:
        logger.error(
            "发布准入读取平台库失败 source_id=%s job_id=%s", source_uuid, job_uuid
        )
        raise PlatformUnavailableError("平台库暂不可用") from None


def _assert_job_admission(
    settings: PlatformSettings, source_uuid: UUID, job_uuid: UUID
) -> None:
    """正式发布请求开始时的作业级准入；不满足不创建发布 ID。"""
    if not _job_is_publishable(settings, source_uuid, job_uuid):
        raise JobNotPublishableError(JOB_NOT_PUBLISHABLE_MESSAGE)


def _log_input_failure(
    exc: Exception, source_id: UUID, job_id: UUID, publish_id: UUID
) -> None:
    reason = (
        str(exc)
        if isinstance(
            exc,
            (
                ProfileInputError,
                DDLInputError,
                JsonPublishInputError,
                DomainPublishInputError,
            ),
        )
        else type(exc).__name__
    )
    logger.error(
        "画像输入失败 source_id=%s job_id=%s publish_id=%s type=%s reason=%s",
        source_id,
        job_id,
        publish_id,
        type(exc).__name__,
        reason,
    )


def _commit_json_current_state(
    settings: PlatformSettings,
    source_id: UUID,
    job_id: UUID,
    publish_id: UUID,
    table_rows: list[dict],
    column_rows: list[dict],
    expected_run_marker: datetime,
    expected_registration_updated_at: datetime,
) -> None:
    """成功提交 JSON 当前画像（doc 19.2 §7.4）。

    事务：锁源 → 锁下复查登记存在/启用且登记版本与预检相同、json 步骤
    仍 success 且运行标识未变 → 翻旧 json current → 删除该源旧
    table_profile 行（字段行由 ON DELETE CASCADE 级联清除）→ 插本批
    对象行与字段行（同一 publish_id）→ 写成功 current → 一次提交。
    """
    with platform_session(settings) as session:
        source = lock_db_source_row(session, source_id)
        if source is None:
            raise MetaLoadSourceNotFoundError("未找到指定 source_id 的数据源")
        if not source.is_active:
            raise MetaLoadSourceDisabledError("指定数据源已禁用，不能加载画像")
        if source.updated_at != expected_registration_updated_at:
            # 登记哪怕只改了显示名称也保守拒绝（doc 19.2 §7.4）
            raise RegistrationChangedError(REGISTRATION_CHANGED_MESSAGE)
        # §7.4 复查清单含"工作区仍属该源"：与预检同口径，防步骤行归属
        # 被并发改写后把不属本源的输入写成当前画像
        assert_workspace_owner(session, job_id, source_id)
        _verify_step_current_under_lock(session, job_id, JSON_STEP, expected_run_marker)
        clear_current(session, source_id, JSON_STEP)
        session.execute(delete(TableProfile).where(TableProfile.source_id == source_id))
        # 先写对象行再写字段行（doc 19.2 §7.4）：两表间只有表级外键、没有
        # ORM relationship，unitofwork 不按外键排序 INSERT，必须显式 flush
        # 让对象行先落库，字段行的外键才能命中。
        session.add_all(TableProfile(**row) for row in table_rows)
        session.flush()
        session.add_all(TableColumnProfile(**row) for row in column_rows)
        insert_publish_row(
            session,
            publish_id,
            source_id,
            job_id,
            JSON_STEP,
            STATUS_SUCCESS,
            is_current=True,
        )
        session.commit()


def _publish_json(
    settings: PlatformSettings,
    source_id: UUID,
    job_id: UUID,
    publish_id: UUID,
    expected_run_marker: datetime,
    registration,
) -> dict:
    try:
        documents = read_managed_json_documents(source_id, job_id, registration.db_name)
        table_rows, column_rows = build_profile_rows(
            source_id,
            job_id,
            publish_id,
            documents,
            registration.selected_schemas,
        )
    except (OSError, UnicodeError, ValueError) as exc:
        _log_input_failure(exc, source_id, job_id, publish_id)
        return _store_failure(
            settings, source_id, job_id, publish_id, JSON_STEP, INPUT_SUMMARY
        )

    try:
        _commit_json_current_state(
            settings,
            source_id,
            job_id,
            publish_id,
            table_rows,
            column_rows,
            expected_run_marker,
            registration.updated_at,
        )
    except (MetaLoadSourceNotFoundError, MetaLoadSourceDisabledError):
        # 锁下重查发现源已删除或禁用：404/409 直接上抛，不写失败记录（doc 16 §5）。
        raise
    except RegistrationChangedError:
        # 源登记版本变化：按本步骤受控失败上抛给 publish_steps 统一处理。
        raise
    except (WorkspaceMissingError, WorkspaceSourceMismatchError):
        # 锁下复查发现工作区归属变化/缺失：输入不再属于该源，受控失败
        logger.error(
            "发布期间工作区归属复查未通过 source_id=%s job_id=%s publish_id=%s",
            source_id,
            job_id,
            publish_id,
        )
        return _store_failure(
            settings, source_id, job_id, publish_id, JSON_STEP, INPUT_SUMMARY
        )
    except PublicationStepNotReadyError:
        # 读取输入期间该作业步骤被重跑：上抛给 publish_steps 统一处理。
        raise
    except IntegrityError as exc:
        _raise_known_conflict(
            exc,
            source_id=source_id,
            job_id=job_id,
            publish_id=publish_id,
            step_name=JSON_STEP,
            phase=_SUCCESS_PHASE,
        )
        return _store_failure(
            settings, source_id, job_id, publish_id, JSON_STEP, WRITE_SUMMARY
        )
    except Exception:
        logger.error(
            "JSON 画像入库失败 source_id=%s job_id=%s publish_id=%s",
            source_id,
            job_id,
            publish_id,
        )
        return _store_failure(
            settings, source_id, job_id, publish_id, JSON_STEP, WRITE_SUMMARY
        )
    return _result(JSON_STEP, STATUS_SUCCESS, JSON_SUCCESS_MESSAGE)


def _publish_ddl(
    settings: PlatformSettings,
    source_id: UUID,
    job_id: UUID,
    publish_id: UUID,
    expected_run_marker: datetime,
) -> dict:
    try:
        rows, _directory = read_ddl_definitions(source_id, job_id)
    except (OSError, UnicodeError, ValueError) as exc:
        _log_input_failure(exc, source_id, job_id, publish_id)
        return _store_failure(
            settings, source_id, job_id, publish_id, DDL_STEP, INPUT_SUMMARY
        )

    try:
        _commit_ddl_current_state(
            settings, source_id, job_id, publish_id, rows, expected_run_marker
        )
    except (MetaLoadSourceNotFoundError, MetaLoadSourceDisabledError):
        # 锁下重查发现源已删除或禁用：404/409 直接上抛，不写失败记录（doc 16 §5）。
        raise
    except PublicationStepNotReadyError:
        # 读取输入期间该作业步骤被重跑：上抛给 publish_steps 统一处理
        # （计入结果并写失败尝试历史行）。
        raise
    except IntegrityError as exc:
        _raise_known_conflict(
            exc,
            source_id=source_id,
            job_id=job_id,
            publish_id=publish_id,
            step_name=DDL_STEP,
            phase=_SUCCESS_PHASE,
        )
        return _store_failure(
            settings, source_id, job_id, publish_id, DDL_STEP, WRITE_SUMMARY
        )
    except Exception:
        logger.error(
            "DDL 入库失败 source_id=%s job_id=%s publish_id=%s",
            source_id,
            job_id,
            publish_id,
        )
        return _store_failure(
            settings, source_id, job_id, publish_id, DDL_STEP, WRITE_SUMMARY
        )
    return _result(DDL_STEP, STATUS_SUCCESS, DDL_SUCCESS_MESSAGE)


def _publish_table_schema(settings, source_id, job_id, publish_id, expected_run_marker):
    """同一源行锁内校验、按源替换 Milvus 记录并登记结果（非跨库事务）。

    current 处理（doc 20.1 §10.3）：首批 upsert 之前失败保留旧 current；一旦已开始
    upsert，Milvus 可能新旧混合，失败时撤销旧 current 并记失败。
    """
    loader = None
    replacement_started = False
    try:
        with platform_session(settings) as session:
            source = lock_db_source_row(session, source_id)
            if source is None:
                raise MetaLoadSourceNotFoundError("未找到指定 source_id 的数据源")
            if not source.is_active:
                raise MetaLoadSourceDisabledError("指定数据源已禁用")
            assert_workspace_owner(session, job_id, source_id)
            # 含整个 Job 的锁下复查
            _verify_step_current_under_lock(session, job_id, "md", expected_run_marker)
            json_step = get_step_row(session, job_id, JSON_STEP)
            if json_step is None or json_step.last_status != STATUS_SUCCESS:
                raise PublicationStepNotReadyError(NOT_READY_MESSAGE)
            loader = build_table_schema_loader(source_id, job_id, publish_id)
            loader.prepare_input(utc_isoformat(json_step.updated_at), source.db_name)
            if not loader.validate():
                raise ValueError("Milvus 或 Embedding 配置、连接不可用")
            # 持锁覆盖实际替换；不在替换前 commit，避免释放源行锁后另一发布插入 current。
            try:
                result = loader.load()
            except Exception as exc:
                logger.error("Milvus 加载异常 type=%s", type(exc).__name__)
                # 异常时无法确认是否已写入，保守按已开始处理
                result = {"success": False, "replacement_started": True}
            success = bool(result.get("success"))
            replacement_started = bool(result.get("replacement_started"))
            if success or replacement_started:
                clear_current(session, source_id, TABLE_SCHEMA_STEP)
            summary = None if success else "表结构未全部发布到 Milvus，请重新发布"
            insert_publish_row(
                session,
                publish_id,
                source_id,
                job_id,
                TABLE_SCHEMA_STEP,
                STATUS_SUCCESS if success else STATUS_FAILED,
                error_summary=summary,
                is_current=success,
            )
            session.commit()
            if not success:
                return _result(TABLE_SCHEMA_STEP, STATUS_FAILED, summary)
    except (
        MetaLoadSourceNotFoundError,
        MetaLoadSourceDisabledError,
        PublicationStepNotReadyError,
        RegistrationChangedError,
    ):
        raise
    except Exception as exc:
        logger.error(
            "table_schema 发布失败 source_id=%s job_id=%s type=%s",
            source_id,
            job_id,
            type(exc).__name__,
        )
        return _store_failure(
            settings,
            source_id,
            job_id,
            publish_id,
            TABLE_SCHEMA_STEP,
            "表结构发布失败，请检查 JSON、MD 产物及 Milvus、Embedding 服务",
            revoke_current=replacement_started,
        )
    finally:
        if loader is not None:
            loader.close()
    return _result(TABLE_SCHEMA_STEP, STATUS_SUCCESS, "表结构已发布到 Milvus")


def _publish_domain(
    settings: PlatformSettings,
    source_id: UUID,
    job_id: UUID,
    publish_id: UUID,
    expected_run_marker: datetime,
    registration,
) -> dict:
    """Domain 映射整份替换（doc 21.2 §4）。

    与 json 不同（§4 规则 7/8）：解析失败、零行、至少一行三态都先做锁下
    复查，复查不符按各自文案拒绝（登记变更、步骤标识已变、整作业不可
    发布不上抛以外由 publish_steps 记失败行）；复查通过后才按解析结果
    用 `INPUT_SUMMARY`、没有表映射或整份替换收尾。`_store_failure` 会另开
    会话再次锁同一源行，所有失败路径都必须等复查会话退出后调用（§4:111）。
    """
    parse_error: Exception | None = None
    rows: list[dict] = []
    try:
        rows = read_domain_rows(source_id, job_id, registration)
    except DomainPublishInputError as exc:
        _log_input_failure(exc, source_id, job_id, publish_id)
        parse_error = exc

    try:
        with platform_session(settings) as session:
            source = lock_db_source_row(session, source_id)
            if source is None:
                raise MetaLoadSourceNotFoundError("未找到指定 source_id 的数据源")
            if not source.is_active:
                raise MetaLoadSourceDisabledError("指定数据源已禁用，不能加载画像")
            if source.updated_at != registration.updated_at:
                raise RegistrationChangedError(REGISTRATION_CHANGED_MESSAGE)
            assert_workspace_owner(session, job_id, source_id)
            _verify_step_current_under_lock(
                session, job_id, DOMAIN_STEP, expected_run_marker
            )
            if parse_error is None and rows:
                clear_current(session, source_id, DOMAIN_STEP)
                session.execute(
                    delete(TableDomain).where(TableDomain.source_id == source_id)
                )
                session.add_all(
                    TableDomain(
                        source_id=source_id,
                        job_id=job_id,
                        publish_id=publish_id,
                        **row,
                    )
                    for row in rows
                )
                insert_publish_row(
                    session,
                    publish_id,
                    source_id,
                    job_id,
                    DOMAIN_STEP,
                    STATUS_SUCCESS,
                    is_current=True,
                )
                session.commit()
    except (MetaLoadSourceNotFoundError, MetaLoadSourceDisabledError):
        # 锁下重查发现源已删除或禁用：404/409 直接上抛，不写失败记录（doc 16 §5）
        raise
    except RegistrationChangedError:
        # 源登记版本变化：上抛给 publish_steps 按受控失败记失败行
        raise
    except (WorkspaceMissingError, WorkspaceSourceMismatchError):
        # 工作区归属变化：本步骤失败行；抛出会让整个请求变 404，
        # 同请求已提交的步骤不回滚，调用方也拿不到 publish_id（doc 21.2 §4:106）
        logger.error(
            "发布期间工作区归属复查未通过 source_id=%s job_id=%s publish_id=%s",
            source_id,
            job_id,
            publish_id,
        )
        return _store_failure(
            settings, source_id, job_id, publish_id, DOMAIN_STEP, INPUT_SUMMARY
        )
    except PublicationStepNotReadyError:
        # 步骤被重跑，或（子类 JobNotPublishableError）整作业不可发布：
        # 上抛给 publish_steps 记失败行；被宽 except 吃掉会误报 WRITE_SUMMARY
        raise
    except IntegrityError as exc:
        _raise_known_conflict(
            exc,
            source_id=source_id,
            job_id=job_id,
            publish_id=publish_id,
            step_name=DOMAIN_STEP,
            phase=_SUCCESS_PHASE,
        )
        return _store_failure(
            settings, source_id, job_id, publish_id, DOMAIN_STEP, WRITE_SUMMARY
        )
    except Exception:
        logger.error(
            "Domain 映射入库失败 source_id=%s job_id=%s publish_id=%s",
            source_id,
            job_id,
            publish_id,
        )
        return _store_failure(
            settings, source_id, job_id, publish_id, DOMAIN_STEP, WRITE_SUMMARY
        )

    # 复查会话已退出，才能让 _store_failure 再次锁同一源行（doc 21.2 §4:111）
    if parse_error is not None:
        return _store_failure(
            settings, source_id, job_id, publish_id, DOMAIN_STEP, INPUT_SUMMARY
        )
    if not rows:
        return _store_failure(
            settings, source_id, job_id, publish_id, DOMAIN_STEP, DOMAIN_EMPTY_MESSAGE
        )
    return _result(DOMAIN_STEP, STATUS_SUCCESS, DOMAIN_SUCCESS_MESSAGE)


def _dim_value_failure_summary(result: dict) -> str:
    """dim_value 失败对外文案（doc 22.2 §7.2 步骤 4 六行表，按优先级写死分派）。"""

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
            f"维度值发布失败：源库查询失败"
            f"（{result.get('failed_table')}：{result.get('error_summary')}），"
            f"请检查维表配置与源库"
        )
    if kind == "input":
        return "维度值发布输入不可用，请检查 dim_config 产物"
    return (
        f"维度值未全部发布到 Milvus（成功 {loaded} 条 / 失败 {failed} 条），请重新发布"
    )


def _publish_dim_value(
    settings: PlatformSettings,
    source_id: UUID,
    job_id: UUID,
    publish_id: UUID,
    expected_run_marker: datetime,
    registration,
    dim_tables,
) -> dict:
    """dim_value 发布执行（doc 22.2 §7.2）：锁内构造受管 loader → validate → load。

    - load() 从不抛出（审核修订 11），success=False 按 replacement_started 决定撤销；
    - 登记版本核对（审核修订 37）：RegistrationChangedError 原样上抛，宽 except 不得吞；
    - validate() 失败文案用六行表①（服务不可用，不得套"请检查 dim_config 产物"）。
    """

    loader = None
    try:
        with platform_session(settings) as session:
            source = lock_db_source_row(session, source_id)
            if source is None:
                raise MetaLoadSourceNotFoundError("未找到指定 source_id 的数据源")
            if not source.is_active:
                raise MetaLoadSourceDisabledError("指定数据源已禁用")
            assert_workspace_owner(session, job_id, source_id)
            # 登记版本核对：dim_value 锁下连源库，登记被改时不得拿旧表清单查新库
            if source.updated_at != registration.updated_at:
                raise RegistrationChangedError(REGISTRATION_CHANGED_MESSAGE)
            _verify_step_current_under_lock(
                session, job_id, "dim_config", expected_run_marker
            )

            loader = dv_web.build_dim_value_loader(
                source,
                dv_web.decrypt_password(source.password_ciphertext),
                job_id,
                publish_id,
                dim_tables,
            )
            if not loader.validate():
                summary = "维度值发布失败：Milvus、源库或 Embedding 服务不可用"
                insert_publish_row(
                    session,
                    publish_id,
                    source_id,
                    job_id,
                    DIM_VALUE_STEP,
                    STATUS_FAILED,
                    error_summary=summary,
                    is_current=False,
                )
                session.commit()
                return _result(DIM_VALUE_STEP, STATUS_FAILED, summary)

            result = loader.load()
            success = bool(result.get("success"))
            replacement_started = bool(result.get("replacement_started"))
            summary = None if success else _dim_value_failure_summary(result)
            if success or replacement_started:
                clear_current(session, source_id, DIM_VALUE_STEP)
            insert_publish_row(
                session,
                publish_id,
                source_id,
                job_id,
                DIM_VALUE_STEP,
                STATUS_SUCCESS if success else STATUS_FAILED,
                error_summary=summary,
                is_current=success,
            )
            session.commit()
            if not success:
                return _result(DIM_VALUE_STEP, STATUS_FAILED, summary)
    except (
        MetaLoadSourceNotFoundError,
        MetaLoadSourceDisabledError,
        PublicationStepNotReadyError,
        RegistrationChangedError,
    ):
        # 锁下重查发现源已删除/禁用、步骤被重跑、登记变更：404/409/受控失败
        # 上抛给 publish_steps，不写失败记录（doc 16 §5、doc 22.2 审核修订 47）
        raise
    except Exception:
        logger.error(
            "dim_value 发布失败 source_id=%s job_id=%s publish_id=%s",
            source_id,
            job_id,
            publish_id,
        )
        return _store_failure(
            settings,
            source_id,
            job_id,
            publish_id,
            DIM_VALUE_STEP,
            "维度值发布失败，请检查配置",
            revoke_current=False,
        )
    finally:
        if loader is not None:
            loader.close()
    loaded = result.get("records_loaded", 0) if isinstance(result, dict) else 0
    return _result(
        DIM_VALUE_STEP, STATUS_SUCCESS, f"维度值已发布到 Milvus（{loaded} 条）"
    )


def _rel_failure_summary(result: dict) -> str:
    """§6.3 失败摘要按序：stats_mismatch → 首个 section 错误 → 加载中断。"""
    mismatches = result.get("stats_mismatch")
    if mismatches:
        detail = "；".join(
            f"{m['item']} 期望 {m['expected']}/实际 {m['actual']}" for m in mismatches
        )
        return f"Rel 发布失败：图内数量与脚本统计不符（{detail}）"
    if result.get("_load_interrupted"):
        return "Rel 发布失败：Neo4j 加载中断，图可能处于半加载状态，请重跑发布"
    errors = result.get("errors")
    if isinstance(errors, list) and errors:
        first = errors[0]
        message = first.get("error_message") if isinstance(first, dict) else str(first)
        return f"Rel 发布失败：{message}"
    return "Rel 发布失败：Neo4j 加载未成功，请重跑发布"


def _publish_rel(
    settings: PlatformSettings,
    source_id: UUID,
    job_id: UUID,
    publish_id: UUID,
    expected_run_marker: datetime,
    snapshot,
) -> dict:
    """rel 发布执行（doc 23.2 §6.3）：锁内复查（登记/三 marker/指纹）→ 清空
    cql 目录 → 生成 cql（含 provenance）→ 核验 → validate → load(clean=True)
    → 发布行（成功 current / 失败非 current）。

    - 锁内禁用 ``_store_failure``（其实现另开会话对同一源行 FOR UPDATE，
      持锁调用即自等待）：确定性失败一律当前 session 直插失败行 + commit；
    - ``CQLScriptValidationError`` 恒在 clean 前抛出（图未动）→ 直插失败行、
      不撤销 current，且该 except 必须列在通用 ``except Exception`` 之前；
    - ``clear_current`` 仅在「未抛 CQLScriptValidationError 且 load() 主体已被
      调用」的路径执行——进入过 load() 旧 current 即不可信（全清语义）。
    """
    loader = None
    gen_counts = None
    replacement_started = False
    try:
        with platform_session(settings) as session:
            source = lock_db_source_row(session, source_id)
            if source is None:
                raise MetaLoadSourceNotFoundError("未找到指定 source_id 的数据源")
            if not source.is_active:
                raise MetaLoadSourceDisabledError("指定数据源已禁用")
            assert_workspace_owner(session, job_id, source_id)
            if source.updated_at != snapshot.registration.updated_at:
                raise RegistrationChangedError(REGISTRATION_CHANGED_MESSAGE)
            _verify_step_current_under_lock(
                session, job_id, REL_STEP, expected_run_marker
            )
            _verify_step_current_under_lock(
                session, job_id, "json", snapshot.json_marker
            )
            _verify_step_current_under_lock(
                session, job_id, "domain", snapshot.domain_marker
            )

            # rel json 指纹复核（行 marker 只反映采集重跑，内容变更只能由指纹捕获）
            rel_dir = resolve_rel_output_dir(source_id, job_id)
            json_path = rel_dir / f"{source.db_name}.relationships_global.json"
            status, content = read_regular_text(json_path, "rel json")
            fingerprint = (
                hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]
                if status == "ok"
                else None
            )
            if fingerprint != snapshot.rel_fingerprint:
                raise PublicationStepNotReadyError(
                    "Rel 产物内容已变更（预检后有编辑保存），请重新发布"
                )

            # ---- 以下锁内顺序执行 ----
            cql_dir = resolve_cql_output_dir(source_id, job_id)
            cql_dir.mkdir(parents=True, exist_ok=True)
            clean_top_level_files(cql_dir)
            # 生成器读 output.json_directory/rel_directory/cql_directory 三键，
            # 必须传受管快照（主配置快照会落到 CLI 目录）；无凭据版兼作误用防线
            managed_snapshot = build_managed_output_snapshot(
                snapshot.main_config,
                source_id,
                job_id,
                source.db_name,
                source.selected_schemas,
            )
            domain_yaml = (
                resolve_domain_output_dir(source_id, job_id) / "db_domains.yaml"
            )
            generator = CQLGenerator(
                resolve_project_path(METADATA_CONFIG),
                domain_resolver=DomainResolver(domain_yaml),
                config=managed_snapshot,
                provenance=(str(source_id), str(publish_id)),
            )
            gen_result = generator.generate()
            gen_counts = (
                gen_result.tables_count,
                gen_result.columns_count,
                gen_result.relationships_count,
            )
            outputs = [Path(p) for p in (gen_result.output_files or [])]
            outputs_ok = (
                bool(outputs)
                and all(
                    p.is_file()
                    and not p.is_symlink()
                    and p.resolve().parent == cql_dir.resolve()
                    for p in outputs
                )
                and any(
                    p.name == f"import_all.{source.db_name}.cypher" for p in outputs
                )
            )
            if not gen_result.success or not outputs_ok:
                first_error = (gen_result.errors or ["产物核验未通过"])[0]
                summary = f"Rel 发布失败：CQL 生成或产物核验未通过（{first_error}）"
                insert_publish_row(
                    session,
                    publish_id,
                    source_id,
                    job_id,
                    REL_STEP,
                    STATUS_FAILED,
                    error_summary=summary,
                    is_current=False,
                )
                session.commit()
                return _result(REL_STEP, STATUS_FAILED, summary)

            # loader_config 取主配置快照的 loaders.cql_loader（Neo4j 凭据不受
            # 工作区覆盖）；数量核验强制开启（§5.5/A24，主配置 false 不生效）
            loader_config = deepcopy(
                (snapshot.main_config.get("loaders") or {}).get("cql_loader") or {}
            )
            loader_config["input_file"] = str(
                cql_dir / f"import_all.{source.db_name}.cypher"
            )
            loader_config.setdefault("options", {})["validate_after_load"] = True
            loader = CQLLoader({"cql_loader": loader_config})
            if not loader.validate():
                summary = "Rel 发布失败：Neo4j 服务不可用"
                insert_publish_row(
                    session,
                    publish_id,
                    source_id,
                    job_id,
                    REL_STEP,
                    STATUS_FAILED,
                    error_summary=summary,
                    is_current=False,
                )
                session.commit()
                return _result(REL_STEP, STATUS_FAILED, summary)

            try:
                result = loader.load(clean=True)
            except CQLScriptValidationError:
                # clean 前抛出（图未动）：直插失败行、不撤销 current
                summary = "Rel 发布失败：CQL 脚本缺少必需章节或统计头"
                insert_publish_row(
                    session,
                    publish_id,
                    source_id,
                    job_id,
                    REL_STEP,
                    STATUS_FAILED,
                    error_summary=summary,
                    is_current=False,
                )
                session.commit()
                return _result(REL_STEP, STATUS_FAILED, summary)
            except Exception:
                # stats 能返回即 clean 已提交；抛异常则是否已提交不可知，保守
                # 按已开始替换处理（table_schema :812-817 同款）
                result = {"success": False, "_load_interrupted": True}

            # 进入过 load()：图已（可能）被替换——此后任何失败（clear_current/
            # 插行/commit 段）都不得保留指向已消失数据的旧 current，宽兜底按
            # 此状态撤销（doc 23.2 §6.3 提交失败撤销）
            replacement_started = True

            success = bool(result.get("success"))
            clear_current(session, source_id, REL_STEP)  # 进入过 load() 即撤销
            summary = None if success else _rel_failure_summary(result)
            insert_publish_row(
                session,
                publish_id,
                source_id,
                job_id,
                REL_STEP,
                STATUS_SUCCESS if success else STATUS_FAILED,
                error_summary=summary,
                is_current=success,
            )
            session.commit()
            if not success:
                return _result(REL_STEP, STATUS_FAILED, summary)
    except (
        MetaLoadSourceNotFoundError,
        MetaLoadSourceDisabledError,
        PublicationStepNotReadyError,
        RegistrationChangedError,
    ):
        # 源消失/禁用/步骤被重跑/登记变更/指纹失配：上抛不写失败行
        raise
    except ValueError as exc:
        if replacement_started:
            # 加载后的意外 ValueError：图已（可能）替换，不得映射 400 保留
            # 旧 current——按已替换处理，锁外补记失败并撤销
            logger.error(
                "rel 发布加载后异常（ValueError） source_id=%s job_id=%s "
                "publish_id=%s: %s",
                source_id,
                job_id,
                publish_id,
                exc,
            )
            return _store_failure(
                settings,
                source_id,
                job_id,
                publish_id,
                REL_STEP,
                "Rel 发布失败，请检查配置",
                revoke_current=True,
            )
        # 配置/产物类（预检已拦，锁下复现，如 generator 构造阈值非法）：400
        raise PublicationStepNotReadyError(str(exc) or NOT_READY_MESSAGE) from None
    except Exception:
        logger.error(
            "rel 发布失败 source_id=%s job_id=%s publish_id=%s "
            "replacement_started=%s",
            source_id,
            job_id,
            publish_id,
            replacement_started,
        )
        # 图已替换（进入过 load()）→ 撤销旧 current；加载前故障（图未动）→ 不撤销
        return _store_failure(
            settings,
            source_id,
            job_id,
            publish_id,
            REL_STEP,
            "Rel 发布失败，请检查配置",
            revoke_current=replacement_started,
        )
    finally:
        if loader is not None:
            loader.close()

    tables, columns, relationships = gen_counts
    message = (
        f"Rel 已发布到 Neo4j（{tables} 张表 / {columns} 列 / {relationships} 条关系）"
    )
    if relationships == 0:
        message += "；关系为空，请检查 Rel 产物与阈值配置"
    return _result(REL_STEP, STATUS_SUCCESS, message)


SQL_MISMATCH_SUMMARY = (
    "sql_example_embeddings 集合结构为旧版本，请手动删除该集合后重新发布"
)


def _sql_failure_summary(result: dict) -> str:
    """失败行文案分派（doc 25.2 §6.3 步骤 5 六行表，按序判定写死）。"""
    error_kind = result.get("error_kind")
    if error_kind == "schema_mismatch":
        return SQL_MISMATCH_SUMMARY
    if error_kind == "input":
        error_file = result.get("error_file")
        if error_file:
            return f"SQL 样例产物不可读或结构非法（{error_file}），请重跑 sql 采集步骤"
        # 无问题文件（合并零条/目录不可读）不渲染 "（None）"（审核修订 27）
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
        # 写入全部成功、删旧失败（§7.3 第 6 条）：replacement_started=True
        # ——与 ③④ 互斥，按六行表序排列（审核修订 27）
        summary = (
            f"SQL 样例已写入 Milvus（共 {loaded} 条），"
            "但本源旧记录未清理，请重新发布"
        )
    else:
        return f"SQL 样例发布失败（{result.get('error_summary')}），请重新发布"
    # ③/④/⑤ 追加规则（审核修订 23、28）：③/④ 非空必来自未分类异常；
    # ⑤ 恒为删旧异常摘要（修订 11），"删旧失败"时保留原因展示
    if result.get("error_summary"):
        summary = f"{summary}；原因：{result['error_summary']}"
    return summary


def _publish_sql(settings, source_id, job_id, publish_id, expected_run_marker):
    """同一源行锁内校验、按源替换 Milvus 记录并登记结果（doc 25.2 §6.3）。

    sql-rag 无编辑 API，run_marker 锁下复查即足够（§6.1）；不连源库、
    不依赖登记快照——5 参通用分支（_PUBLISHERS），table_schema 同款结构。
    """
    loader = None
    replacement_started = False
    loaded_count = 0
    try:
        with platform_session(settings) as session:
            source = lock_db_source_row(session, source_id)
            if source is None:
                raise MetaLoadSourceNotFoundError("未找到指定 source_id 的数据源")
            if not source.is_active:
                raise MetaLoadSourceDisabledError("指定数据源已禁用")
            assert_workspace_owner(session, job_id, source_id)
            _verify_step_current_under_lock(
                session, job_id, SQL_STEP, expected_run_marker
            )
            loader = build_sql_example_loader(source_id, job_id, publish_id)
            if not loader.validate():
                raise ValueError("Milvus 或 Embedding 配置、连接不可用")
            try:
                result = loader.load()
            except Exception as exc:
                logger.error("Milvus 加载异常 type=%s", type(exc).__name__)
                # 异常时无法确认是否已写入，保守按已开始处理；
                # 带上摘要防表⑥渲染 "（None）"（审核修订 27）
                result = {
                    "success": False,
                    "replacement_started": True,
                    "error_summary": str(exc),
                }
            success = bool(result.get("success"))
            replacement_started = bool(result.get("replacement_started"))
            loaded_count = result.get("records_loaded", 0) or 0
            if success or replacement_started:
                clear_current(session, source_id, SQL_STEP)
            summary = None if success else _sql_failure_summary(result)
            insert_publish_row(
                session,
                publish_id,
                source_id,
                job_id,
                SQL_STEP,
                STATUS_SUCCESS if success else STATUS_FAILED,
                error_summary=summary,
                is_current=success,
            )
            session.commit()
            if not success:
                return _result(SQL_STEP, STATUS_FAILED, summary)
    except (
        MetaLoadSourceNotFoundError,
        MetaLoadSourceDisabledError,
        PublicationStepNotReadyError,
        RegistrationChangedError,
    ):
        raise
    except Exception as exc:
        logger.error(
            "sql 发布失败 source_id=%s job_id=%s type=%s",
            source_id,
            job_id,
            type(exc).__name__,
        )
        return _store_failure(
            settings,
            source_id,
            job_id,
            publish_id,
            SQL_STEP,
            "SQL 样例发布失败，请检查 sql-rag 产物及 Milvus、Embedding 服务",
            revoke_current=replacement_started,
        )
    finally:
        if loader is not None:
            loader.close()
    return _result(
        SQL_STEP, STATUS_SUCCESS, f"SQL 样例已发布到 Milvus（共 {loaded_count} 条）"
    )


_PUBLISHERS = {
    DB_STEP: _publish_db,
    DDL_STEP: _publish_ddl,
    TABLE_SCHEMA_STEP: _publish_table_schema,
    SQL_STEP: _publish_sql,
}
_INSPECTORS = {
    DB_STEP: inspect_db_profile_context,
    DDL_STEP: inspect_ddl_definition_context,
    JSON_STEP: inspect_json_profile_context,
    TABLE_SCHEMA_STEP: inspect_table_schema_context,
    DOMAIN_STEP: inspect_domain_context,
    DIM_VALUE_STEP: inspect_dim_value_context,
    REL_STEP: inspect_rel_context,
    SQL_STEP: inspect_sql_context,
}


def publish_steps(
    settings: PlatformSettings, source_id, job_id, steps: list[str]
) -> dict:
    """同步发布所选步骤。前置失败不生成发布 ID。"""
    chosen = normalize_publish_steps(steps)
    source_uuid, job_uuid = _identifiers(source_id, job_id)
    # 预检时记录每个步骤的运行标识（updated_at），提交前在源行锁下核对
    # 未变：预检与读文件期间同一作业被重跑时拒绝发布，避免把重跑中途
    # 的部分产物发布为当前态（doc 16 §5）。json 与 domain 步骤同时记录
    # 登记行快照（db_name/selected_schemas/updated_at），提交时锁下核对
    # 登记版本未变（doc 19.2 §7.4、21.2 §4）。
    run_markers: dict[str, datetime] = {}
    registrations: dict[str, object] = {}
    statuses: dict[str, str | None] = {}
    for step_name in chosen:
        # 按步骤切换日志分流：ddl/json 步骤的发布日志落各自 log（doc 15）
        set_current_step(step_name)
        try:
            with platform_session(settings) as session:
                status, run_marker = _INSPECTORS[step_name](
                    session, source_uuid, job_uuid
                )
                if (
                    step_name in (JSON_STEP, DOMAIN_STEP, DIM_VALUE_STEP)
                    and status == STATUS_SUCCESS
                ):
                    registrations[step_name] = read_registration_snapshot(
                        session, source_uuid
                    )
        except (
            MetaLoadSourceNotFoundError,
            MetaLoadSourceDisabledError,
            WorkspaceMissingError,
            WorkspaceSourceMismatchError,
        ):
            raise
        except Exception:
            logger.error(
                "发布前置读取平台库失败 source_id=%s job_id=%s step_name=%s",
                source_uuid,
                job_uuid,
                step_name,
            )
            raise PlatformUnavailableError("平台库暂不可用") from None
        statuses[step_name] = status
        run_markers[step_name] = run_marker

    # 作业级准入：整个 Job 的现有步骤行全部 success 才能发布，早于生成发布 ID。
    # 必须先于所选步骤自身状态判断：所选步骤 failed/running 时它所在的行已使整个
    # Job 不可发布，应返回 409 job_not_publishable（doc 20.1 §11.2）。
    _assert_job_admission(settings, source_uuid, job_uuid)
    # 作业整体成功时，所选步骤仍可能没有执行记录：属步骤级未就绪（400）
    if any(status != STATUS_SUCCESS for status in statuses.values()):
        raise PublicationStepNotReadyError(NOT_READY_MESSAGE)

    # dim_value 三态拦截（doc 22.2 §6.2 审核修订 14/26/37/42）：在 run_markers
    # 循环与准入/步骤状态两道闸之后、publish_id 之前读 yaml——标记锚定先于内容
    # 读取（读取标记后的重跑必被锁下复查拒绝），作业级问题先报真实原因。
    dim_value_tables = None
    if DIM_VALUE_STEP in chosen:
        registration = registrations[DIM_VALUE_STEP]
        yaml_path = (
            resolve_dim_config_output_dir(source_uuid, job_uuid) / "dim_tables.yaml"
        )
        state, reason, dim_value_cfg = classify_dim_tables_yaml(
            yaml_path, registration.db_name
        )
        if state == "invalid":
            raise DimValueConfigInvalidError(
                "dim_tables.yaml 配置文件无效或不存在，请重新生成后再发布",
                reason or "配置文件无效",
            )
        if state == "empty":
            raise DimValueEmptyTablesError()
        unconfigured = sorted(
            name
            for name, cfg in dim_value_cfg.tables.items()
            if cfg.embedding_col is None
        )
        if unconfigured:
            raise DimValueNotConfiguredError(unconfigured)
        dim_value_tables = dim_value_cfg  # 注入同一份内容（审核修订 19）

    # rel 严格预检与锚点捕获（doc 23.2 §6.2 控制流块）：循环外独立分支——
    # 仅选 rel 时 json/domain 两行不会被循环读到；位于两道闸之后、publish_id
    # 之前，任一失败 400 且不生成发布 ID（与 dim_value yaml 三态拦截同位同序）
    if REL_STEP in chosen:
        assert_rel_publish_strict(settings, source_uuid, job_uuid)
        registrations[REL_STEP] = capture_rel_precheck_snapshot(
            settings, source_uuid, job_uuid
        )

    publish_id = uuid4()
    results = []
    for step in chosen:
        set_current_step(step)
        try:
            if step == JSON_STEP:
                results.append(
                    _publish_json(
                        settings,
                        source_uuid,
                        job_uuid,
                        publish_id,
                        run_markers[step],
                        registrations[step],
                    )
                )
            elif step == DOMAIN_STEP:
                # 与 json 同款专用分支：6 参调用，不走 5 参通用分支（doc 21.2 §4）
                results.append(
                    _publish_domain(
                        settings,
                        source_uuid,
                        job_uuid,
                        publish_id,
                        run_markers[step],
                        registrations[step],
                    )
                )
            elif step == DIM_VALUE_STEP:
                # 与 json/domain 同款单独调用（doc 22.2 审核修订 47）：7 参——
                # 登记快照与拦截归一配置在 5 参通用分支没有入口
                results.append(
                    _publish_dim_value(
                        settings,
                        source_uuid,
                        job_uuid,
                        publish_id,
                        run_markers[step],
                        registrations[step],
                        dim_value_tables,
                    )
                )
            elif step == REL_STEP:
                # doc 23.2 §6.1/§6.3：6 参专用分支（锁内生成+加载 Neo4j），
                # 不进 _PUBLISHERS 五参表
                results.append(
                    _publish_rel(
                        settings,
                        source_uuid,
                        job_uuid,
                        publish_id,
                        run_markers[step],
                        registrations[step],
                    )
                )
            else:
                results.append(
                    _PUBLISHERS[step](
                        settings, source_uuid, job_uuid, publish_id, run_markers[step]
                    )
                )
        except RegistrationChangedError:
            # 源登记信息在预检后发生变化：本步骤受控失败，不误报平台库
            # 故障；同请求已成功的步骤维持成功（doc 19.2 §7.4）。
            logger.error(
                "发布期间登记信息已变化 source_id=%s job_id=%s publish_id=%s step_name=%s",
                source_uuid,
                job_uuid,
                publish_id,
                step,
            )
            results.append(
                _store_failure(
                    settings,
                    source_uuid,
                    job_uuid,
                    publish_id,
                    step,
                    REGISTRATION_CHANGED_MESSAGE,
                )
            )
        except JobNotPublishableError:
            # 执行期间同一 Job 的其他步骤变为 failed/running：本项不执行数据替换，
            # 按逐项失败记录，已完成的其他项不回滚（doc 20.1 §11.2）。
            logger.error(
                "发布期间采集作业状态不再满足准入 source_id=%s job_id=%s publish_id=%s step_name=%s",
                source_uuid,
                job_uuid,
                publish_id,
                step,
            )
            results.append(
                _store_failure(
                    settings,
                    source_uuid,
                    job_uuid,
                    publish_id,
                    step,
                    JOB_NOT_PUBLISHABLE_MESSAGE,
                )
            )
        except PublicationStepNotReadyError as exc:
            # 执行期间该步骤被重跑：此前已提交的步骤保持成功且可追溯，
            # 不整体抛错（调用方会拿不到 publish_id，无法追认已提交的
            # 当前态）。该步骤按未就绪计入结果，并写一条非 current 的
            # failed 历史行（doc 16 §1）：对外报告了失败尝试，发布历史
            # 里就要能凭 publish_id 查到这一步，不留幽灵 ID。
            logger.error(
                "发布期间步骤被重跑 source_id=%s job_id=%s publish_id=%s step_name=%s",
                source_uuid,
                job_uuid,
                publish_id,
                step,
            )
            results.append(
                _store_failure(
                    settings, source_uuid, job_uuid, publish_id, step, str(exc)
                )
            )
    return {"publish_id": str(publish_id), "results": results}
