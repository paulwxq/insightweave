"""DDL 展示切分与样例解析（doc 19.2 §3.2/§3.6/§8.2）。

纯函数模块：只消费已发布的 ``define_text``，按当前 SAMPLED_RECORDS
格式切出对象定义 SQL 与样例记录；旧 SAMPLE_RECORDS 标记只检测并拒绝
（SQL 与样例均不展示），不保留旧格式解码或 {label, data} 包装分支。
"""

import json

from metaweave.core.metadata.sample_block import (
    decode_current_records,
    find_legacy_sample_records_block,
    find_sampled_records_blocks,
    has_sampled_records_marker_hint,
    iter_sampled_records_marker_hints,
)

MAX_SAMPLE_ROWS = 5


class DefinitionInvalidError(ValueError):
    """DDL 样例块格式异常（旧标记/多块/边界不完整/身份不符/记录畸形）。"""


class DefinitionInconsistentError(RuntimeError):
    """DDL 当前行与当前发布标记不一致（由 object_detail 判定后抛出）。"""


def split_definition_sql(define_text: str) -> tuple[str, dict | None]:
    """剥离末尾唯一的 SAMPLED_RECORDS 块，返回 (定义 SQL, 块 payload | None)。

    旧标记、标记起始但边界不完整、多个样例块或块后仍有非空白内容都按
    DefinitionInvalidError 拒绝——不能把含样例值的原文直送 SQL Tab。
    """
    if find_legacy_sample_records_block(define_text):
        raise DefinitionInvalidError("检测到旧 SAMPLE_RECORDS 样例块")
    blocks = find_sampled_records_blocks(define_text)
    if not blocks:
        # hint 覆盖当前/旧两种标记的块注释起始文本：有起始无完整块即边界异常
        if has_sampled_records_marker_hint(define_text):
            raise DefinitionInvalidError("样例块边界不完整")
        return define_text.rstrip("\n"), None
    if len(blocks) > 1:
        raise DefinitionInvalidError("存在多个样例块")
    if len(iter_sampled_records_marker_hints(define_text)) != len(blocks):
        # 标记起点多于完整块：存在不成块的畸形片段，其原文不得进入 SQL
        raise DefinitionInvalidError("样例块边界不完整")
    match = blocks[0]
    if define_text[match.end() :].strip():
        raise DefinitionInvalidError("样例块之后存在额外内容")
    try:
        payload = json.loads(match.group("body"))
    except json.JSONDecodeError:
        raise DefinitionInvalidError("样例块 JSON 解析失败") from None
    if not isinstance(payload, dict):
        raise DefinitionInvalidError("样例块必须是对象")
    return define_text[: match.start()].rstrip("\n"), payload


def extract_sample(
    payload: dict | None,
    *,
    schema_name: str,
    object_name: str,
    object_type: str,
) -> tuple[str, list[str], list[dict]]:
    """按当前格式解析样例，返回 (sample_state, 列头, 最多 5 行记录)。

    身份按限定名核对：块内 object_name 必须等于行的
    ``schema_name + '.' + object_name``（doc 19.2 §8.2）；``data``/``label``
    一律按真实字段处理；各记录字段键集合须一致，列序取首条键序。
    """
    if payload is None:
        return "no_sample_block", [], []
    if payload.get("object_name") != f"{schema_name}.{object_name}":
        raise DefinitionInvalidError("样例块对象名与 DDL 行不一致")
    if payload.get("object_type") != object_type:
        raise DefinitionInvalidError("样例块对象类型与 DDL 行不一致")
    try:
        records = decode_current_records(payload)
    except ValueError as exc:
        raise DefinitionInvalidError(str(exc)) from None
    if not records:
        return "empty", [], []
    first_keys = list(records[0].keys())
    if any(set(record) != set(first_keys) for record in records):
        raise DefinitionInvalidError("样例记录字段键集合不一致")
    return "ready", first_keys, records[:MAX_SAMPLE_ROWS]
