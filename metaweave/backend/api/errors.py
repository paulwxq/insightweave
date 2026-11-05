"""业务异常到不泄露请求体或连接参数的 HTTP 响应映射。"""

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from backend.data_sources.connection_check import SourceConnectionFailedError
from backend.data_sources.step_context import DbStepConfigError, SourceDisabledError
from metaweave.utils.source_id import ShortIdError
from backend.data_sources.domain_step_service import DomainPreparationError
from backend.data_sources.dim_config_step_service import DimConfigPreparationError
from backend.data_sources.rel_step_service import RelPreparationError
from backend.data_sources.sql_step_service import SqlPreparationError
from backend.data_sources.registration_service import (
    DuplicateSourceError,
    OutputDeleteFailedError,
    PasswordRequiredError,
    PlatformUnavailableError,
    SourceBusyError,
    SourceCredentialError,
    SourceHasWorkspacesError,
    SourceNotFoundError,
)
from backend.jobs.collection_run import UnsupportedCollectionStepError
from backend.jobs.job_params import StepConfigError
from backend.jobs.job_service import (
    JobNameExistsError,
    JobNotFoundError,
    JobRevisionNotFoundError,
    JobValidationError,
)
from backend.jobs.collection_run import CollectionRunError
from backend.jobs.run_detail import RunNotFoundError
from backend.profile_loading.db_loader import (
    MetaLoadSourceDisabledError,
    MetaLoadSourceNotFoundError,
)
from backend.profile_loading.dim_value_config_api import (
    DimConfigBackupFailedError,
    DimConfigOutputMissingError,
    DimConfigRegeneratedError,
    DimValueColumnMissingError,
    DimValueConfigInvalidError,
    DimValueEmptyTablesError,
    DimValueNotConfiguredError,
)
from backend.profile_loading.domain_config_api import (
    DomainConfigBackupFailedError,
    DomainConfigInvalidError,
    DomainConfigOutputMissingError,
    DomainConfigRegeneratedError,
    DomainConfigWriteFailedError,
)
from backend.profile_loading.rel_config_api import (
    RelConfigInvalidError,
    RelConfigOutputMissingError,
    RelRegeneratedError,
    RelWriteFailedError,
)
from backend.publishing.repository import RelPublishInProgressError
from backend.publishing.precheck import PublishPrecheckError
from backend.publishing.publish_service import (
    PublishRunError,
    UnsupportedPublicationStepError,
)
from backend.assets.ddl_display import (
    DefinitionInconsistentError,
    DefinitionInvalidError,
)
from backend.assets.object_detail import (
    ObjectNotFoundError,
    ProfileInconsistentError,
)
from backend.workspaces.service import (
    WorkspaceDirectoryError,
    WorkspaceNameExistsError,
    WorkspaceNotFoundError,
    WorkspaceValidationError,
)
from metaweave.core.metadata.db_step import (
    DbProfileValidationError,
    SchemaMissingError,
    SchemaNoUsageError,
    SchemaNotSelectableError,
    UnsupportedServerVersionError,
)

logger = logging.getLogger("backend.api")


def _route_template(request: Request) -> str:
    """取路由模板（doc 15 §4：不把请求输入的原始路径写进日志）。"""
    return getattr(request.scope.get("route"), "path", None) or "-"


