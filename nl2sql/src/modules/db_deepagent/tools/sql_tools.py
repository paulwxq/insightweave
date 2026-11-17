"""SQL 工具：validate_sql、execute_sql（docs/v4/5 §7.7–§7.8；doc 6.3 §6.1/§5.1）

只做管线调用与返回包组装；校验／授权／执行／预算全部在 SqlPipeline 内。
两个工具都返回 Command：账本更新与 ToolMessage 同批（§8.2、§8.5）。

doc 6.3：validate_sql/execute_sql 增加必填 sub_task_id/sub_task_revision
（先登记再产生成果）；任务准入失败与 SQL 校验/执行失败均写错误账本并
在错误包中返回 failure_ref（任务身份未知/参数层失败不伪造业务归属）。
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
from src.modules.db_deepagent.services.sql_pipeline import make_ledger_view
from src.modules.db_deepagent.state import DbDeepAgentState
from src.modules.db_deepagent.tools._envelope import (
    serialize_tool_output,
    tool_command,
)
from src.modules.db_deepagent.tools._task_errors import (
    failure_ref_of,
    make_task_error_record,
    source_task_ref_of,
)
from src.modules.db_deepagent.tools.schema_tools import (
    RuntimeToolArgs,
    _attach_validation_handler,
)
from src.utils.logger import get_module_logger

logger = get_module_logger("nl2sql.db_deepagent.tools.sql")

_EXPLAIN_PLAN_MAX_CHARS = 3000  # §7.7：explain_plan 截断到 3000 字符
_EXECUTED_SQL_MAX_CHARS = 3000  # §7.8：executed_sql 截断到 3000 字符


# 任务准入类失败（R1-08）：目标未通过服务端核验，不产生归属
_ADMISSION_ERROR_TYPES = frozenset(
    {
        "task_not_registered",
        "task_revision_mismatch",
        "task_not_writable",
        "invalid_task_dependency",
        "stale_task_inputs",
        "session_guard_required",
        "invalid_argument",
    }
)


def _task_error_payload(
    *,
    ctx: DbAgentContext,
    tool: str,
    error_type: str,
    message: str,
    retryable: bool,
    hint: str | None,
    tool_call_id: str,
    sub_task_id: str | None,
    sub_task_revision: int | None,
) -> tuple[Any, dict[str, Any]]:
    """构造 (错误账本记录, 错误返回包字典)（doc 6.3 §5.1；R1-08）。

    归属只来自服务端准入结果：仅当错误类型不在准入失败集合（即目标已
    通过准入后发生的真实执行/校验失败）才填 source_task_ref；不得从
    包装后的 error_type 推断未知目标归属。
    """
    source_ref = (
        source_task_ref_of(ctx, sub_task_id, sub_task_revision)
        if error_type not in _ADMISSION_ERROR_TYPES and sub_task_id
        else None
    )
    record = make_task_error_record(
        ctx=ctx,
        tool=tool,
        error_type=error_type,
        message=message,
        retryable=retryable,
        tool_call_id=tool_call_id,
        source_task_ref=source_ref,
    )
    error_block: dict[str, Any] = {
        "error_type": error_type,
        "message": message,
        "retryable": retryable,
    }
    if hint:
        error_block["hint"] = hint
    payload = {
        "status": "error",
        "data": {"failure_ref": failure_ref_of(record)},
        "warnings": [],
        "error": error_block,
    }
    return record, payload


def make_validate_sql_tool(services: DbAgentServices):
    """构造 validate_sql 工具（§7.7）。"""

    class ValidateSqlArgs(RuntimeToolArgs):
        sql: str = Field(min_length=1, max_length=20000, description="待校验的 SQL")
        sub_task_id: str = Field(
            min_length=1,
            max_length=8,
            description="目标任务 ID（须先经 record_sub_tasks 登记且可写）",
        )
        sub_task_revision: int = Field(
            ge=1, description="目标任务版本（当前 revision）"
        )

    @tool(args_schema=ValidateSqlArgs)
    def validate_sql(
        sql: str,
        sub_task_id: str,
        sub_task_revision: int,
        runtime: ToolRuntime[DbAgentContext, DbDeepAgentState],
    ):
        """校验 SQL（语法 / 安全 / EXPLAIN 语义 / 输出列 / 时间与维度证据守卫）。

        成功后 SQL 进入该任务版本的授权状态，可交给 execute_sql 执行；文本
        必须逐字保留到执行。可包含结果占位符
        {{result:<result_ref>:<column>}}（只能作 IN/NOT IN 的整个值集合）。
        目标任务的声明依赖全部 completed 后才允许校验/执行。
        """
        ctx = runtime.context
        pipeline = services.pipeline
        outcome, record, update = pipeline.validate(
            sql,
            ctx,
            make_ledger_view(services, runtime.state, ctx=ctx),
            purpose="query",
            tool_call_id=runtime.tool_call_id or "",
            sub_task_id=sub_task_id,
            sub_task_revision=sub_task_revision,
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
            message = outcome.errors[0] if outcome.errors else "校验失败"
            # wire 契约保持 error_type=validation_failed（§7.1），具体判据
            # 在 data.error_codes；错误账本记录同用该类型（含子码于
            # message）。R1-08：准入类子码以原码记录——错误工厂据此判定
            # 不伪造归属（未知/未核验目标不得带 source_task_ref）。
            first_code = (outcome.error_codes or [""])[0]
            record_type = (
                first_code
                if first_code in _ADMISSION_ERROR_TYPES
                else "validation_failed"
            )
            err_record, error_payload = _task_error_payload(
                ctx=ctx,
                tool="validate_sql",
                error_type=record_type,
                message=f"{message}（codes: "
                f"{', '.join(outcome.error_codes or [])}）",
                retryable=True,
                hint=(
                    "按 error_codes 修正 SQL；时间边界先 resolve_time_range，"
                    "维度值先 search_schema(dim_values) 或 probe_dimension_values；"
                    "任务依赖未完成时先完成上游任务"
                ),
                tool_call_id=runtime.tool_call_id or "",
                sub_task_id=sub_task_id,
                sub_task_revision=sub_task_revision,
            )
            update.tool_errors.append(err_record)
            error_payload["data"].update(
                {
                    "layer": outcome.layer,
                    "errors": outcome.errors,
                    "error_codes": outcome.error_codes,
                }
            )
            payload = error_payload
        content = serialize_tool_output(payload)
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
        sub_task_id: str = Field(
            min_length=1, max_length=8, description="目标任务 ID（与授权时一致）"
        )
        sub_task_revision: int = Field(ge=1, description="目标任务版本")

    @tool(args_schema=ExecuteSqlArgs)
    def execute_sql(
        sql: str,
        sub_task_id: str,
        sub_task_revision: int,
        runtime: ToolRuntime[DbAgentContext, DbDeepAgentState],
    ):
        """执行已通过校验的 SQL（只读事务、超时与行数上限由服务端控制）。

        source_truncated=true 表示数据库结果超过保存上限，未保存部分无法通过
        翻页取得，不能把已保存行当作总体。SQL 与 sub_task_id/sub_task_revision
        必须与 validate_sql 成功时完全一致。
        """
        ctx = runtime.context
        pipeline = services.pipeline
        outcome, update = pipeline.execute(
            sql,
            ctx,
            make_ledger_view(services, runtime.state, ctx=ctx),
            purpose="query",
            tool_call_id=runtime.tool_call_id or "",
            sub_task_id=sub_task_id,
            sub_task_revision=sub_task_revision,
        )
        if not outcome.ok:
            if outcome.error_type == "budget_exhausted":
                # 预算停止是运行级信号，不是任务工具失败，不写错误账本
                payload = {
                    "status": "error",
                    "data": {},
                    "warnings": [],
                    "error": {
                        "error_type": "budget_exhausted",
                        "message": outcome.message or "执行预算已耗尽",
                        "retryable": False,
                        "stop_reason": outcome.stop_reason,
                    },
                }
            else:
                err_record, error_payload = _task_error_payload(
                    ctx=ctx,
                    tool="execute_sql",
                    error_type=outcome.error_type or "internal_error",
                    message=outcome.message or "执行失败",
                    retryable=True,
                    hint=outcome.hint,
                    tool_call_id=runtime.tool_call_id or "",
                    sub_task_id=sub_task_id,
                    sub_task_revision=sub_task_revision,
                )
                update.tool_errors.append(err_record)
                payload = error_payload
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
                # R1-07：新证据的完整引用（后续沿用/清单寻址用同一身份）；
                # 置于段尾以保 result_ref 留在转存预览头 5 行内
                "source_evidence_ref": {
                    "session_id": ctx.session_id,
                    "thread_id": ctx.thread_id,
                    "run_id": ctx.run_id,
                    "evidence_id": exec_record["evidence_id"] if exec_record else "",
                },
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
        content = serialize_tool_output(payload)
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
