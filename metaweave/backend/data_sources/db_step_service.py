"""db 步骤 worker：装配采集请求并执行数据库画像采集（doc 9 §4、6.2 §7.1）。

Web 采集运行由编排层传入 `StepContext`（含工作区与运行身份），返回
`StepOutcome`；状态落库与采集 marker 由编排层完成。CLI 调试路径同样使用
本 worker（无运行身份，不写 run 表和 marker）。
"""

import logging
import time

from backend.data_sources.step_context import (
    DbStepConfigError,
    SourceDisabledError,
    StepContext,
    StepOutcome,
)
from metaweave.core.metadata.db_step import (
    DbExecutionRequest,
    execute_db,
    validate_schema_names,
)

logger = logging.getLogger("backend.data_sources")

DB_STEP_NAME = "db"


def run_db_step(ctx: StepContext) -> StepOutcome:
    """执行 db 步骤：校验登记字段 → 采集 → 核验 database.json。

    目录写入前的整目录备份由编排层完成（备份单元是本次目录更新）。
    """
    started = time.perf_counter()
    source = ctx.source
    if source.db_type != "postgresql":
        raise DbStepConfigError("当前仅支持 PostgreSQL 数据源")
    if not source.username or not source.host or not source.db_name:
        raise DbStepConfigError("数据源登记缺少必要的连接字段")
    if not source.is_active:
        raise SourceDisabledError("该数据源已禁用，不能执行作业")
    try:
        schemas = validate_schema_names(source.selected_schemas)
    except ValueError as exc:
        raise DbStepConfigError(str(exc)) from None

    request = DbExecutionRequest(
        source_id=ctx.source_id,
        job_id=ctx.workspace_id,
        db_type=source.db_type,
        host=source.host,
        port=source.port,
        db_name=source.db_name,
        username=source.username,
        password=ctx.decrypt_password(),
        sslmode=source.sslmode,
        selected_schemas=schemas,
        clean=False,
        main_config=ctx.main_config,
    )
    try:
        result = execute_db(request)
    except Exception as exc:  # 生成器内部失败按步骤失败计（不回显原文）
        logger.error(
            "db 采集执行失败 type=%s source_id=%s workspace_id=%s",
            type(exc).__name__,
            ctx.source_id,
            ctx.workspace_id,
        )
        return StepOutcome(
            success=False, message=f"数据库画像采集失败（{type(exc).__name__}）"
        )
    message = str(result.get("message") or "")
    success = bool(result.get("success"))
    output_files = ["db/database.json"] if success else []
    return StepOutcome(
        success=success,
        message=message
        or (f"数据库画像采集完成（{time.perf_counter() - started:.1f}s）"),
        output_files=output_files,
    )
