"""时间／维度证据守卫（docs/v4/5 §9）

AST 解析成功不等于守卫完整：无法确定含义的形态（列归属不明、谓词形态不受
支持、条件过于复杂）一律返回可修复错误，不放行、不退回更宽松匹配。所有失败
以 validation_failed(layer="guard") 返回，error_codes 为规则码，retryable。

GUARD_RULES_VERSION：config.py 计算 validation_fingerprint 时引用（§4.2），
守卫规则变更时必须递增。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Optional

from sqlglot import exp, parse_one
from sqlglot.errors import ParseError
from sqlglot.optimizer.normalize_identifiers import normalize_identifiers
from sqlglot.optimizer.scope import Scope, traverse_scope

from src.modules.db_deepagent.config import GuardCheckConfig
from src.modules.db_deepagent.context import DbAgentContext
from src.modules.db_deepagent.schemas import (
    DimEvidence,
    GuardEvidenceSnapshot,
    TimeEvidence,
)
from src.modules.db_deepagent.services.business_db import BusinessDbService
from src.modules.db_deepagent.services.dimension_probe import (
    DimensionProbeBuilder,
    ProbeSpec,
)
from src.utils.logger import get_module_logger

logger = get_module_logger("nl2sql.db_deepagent.sql_guard")

# 守卫规则版本：规则集合或判定语义变更时递增。
# validation_fingerprint = sha256(canonical_json(validation 段) + GUARD_RULES_VERSION)，
# 授权记录按指纹作用域（§5.4），版本不变时旧授权仍可复用。
# v2：表身份解析与前置校验对齐——未加引号标识符按 PG 规则折叠、
# 裸名按有效搜索路径（pg_catalog 优先）确定 schema（doc 4 §4.7）。
# v3：分析 AST 统一 normalize_identifiers(postgres)——未引号列名/别名/
# CTE 名与表身份一并折叠（双引号保留）；原始 SQL 继续用于授权与执行。
GUARD_RULES_VERSION = "3"

# §9.1 字面量分类
_TIME_LITERAL_RE = re.compile(r"^\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}(?::\d{2})?)?$")

# §9.4 realtime_clock_in_filter：now() 由 sqlglot 归一为 CurrentTimestamp，
# 其余为 Anonymous
_REALTIME_TYPES = (
    exp.CurrentDate,
    exp.CurrentTimestamp,
    exp.CurrentTime,
    exp.Localtimestamp,
    exp.Localtime,
)
_REALTIME_ANON = {"clock_timestamp", "statement_timestamp", "transaction_timestamp"}

_COMPARISONS = (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE)
_PATTERN_PREDICATES = (
    exp.Like,
    exp.ILike,
    exp.SimilarTo,
    exp.RegexpLike,
    exp.RegexpILike,
)
# BETWEEN 恒不作为取值范围支持：时间命中 time_operator_not_half_open，
# 维度无对应规则，按 §9 基本原则返回 unsupported_predicate_form
_SUPPORTED_PREDICATES = _COMPARISONS + _PATTERN_PREDICATES + (exp.In, exp.Between)

# §9.3/§9.2 限制
_MAX_GROUPS = 64
_MAX_TRACE_DEPTH = 8

# §9.5 空字符串例外允许的类型（不含定长 char）
_EMPTY_STRING_TYPES = {"text", "varchar", "character varying", "citext"}

# §9.8 generate_series 允许的步长
_FIXED_STEPS = {("1", "DAY"): timedelta(days=1), ("1", "WEEK"): timedelta(weeks=1)}

# §8.3 占位符哨兵
_SENTINEL_NAME_RE = re.compile(r"^__dbda_ph_(\d+)__$")


@dataclass(frozen=True)
class GuardResult:
    """守卫判定结果：ok 时 used_evidence 为实际采用的证据完整内容（§9.7）"""

    ok: bool
    error_codes: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    used_evidence: Optional[GuardEvidenceSnapshot] = None


class _GuardFail(Exception):
    """内部提前失败（guard_too_complex 等）"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class _Lit:
    """条件中的字符串字面量（time 或 dim）"""

    kind: str  # "time" / "dim"
    value: str


@dataclass(frozen=True)
class _Owner:
    """列归属（§9.2）：唯一 (schema.table, column)"""

    schema: str
    table: str
    column: str

    @property
    def table_key(self) -> str:
        return f"{self.schema}.{self.table}"


# ---- 来源追溯结果（§9.2 表） ----


@dataclass(frozen=True)
class _TracedLiteral:
    """字面量投影（可带类型转换），视同字面量直接写在条件中"""

    lit: _Lit


@dataclass(frozen=True)
class _TracedConstant:
    """含字符串/时间字面量的表达式投影、VALUES/unnest、不合规 generate_series"""

    code: str = "constant_source_unsupported"
    reason: str = "常量来源不受支持"


@dataclass(frozen=True)
class _TracedData:
    """数据派生值（聚合、算术、基表列、受控 generate_series），不要求证据"""


# ---------------------------------------------------------------- 标识符归一


def _fold_identifier(identifier: Optional[exp.Identifier]) -> str:
    """未加引号的标识符按 PostgreSQL 规则折叠小写；加引号保留原样。

    与前置校验（sql_validation._identifier_text）同一规则：守卫与校验
    必须对同一份 SQL 得出同一表身份，否则校验放行的查询在守卫处
    column_owner_unresolved（doc 4 §4.7）。
    """
    if identifier is None:
        return ""
    return identifier.name if identifier.quoted else identifier.name.lower()


# ---------------------------------------------------------------- 字面量与列


def _classify_literal(value: str) -> _Lit:
    if _TIME_LITERAL_RE.match(value):
        return _Lit(kind="time", value=value)
    return _Lit(kind="dim", value=value)


def _literal_of(node: exp.Expression) -> Optional[_Lit]:
    """字面量侧识别：字符串字面量，可带类型转换。"""
    if isinstance(node, exp.Literal) and node.is_string:
        return _classify_literal(node.this)
    if (
        isinstance(node, exp.Cast)
        and isinstance(node.this, exp.Literal)
        and node.this.is_string
    ):
        return _classify_literal(node.this.this)
    return None


def _column_of(node: exp.Expression) -> Optional[exp.Column]:
    """列侧识别：普通列，可带 CAST／:: 转换（§9.2）。"""
    if isinstance(node, exp.Column):
        return node
    if isinstance(node, exp.Cast) and isinstance(node.this, exp.Column):
        return node.this
    return None


# 字面量在左 → 等价的“列在左”运算符（'v' >= col ⇔ col <= v）；EQ/NEQ 对称不换
_LITERAL_LEFT_FLIP: dict[type, type] = {
    exp.GTE: exp.LTE,
    exp.LTE: exp.GTE,
    exp.GT: exp.LT,
    exp.LT: exp.GT,
}


