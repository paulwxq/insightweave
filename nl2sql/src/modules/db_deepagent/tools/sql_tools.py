"""SQL 工具：validate_sql、execute_sql（docs/v4/5 §7.7–§7.8）

只做管线调用与返回包组装；校验／授权／执行／预算全部在 SqlPipeline 内。
两个工具都返回 Command：账本更新与 ToolMessage 同批（§8.2、§8.5）。
"""

from __future__ import annotations

from typing import Any

from langchain.tools import ToolRuntime
from langchain_core.tools import tool
from pydantic import Field

from src.modules.db_deepagent.context import DbAgentContext
from src.modules.db_deepagent.schemas import ExecutionRecord
from src.modules.db_deepagent.services import DbAgentServices
from src.modules.db_deepagent.services.result_store import _encode_value
from src.modules.db_deepagent.services.sql_pipeline import LedgerView
from src.modules.db_deepagent.state import DbDeepAgentState
from src.modules.db_deepagent.tools._envelope import finalize_output, tool_command
from src.modules.db_deepagent.tools.schema_tools import (
    RuntimeToolArgs,
    _attach_validation_handler,
    _merge_warnings,
)
from src.utils.logger import get_module_logger

logger = get_module_logger("nl2sql.db_deepagent.tools.sql")

_EXPLAIN_PLAN_MAX_CHARS = 3000  # §7.7：explain_plan 截断到 3000 字符
_EXECUTED_SQL_MAX_CHARS = 3000  # §7.8：executed_sql 截断到 3000 字符


def make_validate_sql_tool(services: DbAgentServices):
    """构造 validate_sql 工具（§7.7）。"""

    class ValidateSqlArgs(RuntimeToolArgs):
        sql: str = Field(min_length=1, max_length=20000, description="待校验的 SQL")

    @tool(args_schema=ValidateSqlArgs)
    def validate_sql(
        sql: str,
        runtime: ToolRuntime[DbAgentContext, DbDeepAgentState],
    ):
        """校验 SQL（语法 / 安全 / EXPLAIN 语义 / 输出列 / 时间与维度证据守卫）。

        成功后 SQL 进入授权状态，可交给 execute_sql 执行；文本必须逐字保留到执行。
        可包含结果占位符 {{result:<result_ref>:<column>}}（只能作 IN/NOT IN 的整个值集合）。
        """
        ctx = runtime.context
        pipeline = services.pipeline
        outcome, record, update = pipeline.validate(
            sql,
            ctx,
            LedgerView(runtime.state),
            purpose="query",
            tool_call_id=runtime.tool_call_id or "",
        )
        if outcome.valid:
            data: dict[str, Any] = {
                "layer": "all_passed",
                "warnings": outcome.warnings,
            }
            if outcome.explain_plan:
                data["explain_plan"] = outcome.explain_plan[:_EXPLAIN_PLAN_MAX_CHARS]
            if record and record.get("placeholder_refs"):
                data["placeholder_expansion"] = [
                    {"result_ref": ref} for ref in record["placeholder_refs"]
                ]
            payload = {"status": "ok", "data": data, "warnings": []}
        else:
            payload = {
                "status": "error",
                "data": {
                    "layer": outcome.layer,
                    "errors": outcome.errors,
                    "error_codes": outcome.error_codes,
                },
                "warnings": [],
                "error": {
                    "error_type": "validation_failed",
                    "message": outcome.errors[0] if outcome.errors else "校验失败",
                    "retryable": True,
                    "hint": "按 error_codes 修正 SQL；时间边界先 resolve_time_range，"
                    "维度值先 search_schema(dim_values) 或 probe_dimension_values",
                },
            }
        content, finalize_warnings = finalize_output(
            payload, services.config.results.max_tool_output_chars
        )
        content = _merge_warnings(content, finalize_warnings)
        logger.info(
            "[request_id=%s run_id=%s] validate_sql: valid=%s codes=%s",
            ctx.request_id,
            ctx.run_id,
            outcome.valid,
            outcome.error_codes,
        )
        return tool_command(
            content,
            runtime.tool_call_id or "",
            updates=update.as_state_update() or None,
        )

    _attach_validation_handler(validate_sql)
    return validate_sql


def make_execute_sql_tool(services: DbAgentServices):
    """构造 execute_sql 工具（§7.8）。"""

    class ExecuteSqlArgs(RuntimeToolArgs):
        sql: str = Field(
            min_length=1,
            max_length=20000,
            description="要执行的 SQL，必须与某条有效授权记录的文本逐字相同",
        )

    @tool(args_schema=ExecuteSqlArgs)
    def execute_sql(
        sql: str,
        runtime: ToolRuntime[DbAgentContext, DbDeepAgentState],
    ):
        """执行已通过校验的 SQL（只读事务、超时与行数上限由服务端控制）。

        source_truncated=true 表示数据库结果超过保存上限，未保存部分无法通过
        翻页取得，不能把已保存行当作总体。
        """
        ctx = runtime.context
        pipeline = services.pipeline
        outcome, update = pipeline.execute(
            sql,
            ctx,
            LedgerView(runtime.state),
            purpose="query",
            tool_call_id=runtime.tool_call_id or "",
        )
        if not outcome.ok:
            payload = {
                "status": "error",
                "data": {},
                "warnings": [],
                "error": {
                    "error_type": outcome.error_type or "internal_error",
                    "message": outcome.message or "执行失败",
                    "retryable": True,
                    **({"hint": outcome.hint} if outcome.hint else {}),
                },
            }
        else:
            meta = outcome.meta
            exec_record: ExecutionRecord | None = (
                update.executions[0] if update.executions else None
            )
            data: dict[str, Any] = {
                "evidence_id": exec_record["evidence_id"] if exec_record else "",
                "result_ref": meta.result_ref,
                "columns": meta.columns,
                "saved_row_count": meta.saved_row_count,
                "source_truncated": meta.source_truncated,
                # 预览值按 §6.8 编码（Decimal/date 等转 JSON 安全形态）
                "preview_rows": [
                    [_encode_value(v) for v in row] for row in outcome.preview_rows
                ],
                "preview_truncated": meta.saved_row_count > len(outcome.preview_rows),
                "elapsed_ms": outcome.elapsed_ms,
            }
            if meta.sql_template != meta.executed_sql:
                # 含占位符时返回展开后的实际执行文本（§7.8）
                data["executed_sql"] = (meta.executed_sql or "")[
                    :_EXECUTED_SQL_MAX_CHARS
                ]
            payload = {
                "status": "empty" if meta.saved_row_count == 0 else "ok",
                "data": data,
                "warnings": [],
            }
        content, finalize_warnings = finalize_output(
            payload, services.config.results.max_tool_output_chars
        )
        content = _merge_warnings(content, finalize_warnings)
        logger.info(
            "[request_id=%s run_id=%s] execute_sql: ok=%s error_type=%s",
            ctx.request_id,
            ctx.run_id,
            outcome.ok,
            outcome.error_type,
        )
        return tool_command(
            content,
            runtime.tool_call_id or "",
            updates=update.as_state_update() or None,
        )

    _attach_validation_handler(execute_sql)
    return execute_sql


__all__ = ["make_execute_sql_tool", "make_validate_sql_tool"]
