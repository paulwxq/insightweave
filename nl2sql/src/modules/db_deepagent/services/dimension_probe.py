"""维度探针 SQL 构建（docs/v4/5 §7.6）

三种固定形态（exact / contains / distinct），LIMIT 恒为 n+1（多读一行判断
values_truncated）。守卫对 purpose="probe" 的执行按本模块重建的 SQL 做严格
形态核对（§9 probe_shape_mismatch），故 build 必须是纯函数：同一 spec 恒产生
同一 SQL。只允许文本类型（text/varchar/char/citext）或枚举列。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from psycopg import sql

from src.utils.logger import get_module_logger

logger = get_module_logger("nl2sql.db_deepagent.dimension_probe")

ProbeMode = Literal["exact", "contains", "distinct"]

# §7.6：探针只允许文本或枚举类型。information_schema 中枚举列报
# USER-DEFINED（无 udt_name 可区分其他自定义类型）；探针只读 DISTINCT，
# 第一阶段整类放行
_PROBEABLE_TYPES = re.compile(
    r"^(text|varchar|character varying|char|character|citext|enum|user-defined)",
    re.IGNORECASE,
)

DEFAULT_PROBE_LIMIT = 20


def is_probeable_type(data_type: str) -> bool:
    """列类型是否允许探针（文本族或枚举）。"""
    return bool(_PROBEABLE_TYPES.match(data_type.strip()))


@dataclass(frozen=True)
class ProbeSpec:
    """一次探针的完整参数（可 JSON 序列化，随校验记录保存）"""

    mode: ProbeMode
    schema_name: str
    table: str
    column: str
    value: str | None = None  # exact / contains 必填；distinct 恒 None
    limit: int = DEFAULT_PROBE_LIMIT  # n：返回最多 n 个值，LIMIT 恒写 n+1


class DimensionProbeBuilder:
    """探针 SQL 构建器：形态固定，禁止任何自由文本拼接"""

    def build(self, spec: ProbeSpec) -> sql.Composable:
        """按 spec 生成探针 SQL（psycopg 组合对象，参数全部走 Identifier/Literal）。"""
        self.validate(spec)
        col = sql.Identifier(spec.column)
        tbl = sql.Identifier(spec.schema_name, spec.table)
        head = sql.SQL("SELECT DISTINCT {col} AS value FROM {tbl}").format(
            col=col, tbl=tbl
        )
        if spec.mode == "exact":
            where = sql.SQL(" WHERE {col} = {val}").format(
                col=col, val=sql.Literal(spec.value)
            )
        elif spec.mode == "contains":
            where = sql.SQL(" WHERE {col} ILIKE {pat} ESCAPE '\\'").format(
                col=col, pat=sql.Literal(self.contains_pattern(spec.value or ""))
            )
        else:  # distinct
            where = sql.SQL(" WHERE {col} IS NOT NULL").format(col=col)
        tail = sql.SQL(" ORDER BY 1 LIMIT {n}").format(n=sql.Literal(spec.limit + 1))
        return head + where + tail

    def sql_string(self, spec: ProbeSpec) -> str:
        """build 的字符串形态（无连接上下文；确定性要求见模块 docstring）。"""
        return self.build(spec).as_string(None)

    @staticmethod
    def contains_pattern(value: str) -> str:
        """contains 模式的 ILIKE 模式：转义 % _ \\ 后包裹 %...%。"""
        escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        return f"%{escaped}%"

    @staticmethod
    def validate(spec: ProbeSpec) -> None:
        """spec 自检：模式与参数匹配、limit 在界内。"""
        if spec.mode not in ("exact", "contains", "distinct"):
            raise ValueError(f"未知探针模式: {spec.mode!r}")
        if spec.mode in ("exact", "contains") and not (
            spec.value and spec.value.strip()
        ):
            raise ValueError(f"{spec.mode} 模式必须提供非空 value")
        if spec.mode == "distinct" and spec.value is not None:
            raise ValueError("distinct 模式不接受 value")
        if not (1 <= spec.limit <= 100):
            raise ValueError(f"探针 limit 必须在 1..100，收到 {spec.limit}")
        for name in (spec.schema_name, spec.table, spec.column):
            if not name or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
                raise ValueError(f"探针对象名不合法: {name!r}")