def _flip_to_column_left(
    pred: exp.Expression, col: exp.Column, lit_node: exp.Expression
) -> exp.Expression:
    """字面量在左的比较统一翻转为“列在左、值在右”再检查（§9.4）。

    不翻转会让 'v' >= col 被当成下界、'v' < col 被当成上界，
    恒假条件反而通过守卫；EQ/NEQ 两侧对称，原样返回。
    """
    flipped = _LITERAL_LEFT_FLIP.get(type(pred))
    if flipped is None:
        return pred
    return flipped(this=col, expression=lit_node)


def _children(node: exp.Expression) -> list[exp.Expression]:
    """args 中的直接子表达式（sqlglot 30.x 无 iter_children）。"""
    out: list[exp.Expression] = []
    for v in node.args.values():
        if isinstance(v, exp.Expression):
            out.append(v)
        elif isinstance(v, list):
            out.extend(x for x in v if isinstance(x, exp.Expression))
    return out


def _contains_string_literal(node: exp.Expression) -> bool:
    """子树内是否含字符串字面量（不进入嵌套查询 / CTE / 表 / 区间内部）。"""
    stack = [node]
    while stack:
        cur = stack.pop()
        if isinstance(cur, exp.Literal) and cur.is_string:
            return True
        if isinstance(
            cur, (exp.Select, exp.Union, exp.Subquery, exp.CTE, exp.Table, exp.Interval)
        ):
            continue
        stack.extend(_children(cur))
    return False


def _iter_scope_nodes(root: exp.Expression):
    """遍历作用域内节点，不进入嵌套查询 / CTE / 表。"""
    stack = [root]
    while stack:
        cur = stack.pop()
        yield cur
        for child in _children(cur):
            if isinstance(
                child, (exp.Select, exp.Union, exp.Subquery, exp.CTE, exp.Table)
            ):
                continue
            stack.append(child)


def _union_branches(node: exp.Expression) -> list[exp.Select]:
    if isinstance(node, exp.Union):
        return _union_branches(node.this) + _union_branches(node.expression)
    return [node]


def _projection_index(branch: exp.Select, name: str) -> Optional[int]:
    for i, proj in enumerate(branch.selects):
        if proj.alias_or_name == name:
            return i
    return None


def _parse_date(value: Optional[str]) -> Optional[date]:
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _month_days(year: int, month: int) -> int:
    if month == 2:
        return 29 if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0) else 28
    return [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1]


def _subtract_month(d: date, months: int) -> date:
    total = d.year * 12 + (d.month - 1) - months
    year, month0 = divmod(total, 12)
    return date(year, month0 + 1, min(d.day, _month_days(year, month0 + 1)))


def _has_realtime_clock(root: exp.Expression) -> bool:
    for node in root.walk():
        if isinstance(node, _REALTIME_TYPES):
            return True
        if (
            isinstance(node, exp.Anonymous)
            and (node.name or "").lower() in _REALTIME_ANON
        ):
            return True
    return False


def _uses_any_array(pred: exp.Expression) -> bool:
    """ANY(ARRAY[...]) 且数组内含字符串字面量（§9.8 末条）。"""
    return any(
        isinstance(node, (exp.Any, exp.Array)) for node in _iter_scope_nodes(pred)
    ) and _contains_string_literal(pred)


# ---------------------------------------------------------------- 区间链


def _find_chain(
    evidences: list[TimeEvidence], start: date, end: date
) -> Optional[list[TimeEvidence]]:
    """找一条恰好覆盖 [start, end) 的首尾相接有限证据链（子链即可）。"""
    finite = sorted(
        (
            (e, s, t)
            for e in evidences
            for s in [_parse_date(e.get("start"))]
            for t in [_parse_date(e.get("end"))]
            if s is not None and t is not None and s >= start and t <= end
        ),
        key=lambda x: (x[1], x[2]),
    )
    chain: list[TimeEvidence] = []
    cursor = start
    for e, s, t in finite:
        if s == cursor:
            chain.append(e)
            cursor = t
        if cursor >= end:
            break
    if cursor == end and chain:
        return chain
    return None


def _step_info(interval: exp.Interval) -> Optional[tuple[Optional[timedelta], str]]:
    """(delta, unit)；月步长 delta=None。非 1 day/week/month 返回 None。"""
    this = (
        interval.this.this
        if isinstance(interval.this, exp.Literal)
        else str(interval.this)
    )
    unit = interval.text("unit").upper()
    if this != "1" or unit not in {"DAY", "WEEK", "MONTH"}:
        return None
    if unit == "MONTH":
        return (None, "MONTH")
    return (_FIXED_STEPS[(this, unit)], unit)


def _series_arg_date(node: exp.Expression) -> Optional[date]:
    """generate_series 参数日期：字面量 / Cast(字面量) / Cast - Interval(step)。"""
    if isinstance(node, exp.Literal) and node.is_string:
        return _parse_date(node.this)
    if (
        isinstance(node, exp.Cast)
        and isinstance(node.this, exp.Literal)
        and node.this.is_string
    ):
        return _parse_date(node.this.this)
    if isinstance(node, exp.Sub) and isinstance(node.expression, exp.Interval):
        base = _series_arg_date(node.this)
        step = _step_info(node.expression)
        if base is None or step is None:
            return None
        delta, unit = step
        return _subtract_month(base, 1) if unit == "MONTH" else base - delta  # type: ignore[operator]
    return None


def _add_month(d: date, months: int) -> date:
    total = d.year * 12 + (d.month - 1) + months
    year, month0 = divmod(total, 12)
    return date(year, month0 + 1, min(d.day, _month_days(year, month0 + 1)))


def _validate_generate_series(
    func: exp.ExplodingGenerateSeries,
    evidences: list[TimeEvidence],
) -> tuple[bool, list[TimeEvidence]]:
    """§9.8：受控日期序列；全数值参数按数据派生放行。"""
    start, end, step = (
        func.args.get("start"),
        func.args.get("end"),
        func.args.get("step"),
    )
    if not (start and end and isinstance(step, exp.Interval)):
        return False, []
    start_d, end_d = _series_arg_date(start), _series_arg_date(end)
    if start_d is None or end_d is None:
        numeric = all(
            isinstance(n, exp.Literal) and not n.is_string
            for a in (start, end)
            for n in a.walk()
            if isinstance(n, exp.Literal)
        )
        has_literal = any(
            isinstance(n, exp.Literal) and n.is_string
            for a in (start, end)
            for n in a.walk()
            if isinstance(n, exp.Literal)
        )
        # 数值序列（generate_series(1,10)）不涉及时间/维度；含字符串即不支持
        return (True, []) if numeric and not has_literal else (False, [])
    info = _step_info(step)
    if info is None:
        return False, []
    delta, unit = info
    # L2 = 链尾 end - 1 步 → 目标链尾 = L2 + 1 步
    target_end = _add_month(end_d, 1) if unit == "MONTH" else end_d + delta  # type: ignore[operator]
    chain = _find_chain(evidences, start_d, target_end)
    if chain is not None:
        return True, chain
    return False, []


