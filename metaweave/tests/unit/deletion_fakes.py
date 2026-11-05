"""删除链路测试假会话共用的 scalars 语义过滤。

真实 Session 的 ``scalars(select(Entity).where(...))`` 返回 ORM 对象并
在 SQL 内过滤；假会话用本 helper 按同一语义在池上执行（支持
``==``、``!=``、``in_``、``IS NULL``），避免生产查询迁就假件。
"""

from __future__ import annotations

from typing import Any, Iterable


def _where_items(statement: Iterable | Any):
    where = getattr(statement, "whereclause", None)
    if where is None:
        return
    items = where.clauses if hasattr(where, "clauses") else [where]
    yield from items


def _row_matches(row, items) -> bool:
    for item in items:
        key = item.left.key
        operator = getattr(item.operator, "__name__", "")
        value = getattr(item.right, "value", item.right)
        actual = getattr(row, key, None)
        if operator == "eq":
            if actual != value:
                return False
        elif operator == "ne":
            if actual == value:
                return False
        elif operator == "in_op":
            if actual not in (value or []):
                return False
        elif operator == "is_":
            # col.is_(None) 的右侧编译为 Null 哨兵，等价于判空
            if value is None or type(value).__name__ == "Null":
                if actual is not None:
                    return False
            elif actual is not value:
                return False
        else:
            raise AssertionError(f"假会话未实现的过滤算子: {operator}")
    return True


def scalars_from_pool(pool: list, statement):
    """模拟 ``session.scalars(statement)``：实体选择返回行，列选择返回值。"""
    description = statement.column_descriptions[0]
    entity = description["entity"]
    name = description["name"]
    items = list(_where_items(statement))
    rows = [row for row in pool if _row_matches(row, items)]
    if name == getattr(entity, "__name__", None):
        return iter(rows)
    return iter(getattr(row, name) for row in rows)
