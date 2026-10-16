"""检索工具：search_schema、inspect_tables、plan_joins（docs/v4/5 §7.2–§7.4）

阈值固定取自服务端配置（§7.2），工具参数不含 min_score；args_schema 继承
StrictToolArgs / RuntimeToolArgs（extra="forbid"），模型传入未声明字段时由框架
返回 invalid_argument 错误 ToolMessage，业务函数不执行。
"""

from __future__ import annotations

import hashlib
import json
from typing import Annotated, Any, Literal, Optional

from langchain.tools import ToolRuntime
from langchain_core.tools import InjectedToolArg, tool
from pydantic import BaseModel, ConfigDict, Field

from src.modules.db_deepagent.context import DbAgentContext
from src.modules.db_deepagent.errors import (
    TOOL_ERROR_GRAPH_BACKEND,
    TOOL_ERROR_INVALID_ARGUMENT,
    TOOL_ERROR_INVALID_TABLE_ID,
    TOOL_ERROR_RETRIEVAL_BACKEND,
    InvalidTableIdError,
    RetrievalBackendError,
)
from src.modules.db_deepagent.schemas import DimEvidence
from src.modules.db_deepagent.services import DbAgentServices
from src.modules.db_deepagent.state import DbDeepAgentState
from src.modules.db_deepagent.tools._envelope import (
    error_envelope,
    finalize_output,
    format_tool_validation_error,
    tool_command,
)
from src.tools.schema_retrieval.table_names import to_display_name
from src.utils.logger import get_module_logger

logger = get_module_logger("nl2sql.db_deepagent.tools.schema")

_SQL_TEXT_MAX_CHARS = 1500  # §7.2：SQL 案例截断到 1500 字符

# kinds 值 → retrieval 配置字段前缀（tables→table、dim_values→dim_value…）
_KIND_TO_FIELD = {
    "tables": "table",
    "columns": "column",
    "dim_values": "dim_value",
    "sql_examples": "sql_example",
}


class StrictToolArgs(BaseModel):
    """统一严格参数基类（§7.1）"""

    model_config = ConfigDict(extra="forbid")


class RuntimeToolArgs(StrictToolArgs):
    """需要 runtime 注入的参数基类；runtime 是 ToolNode 注入的隐藏字段"""

    runtime: Annotated[Any, InjectedToolArg] = Field(exclude=True)


def _validation_error(exc) -> str:
    """handle_validation_error 回调（§7.1）：只含字段路径与错误类型，不回显输入值。"""
    return format_tool_validation_error(exc)


def _merge_warnings(content: str, extra: list[str], *, dedupe: bool = False) -> str:
    """把 finalize_output 产生的截短 warning 并回返回包。

    整体截断（hard truncate）时文本已不是合法 JSON，无法并回——此时文本已达
    上限，直接原样返回（截断 warning 已在 finalize_output 内部计入 extra，
    模型仍可从整体截断的表现感知）。
    """
    if not extra:
        return content
    try:
        payload = json.loads(content)
    except json.JSONDecodeError:
        return content
    merged = payload["warnings"] + extra
    payload["warnings"] = sorted(set(merged)) if dedupe else merged
    return json.dumps(payload, ensure_ascii=False)


def _attach_validation_handler(t) -> None:
    # BaseTool 未开 validate_assignment，构造后直接赋值即可生效
    t.handle_validation_error = _validation_error


# ============================================================================
# search_schema（§7.2）
# ============================================================================


