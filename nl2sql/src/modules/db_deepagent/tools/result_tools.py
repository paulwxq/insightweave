"""结果工具：analyze_result、read_result（docs/v4/5 §7.9、§7.10、§7.10.1）

分析服务完成三步路由与计算；工具层负责参数转发、结果落盘、
AnalysisRecord 登记与 Command 组装（§7.10：同一调用内全部账本更新
在一个 Command 中返回）。

doc 6.3 §6.1/§6.3：analyze_result 必填目标任务版本；inputs 升级为
[{result_ref, evidence_id}]（模型选择工具实际返回的记录，服务端核对
两者匹配后填入完整来源任务身份）；同任务输入须属当前 revision，跨任务
输入须为已声明依赖的固定版本，probe 输入拒绝；分析/下推失败写错误账本
并返回 failure_ref。read_result 保持只读（不改变任务终态）。
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any

from langchain.tools import ToolRuntime
from langchain_core.tools import tool
from pydantic import Field

from src.modules.db_deepagent.context import DbAgentContext
from src.modules.db_deepagent.errors import ResultStoreError
from src.modules.db_deepagent.schemas import AnalysisRecord
from src.modules.db_deepagent.services import DbAgentServices
from src.modules.db_deepagent.services.result_analysis import AnalysisError
from src.modules.db_deepagent.services.sql_pipeline import (
    LedgerUpdate,
    LedgerView,
    make_ledger_view,
)
from src.modules.db_deepagent.state import DbDeepAgentState
from src.modules.db_deepagent.tools._envelope import (
    error_envelope,
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

logger = get_module_logger("nl2sql.db_deepagent.tools.result")


def make_read_result_tool(services: DbAgentServices):
    """构造 read_result 工具（§7.9）。"""

    class ReadResultArgs(RuntimeToolArgs):
        result_ref: str = Field(description="结果引用 res_<32位十六进制>")
        columns: list[str] | None = Field(default=None, description="可选列名子集")
        offset: int = Field(default=0, ge=0, description="起始行号，≥0")
        limit: int = Field(default=50, ge=1, description="每页行数（1–read_max_rows）")

    @tool(args_schema=ReadResultArgs)
    def read_result(
        result_ref: str,
        columns: list[str] | None,
        offset: int,
        limit: int,
        runtime: ToolRuntime[DbAgentContext, DbDeepAgentState],
    ):
        """分页读取已保存的查询／分析结果（只读，不改变任务终态）。

        source_truncated=true 表示数据库结果超过保存上限（任何分页原样保留），
        未保存部分无法读取，不能把已保存行当作总体。诊断用失败任务结果
        仍可读取，但读取不授予 finding 引用资格。
        """
        ctx = runtime.context
        limit = min(limit, services.config.results.read_max_rows)
        try:
            page = services.results.read_rows(
                result_ref,
                session_id=ctx.session_id,
                columns=columns,
                offset=offset,
                limit=limit,
            )
        except Exception as exc:  # 结果读取错误统一转错误包
            from src.modules.db_deepagent.errors import ResultStoreError

            if isinstance(exc, ResultStoreError):
                return error_envelope(exc.error_type, exc.message, retryable=False)
            raise
        data = {
            "rows": [[_json_safe(v) for v in row] for row in page.rows],
            "columns": page.columns,
            "offset": page.offset,
            "next_offset": page.next_offset,
            "saved_row_count": page.saved_row_count,
            "preview_truncated": page.next_offset is not None,
            "source_truncated": page.source_truncated,
        }
        content = serialize_tool_output({"status": "ok", "data": data, "warnings": []})
        return tool_command(content, runtime.tool_call_id or "")

    _attach_validation_handler(read_result)
    return read_result


def _json_safe(value: Any) -> Any:
    from src.modules.db_deepagent.services.result_store import _encode_value

    return _encode_value(value)


def _resolve_analysis_inputs(
    *,
    services: DbAgentServices,
    ctx: DbAgentContext,
    ledger: LedgerView,
    inputs: list[dict[str, Any]],
    sub_task_id: str,
    sub_task_revision: int,
) -> tuple[
    list[str] | None,
    str | None,
    str | None,
    str | None,
    dict | None,
    list[dict[str, Any]] | None,
]:
    """校验分析输入并构造结构化血缘（doc 6.3 §6.3）。

    返回 (refs, error_type, message, hint, task_record, lineage)：
    - refs 为输入 result_ref 字符串列表；lineage 为已核验的结构化血缘
      （成功时两者非空对应）。
    - 每项输入解析为本轮记录：result_ref 与 evidence_id 必须匹配同一条
      执行/分析记录（服务端核对，不信模型自报）。
    - probe 输入拒绝；同任务输入必须属当前 revision；跨任务输入必须为
      已声明依赖的固定 revision；血缘递归校验（去重 + 环检测）。
    """
    # R1-06：统一完整准入（含声明依赖全部 completed），与 SQL 路径同一
    # 规则——不只核对实际输入对应的依赖。
    from src.modules.db_deepagent.services.sql_pipeline import (
        require_task_ready_for_work,
    )

    task, terr, tmsg, thint = require_task_ready_for_work(
        ledger, ctx, sub_task_id, sub_task_revision
    )
    if terr is not None:
        return None, terr, tmsg, thint, None, None
    tasks = ledger.current_tasks(ctx.run_id)
    evidence_index = ledger.evidence_by_id(ctx.run_id)
    declared = {
        (d.get("sub_task_id"), d.get("revision"))
        for d in task.get("dependencies") or []
    }
    lineage: list[dict[str, Any]] = []
    refs: list[str] = []
    seen_refs: set[str] = set()
    for item in inputs:
        ref = item.get("result_ref")
        eid = item.get("evidence_id")
        record = evidence_index.get(eid)
        if record is None:
            return (
                None,
                "stale_task_inputs",
                f"输入 evidence_id={eid} 不在本轮可引用证据中",
                "只使用 execute_sql/analyze_result/reuse_evidence 返回的记录",
                task,
                None,
            )
        record_ref = record.get("result_ref") or record.get("output_result_ref")
        if record_ref != ref:
            return (
                None,
                "stale_task_inputs",
                f"输入 {ref} 与证据 {eid} 的 result_ref 不匹配"
                f"（证据指向 {record_ref}）",
                None,
                task,
                None,
            )
        if record.get("purpose") == "probe":
            return (
                None,
                "stale_task_inputs",
                f"输入 {eid} 是探针执行，不能作为分析输入（不只限制 compare）",
                None,
                task,
                None,
            )
        if ref in seen_refs:
            continue
        seen_refs.add(ref)
        refs.append(ref)
        source_task_id = record.get("sub_task_id")
        source_rev = record.get("sub_task_revision")
        if source_task_id is None:
            return (
                None,
                "stale_task_inputs",
                f"输入 {eid} 缺少任务归属（历史/过渡记录），不能计入新门控成果",
                None,
                task,
                None,
            )
        if source_task_id == sub_task_id:
            if source_rev != sub_task_revision:
                return (
                    None,
                    "stale_task_inputs",
                    f"同任务输入 {eid} 属旧 revision={source_rev}，"
                    "不能把上次 revision 的执行重新解释为本次成果",
                    None,
                    task,
                    None,
                )
        else:
            if (source_task_id, source_rev) not in declared:
                return (
                    None,
                    "stale_task_inputs",
                    f"跨任务输入 {eid}（{source_task_id}@{source_rev}）"
                    "未在本任务声明依赖中（固定版本）",
                    "先在 record_sub_tasks 的 dependencies 中声明",
                    task,
                    None,
                )
            dep_task = tasks.get(source_task_id)
            if (
                dep_task is None
                or dep_task.get("status") != "completed"
                or dep_task.get("revision") != source_rev
            ):
                return (
                    None,
                    "stale_task_inputs",
                    f"依赖任务 {source_task_id}@{source_rev} 未 completed，"
                    "不能消费其成果",
                    None,
                    task,
                    None,
                )
            # R2-02：跨任务消费必须来自上游**已选定成果**；未选入
            # evidence_ids 的探索记录不能被下游任务消费（同任务探索输入
            # 按方案保留，不在此限制）。
            if eid not in (dep_task.get("evidence_ids") or []):
                return (
                    None,
                    "stale_task_inputs",
                    f"跨任务输入 {eid} 未选入依赖任务 "
                    f"{source_task_id}@{source_rev} 的成果列表，不能消费",
                    "只消费上游 completed 任务已选定的成果证据",
                    task,
                    None,
                )
        lineage.append(
            {
                "result_ref": ref,
                "evidence_ref": {
                    "session_id": ctx.session_id,
                    "thread_id": ctx.thread_id,
                    "run_id": ctx.run_id,
                    "evidence_id": eid,
                },
                "source_task_ref": {
                    "session_id": ctx.session_id,
                    "thread_id": ctx.thread_id,
                    "run_id": ctx.run_id,
                    "sub_task_id": source_task_id,
                    "revision": source_rev or 0,
                },
            }
        )
    # R3-01：分析入口复用完整身份的共享证据图核验，收敛此前按裸
    # evidence_id 在本轮字典里递归的另一套实现。沿用记录的继承输入属于
    # 原证据 consumer scope，只有共享解析才能正确跨 run/快照解析祖先。
    from src.modules.db_deepagent.services.reuse_qualification import (
        SourceUnqualified,
        evidence_edges,
        verify_consumed_edges,
    )

    try:
        edges = evidence_edges(
            {"input_result_refs": lineage}, session_id=ctx.session_id
        )
    except SourceUnqualified as exc:
        return (
            None,
            "stale_task_inputs",
            f"输入血缘结构非法：{exc.reason}",
            None,
            task,
            None,
        )
    ok, reason = verify_consumed_edges(
        edges,
        session_id=ctx.session_id,
        state=ledger.state,
        archive=ledger.archive,
        carried=ledger.carried,
        consumer_task=task,
        consumer_run=ctx.run_id,
        consumer_thread=ctx.thread_id,
        consumer_task_id=sub_task_id,
        consumer_task_rev=sub_task_revision,
    )
    if not ok:
        return (
            None,
            "stale_task_inputs",
            f"输入血缘不可核验：{reason}",
            None,
            task,
            None,
        )
    return refs, None, None, None, task, lineage


def make_analyze_result_tool(services: DbAgentServices):
    """构造 analyze_result 工具（§7.10；doc 6.3 §6.1/§6.3）。"""

    class AnalyzeResultArgs(RuntimeToolArgs):
        operation: str = Field(
            description=("aggregate｜top_n｜share｜compare｜distinct_values｜describe")
        )
        inputs: list[dict[str, Any]] = Field(
            min_length=1,
            max_length=2,
            description=(
                "结构化输入 [{result_ref, evidence_id}]（1–2 个）；"
                "evidence_id 取自产生该结果的工具返回"
            ),
        )
        params: dict[str, Any] = Field(
            description=(
                "操作参数：aggregate{group_by[],metrics[{column,func}]}；"
                "top_n{order_by,descending,n,ties}；share{value_column,group_by?,round_digits?}；"
                "compare{a:{ref,column,row_index?},b:{...},round_digits?}；"
                "distinct_values{column,max_values}；describe{columns[]}"
            )
        )
        allow_pushdown: bool = Field(
            default=True,
            description=(
                "截断输入或本地不支持的类型时是否按源 SQL 下推重查；"
                "需要严格原时点结论时置 false（源截断将返回 incomplete_source）"
            ),
        )
        sub_task_id: str = Field(min_length=1, max_length=8, description="目标任务 ID")
        sub_task_revision: int = Field(ge=1, description="目标任务版本")

    @tool(args_schema=AnalyzeResultArgs)
    def analyze_result(
        operation: str,
        inputs: list[dict[str, Any]],
        params: dict[str, Any],
        allow_pushdown: bool,
        sub_task_id: str,
        sub_task_revision: int,
        runtime: ToolRuntime[DbAgentContext, DbDeepAgentState],
    ):
        """对已保存结果做本地计算或 SQL 下推（聚合／TopN／占比／比较／去重／描述）。

        证据与引用：返回的 evidence_id 可在 submit_final_answer 中引用；
        下推结果带"非原结果快照"警告。目标任务的声明依赖全部 completed
        后才可分析；跨任务输入必须已声明为依赖。
        """
        ctx = runtime.context
        ledger = make_ledger_view(services, runtime.state, ctx=ctx)
        tool_call_id = runtime.tool_call_id or ""
        merged = LedgerUpdate()
        err_updates: dict[str, Any] = {}

        def _fail(
            error_type: str,
            message: str,
            retryable: bool = True,
            hint: str | None = None,
            record_failure: bool = True,
        ):
            if record_failure:
                record = make_task_error_record(
                    ctx=ctx,
                    tool="analyze_result",
                    error_type=error_type,
                    message=message,
                    retryable=retryable,
                    tool_call_id=tool_call_id,
                    source_task_ref=(
                        source_task_ref_of(ctx, sub_task_id, sub_task_revision)
                        if error_type
                        not in ("task_not_registered", "task_revision_mismatch")
                        else None
                    ),
                )
                err_updates["db_tool_errors"] = [record]
                error_data: dict[str, Any] = {"failure_ref": failure_ref_of(record)}
            else:
                error_data = {}
            from src.modules.db_deepagent.tools._envelope import (
                error_envelope as _env,
            )

            content = _env(error_type, message, retryable, hint=hint, data=error_data)
            updates = {**err_updates, **(merged.as_state_update() or {})}
            return tool_command(content, tool_call_id, updates=updates or None)

        # R2-09：分析工具路径在读取/写入前校验同一会话有效运行 guard，
        # 与内部服务共用 require_session_lease；无凭据不伪造任务归属。
        from src.modules.db_deepagent.services.sql_pipeline import (
            require_session_lease,
        )

        lease_err, lease_msg = require_session_lease(ctx)
        if lease_err is not None:
            return _fail(
                lease_err,
                lease_msg or "缺少有效运行持锁凭据",
                False,
                record_failure=False,
            )

        refs, terr, tmsg, thint, _task, lineage = _resolve_analysis_inputs(
            services=services,
            ctx=ctx,
            ledger=ledger,
            inputs=inputs,
            sub_task_id=sub_task_id,
            sub_task_revision=sub_task_revision,
        )
        if terr is not None:
            return _fail(terr, tmsg or "分析输入校验失败", True, thint)

        def load_bundle(ref: str):
            from src.modules.db_deepagent.services.result_analysis import InputBundle

            meta = services.results.load_meta(ref, session_id=ctx.session_id)
            rows = services.results.read_rows(
                ref, session_id=ctx.session_id, limit=meta.saved_row_count or 1
            ).rows
            return InputBundle(meta=meta, rows=rows)

        def run_pushdown(sql: str, source_metas, row_cap=None):
            # validate_and_execute 返回 ((val_outcome, exec_outcome), update)；
            # 下推继承分析任务（doc 6.3 §6.1）
            (_val, exec), update = services.pipeline.validate_and_execute(
                sql,
                ctx,
                ledger,
                purpose="pushdown",
                source_metas=source_metas,
                tool_call_id=tool_call_id,
                row_cap=row_cap,
                sub_task_id=sub_task_id,
                sub_task_revision=sub_task_revision,
            )
            merged.merge(update)
            return exec, update

        try:
            outcome = services.analysis.analyze(
                operation=operation,
                params=params,
                input_refs=refs,
                ctx=ctx,
                allow_pushdown=allow_pushdown,
                load_bundle=load_bundle,
                run_pushdown=run_pushdown,
            )
        except AnalysisError as exc:
            return _fail(exc.error_type, exc.message, True, exc.hint)
        except ResultStoreError as exc:
            # 输入引用失效（不存在／损坏／他session）与 read_result 同口径
            return _fail(exc.error_type, exc.message, False)
        except Exception as exc:  # 参数模型校验失败等
            from pydantic import ValidationError

            if isinstance(exc, ValidationError):
                return _fail("invalid_argument", f"params 不合法: {exc}", True)
            raise
        if not outcome.ok:
            return _fail(
                outcome.error_type or "internal_error",
                outcome.message or "分析失败",
                True,
                outcome.hint,
            )

        updates: dict[str, Any] = {}
        if outcome.pushdown_updates is not None:
            merged.merge(outcome.pushdown_updates)
        # 输出结果：下推复用执行结果；本地计算另存
        if outcome.pushdown_result_ref is not None:
            output_ref = outcome.pushdown_result_ref
            saved_count = (
                outcome.pushdown_execution.get("saved_row_count", 0)
                if outcome.pushdown_execution
                else 0
            )
            source_truncated = (
                bool(outcome.pushdown_execution.get("source_truncated", False))
                if outcome.pushdown_execution
                else False
            )
        else:
            # 本地计算：截断状态来自分析结果（distinct_values 超 max_values）；
            # 数据源沿用输入结果的数据源（跨数据源输入不得改写为当前值）
            saved = services.results.save(
                ctx=ctx,
                purpose="analysis",
                sql_template=None,
                executed_sql=None,
                columns=outcome.columns,
                rows=outcome.rows,
                source_truncated=outcome.source_truncated,
                derived_from=refs,
                guard_evidence={"time": [], "dim": []},
                datasource_id=outcome.datasource_id,
                source_task=source_task_ref_of(ctx, sub_task_id, sub_task_revision),
            )
            output_ref = saved.result_ref
            saved_count = saved.saved_row_count
            source_truncated = outcome.source_truncated
        record = AnalysisRecord(
            evidence_id="A-" + hashlib.sha256(tool_call_id.encode()).hexdigest()[:8],
            tool_call_id=tool_call_id,
            run_id=ctx.run_id,
            request_id=ctx.request_id,
            # R2-10：记录自身完整可信 scope（轮换携带沿用记录自身身份）
            session_id=ctx.session_id,
            thread_id=ctx.thread_id,
            pushdown_execution_id=(
                outcome.pushdown_execution.get("evidence_id")
                if outcome.pushdown_execution
                else None
            ),
            operation={"name": operation, **params},
            input_result_refs=lineage,
            output_result_ref=output_ref,
            executed_sql=outcome.executed_sql,
            saved_row_count=saved_count,
            source_truncated=source_truncated,
            warnings=list(outcome.warnings),
            result_generated_at=datetime.now()
            .astimezone()
            .isoformat(timespec="seconds"),
            sub_task_id=sub_task_id,
            sub_task_revision=sub_task_revision,
        )
        preview_rows = outcome.rows[: services.config.results.preview_rows]
        data: dict[str, Any] = {
            "evidence_id": record["evidence_id"],
            # R1-07：新证据完整引用
            "source_evidence_ref": {
                "session_id": ctx.session_id,
                "thread_id": ctx.thread_id,
                "run_id": ctx.run_id,
                "evidence_id": record["evidence_id"],
            },
            "result_ref": output_ref,
            "columns": outcome.columns or [],
            "row_count": saved_count,
            "preview_rows": [[_json_safe(v) for v in row] for row in preview_rows],
            "preview_truncated": saved_count > len(preview_rows),
            "source_truncated": source_truncated,
        }
        if outcome.executed_sql:
            data["executed_sql"] = outcome.executed_sql[:3000]
        content = serialize_tool_output(
            {"status": "ok", "data": data, "warnings": list(outcome.warnings)}
        )
        logger.info(
            "[request_id=%s run_id=%s] analyze_result: %s rows=%d pushdown=%s",
            ctx.request_id,
            ctx.run_id,
            operation,
            saved_count,
            outcome.executed_sql is not None,
        )
        state_update = merged.as_state_update()
        updates.update(state_update or {})
        updates["db_analyses"] = [record]  # 去重 reducer 按 evidence_id 追加
        return tool_command(
            content, runtime.tool_call_id or "", updates=updates or None
        )

    _attach_validation_handler(analyze_result)
    return analyze_result


__all__ = ["make_analyze_result_tool", "make_read_result_tool"]
