"""数据源 UUID 规范化。"""

from uuid import UUID


def normalize_source_id(value: str | UUID) -> UUID:
    """返回标准 UUID 对象；非法输入只报告类型，不回显原值。"""
    if isinstance(value, UUID):
        return value
    if not isinstance(value, str):
        raise ValueError("source_id 必须是 UUID 字符串")
    try:
        return UUID(value)
    except (ValueError, AttributeError) as exc:
        raise ValueError("source_id 不是合法 UUID") from exc