def _response(status: int, code: str, message: str, *, details=None) -> JSONResponse:
    from fastapi.encoders import jsonable_encoder

    body = {"code": code, "message": message}
    if details is not None:
        body["details"] = details
    # doc 8.1 §7.3：错误载荷可能携带阻断运行的 UUID/datetime（如作业删除
    # 的 resource_has_unfinished_runs.details），统一经 jsonable_encoder
    # 转换，避免 409 因序列化失败变 500
    return JSONResponse(status_code=status, content=jsonable_encoder(body))


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(RequestValidationError)
    async def invalid_request(_request: Request, _exc: RequestValidationError):
        # 不访问 exc.errors()：其中可能包含完整请求体或明文密码。
        return _response(422, "invalid_request", "请求字段不合法")

    # ---- 数据源 ----

    @app.exception_handler(DuplicateSourceError)
    async def duplicate_source(_request: Request, exc: DuplicateSourceError):
        message = (
            "已有相同连接信息的数据源，请确认是否仍要保存。"
            if exc.updating
            else "已登记相同连接信息的数据源，请确认是否仍要新建"
        )
        return _response(409, "duplicate_source", message, details=exc.details)

    @app.exception_handler(SourceNotFoundError)
    async def source_not_found(_request: Request, _exc: SourceNotFoundError):
        return _response(404, "not_found", "数据源不存在")

    @app.exception_handler(SourceHasWorkspacesError)
    async def source_has_workspaces(_request: Request, _exc: SourceHasWorkspacesError):
        return _response(
            409,
            "source_has_workspaces",
            "该数据源已关联工作区，不能删除；请先人工处理工作区相关记录",
        )

    @app.exception_handler(SourceBusyError)
    async def source_busy(_request: Request, _exc: SourceBusyError):
        return _response(
            409, "source_busy", "该数据源有采集或发布步骤正在执行，不能删除"
        )

    @app.exception_handler(MetaLoadSourceNotFoundError)
    async def publish_source_not_found(
        _request: Request, _exc: MetaLoadSourceNotFoundError
    ):
        return _response(404, "not_found", "数据源不存在")

    @app.exception_handler(MetaLoadSourceDisabledError)
    async def publish_source_disabled(
        _request: Request, _exc: MetaLoadSourceDisabledError
    ):
        return _response(409, "source_disabled", "该数据源已禁用，不能发布画像")

    @app.exception_handler(SourceDisabledError)
    async def source_disabled(_request: Request, _exc: SourceDisabledError):
        return _response(409, "source_disabled", "该数据源已禁用，不能发起采集作业")

    @app.exception_handler(DbStepConfigError)
    async def db_step_config(_request: Request, exc: DbStepConfigError):
        return _response(400, "invalid_request", str(exc))

    @app.exception_handler(PasswordRequiredError)
    async def password_required(_request: Request, _exc: PasswordRequiredError):
        return _response(422, "password_required", "连接信息已修改，请填写密码后重试")

    @app.exception_handler(SourceCredentialError)
    async def source_credential(request: Request, exc: SourceCredentialError):
        logger.error(
            "API 处理失败: route=%s type=%s",
            _route_template(request),
            type(exc).__name__,
        )
        return _response(500, "internal_error", "服务暂时无法处理请求")

    @app.exception_handler(UnsupportedServerVersionError)
    async def unsupported_version(
        _request: Request, _exc: UnsupportedServerVersionError
    ):
        return _response(
            422, "unsupported_postgresql_version", "仅支持 PostgreSQL 15.0 及以上"
        )

    @app.exception_handler(SchemaMissingError)
    async def schema_missing(_request: Request, _exc: SchemaMissingError):
        return _response(422, "schema_missing", "所选 Schema 不存在")

    @app.exception_handler(SchemaNoUsageError)
    async def schema_no_usage(_request: Request, _exc: SchemaNoUsageError):
        return _response(422, "schema_no_usage", "对所选 Schema 没有访问权限")

    @app.exception_handler(SchemaNotSelectableError)
    async def schema_not_selectable(_request: Request, _exc: SchemaNotSelectableError):
        return _response(422, "schema_not_selectable", "系统或临时 Schema 不可选")

    @app.exception_handler(DbProfileValidationError)
    async def profile_invalid(_request: Request, _exc: DbProfileValidationError):
        return _response(422, "invalid_request", "数据源信息不合法")

    @app.exception_handler(SourceConnectionFailedError)
    async def source_failed(_request: Request, _exc: SourceConnectionFailedError):
        return _response(422, "connection_failed", "源库连接或查询失败")

    # ---- 工作区 ----

    @app.exception_handler(WorkspaceNotFoundError)
    async def workspace_not_found(_request: Request, _exc: WorkspaceNotFoundError):
        return _response(404, "not_found", "工作区不存在")

    @app.exception_handler(WorkspaceNameExistsError)
    async def workspace_name_exists(_request: Request, _exc: WorkspaceNameExistsError):
        return _response(409, "workspace_name_exists", "同一数据源下已存在同名工作区")

    @app.exception_handler(WorkspaceValidationError)
    async def workspace_invalid(_request: Request, exc: WorkspaceValidationError):
        return _response(400, "invalid_request", str(exc))

    @app.exception_handler(WorkspaceDirectoryError)
    async def workspace_directory_failed(
        request: Request, _exc: WorkspaceDirectoryError
    ):
        logger.error(
            "API 处理失败: route=%s type=WorkspaceDirectoryError",
            _route_template(request),
        )
        return _response(500, "workspace_directory_failed", "工作区目录创建失败")

    # ---- 作业与版本 ----

    @app.exception_handler(JobNotFoundError)
    async def job_not_found(_request: Request, _exc: JobNotFoundError):
        return _response(404, "not_found", "作业不存在")

    @app.exception_handler(JobRevisionNotFoundError)
    async def job_revision_not_found(_request: Request, _exc: JobRevisionNotFoundError):
        return _response(404, "not_found", "作业版本不存在")

    @app.exception_handler(JobNameExistsError)
    async def job_name_exists(_request: Request, _exc: JobNameExistsError):
        return _response(409, "job_name_exists", "同一工作区内已存在同名作业")

    @app.exception_handler(JobValidationError)
    async def job_invalid(_request: Request, exc: JobValidationError):
        return _response(400, "invalid_request", str(exc))

    @app.exception_handler(StepConfigError)
    async def step_config_invalid(_request: Request, exc: StepConfigError):
        return _response(400, "step_config_invalid", str(exc))

    # ---- 运行 ----

    @app.exception_handler(RunNotFoundError)
    async def run_not_found(_request: Request, _exc: RunNotFoundError):
        return _response(404, "not_found", "运行不存在")

    @app.exception_handler(CollectionRunError)
    async def collection_run_invalid(_request: Request, exc: CollectionRunError):
        return _response(400, "invalid_request", str(exc))

    @app.exception_handler(PublishRunError)
    async def publish_run_invalid(_request: Request, exc: PublishRunError):
        return _response(400, "invalid_request", str(exc))

    @app.exception_handler(UnsupportedCollectionStepError)
    async def unsupported_collection_step(
        _request: Request, exc: UnsupportedCollectionStepError
    ):
        return _response(400, "step_not_supported", str(exc))

    @app.exception_handler(UnsupportedPublicationStepError)
    async def unsupported_publication_step(
        _request: Request, exc: UnsupportedPublicationStepError
    ):
        return _response(400, "step_not_supported", str(exc))

    @app.exception_handler(PublishPrecheckError)
    async def publish_precheck_failed(_request: Request, exc: PublishPrecheckError):
        # 整单 400 并指出各步骤及原因（A12/A15/A24）；不生成运行、无业务记录。
        return _response(
            400,
            "publish_precheck_failed",
            "发布预检未通过，本次请求未创建发布运行",
            details=exc.failures,
        )

    # ---- 配置编辑 ----

    @app.exception_handler(DimConfigOutputMissingError)
    async def dim_config_output_missing(
        _request: Request, exc: DimConfigOutputMissingError
    ):
        return _response(
            404, "dim_config_output_missing", str(exc), details=exc.details
        )

    @app.exception_handler(DimValueConfigInvalidError)
    async def dim_value_config_invalid(
        _request: Request, exc: DimValueConfigInvalidError
    ):
        return _response(400, "dim_value_config_invalid", str(exc), details=exc.details)

    @app.exception_handler(DimValueEmptyTablesError)
    async def dim_value_empty_tables(_request: Request, exc: DimValueEmptyTablesError):
        return _response(400, "dim_value_empty_tables", str(exc), details=exc.details)

    @app.exception_handler(DimValueNotConfiguredError)
    async def dim_value_not_configured(
        _request: Request, exc: DimValueNotConfiguredError
    ):
        return _response(400, "dim_value_not_configured", str(exc), details=exc.details)

    @app.exception_handler(DimValueColumnMissingError)
    async def dim_value_column_missing(
        _request: Request, exc: DimValueColumnMissingError
    ):
        return _response(400, "dim_value_column_missing", str(exc), details=exc.details)

    @app.exception_handler(DimConfigBackupFailedError)
    async def dim_config_backup_failed(
        _request: Request, exc: DimConfigBackupFailedError
    ):
        return _response(500, "dim_config_backup_failed", str(exc))

    @app.exception_handler(DimConfigRegeneratedError)
    async def dim_config_regenerated(_request: Request, exc: DimConfigRegeneratedError):
        return _response(409, "dim_config_regenerated", str(exc), details=exc.details)

    @app.exception_handler(RelConfigOutputMissingError)
    async def rel_config_output_missing(
        _request: Request, exc: RelConfigOutputMissingError
    ):
        return _response(404, "rel_config_output_missing", str(exc))

    @app.exception_handler(RelConfigInvalidError)
    async def rel_config_invalid(_request: Request, exc: RelConfigInvalidError):
        return _response(400, "rel_config_invalid", str(exc), details=exc.details)

    @app.exception_handler(RelRegeneratedError)
    async def rel_regenerated(_request: Request, _exc: RelRegeneratedError):
        return _response(409, "rel_regenerated", "Rel 产物已重新生成，请重新加载")

    @app.exception_handler(RelPublishInProgressError)
    async def rel_publish_in_progress(
        _request: Request, exc: RelPublishInProgressError
    ):
        # doc 8.1 §5.1.1：发布执行期与配置保存的互斥（替代原工作区行锁）；
        # 错误类统一来自 publishing.repository（domain/rel 配置保存共用）
        return _response(409, "rel_publish_in_progress", exc.message)

    @app.exception_handler(RelWriteFailedError)
    async def rel_write_failed(_request: Request, exc: RelWriteFailedError):
        return _response(500, "rel_write_failed", str(exc))

    @app.exception_handler(DomainConfigOutputMissingError)
    async def domain_config_output_missing(
        _request: Request, exc: DomainConfigOutputMissingError
    ):
        return _response(404, "domain_config_output_missing", str(exc))

    @app.exception_handler(DomainConfigInvalidError)
    async def domain_config_invalid(_request: Request, exc: DomainConfigInvalidError):
        return _response(400, "domain_config_invalid", str(exc), details=exc.details)

    @app.exception_handler(DomainConfigRegeneratedError)
    async def domain_config_regenerated(
        _request: Request, exc: DomainConfigRegeneratedError
    ):
        return _response(409, "domain_config_regenerated", str(exc))

    @app.exception_handler(DomainConfigBackupFailedError)
    async def domain_config_backup_failed(
        _request: Request, exc: DomainConfigBackupFailedError
    ):
        return _response(500, "domain_config_backup_failed", str(exc))

    @app.exception_handler(DomainConfigWriteFailedError)
    async def domain_config_write_failed(
        _request: Request, exc: DomainConfigWriteFailedError
    ):
        return _response(500, "domain_config_write_failed", str(exc))

    # ---- 平台库与准备阶段故障 ----

    @app.exception_handler(PlatformUnavailableError)
    async def platform_failed(request: Request, exc: PlatformUnavailableError):
        logger.error(
            "API 处理失败: route=%s type=%s",
            _route_template(request),
            type(exc).__name__,
        )
        return _response(503, "platform_unavailable", "平台库暂不可用")

    @app.exception_handler(DomainPreparationError)
    async def domain_preparation_failed(request: Request, exc: DomainPreparationError):
        logger.error(
            "API 处理失败: route=%s type=%s",
            _route_template(request),
            type(exc).__name__,
        )
        return _response(
            503, "domain_preparation_error", "Domain 准备阶段故障，请查看日志"
        )

    @app.exception_handler(DimConfigPreparationError)
    async def dim_config_preparation_failed(
        request: Request, exc: DimConfigPreparationError
    ):
        logger.error(
            "API 处理失败: route=%s type=%s",
            _route_template(request),
            type(exc).__name__,
        )
        return _response(
            503, "dim_config_preparation_error", "Dim 准备阶段故障，请查看日志"
        )

    @app.exception_handler(RelPreparationError)
    async def rel_preparation_failed(request: Request, exc: RelPreparationError):
        logger.error(
            "API 处理失败: route=%s type=%s",
            _route_template(request),
            type(exc).__name__,
        )
        return _response(503, "rel_preparation_error", "Rel 准备阶段故障，请查看日志")

    @app.exception_handler(SqlPreparationError)
    async def sql_preparation_failed(request: Request, exc: SqlPreparationError):
        logger.error(
            "API 处理失败: route=%s type=%s",
            _route_template(request),
            type(exc).__name__,
        )
        return _response(503, "sql_preparation_error", "SQL 准备阶段故障，请查看日志")

    @app.exception_handler(OutputDeleteFailedError)
    async def output_delete_failed(request: Request, exc: OutputDeleteFailedError):
        logger.error(
            "API 处理失败: route=%s type=%s",
            _route_template(request),
            type(exc).__name__,
        )
        return _response(
            500, "output_delete_failed", "该数据源的输出目录删除失败，登记记录仍保留"
        )

    # ---- 对象读取（doc 19.2 §8：404/409 区分客户端场景） ----

    @app.exception_handler(ObjectNotFoundError)
    async def object_not_found(_request: Request, _exc: ObjectNotFoundError):
        return _response(404, "not_found", "对象不存在")

    @app.exception_handler(ProfileInconsistentError)
    async def profile_inconsistent(_request: Request, _exc: ProfileInconsistentError):
        return _response(
            409, "profile_inconsistent", "已发布画像状态不一致，暂无法展示"
        )

    @app.exception_handler(DefinitionInconsistentError)
    async def definition_inconsistent(
        _request: Request, _exc: DefinitionInconsistentError
    ):
        return _response(
            409, "definition_inconsistent", "已发布 DDL 状态不一致，暂无法展示"
        )

    @app.exception_handler(DefinitionInvalidError)
    async def definition_invalid(_request: Request, _exc: DefinitionInvalidError):
        # str(exc) 只含类别描述；不回传 DDL 原文或样例值（doc 19.2 §8.1.1）
        return _response(409, "definition_invalid", "DDL 样例块格式异常，暂无法展示")

    # ---- 短 ID 非法（路径/查询参数形态错误 → 422，不与内部错误混淆） ----

    @app.exception_handler(ShortIdError)
    async def short_id_invalid(_request: Request, exc: ShortIdError):
        return _response(422, "invalid_request", str(exc))

    # ---- 删除与知识清理（doc 8.1 §7.3：稳定错误码统一映射） ----

    from backend.deletion.errors import DeletionError

    @app.exception_handler(DeletionError)
    async def deletion_error(_request: Request, exc: DeletionError):
        return _response(
            exc.http_status, exc.code, exc.message, details=exc.payload or None
        )

    @app.exception_handler(Exception)
    async def internal_error(request: Request, exc: Exception):
        logger.error(
            "API 处理失败: route=%s type=%s",
            _route_template(request),
            type(exc).__name__,
        )
        return _response(500, "internal_error", "服务暂时无法处理请求")
