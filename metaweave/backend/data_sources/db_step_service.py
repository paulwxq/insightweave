"""db 步骤的入口准备链；独立 CLI 与未来 Worker 共用（doc 9 §4）。"""

import logging
import time
from uuid import uuid4

from backend.data_sources.repository import get_db_source
from backend.jobs.repository import (
    STATUS_FAILED,
    STATUS_SUCCESS,
    SourceRegistrationMissingError,
    StepAlreadyRunningError,
    WorkspaceMissingError,
    WorkspaceSourceMismatchError,
    assert_workspace_owner,
    finish_step,
    start_step,
)
from backend.persistence.platform_db import (
    load_platform_settings_from_env,
    platform_session,
)
from backend.security.source_password import decrypt_password
from metaweave.core.clean_targets import resolve_db_output_dir
from metaweave.core.metadata.db_step import (
    DbExecutionRequest,
    execute_db,
    validate_schema_names,
)
from metaweave.core.metadata.execution import (
    load_main_config_snapshot,
    set_operation_step,
)
from metaweave.utils.source_id import normalize_source_id

logger = logging.getLogger("backend.data_sources")

DB_STEP_NAME = "db"


class DbStepConfigError(ValueError):
    """参数、主配置或数据源登记错误；CLI 将其映射为 exit 2。"""


class SourceDisabledError(DbStepConfigError):
    """数据源已禁用，不能执行作业（doc 11 §4）。

    继承 `DbStepConfigError` 以便 CLI 沿用现有分支映射为 exit 2；
    API 侧为这个子类单独注册 409 `source_disabled`。
    """


def _failure(started: float, message: str, job_id=None) -> dict:
    result = {
        "success": False,
        "execution_time": time.perf_counter() - started,
        "message": message,
    }
    if job_id is not None:
        result["job_id"] = str(job_id)
    return result


def run_db_step(config_path, source_id, clean: bool, job_id=None) -> dict:
    """准备链：校验 → 登记作业状态 → 采集 → 写回状态。

    配置错误（参数、主配置、数据源、--job-id 归属）抛 `DbStepConfigError`
    （exit 2）；平台库写入失败与采集失败返回 success=False（exit 1）。
    """
    started = time.perf_counter()
    set_operation_step("db")
    try:
        source_uuid = normalize_source_id(source_id)
    except ValueError as exc:
        raise DbStepConfigError(str(exc)) from None
    job_uuid = None
    if job_id is not None:
        try:
            job_uuid = normalize_source_id(job_id)
        except ValueError:
            raise DbStepConfigError("--job-id 不是合法 UUID") from None
    try:
        main_config = load_main_config_snapshot(config_path)
    except ValueError as exc:
        raise DbStepConfigError(str(exc)) from None
    except Exception as exc:
        raise DbStepConfigError(
            f"主配置加载或校验失败（{type(exc).__name__}）"
        ) from None
    try:
        settings = load_platform_settings_from_env()
    except ValueError as exc:
        raise DbStepConfigError(str(exc)) from None

    if job_uuid is None:
        job_uuid = uuid4()

    request = None
    try:
        with platform_session(settings) as session:
            source = get_db_source(session, source_uuid)
            if source is None:
                raise DbStepConfigError("未找到指定 source_id 的数据源")
            if source.db_type != "postgresql":
                raise DbStepConfigError("当前仅支持 PostgreSQL 数据源")
            if not source.username or not source.host or not source.db_name:
                raise DbStepConfigError("数据源登记缺少必要的连接字段")
            if not source.is_active:
                raise SourceDisabledError("该数据源已禁用，不能执行作业")
            if job_id is not None:
                try:
                    assert_workspace_owner(session, job_uuid, source_uuid)
                except (WorkspaceMissingError, WorkspaceSourceMismatchError) as exc:
                    raise DbStepConfigError(str(exc)) from None
            try:
                schemas = validate_schema_names(source.selected_schemas)
                password = decrypt_password(source.password_ciphertext)
            except ValueError as exc:
                raise DbStepConfigError(str(exc)) from None
            request = DbExecutionRequest(
                source_id=source_uuid,
                job_id=job_uuid,
                db_type=source.db_type,
                host=source.host,
                port=source.port,
                db_name=source.db_name,
                username=source.username,
                password=password,
                sslmode=source.sslmode,
                selected_schemas=schemas,
                clean=clean,
                main_config=main_config,
            )
    except DbStepConfigError:
        raise
    except Exception as exc:
        return _failure(started, f"平台库连接或读取失败（{type(exc).__name__}）")

    try:
        with platform_session(settings) as session:
            start_step(session, job_uuid, source_uuid, DB_STEP_NAME)
    except SourceRegistrationMissingError:
        # 锁下重查发现登记已被并发删除（doc 16 §6）：不写 running、不新增步骤行
        raise DbStepConfigError("未找到指定 source_id 的数据源") from None
    except StepAlreadyRunningError as exc:
        return _failure(started, str(exc), job_uuid)
    except Exception as exc:
        return _failure(started, f"作业状态登记失败（{type(exc).__name__}）", job_uuid)

    # 状态写回失败的日志需要产物路径；提前解析一次，不在 except 块里现调
    # resolver——若此时工作区已被换成符号链接或越界，resolver 抛出的
    # ValueError 会吞掉下面的设计文案。解析失败时 execute_db 会以同一
    # 原因失败并写 failed，这里只记占位。
    try:
        db_artifact = resolve_db_output_dir(source_uuid, job_uuid) / "database.json"
    except (ValueError, OSError):
        db_artifact = None

    result = execute_db(request)

    try:
        with platform_session(settings) as session:
            finish_step(
                session,
                job_uuid,
                DB_STEP_NAME,
                STATUS_SUCCESS if result.get("success") else STATUS_FAILED,
            )
    except Exception as exc:
        # 产物路径只进日志，API 回显仅 job_id（受控路径不出错误消息）。
        logger.error(
            "作业状态更新失败（%s） source_id=%s job_id=%s 产物路径=%s",
            type(exc).__name__,
            source_uuid,
            job_uuid,
            db_artifact,
        )
        return _failure(
            started,
            "采集产物可能已生成，但作业状态更新失败"
            f"（{type(exc).__name__}）；job_id={job_uuid}，请查看日志确认作业状态与产物",
            job_uuid,
        )

    result["job_id"] = str(job_uuid)
    return result