def make_search_schema_tool(services: DbAgentServices):
    """构造 search_schema 工具（闭包注入服务；top_k 上限来自配置）。"""
    cfg = services.config
    max_top_k = cfg.retrieval.max_top_k

    class SearchSchemaArgs(RuntimeToolArgs):
        query: str = Field(min_length=1, max_length=500, description="检索意图文本")
        kinds: list[Literal["tables", "columns", "dim_values", "sql_examples"]] = Field(
            default=["tables", "columns"], description="检索类型"
        )
        value_texts: Optional[list[str]] = Field(
            default=None,
            max_length=10,
            description="dim_values 的待匹配原文；kinds 含 dim_values 时必填",
        )
        top_k: Optional[int] = Field(
            default=None, ge=1, le=max_top_k, description="覆盖各类型默认 top_k"
        )

    @tool(args_schema=SearchSchemaArgs)
    def search_schema(
        query: str,
        kinds: list[str],
        value_texts: list[str] | None,
        top_k: int | None,
        runtime: ToolRuntime[DbAgentContext, DbDeepAgentState],
    ):
        """检索候选表、列、维度值与历史 SQL 案例。

        用途：在编写 SQL 前了解可用的表结构、列含义、维度取值与历史写法。
        阈值由服务端配置固定（返回中的 effective_min_scores）；未命中的维度值
        不代表可用，需用 probe_dimension_values 探测或向用户澄清。
        """
        ctx = runtime.context
        r = cfg.retrieval
        kinds = list(kinds)

        if "dim_values" in kinds and not value_texts:
            return error_envelope(
                TOOL_ERROR_INVALID_ARGUMENT,
                "kinds 含 dim_values 时 value_texts 必填",
                retryable=True,
                hint="提供 1–10 个待匹配的原文值",
            )

        # 阈值固定在服务端（§7.2）：工具不允许传 min_score，此处按类型取配置值回显
        effective_min_scores = {
            k: getattr(r, f"{_KIND_TO_FIELD[k]}_min_score") for k in kinds
        }
        top_ks = {
            k: top_k or getattr(r, f"{_KIND_TO_FIELD[k]}_top_k") for k in _KIND_TO_FIELD
        }

        data: dict[str, Any] = {"effective_min_scores": effective_min_scores}
        new_evidence: list[DimEvidence] = []
        warnings: list[str] = []

        try:
            query_embedding = services.embedding.embed_query(query)

            if "tables" in kinds:
                hits = services.milvus.search_tables(
                    query_embedding, top_ks["tables"], r.table_min_score
                )
                for h in hits:
                    h["category_group"] = services.catalog.classify(
                        h.get("table_category", "")
                    )
                    h["display"] = to_display_name(h["object_id"])
                data["tables"] = hits

            if "columns" in kinds:
                hits = services.milvus.search_columns(
                    query_embedding, top_ks["columns"], r.column_min_score
                )
                for h in hits:
                    h["parent_display"] = to_display_name(h["parent_id"])
                data["columns"] = hits

            if "dim_values" in kinds:
                matched_groups: dict[tuple[str, str], dict] = {}
                unmatched: list[str] = []
                for value_text in value_texts or []:
                    hits = services.milvus.search_dim_values(
                        value_text, top_ks["dim_values"], r.dim_value_min_score
                    )
                    if not hits:
                        unmatched.append(value_text)
                        continue
                    for h in hits:
                        key = (h["dim_table"], h["dim_col"])
                        group = matched_groups.setdefault(
                            key,
                            {
                                "dim_table": h["dim_table"],
                                "dim_col": h["dim_col"],
                                "values": [],
                                "best_score": 0.0,
                                "query_values": [],
                            },
                        )
                        if h["matched_text"] not in group["values"]:
                            group["values"].append(h["matched_text"])
                        group["best_score"] = max(group["best_score"], h["score"])
                        if value_text not in group["query_values"]:
                            group["query_values"].append(value_text)

                dim_entries = []
                for (dim_table, dim_col), group in sorted(matched_groups.items()):
                    # 分组用 Milvus 原值（三段键）；证据与输出用两段名（§5.3 DimEvidence.table）
                    dim_display = to_display_name(dim_table)
                    evidence_id = (
                        "D-"
                        + hashlib.sha256(
                            "\x1f".join([ctx.run_id, dim_table, dim_col]).encode(
                                "utf-8"
                            )
                        ).hexdigest()[:8]
                    )
                    new_evidence.append(
                        DimEvidence(
                            dim_evidence_id=evidence_id,
                            run_id=ctx.run_id,
                            source="milvus_dim_hit",
                            table=dim_display,
                            column=dim_col,
                            values=group["values"],
                            patterns=[],
                            query_text=None,
                            ref=f"milvus best_score={group['best_score']:.3f} threshold={r.dim_value_min_score}",
                            datasource_id=ctx.datasource_id,
                            inherited_from=None,
                        )
                    )
                    dim_entries.append(
                        {
                            "dim_evidence_id": evidence_id,
                            "dim_table": dim_display,
                            "dim_col": dim_col,
                            "query_values": group["query_values"],
                            "matched_values": group["values"],
                            "best_score": round(group["best_score"], 3),
                        }
                    )
                data["dim_values"] = dim_entries
                data["unmatched_values"] = unmatched
                if unmatched:
                    warnings.append(
                        "未命中值不能直接作为过滤条件；可用 probe_dimension_values "
                        "在明确的候选列上探测，或向用户澄清"
                    )

            if "sql_examples" in kinds:
                hits = services.milvus.search_similar_sqls(
                    query_embedding, top_ks["sql_examples"], r.sql_example_min_score
                )
                for h in hits:
                    if len(h["sql"]) > _SQL_TEXT_MAX_CHARS:
                        h["sql"] = h["sql"][:_SQL_TEXT_MAX_CHARS] + "…(截断)"
                data["sql_examples"] = hits

        except RetrievalBackendError as exc:
            return error_envelope(
                TOOL_ERROR_RETRIEVAL_BACKEND, str(exc), retryable=False
            )

        logger.info(
            "[request_id=%s run_id=%s] search_schema: kinds=%s effective_min_scores=%s dim_evidence=%d unmatched=%d",
            ctx.request_id,
            ctx.run_id,
            kinds,
            effective_min_scores,
            len(new_evidence),
            len(data.get("unmatched_values", [])),
        )

        status_empty = not any(
            data.get(k) for k in ("tables", "columns", "dim_values", "sql_examples")
        )
        content, finalize_warnings = finalize_output(
            {
                "status": "empty" if status_empty else "ok",
                "data": data,
                "warnings": warnings,
            },
            services.config.results.max_tool_output_chars,
        )
        content = _merge_warnings(content, finalize_warnings)
        return tool_command(
            content,
            runtime.tool_call_id or "",
            updates={"db_dim_evidence": new_evidence} if new_evidence else None,
        )

    _attach_validation_handler(search_schema)
    return search_schema


