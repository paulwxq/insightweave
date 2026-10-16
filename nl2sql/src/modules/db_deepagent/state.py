"""扩展状态 DbDeepAgentState（docs/v4/5 §5.2）

按概要设计 §9.3 与 K11，继承 DeepAgentState，不覆盖 messages。新增字段全部标注
PrivateStateAttr（K13）：调用方不能通过 invoke 输入注入，模型也看不到；
Runner 通过 get_state().values 读取。

勘误（相对 §5.2 代码示例）：metadata 顺序必须为 [PrivateStateAttr, reducer]。
langgraph `_is_field_binop` 只把 metadata 末元素识别为 reducer（state.py:1904-1910），
而 PrivateStateAttr 是 OmitFromSchema 实例（非 callable）——若垫在末位，reducer 被
静默丢弃，通道退化为 LastValue；langchain 侧 OmitFromSchema 识别扫描全部 metadata
（factory.py:_resolve_schema），marker 在前不影响输入/输出 Schema 剔除。
"""

from __future__ import annotations

import operator
from typing import Annotated, NotRequired

from deepagents import DeepAgentState
from langchain.agents.middleware.types import PrivateStateAttr

from src.modules.db_deepagent.schemas import (
    AnalysisRecord,
    DimEvidence,
    ExecutionRecord,
    FinalAnswerRecord,
    RunStatusRecord,
    TimeEvidence,
    ToolErrorRecord,
    ValidationRecord,
)


def merge_by_key(left: dict | None, right: dict | None) -> dict:
    """浅合并字典，右值覆盖左值（授权记录的创建与撤销、运行状态与最终答案）。"""
    if left is None and right is None:
        return {}
    if left is None:
        return dict(right)
    if right is None:
        return dict(left)
    merged = dict(left)
    merged.update(right)
    return merged


class LedgerConsistencyError(Exception):
    """同 evidence_id 两次登记的事实字段不一致（reuse 幂等冲突，§7.12.1）"""


def _append_dedup(
    left: list | None,
    right: list | None,
    fact_fields: tuple[str, ...],
) -> list:
    """按 evidence_id 去重追加：同 ID 同事实保留首条；同 ID 异事实抛一致性错误。

    比较只针对事实字段，排除 tool_call_id 等登记调用字段（允许不同）；
    其余字段（run_id／request_id）在同 ID 下本就相同。
    """
    if not left:
        return list(right or [])
    if not right:
        return list(left)
    merged = list(left)
    seen = {r["evidence_id"]: r for r in merged}
    for rec in right:
        existing = seen.get(rec["evidence_id"])
        if existing is None:
            merged.append(rec)
            seen[rec["evidence_id"]] = rec
            continue
        for field in fact_fields:
            if existing.get(field) != rec.get(field):
                raise LedgerConsistencyError(
                    f"evidence_id={rec['evidence_id']} 的 {field} 不一致："
                    f"{existing.get(field)!r} != {rec.get(field)!r}"
                )
        # 同 ID 同事实：保留首条（含其登记调用 tool_call_id）
    return merged


# §7.12.1：执行／分析记录分别定义事实比较字段
_EXECUTION_FACT_FIELDS = (
    "reused_from",
    "result_ref",
    "executed_sql_sha256",
    "saved_row_count",
    "source_truncated",
    "columns",
    "result_generated_at",
)
_ANALYSIS_FACT_FIELDS = (
    "reused_from",
    "output_result_ref",
    "operation",
    "input_result_refs",
    "executed_sql",
    "saved_row_count",
    "source_truncated",
    "result_generated_at",
)


def append_dedup_execution(left, right) -> list:
    return _append_dedup(left, right, _EXECUTION_FACT_FIELDS)


def append_dedup_analysis(left, right) -> list:
    return _append_dedup(left, right, _ANALYSIS_FACT_FIELDS)


class DbDeepAgentState(DeepAgentState):
    """db_deepagent 图状态：账本字段全部为 PrivateStateAttr，只由本模块工具写入（§5.2）"""

    db_validations: NotRequired[
        Annotated[dict[str, ValidationRecord], PrivateStateAttr, merge_by_key]
    ]
    # §7.12.1：按 evidence_id 去重追加（reuse 幂等；同 ID 异事实抛一致性错误）
    db_executions: NotRequired[
        Annotated[list[ExecutionRecord], PrivateStateAttr, append_dedup_execution]
    ]
    db_analyses: NotRequired[
        Annotated[list[AnalysisRecord], PrivateStateAttr, append_dedup_analysis]
    ]
    db_time_evidence: NotRequired[
        Annotated[list[TimeEvidence], PrivateStateAttr, operator.add]
    ]
    db_dim_evidence: NotRequired[
        Annotated[list[DimEvidence], PrivateStateAttr, operator.add]
    ]
    db_tool_errors: NotRequired[
        Annotated[list[ToolErrorRecord], PrivateStateAttr, operator.add]
    ]
    db_run_status: NotRequired[
        Annotated[dict[str, RunStatusRecord], PrivateStateAttr, merge_by_key]
    ]
    db_final_answers: NotRequired[
        Annotated[dict[str, FinalAnswerRecord], PrivateStateAttr, merge_by_key]
    ]
