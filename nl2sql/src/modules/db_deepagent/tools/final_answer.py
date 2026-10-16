"""submit_final_answer：最终答案提交工具（docs/v4/5 §7.11、§14.1）

检查顺序：单独调用 → 未重复提交 → 证据引用（本运行 db_executions
purpose="query" 或 db_analyses，含沿用登记）→ 状态与证据一致。全部通过才
登记 FinalAnswerRecord；结束运行由 FinalAnswerGateMiddleware 判断（K19）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from langchain.tools import ToolRuntime
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import InjectedToolArg, tool
from langgraph.types import Command
from pydantic import Field

from src.modules.db_deepagent.context import DbAgentContext
from src.modules.db_deepagent.errors import (
    TOOL_ERROR_FINAL_ANSWER_ALREADY_SUBMITTED,
    TOOL_ERROR_FINAL_ANSWER_NOT_ALONE,
    TOOL_ERROR_INVALID_EVIDENCE_REFERENCE,
)
from src.modules.db_deepagent.schemas import DbAgentFinalAnswer, FinalAnswerRecord
from src.modules.db_deepagent.state import DbDeepAgentState
from src.modules.db_deepagent.tools._envelope import error_envelope
from src.modules.db_deepagent.tools.schema_tools import (
    _attach_validation_handler,
)
from src.utils.logger import get_module_logger

logger = get_module_logger("nl2sql.db_deepagent.tools.final_answer")


def make_submit_final_answer_tool():
    """构造 submit_final_answer 工具（无服务依赖，只读写图状态）。"""

    class SubmitFinalAnswerArgs(DbAgentFinalAnswer):
        runtime: Annotated[Any, InjectedToolArg] = Field(exclude=True)

    @tool(args_schema=SubmitFinalAnswerArgs)
    def submit_final_answer(
        status: str,
        answer: str,
        findings: list[dict[str, Any]],
        metric_definitions: list[str],
        limitations: list[str],
        open_questions: list[str],
        runtime: ToolRuntime[DbAgentContext, DbDeepAgentState],
    ):
        """提交本运行的最终答案；必须单独调用（同一条消息不调用其他工具）。

        每条 finding 引用 execute_sql／analyze_result／reuse_evidence 返回过的
        evidence_id；探针执行记录不能引用。提交后运行由系统结束。
        """
        ctx = runtime.context
        run_id = ctx.run_id
        submitted = DbAgentFinalAnswer(
            status=status,  # type: ignore[arg-type]
            answer=answer,
            findings=findings,  # type: ignore[arg-type]
            metric_definitions=metric_definitions,
            limitations=limitations,
            open_questions=open_questions,
        )
        # 1. 单独调用：最后一条 AIMessage（发出本调用的消息）不得还有别的工具（K20）
        messages = runtime.state.get("messages") or []
        last = messages[-1] if messages else None
        if isinstance(last, AIMessage):
            others = [
                tc
                for tc in (last.tool_calls or [])
                if tc.get("id") != runtime.tool_call_id
            ]
            if others:
                return error_envelope(
                    TOOL_ERROR_FINAL_ANSWER_NOT_ALONE,
                    "submit_final_answer 必须单独调用，同一条消息不能调用其他工具",
                    retryable=True,
                    hint="其他工具的结果会在下一轮可见，之后单独提交",
                )
        # 2. 未重复提交
        if run_id in (runtime.state.get("db_final_answers") or {}):
            return error_envelope(
                TOOL_ERROR_FINAL_ANSWER_ALREADY_SUBMITTED,
                "本运行已提交过最终答案",
                retryable=False,
            )
        # 3. 证据引用：本运行 query 执行与分析记录（含沿用登记）
        valid_ids = {
            r["evidence_id"]
            for r in (runtime.state.get("db_executions") or [])
            if r.get("run_id") == run_id and r.get("purpose") == "query"
        }
        valid_ids |= {
            r["evidence_id"]
            for r in (runtime.state.get("db_analyses") or [])
            if r.get("run_id") == run_id
        }
        missing = [
            eid
            for finding in submitted.findings
            for eid in finding.evidence_ids
            if eid not in valid_ids
        ]
        if missing:
            return error_envelope(
                TOOL_ERROR_INVALID_EVIDENCE_REFERENCE,
                "以下 evidence_id 不在本运行证据中: " + ", ".join(sorted(set(missing))),
                retryable=True,
                hint="只引用 execute_sql／analyze_result 返回过、"
                "或经 reuse_evidence 登记的 evidence_id；探针执行记录不能引用",
            )
        # 4. 状态与证据一致
        if submitted.status == "completed" and not submitted.findings:
            return error_envelope(
                "invalid_argument",
                "status=completed 时 findings 不能为空",
                retryable=True,
            )
        record = FinalAnswerRecord(
            run_id=run_id,
            request_id=ctx.request_id,
            tool_call_id=runtime.tool_call_id or "",
            answer=submitted.model_dump(),
            submitted_at=datetime.now().astimezone().isoformat(timespec="seconds"),
        )
        logger.info(
            "[request_id=%s run_id=%s] 最终答案已接受: status=%s findings=%d",
            ctx.request_id,
            run_id,
            submitted.status,
            len(submitted.findings),
        )
        return Command(
            update={
                "db_final_answers": {run_id: record},
                "messages": [
                    ToolMessage(
                        content="已接收最终答案",
                        tool_call_id=runtime.tool_call_id or "",
                    )
                ],
            }
        )

    _attach_validation_handler(submit_final_answer)
    return submit_final_answer