# ============================================================================
# inspect_tables（§7.3）
# ============================================================================


def make_inspect_tables_tool(services: DbAgentServices):
    """构造 inspect_tables 工具（无状态写入，不需要 runtime）。"""

    class InspectTablesArgs(StrictToolArgs):
        table_ids: list[str] = Field(
            min_length=1, max_length=10, description="两段或三段表标识"
        )
        include_live_columns: bool = Field(
            default=True, description="是否合并业务库实时列结构"
        )

    @tool(args_schema=InspectTablesArgs)
    def inspect_tables(table_ids: list[str], include_live_columns: bool) -> str:
        """查看表的类别、业务说明卡片与列结构；返回的 display 为 SQL 中应使用的 schema.table。"""
        results = []
        all_warnings: list[str] = []
        for table_id in table_ids:
            try:
                table_key = services.catalog.normalize_table_id(table_id)
            except InvalidTableIdError as exc:
                results.append(
                    {
                        "table_id": table_id,
                        "status": "error",
                        "error_type": TOOL_ERROR_INVALID_TABLE_ID,
                        "message": str(exc),
                    }
                )
                continue
            try:
                entry = services.catalog.describe([table_key], include_live_columns)[0]
                entry["status"] = "ok"
                results.append(entry)
                all_warnings.extend(entry.get("warnings", []))
            except RetrievalBackendError as exc:
                results.append(
                    {
                        "table_id": table_id,
                        "status": "error",
                        "error_type": TOOL_ERROR_RETRIEVAL_BACKEND,
                        "message": str(exc),
                    }
                )
            except Exception as exc:  # noqa: BLE001（逐表隔离，不影响其他表）
                logger.exception("inspect_tables 单表失败: %s", table_id)
                results.append(
                    {
                        "table_id": table_id,
                        "status": "error",
                        "error_type": "internal_error",
                        "message": type(exc).__name__,
                    }
                )
        content, finalize_warnings = finalize_output(
            {
                "status": "ok",
                "data": {"tables": results},
                "warnings": sorted(set(all_warnings)),
            },
            services.config.results.max_tool_output_chars,
        )
        return _merge_warnings(content, finalize_warnings, dedupe=True)

    _attach_validation_handler(inspect_tables)
    return inspect_tables


# ============================================================================
# plan_joins（§7.4）
# ============================================================================


