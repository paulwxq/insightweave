"""表键空间契约（doc 4 §4.2；与 MetaWeave cql_generator.keyspace 同一判定式）

db 前缀：字符串 ∧ 非空 ∧ ``name == name.strip()`` ∧ 不含 ``.`` ∧ 每字符
Unicode 类别不为 ``Cc``。内部普通空格允许（``my orders``），连字符与数字
开头可用（``my-orders``、``2026orders``）；首尾空白、点号、控制字符拒绝。

schema/table：``[A-Za-z_][A-Za-z0-9_]*``，保留实际大小写。

两模块各自实现、同一组输入输出样例验证，不建立跨模块依赖。
"""

from __future__ import annotations

import re
import unicodedata

PLAIN_IDENTIFIER_PATTERN = r"[A-Za-z_][A-Za-z0-9_]*"

_PLAIN_IDENTIFIER_RE = re.compile(PLAIN_IDENTIFIER_PATTERN)


def db_prefix_problem(name: object) -> str | None:
    """按统一判定式校验 db 前缀；合法返回 None，否则返回问题描述。"""
    if not isinstance(name, str):
        return f"db 前缀必须是字符串，得到 {type(name).__name__}"
    if not name:
        return "db 前缀不能为空字符串"
    if name != name.strip():
        return f"db 前缀 {name!r} 含首尾空白"
    if "." in name:
        return f"db 前缀 {name!r} 不能包含点号（点号是键的分隔符）"
    if any(unicodedata.category(ch) == "Cc" for ch in name):
        return f"db 前缀 {name!r} 含控制字符"
    return None


def plain_identifier_problem(name: object, *, kind: str) -> str | None:
    """校验 schema/table 普通标识符；合法返回 None，否则返回问题描述。"""
    if not isinstance(name, str) or not _PLAIN_IDENTIFIER_RE.fullmatch(name):
        return (
            f"{kind} {name!r} 不在支持的键空间内"
            f"（本期 schema/表仅支持 {PLAIN_IDENTIFIER_PATTERN}）"
        )
    return None


def build_entity_id(source_id: str, logical_id: str) -> str:
    """图实体键：``<source_id>:<逻辑 id>``（与 MetaWeave writer 构造一致）。"""
    return f"{source_id}:{logical_id}"
