"""结果事实纯函数（doc 6.3 §7.4）

known_saved_row_count / is_proven_untruncated：按完整证据身份聚合原始记录的
可靠行数与"显式未截断"证明。门控不读默认投影值：

- 可靠行数要求 ``type(value) is int and value >= 0``；缺失、None、布尔
  （包括 False）、负数、字符串、浮点数均为未知。
- 未截断证明要求该组非空且每条原始记录 ``source_truncated is False``；
  任一条为 True、缺失、None 或非布尔值都不能证明未截断；记录顺序不影响
  结论。

身份聚合与冲突处理集中在映射构建处（aggregate_known_saved_row_counts /
aggregate_proven_untruncated），6.2 的 ``_known_saved_row_counts()`` 与任务
门控复用同一映射。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any


def known_saved_row_count(record: Mapping[str, Any]) -> int | None:
    """单条记录的可靠保存行数；未知返回 None（doc 6.3 §7.4）。"""
    raw = record.get("saved_row_count")
    return raw if type(raw) is int and raw >= 0 else None


def is_record_untruncated(record: Mapping[str, Any]) -> bool | None:
    """单条记录的显式未截断判定：True/False 为证明，None 为不可证明。"""
    value = record.get("source_truncated")
    if value is False:
        return True
    if value is True:
        return False
    return None  # 缺失、None、整数（含 0）或其他非布尔值


def is_proven_untruncated(records: Iterable[Mapping[str, Any]]) -> bool:
    """一组同身份原始记录能否证明未截断（doc 6.3 §7.4/§13.7）。

    非空且每条都 ``source_truncated is False`` 才成立；重复的显式 False
    可保持证明；空组、任一 True/缺失/非布尔值均不能证明。
    """
    group = list(records)
    if not group:
        return False
    return all(is_record_untruncated(record) is True for record in group)


def _records_of_run(
    values: Mapping[str, Any] | None,
    run_id: str,
) -> list[tuple[str, Mapping[str, Any]]]:
    """本轮原始记录（executions 仅 query、analyses 全收）→ (evidence_id, record)。"""
    out: list[tuple[str, Mapping[str, Any]]] = []
    for channel in ("db_executions", "db_analyses"):
        for record in (values or {}).get(channel) or []:
            if not isinstance(record, Mapping) or record.get("run_id") != run_id:
                continue
            if channel == "db_executions" and record.get("purpose") != "query":
                continue
            evidence_id = record.get("evidence_id")
            if isinstance(evidence_id, str):
                out.append((evidence_id, record))
    return out


def aggregate_known_saved_row_counts(
    values: Mapping[str, Any] | None,
    run_id: str,
) -> dict[str, int | None]:
    """按证据身份聚合的可靠行数映射（与 6.2 `_known_saved_row_counts` 同口径）。

    同 evidence_id 冲突记录（同身份不同可靠值）降为未知 None，不取首条。
    """
    counts: dict[str, int | None] = {}
    for evidence_id, record in _records_of_run(values, run_id):
        count = known_saved_row_count(record)
        if evidence_id in counts and counts[evidence_id] != count:
            counts[evidence_id] = None  # 冲突不能推定为可靠行数
        else:
            counts[evidence_id] = count
    return counts


def aggregate_proven_untruncated(
    values: Mapping[str, Any] | None,
    run_id: str,
) -> dict[str, bool]:
    """按证据身份聚合的未截断证明映射（doc 6.3 §7.4）。

    身份组内全部原始记录显式 ``source_truncated is False`` 才为 True；
    打乱记录顺序结论不变。未知/冲突不视为已证明（保守降级）。
    """
    groups: dict[str, list[Mapping[str, Any]]] = {}
    for evidence_id, record in _records_of_run(values, run_id):
        groups.setdefault(evidence_id, []).append(record)
    return {
        evidence_id: is_proven_untruncated(records)
        for evidence_id, records in groups.items()
    }
