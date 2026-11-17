"""record_sub_tasks：业务任务登记工具（doc 6.3 §5）

一个工具维护计划与终态：add/revise/status/void 四类操作整批提交。
单独调用检查、expected_plan_version 乐观前置、整批校验与幂等全部在
TaskLedger 服务内；本层只做参数转发、单独调用检查与 Command 组装。
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from langchain.tools import ToolRuntime
from langchain_core.messages import AIMessage
from langchain_core.tools import tool
from pydantic import BaseModel, ConfigDict, Field

from src.modules.db_deepagent.context import DbAgentContext
from src.modules.db_deepagent.services.task_ledger import (
    SUB_TASK_ID_PATTERN,
    LedgerOperation,
    TaskLedger,
    TaskLedgerError,
)
from src.modules.db_deepagent.state import DbDeepAgentState
from src.modules.db_deepagent.tools._envelope import (
    error_envelope,
    serialize_tool_output,
    tool_command,
)
from src.modules.db_deepagent.tools.schema_tools import (
    RuntimeToolArgs,
    _attach_validation_handler,
)
from src.utils.logger import get_module_logger

logger = get_module_logger("nl2sql.db_deepagent.tools.task")

TOOL_ERROR_TASK_NOT_ALONE = "task_tool_not_alone"


class _TaskOpBase(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TaskAddOperation(_TaskOpBase):
    """新建任务：服务端指定 revision=1、pending"""

    op: Literal["add"]
    sub_task_id: str = Field(
        pattern=SUB_TASK_ID_PATTERN, description="本运行内稳定逻辑 ID（T1、T2…）"
    )
    content: str = Field(min_length=1, max_length=500, description="业务目标")
    dependencies: list[TaskDependencyItem] = Field(
        default_factory=list, description="依赖的固定任务版本"
    )


class TaskReviseOperation(_TaskOpBase):
    """针对当前 revision 创建下一版：清空成果、重新校验依赖图"""

    op: Literal["revise"]
    sub_task_id: str = Field(pattern=SUB_TASK_ID_PATTERN)
    content: str = Field(min_length=1, max_length=500)
    dependencies: list[TaskDependencyItem] = Field(default_factory=list)


class TaskStatusOperation(_TaskOpBase):
    """状态转移：pending→in_progress；pending/in_progress→终态"""

    op: Literal["status"]
    sub_task_id: str = Field(pattern=SUB_TASK_ID_PATTERN)
    status: Literal["in_progress", "completed", "failed", "needs_clarification"]
    evidence_ids: list[str] = Field(
        default_factory=list,
        description="completed 必填：该任务当前版本产生的成果证据 ID",
    )
    reason_code: (
        Literal["tool_failed", "agent_abandoned", "dependency_blocked"] | None
    ) = None
    reason: str | None = Field(
        default=None, max_length=1000, description="failed/needs_clarification 必填"
    )
    clarification_question: str | None = Field(
        default=None, max_length=1000, description="needs_clarification 必填"
    )
    failure_refs: list[TaskFailureRefItem] = Field(
        default_factory=list, description="reason_code=tool_failed 必填"
    )
    blocked_by: list[TaskDependencyItem] = Field(
        default_factory=list, description="reason_code=dependency_blocked 必填"
    )


class TaskVoidOperation(_TaskOpBase):
    """作废当前版本：计划替换/重复合并/范围调整；执行失败用 failed"""

    op: Literal["void"]
    sub_task_id: str = Field(pattern=SUB_TASK_ID_PATTERN)
    reason: str = Field(min_length=1, max_length=1000)
    replacement: TaskDependencyItem | None = Field(
        default=None, description="计划替换时指向同一 run 的替代任务版本"
    )


class TaskDependencyItem(_TaskOpBase):
    sub_task_id: str = Field(pattern=SUB_TASK_ID_PATTERN)
    revision: int = Field(ge=1)


class FailureErrorRefItem(_TaskOpBase):
    session_id: str
    thread_id: str
    run_id: str
    error_id: str


class TaskFailureRefItem(_TaskOpBase):
    """失败引用：从工具错误包的 failure_ref 原样复制"""

    error_ref: FailureErrorRefItem
    tool_call_id: str
    error_type: str
    source_task_ref: dict[str, Any]


TaskOperation = Annotated[
    TaskAddOperation | TaskReviseOperation | TaskStatusOperation | TaskVoidOperation,
    Field(discriminator="op"),
]


class RecordSubTasksArgs(RuntimeToolArgs):
    expected_plan_version: int = Field(ge=0, description="当前计划版本（首次登记为 0）")
    operations: list[TaskOperation] = Field(
        min_length=1, max_length=50, description="同一批次整批提交的任务操作"
    )


def _to_ledger_operation(item: Any) -> LedgerOperation:
    """模型参数 → 规范化操作。"""
    if isinstance(item, TaskAddOperation) or (
        not isinstance(item, BaseModel) and item.get("op") == "add"
    ):
        data = item.model_dump() if isinstance(item, BaseModel) else item
        return LedgerOperation(
            op="add",
            sub_task_id=data["sub_task_id"],
            content=data["content"],
            dependencies=data.get("dependencies") or [],
        )
    if isinstance(item, TaskReviseOperation) or (
        not isinstance(item, BaseModel) and item.get("op") == "revise"
    ):
        data = item.model_dump() if isinstance(item, BaseModel) else item
        return LedgerOperation(
            op="revise",
            sub_task_id=data["sub_task_id"],
            content=data["content"],
            dependencies=data.get("dependencies") or [],
        )
    if isinstance(item, TaskStatusOperation) or (
        not isinstance(item, BaseModel) and item.get("op") == "status"
    ):
        data = item.model_dump() if isinstance(item, BaseModel) else item
        return LedgerOperation(
            op="status",
            sub_task_id=data["sub_task_id"],
            status=data["status"],
            evidence_ids=data.get("evidence_ids") or [],
            reason_code=data.get("reason_code"),
            reason=data.get("reason"),
            clarification_question=data.get("clarification_question"),
            failure_refs=data.get("failure_refs") or [],
            blocked_by=data.get("blocked_by") or [],
        )
    if isinstance(item, TaskVoidOperation) or (
        not isinstance(item, BaseModel) and item.get("op") == "void"
    ):
        data = item.model_dump() if isinstance(item, BaseModel) else item
        return LedgerOperation(
            op="void",
            sub_task_id=data["sub_task_id"],
            reason=data["reason"],
            replacement=data.get("replacement"),
        )
    raise ValueError(f"未知操作: {item!r}")


def make_record_sub_tasks_tool(services=None):
    """构造 record_sub_tasks 工具（services 可选：提供 store 时启用来源核验）。"""

    @tool(args_schema=RecordSubTasksArgs)
    def record_sub_tasks(
        expected_plan_version: int,
        operations: list[dict[str, Any]],
        runtime: ToolRuntime[DbAgentContext, DbDeepAgentState],
    ):
        """登记或更新本运行的业务任务计划（add/revise/status/void 整批提交）。

        必须单独调用（同一条消息不调用其他工具）；每次调用携带当前
        expected_plan_version，冲突时按返回摘要重新确认后整批重提。
        """
        ctx = runtime.context
        # §5.3 串行约束：一条 AIMessage 只能有一次 record_sub_tasks，且不能
        # 同时调用任何其他工具
        messages = runtime.state.get("messages") or []
        last = messages[-1] if messages else None
        if isinstance(last, AIMessage):
            others = [
                tc
                for tc in (last.tool_calls or [])
                if tc.get("id") != runtime.tool_call_id
            ]
            record_calls = [
                tc
                for tc in (last.tool_calls or [])
                if tc.get("name") == "record_sub_tasks"
            ]
            if others or len(record_calls) > 1:
                return error_envelope(
                    TOOL_ERROR_TASK_NOT_ALONE,
                    "record_sub_tasks 必须单独调用，同一条消息不能调用其他"
                    "工具或重复调用",
                    retryable=True,
                    hint="其他工具的结果会在下一轮可见，之后单独登记",
                )
        try:
            ledger_ops = [_to_ledger_operation(item) for item in operations]
        except (KeyError, ValueError) as exc:
            return error_envelope(
                "invalid_argument", f"操作解析失败: {exc}", retryable=True
            )
        archive = None
        if getattr(services, "store", None) is not None:
            from src.modules.db_deepagent.persistence.archive import QaArchive

            archive = QaArchive(services.store)  # R1-02：来源资格核验
        ledger = TaskLedger(runtime.state, archive=archive)
        try:
            result = ledger.apply(
                run_id=ctx.run_id,
                session_id=ctx.session_id,
                thread_id=ctx.thread_id,
                request_id=ctx.request_id,
                expected_plan_version=expected_plan_version,
                operations=ledger_ops,
                now=datetime.now(),
                tool_call_id=runtime.tool_call_id or "",
            )
        except TaskLedgerError as exc:
            data: dict[str, Any] = {"task_refs": exc.task_refs}
            if exc.current_plan_version is not None:
                data["current_plan_version"] = exc.current_plan_version
            if exc.tasks_summary:
                data["tasks"] = exc.tasks_summary
            logger.info(
                "[request_id=%s run_id=%s] record_sub_tasks 拒绝: %s %s",
                ctx.request_id,
                ctx.run_id,
                exc.error_type,
                exc.message,
            )
            return error_envelope(
                exc.error_type,
                exc.message,
                retryable=exc.error_type not in ("task_plan_conflict",),
                hint=exc.hint,
                data=data,
            )
        updates: dict[str, Any] = {}
        if result.task_records:
            updates["db_sub_tasks"] = result.task_records
        if result.plan_record is not None:
            updates["db_task_plans"] = {ctx.run_id: result.plan_record}
        payload = serialize_tool_output(
            {
                "status": "ok",
                "data": {
                    "plan_version": result.plan_version,
                    "no_op": result.no_op,
                    "operations": result.op_receipts,
                },
                "warnings": [],
            }
        )
        logger.info(
            "[request_id=%s run_id=%s] record_sub_tasks: plan_version=%s no_op=%s",
            ctx.request_id,
            ctx.run_id,
            result.plan_version,
            result.no_op,
        )
        return tool_command(
            payload, runtime.tool_call_id or "", updates=updates or None
        )

    _attach_validation_handler(record_sub_tasks)
    return record_sub_tasks


__all__ = ["make_record_sub_tasks_tool"]