# ---------------------------------------------------------------- 归属与追溯


class _Resolver:
    """单次 check 内的列归属解析与来源追溯（每次运行缓存）"""

    def __init__(self, db: BusinessDbService):
        self._db = db
        self._columns_cache: dict[tuple[str, str], set[str]] = {}
        self._table_schema_cache: dict[str, Optional[str]] = {}
        self._owner_cache: dict[int, Optional[_Owner]] = {}
        self._type_cache: dict[tuple[str, str, str], Optional[str]] = {}
        self._series_ok: dict[int, bool] = {}
        self.series_used: list[TimeEvidence] = []
        self.scope_of: dict[int, Scope] = {}
        self.parent_of: dict[int, Scope] = {}
        self.time_evidence: list[TimeEvidence] = []  # check() 注入，供 §9.8 校验

    # ---- 结构缓存 ----

    def table_columns(self, schema: str, table: str) -> set[str]:
        key = (schema, table)
        if key not in self._columns_cache:
            try:
                cols = self._db.get_columns(schema, table)
            except Exception as exc:  # noqa: BLE001（结构查询失败按未知列处理）
                logger.warning("get_columns(%s.%s) 失败: %s", schema, table, exc)
                cols = []
            self._columns_cache[key] = {c.column_name for c in cols}
        return self._columns_cache[key]

    def column_type(self, owner: _Owner) -> Optional[str]:
        key = (owner.schema, owner.table, owner.column)
        if key not in self._type_cache:
            try:
                self._type_cache[key] = self._db.column_type(
                    owner.schema, owner.table, owner.column
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("column_type(%s) 失败: %s", key, exc)
                self._type_cache[key] = None
        return self._type_cache[key]

    # ---- 表身份（与前置校验一致的解析规则） ----

    def _table_identity(self, table: exp.Table) -> tuple[Optional[str], str]:
        """(schema, table) 按 PG 规则归一：未加引号的标识符折叠小写
        （PUBLIC.orders → public.orders），加引号保留原样；裸名按有效
        搜索路径确定 schema。schema 无法确认时返回 None——无法确认
        归属按列归属未解析处理（fail closed，与前置校验口径一致）。
        """
        schema = _fold_identifier(table.args.get("db")) or None
        name = (
            _fold_identifier(table.this)
            if isinstance(table.this, exp.Identifier)
            else table.name
        )
        if schema is None and name:
            schema = self._bare_name_schema(name)
        return schema, name

    def _bare_name_schema(self, name: str) -> Optional[str]:
        """裸名实际解析到的 schema：有效搜索路径为 pg_catalog 在登记
        清单之前（两边同名优先系统目录，与前置校验第 9 轮判定一致），
        其次按登记顺序取第一个命中；目录查询失败按无归属处理。
        """
        if name in self._table_schema_cache:
            return self._table_schema_cache[name]
        allowed = getattr(self._db, "allowed_schemas", None)
        if not allowed:
            # 未按登记装配（旧部署/未作用域假件）：沿用连接默认 public
            schema: Optional[str] = "public"
        else:
            try:
                located = self._db.locate_table_schemas(
                    [name], [*allowed, "pg_catalog"]
                ).get(name, [])
            except Exception as exc:  # noqa: BLE001（目录查询失败按无归属处理）
                logger.warning("locate_table_schemas(%s) 失败: %s", name, exc)
                located = []
            schema = (
                "pg_catalog"
                if "pg_catalog" in located
                else next((s for s in allowed if s in located), None)
            )
        self._table_schema_cache[name] = schema
        return schema

    # ---- 归属解析（§9.2） ----

    def resolve(self, col: exp.Column) -> Optional[_Owner]:
        node_id = id(col)
        if node_id in self._owner_cache:
            return self._owner_cache[node_id]
        scope = self.scope_of.get(id(col))
        owner = self._resolve_in_scope(scope, col) if scope is not None else None
        self._owner_cache[node_id] = owner
        return owner

    def _resolve_in_scope(self, scope: Scope, col: exp.Column) -> Optional[_Owner]:
        qualifier = col.table
        if qualifier:
            src = self._lookup_source(scope, qualifier)
            if src is None:
                return None
            return self._resolve_source(src, col.name)
        if len(scope.sources) == 1:
            return self._resolve_source(next(iter(scope.sources.values())), col.name)
        if not scope.sources:
            return None
        # 多来源：实时列清单消歧（§9.2 第 2 条）
        candidates = [
            src
            for src in scope.sources.values()
            if col.name in (self._source_output_names(src) or set())
        ]
        if len(candidates) != 1:
            return None
        return self._resolve_source(candidates[0], col.name)

    def _lookup_source(self, scope: Scope, qualifier: str) -> Any:
        """限定名来源查找：本作用域优先，其次逐层外层作用域（相关子查询）。

        traverse_scope 不填充 Scope.parents，父子关系在 check() 中按
        表达式祖先链重建（parent_of）。
        """
        current: Optional[Scope] = scope
        while current is not None:
            src = current.sources.get(qualifier)
            if src is not None:
                return src
            current = self.parent_of.get(id(current.expression))
        return None

    def _resolve_source(self, src: Any, name: str) -> Optional[_Owner]:
        if isinstance(src, exp.Table):
            if isinstance(src.this, exp.Func):
                return None  # 表函数列无基表归属；条件中引用走来源追溯
            schema, table = self._table_identity(src)
            if schema is None or name not in self.table_columns(schema, table):
                return None
            return _Owner(schema=schema, table=table, column=name)
        if isinstance(src, Scope):
            return self._resolve_projection(src, name)
        return None

    def _resolve_projection(self, scope: Scope, name: str) -> Optional[_Owner]:
        """CTE／派生表／UNION 投影的同名列直接透传（§9.2 第 1 条）。"""
        owners: list[_Owner] = []
        for branch in _union_branches(scope.expression):
            idx = _projection_index(branch, name)
            if idx is None:
                return None
            proj = branch.selects[idx]
            inner = proj.unalias() if isinstance(proj, exp.Alias) else proj
            if not isinstance(inner, exp.Column):
                return None  # 表达式投影无法确认归属
            resolved = self.resolve(inner)
            if resolved is None:
                return None
            owners.append(resolved)
        return owners[0] if len(set(owners)) == 1 else None

    def _source_output_names(self, src: Any) -> Optional[set[str]]:
        if isinstance(src, exp.Table):
            if isinstance(src.this, exp.Func):
                alias = src.args.get("alias")
                if alias is not None:
                    return {c.name for c in alias.args.get("columns") or []}
                return set()
            schema, table = self._table_identity(src)
            return self.table_columns(schema, table) if schema is not None else set()
        if isinstance(src, Scope):
            names: set[str] = set()
            for branch in _union_branches(src.expression):
                for proj in branch.selects:
                    name = proj.alias_or_name
                    if not name:
                        return None
                    names.add(name)
            return names
        return None

    # ---- 来源追溯（§9.2 表） ----

    def trace_column(self, col: exp.Column, depth: int = 0):
        """追溯列的值来源；depth 超限抛 guard_too_complex。"""
        if depth > _MAX_TRACE_DEPTH:
            raise _GuardFail("guard_too_complex", "来源追溯超过 8 层")
        scope = self.scope_of.get(id(col))
        if scope is None:
            raise _GuardFail(
                "column_owner_unresolved",
                f"列 {col.sql()} 不在任何已知作用域内",
            )
        if isinstance(scope.expression, (exp.Unnest, exp.Values, exp.Struct)):
            return _TracedConstant()
        qualifier = col.table
        if qualifier:
            src = self._lookup_source(scope, qualifier)
            if src is None:
                raise _GuardFail(
                    "column_owner_unresolved", f"未知来源别名: {qualifier!r}"
                )
        elif len(scope.sources) == 1:
            src = next(iter(scope.sources.values()))
        else:
            return _TracedData()  # 无法定位唯一来源时不作为常量来源
        return self._trace_source(src, col.name, depth)

    def _trace_source(self, src: Any, name: str, depth: int):
        if isinstance(src, exp.Table):
            if isinstance(src.this, exp.Func):
                return self._trace_table_function(src)
            return _TracedData()  # 基表列：数据驱动
        if isinstance(src, Scope):
            if isinstance(src.expression, (exp.Values, exp.Unnest, exp.Struct)):
                return _TracedConstant()
            traced = [
                self._trace_projection(branch, name, depth + 1)
                for branch in _union_branches(src.expression)
            ]
            if any(isinstance(t, _TracedConstant) for t in traced):
                return _TracedConstant()
            for t in traced:
                if isinstance(t, _TracedLiteral):
                    return t
            return _TracedData()
        return _TracedData()

    def _trace_projection(self, branch: exp.Select, name: str, depth: int):
        if depth > _MAX_TRACE_DEPTH:
            raise _GuardFail("guard_too_complex", "来源追溯超过 8 层")
        idx = _projection_index(branch, name)
        if idx is None:
            return _TracedData()
        proj = branch.selects[idx]
        inner = proj.unalias() if isinstance(proj, exp.Alias) else proj
        return self.trace_expr(inner, depth)

    def trace_expr(self, node: exp.Expression, depth: int = 0):
        """投影 / 标量子查询表达式的来源追溯。"""
        if depth > _MAX_TRACE_DEPTH:
            raise _GuardFail("guard_too_complex", "来源追溯超过 8 层")
        if isinstance(node, exp.Literal):
            if node.is_string:
                return _TracedLiteral(_classify_literal(node.this))
            return _TracedData()  # 数值等字面量不检查
        if (
            isinstance(node, exp.Cast)
            and isinstance(node.this, exp.Literal)
            and node.this.is_string
        ):
            return _TracedLiteral(_classify_literal(node.this.this))
        if isinstance(node, exp.Column):
            return self.trace_column(node, depth)
        if isinstance(node, (exp.Subquery, exp.Select, exp.Union)):
            inner = node.this if isinstance(node, exp.Subquery) else node
            traced = [
                self.trace_expr(self._scalar_projection(b), depth + 1)
                for b in _union_branches(inner)
            ]
            if any(isinstance(t, _TracedConstant) for t in traced):
                return _TracedConstant()
            for t in traced:
                if isinstance(t, _TracedLiteral):
                    return t
            return _TracedData()
        # 一般表达式投影：含字符串/时间字面量 → 常量来源；否则数据派生
        if _contains_string_literal(node):
            return _TracedConstant()
        return _TracedData()

    @staticmethod
    def _scalar_projection(branch: exp.Select) -> exp.Expression:
        proj = branch.selects[0] if branch.selects else exp.Null()
        return proj.unalias() if isinstance(proj, exp.Alias) else proj

    def _trace_table_function(self, table: exp.Table):
        """§9.8：FROM/JOIN 中的 generate_series 是唯一放行的常量来源。"""
        func = table.this
        if isinstance(func, exp.ExplodingGenerateSeries):
            node_id = id(func)
            if node_id in self._series_ok:
                return _TracedData() if self._series_ok[node_id] else _TracedConstant()
            ok, used = _validate_generate_series(func, self.time_evidence)
            self._series_ok[node_id] = ok
            if ok:
                self.series_used.extend(used)
                return _TracedData()  # 受控序列视为数据来源，证据计入快照
            return _TracedConstant()
        return _TracedConstant()


# ---------------------------------------------------------------- 条件分组


def _split_groups(expr: exp.Expression) -> list[list[tuple[exp.Expression, bool]]]:
    """§9.3：AND 合并、OR 分支（继承外层合取项）、NOT 取反标记。"""
    groups = _split(expr, False)
    if len(groups) > _MAX_GROUPS:
        raise _GuardFail(
            "guard_too_complex", f"条件分组超过 {_MAX_GROUPS} 组，请拆分查询"
        )
    return groups


def _split(
    expr: exp.Expression, negated: bool
) -> list[list[tuple[exp.Expression, bool]]]:
    if isinstance(expr, exp.Paren):
        return _split(expr.this, negated)
    if isinstance(expr, exp.And):
        left = _split(expr.this, negated)
        right = _split(expr.expression, negated)
        if len(left) * len(right) > _MAX_GROUPS:
            raise _GuardFail("guard_too_complex", f"条件分组超过 {_MAX_GROUPS} 组")
        return [a + b for a in left for b in right]
    if isinstance(expr, exp.Or):
        return _split(expr.this, negated) + _split(expr.expression, negated)
    if isinstance(expr, exp.Not):
        return _split(expr.this, not negated)
    return [[(expr, negated)]]


def _necessary_conjuncts(where: exp.Expression) -> list[exp.Expression]:
    """WHERE 顶层且不在 OR/NOT 之下的合取项（§9.3 第 3 条“必然项”）。"""
    necessary: list[exp.Expression] = []

    def walk(node: exp.Expression, top: bool) -> None:
        if isinstance(node, exp.Paren):
            walk(node.this, top)
            return
        if isinstance(node, exp.And) and top:
            walk(node.this, top)
            walk(node.expression, top)
            return
        if top and not isinstance(node, (exp.Or, exp.Not)):
            necessary.append(node)

    walk(where.this if isinstance(where, exp.Where) else where, True)
    return necessary


# ---------------------------------------------------------------- 守卫主体


class SqlEvidenceGuard:
    """时间／维度证据守卫（§9）"""

    def __init__(
        self,
        config: GuardCheckConfig,
        db: BusinessDbService,
        probe_builder: DimensionProbeBuilder,
    ):
        self._cfg = config
        self._db = db
        self._probe_builder = probe_builder

    # ------------------------------------------------------------ 入口

    def check(
        self,
        ctx: DbAgentContext,
        sql: str,
        *,
        time_evidence: list[TimeEvidence],
        dim_evidence: list[DimEvidence],
        purpose: str = "query",
        probe_spec: Optional[ProbeSpec] = None,
    ) -> GuardResult:
        if purpose == "probe":
            return self._check_probe(sql, probe_spec)
        try:
            tree = parse_one(sql, read="postgres")
        except ParseError as exc:
            return GuardResult(False, ["guard_unparseable"], [f"SQL 无法解析: {exc}"])
        # 分析 AST 统一按 PG 规则折叠未引号标识符（列名/别名/CTE 名/
        # 表身份），双引号保留原样——与前置校验同一归一口径；归一只
        # 发生在这棵局部 AST 上，原始 SQL 字符串继续用于授权与执行
        tree = normalize_identifiers(tree, dialect="postgres")
        # §9.5：只有当前数据源的维度证据进入 D
        dim_pool = [
            d for d in dim_evidence if d.get("datasource_id") == ctx.datasource_id
        ]
        errors: dict[str, str] = {}
        used_time: dict[str, TimeEvidence] = {}
        used_dim: dict[str, DimEvidence] = {}
        resolver = _Resolver(self._db)
        resolver.time_evidence = time_evidence
        scopes = traverse_scope(tree)
        # 节点 → 所属作用域（内层覆盖外层；不进入嵌套查询）；
        # 作用域 → 父作用域（按表达式祖先链，供相关子查询的限定名解析）
        expr_scope = {id(scope.expression): scope for scope in scopes}
        for scope in scopes:
            for node in _iter_scope_nodes(scope.expression):
                resolver.scope_of[id(node)] = scope
            ancestor = scope.expression.parent
            while ancestor is not None:
                parent_scope = expr_scope.get(id(ancestor))
                if parent_scope is not None and parent_scope is not scope:
                    resolver.parent_of[id(scope.expression)] = parent_scope
                    break
                ancestor = ancestor.parent
        try:
            for scope in scopes:
                if isinstance(scope.expression, exp.Select):
                    self._check_scope(
                        scope,
                        resolver,
                        time_evidence,
                        dim_pool,
                        errors,
                        used_time,
                        used_dim,
                    )
        except _GuardFail as fail:
            return GuardResult(False, [fail.code], [fail.message])
        if errors:
            codes = list(errors)
            return GuardResult(False, codes, [errors[c] for c in codes])
        for e in resolver.series_used:
            used_time[e["time_evidence_id"]] = e
        return GuardResult(
            True,
            [],
            [],
            {"time": list(used_time.values()), "dim": list(used_dim.values())},
        )

    # ------------------------------------------------------------ 作用域

    def _check_scope(
        self,
        scope: Scope,
        resolver: _Resolver,
        time_ev: list[TimeEvidence],
        dim_ev: list[DimEvidence],
        errors: dict[str, str],
        used_time: dict[str, TimeEvidence],
        used_dim: dict[str, DimEvidence],
    ) -> None:
        sel = scope.expression
        where = sel.args.get("where")
        having = sel.args.get("having")
        on_conditions = [
            j.args["on"] for j in (sel.args.get("joins") or []) if j.args.get("on")
        ]
        case_conditions = [
            cond.this
            for node in _iter_scope_nodes(sel)
            if isinstance(node, exp.Case)
            for cond in (node.args.get("ifs") or [])
        ]
        filter_conditions = [
            (
                node.args["expression"].this
                if isinstance(node.args.get("expression"), exp.Where)
                else node.args["expression"]
            )
            for node in _iter_scope_nodes(sel)
            if isinstance(node, exp.Filter) and node.args.get("expression")
        ]
        # §9.3 第 3 条：CASE/FILTER 组并入 WHERE 必然项
        necessary = (
            [(p, False) for p in _necessary_conjuncts(where)]
            if where is not None
            else []
        )
        if self._cfg.forbid_realtime_clock_in_filters:
            for cond in (
                where,
                having,
                *on_conditions,
                *case_conditions,
                *filter_conditions,
            ):
                if cond is not None and _has_realtime_clock(cond):
                    errors.setdefault(
                        "realtime_clock_in_filter",
                        "时间边界必须来自 resolve_time_range，不能使用实时时钟函数",
                    )
        groups: list[list[tuple[exp.Expression, bool]]] = []
        for cond in (where, having, *on_conditions):
            if cond is not None:
                groups.extend(
                    _split_groups(cond.this if isinstance(cond, exp.Where) else cond)
                )
        for cond in (*case_conditions, *filter_conditions):
            if cond is not None:
                groups.extend([g + necessary for g in _split_groups(cond)])
        for group in groups:
            self._check_group(
                group, resolver, time_ev, dim_ev, errors, used_time, used_dim
            )

    def _check_group(
        self,
        predicates: list[tuple[exp.Expression, bool]],
        resolver: _Resolver,
        time_ev: list[TimeEvidence],
        dim_ev: list[DimEvidence],
        errors: dict[str, str],
        used_time: dict[str, TimeEvidence],
        used_dim: dict[str, DimEvidence],
    ) -> None:
        lowers: dict[_Owner, list[str]] = {}
        uppers: dict[_Owner, list[str]] = {}
        for pred, negated in predicates:
            self._check_predicate(
                pred,
                negated,
                resolver,
                time_ev,
                dim_ev,
                errors,
                used_dim,
                lowers,
                uppers,
            )
        # §9.4：同一合取组、同一归属列的有效区间
        for owner in dict.fromkeys([*lowers, *uppers]):
            ls, us = lowers.get(owner, []), uppers.get(owner, [])
            if ls and us:
                lo, hi = max(ls), min(us)
                if lo >= hi:
                    errors.setdefault("empty_time_range", f"时间区间为空: [{lo}, {hi})")
                elif not self._match_finite(lo, hi, time_ev, used_time):
                    errors.setdefault(
                        "time_range_not_evidenced",
                        f"区间 [{lo}, {hi}) 没有匹配的时间证据（或证据未首尾相接）",
                    )
            elif us:
                if not self._match_upper(us[0], time_ev, used_time):
                    errors.setdefault(
                        "time_range_not_evidenced",
                        f"只有上界 {us[0]}，没有 (None, {us[0]}) 的单边证据",
                    )
            elif ls:
                if not self._match_lower(ls[0], time_ev, used_time):
                    errors.setdefault(
                        "time_range_not_evidenced",
                        f"只有下界 {ls[0]}，没有 ({ls[0]}, None) 的单边证据",
                    )

    # ------------------------------------------------------------ 谓词

    def _check_predicate(
        self,
        pred: exp.Expression,
        negated: bool,
        resolver: _Resolver,
        time_ev: list[TimeEvidence],
        dim_ev: list[DimEvidence],
        errors: dict[str, str],
        used_dim: dict[str, DimEvidence],
        lowers: dict[_Owner, list[str]],
        uppers: dict[_Owner, list[str]],
    ) -> None:
        if isinstance(pred, exp.Is):
            col = _column_of(pred.this)
            if col is None:
                if _contains_string_literal(pred):
                    errors.setdefault(
                        "unsupported_predicate_form", "IS 判断的左侧必须是普通列"
                    )
                return
            if resolver.resolve(col) is None:
                errors.setdefault(
                    "column_owner_unresolved",
                    f"列 {col.sql()} 归属无法确认，请加表别名并把过滤写在基表所在的查询层",
                )
            return
        if isinstance(pred, (exp.Case, exp.Filter)):
            return  # 内部条件已按独立位置收集
        if isinstance(pred, exp.Predicate) and _uses_any_array(pred):
            # §9.8：ANY(ARRAY[...]) 等把字面量当过滤值的写法不受支持
            errors.setdefault(
                "unsupported_predicate_form",
                "ANY(ARRAY[...]) 不受支持，请用 IN (...) 列表",
            )
            return
        if isinstance(pred, exp.Exists):
            if _contains_string_literal(pred):
                errors.setdefault(
                    "unsupported_predicate_form", "EXISTS (VALUES ...) 不受支持"
                )
            return
        if not isinstance(pred, _SUPPORTED_PREDICATES):
            if _contains_string_literal(pred):
                errors.setdefault(
                    "unsupported_predicate_form",
                    "谓词形态不受支持：请改为“列 与 字面量”直接比较",
                )
            return
        if isinstance(pred, exp.Between):
            self._check_between(pred, errors)
            return
        if isinstance(pred, _PATTERN_PREDICATES):
            self._check_pattern(pred, resolver, dim_ev, errors, used_dim)
            return
        if isinstance(pred, exp.In):
            self._check_in(pred, resolver, dim_ev, errors, used_dim)
            return
        self._check_comparison(
            pred, negated, resolver, time_ev, dim_ev, errors, used_dim, lowers, uppers
        )

    def _check_comparison(
        self,
        pred: exp.Expression,
        negated: bool,
        resolver: _Resolver,
        time_ev: list[TimeEvidence],
        dim_ev: list[DimEvidence],
        errors: dict[str, str],
        used_dim: dict[str, DimEvidence],
        lowers: dict[_Owner, list[str]],
        uppers: dict[_Owner, list[str]],
    ) -> None:
        left, right = pred.this, pred.expression
        lit_left, lit_right = _literal_of(left), _literal_of(right)
        col_left, col_right = _column_of(left), _column_of(right)
        if lit_left or lit_right:
            lit = lit_left if lit_left is not None else lit_right
            assert lit is not None  # 上面的 if 保证
            col = col_right if lit_left else col_left
            if col is None:
                # 列被函数/表达式包裹（TRIM(name)<>''、date_trunc(...)=... 等），
                # 或字面量对字面量
                errors.setdefault(
                    "unsupported_predicate_form",
                    "列被函数或表达式包裹：请改为“列 与 字面量”直接比较",
                )
                return
            if lit_left is not None:
                # 字面量在左：先翻转为“列在左、值在右”，方向判读才正确
                pred = _flip_to_column_left(pred, col, left)
            owner = resolver.resolve(col)
            if owner is None:
                # 先尝试来源追溯：常量投影给出 constant_source_unsupported，
                # 其余归属失败仍为 column_owner_unresolved（§9.2 / §9.9 派生列）
                try:
                    traced = resolver.trace_column(col)
                except _GuardFail:
                    traced = None
                if isinstance(traced, _TracedConstant):
                    errors.setdefault(traced.code, traced.reason)
                    return
                errors.setdefault(
                    "column_owner_unresolved",
                    f"列 {col.sql()} 归属无法确认，请加表别名并把过滤写在基表所在的查询层",
                )
                return
            if lit.kind == "time":
                self._time_bound(
                    pred, negated, owner, lit.value, time_ev, errors, lowers, uppers
                )
            else:
                self._dim_value(
                    pred, owner, lit.value, resolver, dim_ev, errors, used_dim
                )
            return
        if col_left is not None or col_right is not None:
            self._check_column_side(
                pred,
                negated,
                col_left,
                col_right,
                left,
                right,
                resolver,
                time_ev,
                dim_ev,
                errors,
                used_dim,
                lowers,
                uppers,
            )
            return
        if _contains_string_literal(pred):
            errors.setdefault(
                "unsupported_predicate_form",
                "谓词形态不受支持：请改为“列 与 字面量”直接比较",
            )

    def _check_column_side(
        self,
        pred: exp.Expression,
        negated: bool,
        col_left,
        col_right,
        left,
        right,
        resolver: _Resolver,
        time_ev: list[TimeEvidence],
        dim_ev: list[DimEvidence],
        errors: dict[str, str],
        used_dim: dict[str, DimEvidence],
        lowers: dict[_Owner, list[str]],
        uppers: dict[_Owner, list[str]],
    ) -> None:
        """列与列 / 列与子查询比较：对非基表一侧来源追溯（§9.2 表）。"""
        if col_left is not None and col_right is not None:
            owner_l = resolver.resolve(col_left)
            owner_r = resolver.resolve(col_right)
            if owner_l is not None and owner_r is not None:
                return  # 基表列对基表列：数据驱动
            if owner_r is not None:
                base_owner, target = owner_r, col_left  # 追溯未归属一侧
            elif owner_l is not None:
                base_owner, target = owner_l, col_right
            else:
                errors.setdefault(
                    "column_owner_unresolved",
                    f"列 {col_left.sql()} / {col_right.sql()} 归属无法确认",
                )
                return
        else:
            col = col_left if col_left is not None else col_right
            base_owner = resolver.resolve(col)
            if base_owner is None:
                errors.setdefault(
                    "column_owner_unresolved",
                    f"列 {col.sql()} 归属无法确认，请加表别名并把过滤写在基表所在的查询层",
                )
                return
            target = right if col is col_left else left
        try:
            traced = resolver.trace_expr(target)
        except _GuardFail as fail:
            errors.setdefault(fail.code, fail.message)
            return
        if isinstance(traced, _TracedConstant):
            errors.setdefault(traced.code, traced.reason)
            return
        if isinstance(traced, _TracedData):
            return
        if isinstance(traced, _TracedLiteral):
            if col_left is None and col_right is not None:
                # 基表列在右侧（字面量投影在左）：翻转后再判方向
                pred = _flip_to_column_left(pred, col_right, left)
            if traced.lit.kind == "time":
                self._time_bound(
                    pred,
                    negated,
                    base_owner,
                    traced.lit.value,
                    time_ev,
                    errors,
                    lowers,
                    uppers,
                )
            else:
                self._dim_value(
                    pred,
                    base_owner,
                    traced.lit.value,
                    resolver,
                    dim_ev,
                    errors,
                    used_dim,
                )

    def _check_between(self, pred: exp.Between, errors: dict[str, str]) -> None:
        col = _column_of(pred.this)
        lits = [
            x
            for x in (
                _literal_of(pred.args.get("low")),
                _literal_of(pred.args.get("high")),
            )
            if x is not None
        ]
        if col is None or not lits:
            if _contains_string_literal(pred):
                errors.setdefault(
                    "unsupported_predicate_form",
                    "BETWEEN 只支持“列 BETWEEN 字面量 AND 字面量”",
                )
            return
        if any(li.kind == "time" for li in lits):
            errors.setdefault(
                "time_operator_not_half_open",
                "时间过滤只能用 列 >= start AND 列 < end（禁止 BETWEEN）",
            )
            return
        errors.setdefault(
            "unsupported_predicate_form",
            "维度范围过滤不受支持：请用已确认值的 IN，或探针确认的 ILIKE 模式",
        )

    def _check_pattern(
        self,
        pred: exp.Expression,
        resolver: _Resolver,
        dim_ev: list[DimEvidence],
        errors: dict[str, str],
        used_dim: dict[str, DimEvidence],
    ) -> None:
        col = _column_of(pred.this)
        lit = _literal_of(pred.expression)
        if col is None or lit is None:
            if _contains_string_literal(pred):
                errors.setdefault(
                    "unsupported_predicate_form", "LIKE/ILIKE 只支持“列 模式 字面量”"
                )
            return
        owner = self._owner_or_error(col, resolver, errors)
        if owner is None:
            return
        hit = next(
            (
                d
                for d in dim_ev
                if d.get("source") == "db_probe"
                and d.get("table") == owner.table_key
                and d.get("column") == owner.column
                and lit.value in (d.get("patterns") or [])
            ),
            None,
        )
        if hit is not None:
            used_dim[hit["dim_evidence_id"]] = hit
        else:
            errors.setdefault(
                "unsupported_pattern_filter",
                f"模式 {lit.value!r} 未经过探针确认（probe_dimension_values contains）",
            )

    def _check_in(
        self,
        pred: exp.In,
        resolver: _Resolver,
        dim_ev: list[DimEvidence],
        errors: dict[str, str],
        used_dim: dict[str, DimEvidence],
    ) -> None:
        col = _column_of(pred.this)
        if col is None:
            if _contains_string_literal(pred):
                errors.setdefault("unsupported_predicate_form", "IN 的左侧必须是普通列")
            return
        owner = self._owner_or_error(col, resolver, errors)
        if owner is None:
            return
        values: list[_Lit] = []
        query = pred.args.get("query")
        if query is not None:
            try:
                traced = resolver.trace_expr(query)
            except _GuardFail as fail:
                errors.setdefault(fail.code, fail.message)
                return
            if isinstance(traced, _TracedConstant):
                errors.setdefault(
                    "constant_source_unsupported", "IN 子查询的取值来源不受支持"
                )
                return
            if isinstance(traced, _TracedLiteral):
                values = [traced.lit]
            # _TracedData：数据驱动，放行
        elif pred.expressions:
            exprs = pred.expressions
            literal_values = [_literal_of(x) for x in exprs]
            if all(v is not None for v in literal_values):
                values = [v for v in literal_values if v is not None]
            else:
                for x in exprs:
                    if _literal_of(x) is not None:
                        continue
                    try:
                        traced = resolver.trace_expr(x)
                    except _GuardFail as fail:
                        errors.setdefault(fail.code, fail.message)
                        return
                    if isinstance(traced, _TracedConstant):
                        errors.setdefault(
                            "constant_source_unsupported", "IN 列表的取值来源不受支持"
                        )
                        return
                    if isinstance(traced, _TracedLiteral):
                        values.append(traced.lit)
        if any(v.kind == "time" for v in values):
            errors.setdefault(
                "time_operator_not_half_open",
                "时间过滤只能用 列 >= start AND 列 < end（禁止 IN）",
            )
            return
        for v in values:
            self._dim_value(pred, owner, v.value, resolver, dim_ev, errors, used_dim)

    # ------------------------------------------------------------ 规则实现

    def _time_bound(
        self,
        pred: exp.Expression,
        negated: bool,
        owner: _Owner,
        value: str,
        time_ev: list[TimeEvidence],
        errors: dict[str, str],
        lowers: dict[_Owner, list[str]],
        uppers: dict[_Owner, list[str]],
    ) -> None:
        if negated:
            errors.setdefault(
                "time_predicate_under_not",
                "时间谓词不能位于 NOT 之下：请直接写成正向半开区间",
            )
            return
        boundaries = {e.get("start") for e in time_ev} | {e.get("end") for e in time_ev}
        if value not in boundaries:
            errors.setdefault(
                "unsupported_time_literal",
                f"时间字面量 {value!r} 不是任何已登记证据的边界，请先调用 resolve_time_range",
            )
        if isinstance(pred, exp.GTE):
            if not any(e.get("start") == value for e in time_ev):
                errors.setdefault(
                    "time_lower_bound_mismatch", f"{value} 不是任何证据的 start"
                )
            lowers.setdefault(owner, []).append(value)
        elif isinstance(pred, exp.LT):
            if not any(e.get("end") == value for e in time_ev):
                errors.setdefault(
                    "time_upper_bound_mismatch", f"{value} 不是任何证据的 end"
                )
            uppers.setdefault(owner, []).append(value)
        else:
            errors.setdefault(
                "time_operator_not_half_open",
                "时间过滤只能用 列 >= start AND 列 < end（禁止 >、<=、=、<>、IN、BETWEEN）",
            )

    def _dim_value(
        self,
        pred: exp.Expression,
        owner: _Owner,
        value: str,
        resolver: _Resolver,
        dim_ev: list[DimEvidence],
        errors: dict[str, str],
        used_dim: dict[str, DimEvidence],
    ) -> None:
        # §9.5 空字符串例外：= '' / <> '' 且实时类型为 text/varchar/citext
        if value == "" and isinstance(pred, (exp.EQ, exp.NEQ)):
            dtype = resolver.column_type(owner)
            if dtype is not None and dtype.strip().lower() in _EMPTY_STRING_TYPES:
                return
        hit = next(
            (
                d
                for d in dim_ev
                if d.get("table") == owner.table_key
                and d.get("column") == owner.column
                and value in (d.get("values") or [])
            ),
            None,
        )
        if hit is not None:
            used_dim[hit["dim_evidence_id"]] = hit
        else:
            errors.setdefault(
                "unsupported_dimension_value",
                f"值 {value!r} 没有证据（{owner.table_key}.{owner.column}），"
                "请用 search_schema(dim_values) 或 probe_dimension_values 确认，或向用户澄清",
            )

    def _match_finite(
        self,
        lo: str,
        hi: str,
        time_ev: list[TimeEvidence],
        used: dict[str, TimeEvidence],
    ) -> bool:
        s, t = _parse_date(lo), _parse_date(hi)
        if s is None or t is None:
            return False
        chain = _find_chain(time_ev, s, t)
        if chain is None:
            return False
        for e in chain:
            used[e["time_evidence_id"]] = e
        return True

    def _match_upper(
        self,
        u: str,
        time_ev: list[TimeEvidence],
        used: dict[str, TimeEvidence],
    ) -> bool:
        for e in time_ev:
            if e.get("start") is None and e.get("end") == u:
                used[e["time_evidence_id"]] = e
                return True
        return False

    def _match_lower(
        self,
        lower: str,
        time_ev: list[TimeEvidence],
        used: dict[str, TimeEvidence],
    ) -> bool:
        for e in time_ev:
            if e.get("end") is None and e.get("start") == lower:
                used[e["time_evidence_id"]] = e
                return True
        return False

    @staticmethod
    def _owner_or_error(
        col: exp.Column,
        resolver: _Resolver,
        errors: dict[str, str],
    ) -> Optional[_Owner]:
        owner = resolver.resolve(col)
        if owner is None:
            errors.setdefault(
                "column_owner_unresolved",
                f"列 {col.sql()} 归属无法确认，请加表别名并把过滤写在基表所在的查询层",
            )
        return owner

    # ------------------------------------------------------------ 占位符

    def in_left_owners(self, sql: str, expected: int) -> list[Optional[_Owner]]:
        """§8.3 占位符位置检查：哨兵列按序解析 IN 左侧列的归属。

        哨兵必须是 exp.Column、其父为 exp.In 且是 expressions 中的唯一元素、
        In 左侧为普通列；违反时抛 ValueError（由管线转为
        placeholder_position_invalid）。
        """
        tree = parse_one(sql, read="postgres")
        # 与 check() 同一归一口径：未引号标识符折叠后再做归属解析
        tree = normalize_identifiers(tree, dialect="postgres")
        resolver = _Resolver(self._db)
        scopes = traverse_scope(tree)
        for scope in scopes:
            for node in _iter_scope_nodes(scope.expression):
                resolver.scope_of[id(node)] = scope
        sentinels: list[tuple[int, exp.Column]] = []
        for node in tree.walk():
            if isinstance(node, exp.Column):
                m = _SENTINEL_NAME_RE.match(node.name)
                if m:
                    sentinels.append((int(m.group(1)), node))
        sentinels.sort(key=lambda t: t[0])
        if len(sentinels) != expected:
            raise ValueError(
                f"哨兵数量 {len(sentinels)} 与占位符 {expected} 不符"
                "（占位符可能位于字符串或注释中）"
            )
        owners: list[Optional[_Owner]] = []
        for _, col in sentinels:
            parent = col.parent
            if not isinstance(parent, exp.In):
                raise ValueError("占位符只能作为 IN/NOT IN 的整个值集合")
            if (
                list(parent.expressions) != [col]
                or parent.args.get("query") is not None
            ):
                raise ValueError("占位符必须是 IN 值集合中的唯一元素")
            left = parent.this
            if isinstance(left, exp.Cast) and isinstance(left.this, exp.Column):
                left = left.this
            if not isinstance(left, exp.Column):
                raise ValueError("IN 左侧必须是普通列")
            owners.append(resolver.resolve(left))
        return owners

    # ------------------------------------------------------------ 探针

    def _check_probe(self, sql: str, probe_spec: Optional[ProbeSpec]) -> GuardResult:
        """§9.6：探针只做严格形态检查，不按 §9.5 要求取值证据。"""
        if probe_spec is None:
            return GuardResult(False, ["probe_shape_mismatch"], ["缺少 probe_spec"])
        expected = self._probe_builder.sql_string(probe_spec)
        if sql != expected:
            return GuardResult(
                False,
                ["probe_shape_mismatch"],
                [f"探针 SQL 与固定形态不符：{sql!r} != {expected!r}"],
            )
        try:
            tree = parse_one(sql, read="postgres")
        except ParseError as exc:
            return GuardResult(
                False, ["probe_shape_mismatch"], [f"探针 SQL 解析失败: {exc}"]
            )
        sel = tree if isinstance(tree, exp.Select) else None
        if sel is None or not sel.args.get("distinct"):
            return GuardResult(False, ["probe_shape_mismatch"], ["缺少 DISTINCT"])
        if len(sel.selects) != 1 or sel.selects[0].alias_or_name != "value":
            return GuardResult(
                False, ["probe_shape_mismatch"], ["投影必须是单列 value"]
            )
        limit = sel.args.get("limit")
        try:
            limit_n = int(limit.expression.this) if limit is not None else None
        except (AttributeError, ValueError):
            limit_n = None
        if limit_n != probe_spec.limit + 1:
            return GuardResult(
                False, ["probe_shape_mismatch"], ["LIMIT 必须等于 limit+1"]
            )
        if _has_realtime_clock(tree):
            return GuardResult(
                False, ["probe_shape_mismatch"], ["探针不得含实时时钟函数"]
            )
        for node in tree.walk():
            if (
                isinstance(node, exp.Literal)
                and node.is_string
                and _TIME_LITERAL_RE.match(node.this)
            ):
                return GuardResult(
                    False, ["probe_shape_mismatch"], ["探针不得含时间字面量"]
                )
        return GuardResult(True, [], [], {"time": [], "dim": []})
