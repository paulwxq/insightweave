"""图实体键空间契约（doc 4 §4.2）

db 前缀与 schema/table/column 采用两套规则：

- db 前缀：统一判定式——字符串 ∧ 非空 ∧ ``name == name.strip()`` ∧ 不含
  ``.`` ∧ 每个字符的 Unicode 类别均不为 ``Cc``（控制字符）。内部普通空格
  允许，首尾空白与制表符等控制字符拒绝，不做自动 strip 修正。
- schema/table/column：``[A-Za-z_][A-Za-z0-9_]*``，保留实际大小写。

NL2SQL ``catalog.normalize_table_id`` 实现同一判定式（两模块各自实现、
同一组样例验证，不建立跨模块依赖）。
"""

import re
import unicodedata
from typing import List

_PLAIN_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

# schema / table / column 合法字符集
IDENTIFIER_PATTERN = r"[A-Za-z_][A-Za-z0-9_]*"


def db_prefix_problem(name: object) -> str | None:
    """按统一判定式校验 db 前缀；合法返回 None，否则返回问题描述。"""
    if not isinstance(name, str):
        return f"必须是字符串，得到 {type(name).__name__}"
    if not name:
        return "不能为空字符串"
    if name != name.strip():
        return "含首尾空白（不自动修正，请修正登记库名后重试）"
    if "." in name:
        return "不能包含点号 '.'（点号是键的分隔符）"
    controls = [ch for ch in name if unicodedata.category(ch) == "Cc"]
    if controls:
        return "含控制字符"
    return None


def identifier_problem(name: object, *, kind: str) -> str | None:
    """校验 schema/table/column 普通标识符；合法返回 None，否则返回问题描述。"""
    if not isinstance(name, str):
        return f"{kind} 必须是字符串，得到 {type(name).__name__}"
    if not _PLAIN_IDENTIFIER_RE.fullmatch(name):
        return (
            f"{kind} {name!r} 不在支持的键空间内"
            f"（本期 schema/表/列仅支持 {IDENTIFIER_PATTERN}，"
            "不改写名称；带引号标识符解析不在本期范围）"
        )
    return None


def build_entity_id(source_id: str, logical_id: str) -> str:
    """图实体键：``<source_id>:<逻辑 id>``（doc 4 §4.2）。

    source_id 为数据源短 ID（``db_src_N``，doc 5.1 §1）；Table 逻辑 id 为
    ``db.schema.table``，Column 再追加列名。与 NL2SQL ``join_graph`` 的
    构造方式一致。
    """
    return f"{source_id}:{logical_id}"


def validate_graph_keyspace(
    database: object,
    entries: List[tuple],
) -> None:
    """整源校验本批对象的键空间（doc 4 §4.2：不支持的对象阻止整次发布）。

    Args:
        database: 本批画像的库名（db 前缀）
        entries: ``(对象类别, schema, 名称)`` 三元组列表——Table 传
            ``("表", schema, table)``，Column 传 ``("列", schema, column)``

    Raises:
        ValueError: 汇总全部违规对象与规则；调用方在删除/写入知识之前
            拒绝本次整源发布。
    """
    problems: list[str] = []
    db_problem = db_prefix_problem(database)
    if db_problem:
        problems.append(f"库名前缀（database）{database!r}: {db_problem}")
    for kind, schema, name in entries:
        for part_kind, value in (("schema", schema), (kind, name)):
            problem = identifier_problem(value, kind=part_kind)
            if problem:
                problems.append(problem)
    if problems:
        raise ValueError(
            "键空间校验未通过，本次发布整体拒绝（对象与规则如下；"
            "请修正采集对象或登记信息后重试）: " + "; ".join(problems)
        )