def make_plan_joins_tool(services: DbAgentServices):
    """构造 plan_joins 工具（无状态写入，不需要 runtime）。"""

    class PlanJoinsArgs(StrictToolArgs):
        base_table: str = Field(description="基表标识（两段或三段）")
        target_tables: list[str] = Field(
            min_length=1, max_length=10, description="目标表标识"
        )
        max_hops: Optional[int] = Field(
            default=None, ge=1, le=5, description="路径最大跳数，默认取配置"
        )

    @tool(args_schema=PlanJoinsArgs)
    def plan_joins(
        base_table: str, target_tables: list[str], max_hops: int | None
    ) -> str:
        """规划基表到各目标表的 JOIN 路径；返回逐步边与 ON 条件（边中表名可能是三段键，SQL 中需改用两段名）。"""
        cfg = services.config
        try:
            base_key = services.catalog.normalize_table_id(base_table)
        except InvalidTableIdError as exc:
            return error_envelope(
                TOOL_ERROR_INVALID_TABLE_ID, f"base_table: {exc}", retryable=True
            )

        targets: list[str] = []
        per_target = []
        no_path_targets: list[str] = []
        error_targets: list[str] = []
        bridge_tables: set[str] = set()

        for target in target_tables:
            try:
                target_key = services.catalog.normalize_table_id(target)
            except InvalidTableIdError as exc:
                per_target.append(
                    {
                        "target": target,
                        "status": "error",
                        "error_type": TOOL_ERROR_INVALID_TABLE_ID,
                        "message": str(exc),
                    }
                )
                error_targets.append(target)
                continue
            outcome = services.join_graph.find_join_path(
                base_key.key,
                target_key.key,
                max_hops=max_hops,
                strategy=cfg.neo4j.join_strategy,
                allow_shortest_path_fallback=cfg.neo4j.allow_shortest_path_fallback,
                query_timeout_s=cfg.neo4j.query_timeout_seconds,
            )
            entry: dict[str, Any] = {
                "target": target_key.display,
                "status": outcome.status,
                "strategy": outcome.strategy,
                "fallback_used": outcome.fallback_used,
            }
            if outcome.status == "found":
                targets.append(target_key.display)
                input_keys = {base_key.key, target_key.key}
                edges = []
                for edge in outcome.edges:
                    edges.append(
                        {
                            "src_table": to_display_name(edge["src_table"]),
                            "dst_table": to_display_name(edge["dst_table"]),
                            "join_type": edge.get("join_type"),
                            "cardinality": edge.get("cardinality"),
                            "on": edge.get("on"),
                        }
                    )
                    for raw in (edge["src_table"], edge["dst_table"]):
                        if raw not in input_keys:
                            bridge_tables.add(to_display_name(raw))
                entry["edges"] = edges
                entry["total_cost"] = outcome.total_cost
            elif outcome.status == "no_path":
                no_path_targets.append(target_key.display)
                entry["message"] = "两表之间没有 JOIN 路径"
            else:  # table_not_in_graph / error
                error_targets.append(target)
                entry["error_type"] = (
                    TOOL_ERROR_GRAPH_BACKEND
                    if outcome.status == "error"
                    else "table_not_found"
                )
                entry["message"] = outcome.error or "表不在 JOIN 图中"
            per_target.append(entry)

        warnings = []
        if no_path_targets:
            warnings.append(f"无路径: {', '.join(no_path_targets)}")
        if bridge_tables:
            warnings.append(
                f"路径中的桥接表未在输入中: {', '.join(sorted(bridge_tables))}，建议用 inspect_tables 补查结构"
            )

        payload = {
            "status": "error" if len(error_targets) == len(target_tables) else "ok",
            "data": {
                "base": base_key.display,
                "targets": targets,
                "plans": per_target,
                "bridge_tables": sorted(bridge_tables),
            },
            "warnings": warnings,
        }
        if payload["status"] == "error":
            payload["error"] = {
                "error_type": TOOL_ERROR_GRAPH_BACKEND,
                "message": "全部目标查询失败",
                "retryable": False,
            }
        content, _ = finalize_output(
            payload, services.config.results.max_tool_output_chars
        )
        return content

    _attach_validation_handler(plan_joins)
    return plan_joins


__all__ = [
    "StrictToolArgs",
    "RuntimeToolArgs",
    "make_search_schema_tool",
    "make_inspect_tables_tool",
    "make_plan_joins_tool",
]
