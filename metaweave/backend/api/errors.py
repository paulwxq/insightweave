"""业务异常到不泄露请求体或连接参数的 HTTP 响应映射。"""

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from backend.data_sources.connection_check import SourceConnectionFailedError
from backend.profile_loading.domain_config_api import (
    DomainConfigInvalidError,
    DomainConfigOutputMissingError,
    DomainConfigRegeneratedError,
    DomainConfigWriteFailedError,
)
from backend.profile_loading.dim_value_config_api import (
    DimConfigOutputMissingError,
    DimConfigRegeneratedError,
    DimValueColumnMissingError,
    DimValueConfigInvalidError,
    DimValueEmptyTablesError,
    DimValueNotConfiguredError,
)
from backend.data_sources.db_step_service import (
    DbStepConfigError,
    SourceDisabledError,
)
from backend.data_sources.domain_step_service import DomainPreparationError
from backend.data_sources.dim_config_step_service import DimConfigPreparationError
from backend.data_sources.rel_step_service import RelPreparationError
from backend.data_sources.sql_step_service import SqlPreparationError
from backend.jobs.collection_run import UnsupportedCollectionStepError
from backend.jobs.repository import WorkspaceMissingError, WorkspaceSourceMismatchError
from backend.profile_loading.db_loader import (
    MetaLoadSourceDisabledError,
    MetaLoadSourceNotFoundError,
)
from backend.profile_loading.rel_config_api import (
    RelConfigInvalidError,
    RelConfigOutputMissingError,
    RelRegeneratedError,
    RelWriteFailedError,
)
from backend.publishing.errors import JobNotPublishableError
from backend.publishing.listing import PublishNotFoundError
from backend.publishing.publish_service import (
    PublicationConflictError,
    PublicationStepNotReadyError,
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
from backend.data_sources.registration_service import (
    DuplicateSourceError,
    PasswordRequiredError,
    OutputDeleteFailedError,
    PlatformUnavailableError,
    SourceBusyError,
    SourceCredentialError,
    SourceNotFoundError,
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
    body = {"code": code, "message": message}
    if details is not None:
        body["details"] = details
    return JSONResponse(status_code=status, content=body)


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(RequestValidationError)
    async def invalid_request(_request: Request, _exc: RequestValidationError):
        # 不访问 exc.errors()：其中可能包含完整请求体或明文密码。
        return _response(422, "invalid_request", "请求字段不合法")

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

    @app.exception_handler(PublishNotFoundError)
    async def publish_not_found(_request: Request, _exc: PublishNotFoundError):
        return _response(404, "not_found", "发布不存在或不属于该数据源")

    @app.exception_handler(MetaLoadSourceNotFoundError)
    async def publish_source_not_found(
        _request: Request, _exc: MetaLoadSourceNotFoundError
    ):
        return _response(404, "not_found", "数据源不存在")

    @app.exception_handler(WorkspaceMissingError)
    async def workspace_missing(_request: Request, _exc: WorkspaceMissingError):
        return _response(404, "not_found", "作业不存在或不属于该数据源")

    @app.exception_handler(WorkspaceSourceMismatchError)
    async def workspace_mismatch(_request: Request, _exc: WorkspaceSourceMismatchError):
        return _response(404, "not_found", "作业不存在或不属于该数据源")

    @app.exception_handler(MetaLoadSourceDisabledError)
    async def publish_source_disabled(
        _request: Request, _exc: MetaLoadSourceDisabledError
    ):
        return _response(409, "source_disabled", "该数据源已禁用，不能发布画像")

    @app.exception_handler(UnsupportedPublicationStepError)
    async def unsupported_publication_step(
        _request: Request, exc: UnsupportedPublicationStepError
    ):
        return _response(400, "step_not_supported", str(exc))

    # ---- dim_value 配置编辑与发布拦截错误出口（doc 22.2 审核修订 36、49）----

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

    @app.exception_handler(DimConfigRegeneratedError)
    async def dim_config_regenerated(_request: Request, exc: DimConfigRegeneratedError):
        return _response(409, "dim_config_regenerated", str(exc), details=exc.details)

    # ---- rel 关系编辑错误出口（doc 23.2 §4.5）----
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

    @app.exception_handler(RelWriteFailedError)
    async def rel_write_failed(_request: Request, exc: RelWriteFailedError):
        return _response(500, "rel_write_failed", str(exc))

    # ---- domain 配置编辑错误出口（doc 24.1 §7）----
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

    @app.exception_handler(DomainConfigWriteFailedError)
    async def domain_config_write_failed(
        _request: Request, exc: DomainConfigWriteFailedError
    ):
        return _response(500, "domain_config_write_failed", str(exc))

    @app.exception_handler(JobNotPublishableError)
    async def job_not_publishable(_request: Request, _exc: JobNotPublishableError):
        return _response(
            409, "job_not_publishable", "采集作业存在失败或运行中的步骤，不能发布"
        )

    @app.exception_handler(PublicationStepNotReadyError)
    async def publication_step_not_ready(
        _request: Request, exc: PublicationStepNotReadyError
    ):
        # 消息随异常文本（doc 23.2 §4.5）：各 raise 点本就显式传
        # NOT_READY_MESSAGE 或具体文案（rel 过期/三行/文件面等）——固定
        # 文案会把「Rel 已过期请重跑」等关键提示全部覆盖成「尚未采集成功」
        return _response(400, "step_not_ready", str(exc))

    @app.exception_handler(PublicationConflictError)
    async def publication_conflict(_request: Request, _exc: PublicationConflictError):
        return _response(409, "publish_conflict", "发布发生并发冲突，请重新发布")

    @app.exception_handler(UnsupportedCollectionStepError)
    async def unsupported_collection_step(
        _request: Request, exc: UnsupportedCollectionStepError
    ):
        return _response(400, "step_not_supported", str(exc))

    @app.exception_handler(SourceDisabledError)
    async def source_disabled(_request: Request, _exc: SourceDisabledError):
        # 子类先于父类 DbStepConfigError 命中（FastAPI 按异常的 MRO 选择最具体的处理器）
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

    @app.exception_handler(PlatformUnavailableError)
    async def platform_failed(request: Request, exc: PlatformUnavailableError):
        # 部分抛出链路（registration_service、jobs 等）无业务日志，处理器是
        # 其唯一记录点；有业务日志的路径（发布预检等）会同时有一条业务定位
        # 记录与这条边界诊断，内容不同、各有用途（doc 15 §5）。
        # 不写 str(exc)（cause 可能含连接串）
        logger.error(
            "API 处理失败: route=%s type=%s",
            _route_template(request),
            type(exc).__name__,
        )
        return _response(503, "platform_unavailable", "平台库暂不可用")

    @app.exception_handler(DomainPreparationError)
    async def domain_preparation_failed(request: Request, exc: DomainPreparationError):
        # 子类先于父类 PlatformUnavailableError 命中（FastAPI 按 MRO 选择最具体
        # 的处理器，doc 21.1 §9）；服务捕获处已按阶段记录原始异常类型，这里
        # 只补边界诊断，不回显 str(exc)（封装消息在下表由服务日志承载）。
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
        # 与 DomainPreparationError 同款（doc 22.1 §7/§9）：MRO 先于
        # platform_unavailable 命中；不回显 str(exc)。
        logger.error(
            "API 处理失败: route=%s type=%s",
            _route_template(request),
            type(exc).__name__,
        )
        return _response(
            503, "dim_config_preparation_error", "Dim Config 准备阶段故障，请查看日志"
        )

    @app.exception_handler(RelPreparationError)
    async def rel_preparation_failed(request: Request, exc: RelPreparationError):
        # 与 Domain/DimConfig 同款（doc 23.1 §7/§9）：MRO 先于
        # platform_unavailable 命中；不回显 str(exc)。
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
            500,
            "output_delete_failed",
            "该数据源的输出目录删除失败，登记记录仍保留",
        )

    @app.exception_handler(SourceBusyError)
    async def source_busy(_request: Request, _exc: SourceBusyError):
        return _response(
            409, "source_busy", "该数据源有采集或发布步骤正在执行，不能删除"
        )

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

    @app.exception_handler(ObjectNotFoundError)
    async def object_not_found(_request: Request, _exc: ObjectNotFoundError):
        return _response(404, "not_found", "对象不存在")

    @app.exception_handler(Exception)
    async def internal_error(request: Request, exc: Exception):
        logger.error(
            "API 处理失败: route=%s type=%s",
            _route_template(request),
            type(exc).__name__,
        )
        return _response(500, "internal_error", "服务暂时无法处理请求")
