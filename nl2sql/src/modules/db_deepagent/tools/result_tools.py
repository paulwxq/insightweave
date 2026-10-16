"""结果工具：analyze_result、read_result（docs/v4/5 §7.9、§7.10、§7.10.1）

分析服务完成三步路由与计算；工具层负责参数转发、结果落盘、
AnalysisRecord 登记与 Command 组装（§7.10：同一调用内全部账本更新
在一个 Command 中返回）。
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any, Optional

from langchain.tools import ToolRuntime
from langchain_core.tools import tool
from pydantic import Field

from src.modules.db_deepagent.context import DbAgentContext
from src.modules.db_deepagent.errors import ResultStoreError
from src.modules.db_deepagent.schemas import AnalysisRecord
from src.modules.db_deepagent.services import DbAgentServices
from src.modules.db_deepagent.services.result_analysis import AnalysisError
from src.modules.db_deepagent.services.sql_pipeline import LedgerView, LedgerUpdate
from src.modules.db_deepagent.state import DbDeepAgentState
from src.modules.db_deepagent.tools._envelope import (
    error_envelope,
    finalize_output,
    tool_command,
)
from src.modules.db_deepagent.tools.schema_tools import (
    RuntimeToolArgs,
    _attach_validation_handler,
    _merge_warnings,
)
from src.utils.logger import get_module_logger

logger = get_module_logger("nl2sql.db_deepagent.tools.result")


def make_read_result_tool(services: DbAgentServices):
    """构造 read_result 工具（§7.9）。"""

    class ReadResultArgs(RuntimeToolArgs):
        result_ref: str = Field(description="结果引用 res_<32位十六进制>")
        columns: Optional[list[str]] = Field(default=None, description="可选列名子集")
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
        """分页读取已保存的查询／分析结果。

        source_truncated=true 表示数据库结果超过保存上限（任何分页原样保留），
        未保存部分无法读取，不能把已保存行当作总体。
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
        content, warnings = finalize_output(
            {"status": "ok", "data": data, "warnings": []},
            services.config.results.max_tool_output_chars,
        )
        content = _merge_warnings(content, warnings)
        return tool_command(content, runtime.tool_call_id or "")

    _attach_validation_handler(read_result)
    return read_result


def _json_safe(value: Any) -> Any:
    from src.modules.db_deepagent.services.result_store import _encode_value

    return _encode_value(value)


def make_analyze_result_tool(services: DbAgentServices):
    """构造 analyze_result 工具（§7.10）。"""

    class AnalyzeResultArgs(RuntimeToolArgs):
        operation: str = Field(
            description=("aggregate｜top_n｜share｜compare｜distinct_values｜describe")
        )
        inputs: list[str] = Field(
            min_length=1, max_length=2, description="输入 result_ref（1–2 个）"
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

    @tool(args_schema=AnalyzeResultArgs)
    def analyze_result(
        operation: str,
        inputs: list[str],
        params: dict[str, Any],
        allow_pushdown: bool,
        runtime: ToolRuntime[DbAgentContext, DbDeepAgentState],
    ):
        """对已保存结果做本地计算或 SQL 下推（聚合／TopN／占比／比较／去重／描述）。

        证据与引用：返回的 evidence_id 可在 submit_final_answer 中引用；
        下推结果带"非原结果快照"警告。
        """
        ctx = runtime.context
        ledger = LedgerView(runtime.state)

        def load_bundle(ref: str):
            from src.modules.db_deepagent.services.result_analysis import InputBundle

            meta = services.results.load_meta(ref, session_id=ctx.session_id)
            rows = services.results.read_rows(
                ref, session_id=ctx.session_id, limit=meta.saved_row_count or 1
            ).rows
            return InputBundle(meta=meta, rows=rows)

        def run_pushdown(sql: str, source_metas, row_cap=None):
            # validate_and_execute 返回 ((val_outcome, exec_outcome), update)；
            # 服务层只需要 exec_outcome（校验失败时 exec_outcome.ok=False）
            (_val, exec), update = services.pipeline.validate_and_execute(
                sql,
                ctx,
                ledger,
                purpose="pushdown",
                source_metas=source_metas,
                tool_call_id=runtime.tool_call_id or "",
                row_cap=row_cap,
            )
            return exec, update

        try:
            outcome = services.analysis.analyze(
                operation=operation,
                params=params,
                input_refs=inputs,
                ctx=ctx,
                allow_pushdown=allow_pushdown,
                load_bundle=load_bundle,
                run_pushdown=run_pushdown,
            )
        except AnalysisError as exc:
            return error_envelope(
                exc.error_type, exc.message, retryable=True, hint=exc.hint
            )
        except ResultStoreError as exc:
            # 输入引用失效（不存在／损坏／他session）与 read_result 同口径：
            # 结构化错误包让 Agent 换引用或重新查询，而非终止整轮运行
            return error_envelope(exc.error_type, exc.message, retryable=False)
        except Exception as exc:  # 参数模型校验失败等
            from pydantic import ValidationError

            if isinstance(exc, ValidationError):
                return error_envelope(
                    "invalid_argument", f"params 不合法: {exc}", retryable=True
                )
            raise
        if not outcome.ok:
            return error_envelope(
                outcome.error_type or "internal_error",
                outcome.message or "分析失败",
                retryable=True,
                hint=outcome.hint,
            )

        updates: dict[str, Any] = {}
        merged = LedgerUpdate()
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
                derived_from=inputs,
                guard_evidence={"time": [], "dim": []},
                datasource_id=outcome.datasource_id,
            )
            output_ref = saved.result_ref
            saved_count = saved.saved_row_count
            source_truncated = outcome.source_truncated
        record = AnalysisRecord(
            evidence_id="A-"
            + hashlib.sha256((runtime.tool_call_id or "").encode()).hexdigest()[:8],
            tool_call_id=runtime.tool_call_id or "",
            run_id=ctx.run_id,
            request_id=ctx.request_id,
            pushdown_execution_id=(
                outcome.pushdown_execution.get("evidence_id")
                if outcome.pushdown_execution
                else None
            ),
            operation={"name": operation, **params},
            input_result_refs=list(inputs),
            output_result_ref=output_ref,
            executed_sql=outcome.executed_sql,
            saved_row_count=saved_count,
            source_truncated=source_truncated,
            warnings=list(outcome.warnings),
            result_generated_at=datetime.now()
            .astimezone()
            .isoformat(timespec="seconds"),
        )
        preview_rows = outcome.rows[: services.config.results.preview_rows]
        data: dict[str, Any] = {
            "evidence_id": record["evidence_id"],
            "result_ref": output_ref,
            "columns": outcome.columns or [],
            "row_count": saved_count,
            "preview_rows": [[_json_safe(v) for v in row] for row in preview_rows],
            "preview_truncated": saved_count > len(preview_rows),
            "source_truncated": source_truncated,
        }
        if outcome.executed_sql:
            data["executed_sql"] = outcome.executed_sql[:3000]
        content, finalize_warnings = finalize_output(
            {"status": "ok", "data": data, "warnings": list(outcome.warnings)},
            services.config.results.max_tool_output_chars,
        )
        content = _merge_warnings(content, finalize_warnings)
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
