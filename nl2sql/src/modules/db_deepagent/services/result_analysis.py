"""ResultAnalysisService：本地计算与 SQL 下推（docs/v4/5 §7.10、§7.10.1＝doc 7）

三步路由（doc 7 §1.6）：第 0 步统一数据源检查 → 第 1 步单路径操作
（describe／compare）→ 第 2 步通用表（aggregate／top_n／share／
distinct_values）。本地计算逐条镜像 PG 语义；类型白名单按 typname 归一化。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal, localcontext
from typing import Any, Callable, Literal

from pydantic import BaseModel, ConfigDict, Field

from src.modules.db_deepagent.config import DbAgentConfig
from src.modules.db_deepagent.context import DbAgentContext
from src.modules.db_deepagent.schemas import ColumnMeta
from src.modules.db_deepagent.services.result_store import ResultMeta, ResultStore
from src.utils.logger import get_module_logger

logger = get_module_logger("nl2sql.db_deepagent.result_analysis")

PUSHDOWN_WARNING = "结果由源 SQL 重新执行获得，非原结果快照"

# ---------------------------------------------------------------------------
# 类型归一化（doc 7 §1.2：meta.pg_type 实为 pg_type.typname）
# ---------------------------------------------------------------------------

TypeFamily = Literal[
    "integer",
    "numeric",
    "float",
    "text",
    "bpchar",
    "temporal",
    "time",
    "boolean",
    "other",
]

_TYPE_FAMILIES: dict[str, TypeFamily] = {
    "int2": "integer",
    "int4": "integer",
    "int8": "integer",
    "numeric": "numeric",
    "float4": "float",
    "float8": "float",
    "text": "text",
    "varchar": "text",
    "bpchar": "bpchar",
    "date": "temporal",
    "timestamp": "temporal",
    "timestamptz": "temporal",
    "time": "time",
    "timetz": "time",
    "bool": "boolean",
}

# 能力矩阵（doc 7 §1.2）：判等／排序／算术（文本族整族、bpchar 仅下推）
_EQ_OK = frozenset({"integer", "numeric", "float", "temporal", "boolean"})
_ORD_OK = frozenset({"integer", "numeric", "float", "temporal", "boolean"})
_ARITH_OK = frozenset({"integer", "numeric"})


def family_of(pg_type: str) -> TypeFamily:
    return _TYPE_FAMILIES.get((pg_type or "").strip().lower(), "other")


def _q(name: str) -> str:
    """标识符双引号转义（doc 5 §7.10）。"""
    return '"' + name.replace('"', '""') + '"'


# ---------------------------------------------------------------------------
# 操作参数（工具 args_schema 与服务共用；§7.10 参数表）
# ---------------------------------------------------------------------------


class MetricSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    column: str
    func: Literal["sum", "avg", "min", "max", "count", "count_distinct"]


class AggregateParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    group_by: list[str] = []
    metrics: list[MetricSpec] = Field(min_length=1)


class TopNParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    order_by: str
    descending: bool = False
    n: int = Field(ge=1, le=100)
    ties: Literal["include", "strict"] = "strict"


class ShareParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value_column: str
    group_by: list[str] = []
    round_digits: int | None = Field(default=None, ge=0, le=20)


class _RefColumn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ref: str
    column: str
    row_index: int | None = Field(default=None, ge=0)


class CompareParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    a: _RefColumn
    b: _RefColumn
    round_digits: int | None = Field(default=None, ge=0, le=20)


class DistinctValuesParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    column: str
    max_values: int = Field(default=20, ge=1, le=1000)


class DescribeParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    columns: list[str] = Field(min_length=1)


# ---------------------------------------------------------------------------
# 输入束与结果
# ---------------------------------------------------------------------------


@dataclass
class InputBundle:
    """一个输入结果：元数据＋已解码行＋列类型缓存"""

    meta: ResultMeta
    rows: list[list[Any]]

    def __post_init__(self) -> None:
        self._families = {c["name"]: family_of(c["pg_type"]) for c in self.meta.columns}
        self._index = {c["name"]: i for i, c in enumerate(self.meta.columns)}

    def family(self, column: str) -> TypeFamily:
        if column not in self._families:
            raise AnalysisError("invalid_argument", f"输入结果没有列 {column!r}")
        return self._families[column]

    def pos(self, column: str) -> int:
        if column not in self._index:
            raise AnalysisError("invalid_argument", f"输入结果没有列 {column!r}")
        return self._index[column]


@dataclass
class AnalysisOutcome:
    """一次分析的结果"""

    ok: bool
    error_type: str | None = None
    message: str | None = None
    hint: str | None = None
    rows: list[list[Any]] = field(default_factory=list)
    columns: list[ColumnMeta] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    executed_sql: str | None = None  # 下推 SQL 全文；本地为 None
    pushdown_result_ref: str | None = None  # 下推输出的 result_ref（即执行结果）
    pushdown_execution: dict[str, Any] | None = None  # 下推 ExecutionRecord
    pushdown_updates: Any | None = None  # 下推产生的 LedgerUpdate
    # 输出结果是否受截断影响（distinct_values 超 max_values、下推读满上限）
    source_truncated: bool = False
    # 本地输出沿用输入结果的数据源（跨数据源输入不得改写为当前数据源）
    datasource_id: str | None = None


class AnalysisError(Exception):
    """分析失败；error_type 对应 §7.1"""

    def __init__(self, error_type: str, message: str, hint: str | None = None):
        self.error_type = error_type
        self.message = message
        self.hint = hint
        super().__init__(message)


# ---------------------------------------------------------------------------
# 精度与值工具
# ---------------------------------------------------------------------------


def _sum_precision(values: list[Decimal]) -> int:
    """求和精度＝最大整数位数＋最大小数位数＋进位余量＋2（doc 7 §1.3）。"""
    finite = [v for v in values if v.is_finite()]
    if not finite:
        return 28
    max_adj = max(v.adjusted() for v in finite)
    max_frac = max(-v.as_tuple().exponent for v in finite)
    int_digits = max(max_adj + 1, 1)
    carry = max(1, math.ceil(math.log10(len(finite) + 1)))
    return max(28, int_digits + max_frac + carry + 2)


def _div_precision(numerator: Decimal, denominator: Decimal, target_digits: int) -> int:
    """商所需精度（doc 7 §1.3）。

    两类下界取大：① 商的整数位数＋目标小数位＋余量——只数操作数有效
    位数会低估（1 / 3E-28 商约 3.3E+27，28 位全被整数部分占满，量化后
    尾数恒为 .00）；② 操作数有效位数之和＋目标小数位＋余量——否则商
    会在量化前被提前舍入、丢掉决定舍入方向的数字（略小于 0.005 的值
    除以 1 先被舍成 0.005，保留两位错得 0.01）。
    """
    q_int_digits = max(numerator.adjusted() - denominator.adjusted() + 1, 1)
    operand_digits = len(numerator.as_tuple().digits) + len(
        denominator.as_tuple().digits
    )
    return max(28, q_int_digits + target_digits + 2, operand_digits + target_digits + 2)


def exact_sum(values: list[Decimal]) -> Decimal:
    """指数跨度动态精度的精确求和（`1E+28 + 1E-28` 不丢位）。"""
    finite = [v for v in values if v.is_finite()]
    if not finite:
        return Decimal(0)
    with localcontext() as context:
        context.prec = _sum_precision(finite)
        total = Decimal(0)
        for v in finite:
            total += v
        return total


def exact_sub(a: Decimal, b: Decimal) -> Decimal:
    """局部精度的精确减法（compare 差值）。

    精度取求和的指数跨度上界（1E+28 − 1E-28 需要 56 位，只数有效位数
    会丢小数部分）与操作数位数的较大者；默认 28 位会把 10^28+1 舍成 1E+28。
    """
    with localcontext() as context:
        context.prec = max(
            _sum_precision([a, b]),
            len(a.as_tuple().digits),
            len(b.as_tuple().digits),
            28,
        )
        return a - b


def divide(numerator: Decimal, denominator: Decimal, target_digits: int) -> Decimal:
    """显式局部精度的除法（doc 7 §1.3）。"""
    with localcontext() as context:
        context.prec = _div_precision(numerator, denominator, target_digits)
        return numerator / denominator


def _round_half_up(value: Decimal, digits: int) -> Decimal:
    """ROUND_HALF_UP（doc 7 §1.3：2.5→3、-2.5→-3）。

    quantize 默认沿用上下文的 ROUND_HALF_EVEN（2.5→2）；另在局部精度中
    量化，大数不会因 28 位默认精度抛 InvalidOperation。
    """
    with localcontext() as context:
        int_digits = max(value.adjusted() + 1, 1) if value else 1
        context.prec = max(28, int_digits + digits + 2)
        return value.quantize(Decimal(1).scaleb(-digits), rounding=ROUND_HALF_UP)


def _restore(family: TypeFamily, value: Any) -> Any:
    """时间族按类型恢复（不做字符串比较）；其余原样。"""
    if value is None:
        return None
    if family == "temporal" and isinstance(value, str):
        if len(value) == 10:
            return date.fromisoformat(value)
        return datetime.fromisoformat(value)
    return value


def _nonfinite(family: TypeFamily, value: Any) -> bool:
    if isinstance(value, Decimal):
        return not value.is_finite()
    if isinstance(value, float):
        return math.isnan(value) or math.isinf(value)
    return False


def _as_decimal(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _sort_key(family: TypeFamily, value: Any, descending: bool) -> tuple[int, Any]:
    """PG 默认 NULLS：ASC→NULLS LAST（rank 1），DESC→NULLS FIRST（rank -1）。"""
    if value is None:
        return (1 if not descending else -1, _MIN_SENTINEL)
    return (0, _restore(family, value))


# 排序占位：None 与值不同混排时只用第一元足够（同 rank 的 None 互不比较）
# 注：判等／去重一律用 _restore 后的值，不得用序列化字符串——
# Decimal("1.0") 与 Decimal("1.00") 数值相等但序列化不同（doc 7 §1.3）
_MIN_SENTINEL = ()


# ---------------------------------------------------------------------------
# 服务
# ---------------------------------------------------------------------------


LoadBundle = Callable[[str], InputBundle]
# run_pushdown(sql, source_metas, row_cap=None) -> (ExecOutcome, LedgerUpdate)
RunPushdown = Callable[..., tuple[Any, Any]]


class ResultAnalysisService:
    """analyze_result 的本地计算与下推（§7.10／§7.10.1）"""

    def __init__(self, config: DbAgentConfig, results: ResultStore):
        self._cfg = config
        self._results = results

    # ------------------------------------------------------------ 入口

    def analyze(
        self,
        *,
        operation: str,
        params: dict[str, Any],
        input_refs: list[str],
        ctx: DbAgentContext,
        allow_pushdown: bool = True,
        load_bundle: LoadBundle,
        run_pushdown: RunPushdown,
    ) -> AnalysisOutcome:
        if operation == "describe":
            return self._describe(DescribeParams(**params), input_refs, load_bundle)
        if operation == "compare":
            return self._compare(CompareParams(**params), input_refs, load_bundle)
        bundles = [load_bundle(ref) for ref in input_refs]
        distinct_ds = {b.meta.datasource_id for b in bundles}
        if len(distinct_ds) > 1:
            raise AnalysisError(
                "result_datasource_mismatch",
                f"输入结果数据源不同: {sorted(distinct_ds)}",
                hint="只能分析同一数据源的结果",
            )
        handlers = {
            "aggregate": (AggregateParams, self._aggregate),
            "top_n": (TopNParams, self._top_n),
            "share": (ShareParams, self._share),
            "distinct_values": (DistinctValuesParams, self._distinct_values),
        }
        entry = handlers.get(operation)
        if entry is None:
            raise AnalysisError("invalid_argument", f"未知操作: {operation!r}")
        params_model, handler = entry
        return handler(
            params_model(**params), bundles, ctx, allow_pushdown, run_pushdown
        )

    # ------------------------------------------------------------ 单路径：describe

    def _describe(
        self, params: DescribeParams, refs: list[str], load_bundle: LoadBundle
    ) -> AnalysisOutcome:
        """任何时候本地执行（基于已保存行）；类型不支持仅省略对应统计。"""
        if len(refs) != 1:
            raise AnalysisError("invalid_argument", "describe 只接受一个输入结果")
        bundle = load_bundle(refs[0])
        columns = [
            ColumnMeta(name="column", pg_type="text"),
            ColumnMeta(name="count", pg_type="int8"),
            ColumnMeta(name="non_null", pg_type="int8"),
            ColumnMeta(name="null_count", pg_type="int8"),
            ColumnMeta(name="min", pg_type="text"),
            ColumnMeta(name="max", pg_type="text"),
            ColumnMeta(name="mean", pg_type="numeric"),
        ]
        warnings = [f"describe 基于已保存行（{bundle.meta.saved_row_count} 行）"]
        rows: list[list[Any]] = []
        for column in params.columns:
            family = bundle.family(column)
            pos = bundle.pos(column)
            values = [row[pos] for row in bundle.rows]
            non_null = [v for v in values if v is not None]
            finite = [v for v in non_null if not _nonfinite(family, v)]
            has_nonfinite = len(finite) != len(non_null)
            row: list[Any] = [
                column,
                len(values),
                len(non_null),
                len(values) - len(non_null),
            ]
            # 列含非有限值时 min/max/均值真正省略（doc 7 §1.2：省略统计并警告，
            # 不是剔除后照算——否则警告与结果矛盾）
            usable = finite and not has_nonfinite
            if family in _ORD_OK and usable:
                keys = [_restore(family, v) for v in finite]
                row += [str(min(keys)), str(max(keys))]
            else:
                row += [None, None]
            if family in ("integer", "numeric", "float") and usable:
                decimals = [_as_decimal(v) for v in finite]
                total = exact_sum(decimals)
                row.append(divide(total, Decimal(len(decimals)), 10))
                if family == "float":
                    warnings.append(f"列 {column} 为浮点近似，均值为近似值")
            else:
                row.append(None)
            if has_nonfinite:
                warnings.append(f"列 {column} 含非有限值，min/max/均值已省略")
            if family not in _ORD_OK and not has_nonfinite:
                warnings.append(f"列 {column} 类型不提供 min/max")
            rows.append(row)
        return AnalysisOutcome(
            ok=True,
            rows=rows,
            columns=columns,
            warnings=warnings,
            datasource_id=bundle.meta.datasource_id,
        )

    # ------------------------------------------------------------ 单路径：compare

    def _compare(
        self, params: CompareParams, refs: list[str], load_bundle: LoadBundle
    ) -> AnalysisOutcome:
        """不下推；截断→incomplete_source；行缺失/越界→invalid_argument。"""
        # 实际参与计算的是 a.ref/b.ref；inputs 只作登记来源，必须与之完全
        # 一致，否则 derived_from 会指向未参与计算的引用
        if sorted(refs) != sorted([params.a.ref, params.b.ref]):
            raise AnalysisError(
                "invalid_argument",
                "compare 的 inputs 必须与 a.ref／b.ref 完全一致（登记来源按实际输入）",
            )
        a_bundle = load_bundle(params.a.ref)
        b_bundle = load_bundle(params.b.ref)
        if a_bundle.meta.datasource_id != b_bundle.meta.datasource_id:
            raise AnalysisError(
                "result_datasource_mismatch", "两个输入结果数据源不同，不能比较"
            )
        if a_bundle.meta.source_truncated or b_bundle.meta.source_truncated:
            raise AnalysisError(
                "incomplete_source",
                "输入结果被截断，无法确认标量完整性",
                hint="对未截断的聚合结果进行比较，或重新查询",
            )
        warnings: list[str] = []
        values: list[Decimal | None] = []
        for side, spec, bundle in (
            ("a", params.a, a_bundle),
            ("b", params.b, b_bundle),
        ):
            family = bundle.family(spec.column)
            if family not in ("integer", "numeric", "float"):
                raise AnalysisError(
                    "invalid_argument", f"compare 取值列 {spec.column} 必须为数值列"
                )
            index = spec.row_index if spec.row_index is not None else 0
            if index >= len(bundle.rows):
                raise AnalysisError(
                    "invalid_argument",
                    f"输入 {side} 行号 {index} 越界（共 {len(bundle.rows)} 行）",
                )
            raw = bundle.rows[index][bundle.pos(spec.column)]
            if raw is None:
                warnings.append(f"输入 {side} 的 {spec.column} 为 NULL")
                values.append(None)
                continue
            if _nonfinite(family, raw):
                raise AnalysisError(
                    "invalid_argument",
                    f"输入 {side} 的 {spec.column} 含非有限值，无法比较",
                )
            values.append(_as_decimal(raw))
        a_val, b_val = values
        diff = None if a_val is None or b_val is None else exact_sub(a_val, b_val)
        pct_change = None
        if diff is not None and b_val == 0:
            warnings.append("分母为零，环比无定义")
        elif diff is not None:
            pct_change = divide(
                exact_sub(a_val, b_val), b_val, params.round_digits or 10
            )
        if params.round_digits is not None and diff is not None:
            diff = _round_half_up(diff, params.round_digits)
            if pct_change is not None:
                pct_change = _round_half_up(pct_change, params.round_digits)
        return AnalysisOutcome(
            ok=True,
            rows=[[diff, pct_change]],
            columns=[
                ColumnMeta(name="diff", pg_type="numeric"),
                ColumnMeta(name="pct_change", pg_type="numeric"),
            ],
            warnings=warnings,
            datasource_id=a_bundle.meta.datasource_id,
        )

    # ------------------------------------------------------------ 通用路由（doc 7 §1.6 第 2 步）

    def _route(
        self,
        *,
        local_result: AnalysisOutcome | None,
        local_block_reason: str | None,
        bundles: list[InputBundle],
        ctx: DbAgentContext,
        allow_pushdown: bool,
        run_pushdown: RunPushdown,
        build_sql: Callable[[list[InputBundle]], str],
        row_cap: int | None = None,
    ) -> AnalysisOutcome:
        """分支 1 本地→返回 local_result；2/4 下推；3 incomplete_source；5 报错。

        row_cap：下推读取行上限（读上限+1 行判定截断后存上限行，
        distinct_values 用 max_values 控制）；None 用管线默认保存上限。
        """
        truncated = any(b.meta.source_truncated for b in bundles)
        src_available = all(b.meta.executed_sql for b in bundles)
        same_as_current = all(
            b.meta.datasource_id == ctx.datasource_id for b in bundles
        )
        can_pushdown = allow_pushdown and src_available and same_as_current
        local_ok = local_result is not None
        if local_ok and not truncated:
            local_result.datasource_id = bundles[0].meta.datasource_id
            return local_result  # 分支 1
        if can_pushdown:  # 分支 2（截断）／4（本地不支持）
            sql = build_sql(bundles)
            metas = [b.meta for b in bundles]
            if row_cap is None:
                exec_outcome, updates = run_pushdown(sql, metas)
            else:
                exec_outcome, updates = run_pushdown(sql, metas, row_cap=row_cap)
            if not exec_outcome.ok:
                return AnalysisOutcome(
                    ok=False,
                    error_type=exec_outcome.error_type,
                    message=exec_outcome.message,
                    hint=exec_outcome.hint,
                )  # 分支 6：按错误返回，不回退本地
            record = updates.executions[0] if updates.executions else None
            return AnalysisOutcome(
                ok=True,
                executed_sql=sql,
                pushdown_result_ref=exec_outcome.meta.result_ref,
                pushdown_execution=record,
                pushdown_updates=updates,
                warnings=[PUSHDOWN_WARNING],
                source_truncated=bool((record or {}).get("source_truncated", False)),
                datasource_id=ctx.datasource_id,
            )
        if local_ok:  # 分支 3
            raise AnalysisError(
                "incomplete_source",
                "输入结果被截断，且当前不可下推（缺 allow_pushdown／源 SQL）",
            )
        raise AnalysisError(  # 分支 5
            "invalid_argument",
            f"该操作/列仅支持下推，且当前不可下推（{local_block_reason}）",
        )

    # ------------------------------------------------------------ aggregate

    def _aggregate(
        self, params: AggregateParams, bundles, ctx, allow_pushdown, run_pushdown
    ) -> AnalysisOutcome:
        if len(bundles) != 1:
            raise AnalysisError("invalid_argument", "aggregate 只接受一个输入结果")
        bundle = bundles[0]
        local_result = None
        reason = _aggregate_local_ok(params, bundle)
        if reason is None:
            local_result = _aggregate_local(params, bundle)

        def build_sql(bs: list[InputBundle]) -> str:
            b = bs[0]
            src = (b.meta.executed_sql or "").rstrip().rstrip(";")
            parts = [f"src.{_q(g)}" for g in params.group_by]
            for metric in params.metrics:
                col = f"src.{_q(metric.column)}"
                if metric.func == "sum":
                    expr = f"SUM({col}::numeric)"
                elif metric.func == "avg":
                    expr = f"AVG({col}::numeric)"
                elif metric.func == "count_distinct":
                    expr = f"COUNT(DISTINCT {col})"
                else:
                    expr = f"{metric.func.upper()}({col})"
                parts.append(f"{expr} AS {_q(f'{metric.func}_{metric.column}')}")
            group = (
                f" GROUP BY {', '.join(f'src.{_q(g)}' for g in params.group_by)}"
                if params.group_by
                else ""
            )
            return f"SELECT {', '.join(parts)} FROM ({src}) AS src{group}"

        return self._route(
            local_result=local_result,
            local_block_reason=reason,
            bundles=bundles,
            ctx=ctx,
            allow_pushdown=allow_pushdown,
            run_pushdown=run_pushdown,
            build_sql=build_sql,
        )

    # ------------------------------------------------------------ top_n

    def _top_n(self, params: TopNParams, bundles, ctx, allow_pushdown, run_pushdown):
        if len(bundles) != 1:
            raise AnalysisError("invalid_argument", "top_n 只接受一个输入结果")
        bundle = bundles[0]
        local_result = None
        reason = _top_n_local_ok(params, bundle)
        if reason is None:
            local_result = _top_n_local(params, bundle)

        def build_sql(bs: list[InputBundle]) -> str:
            b = bs[0]
            src = (b.meta.executed_sql or "").rstrip().rstrip(";")
            key = f"src.{_q(params.order_by)}"
            direction = "DESC" if params.descending else "ASC"
            out_cols = [c["name"] for c in b.meta.columns]
            comparable = all(b.family(c) in _ORD_OK for c in out_cols)
            secondary = (
                ", ".join(f"src.{_q(c)}" for c in out_cols) if comparable else ""
            )
            order = f"{key} {direction}" + (f", {secondary}" if secondary else "")
            if params.ties == "strict":
                return (
                    f"SELECT {', '.join(f'src.{_q(c)}' for c in out_cols)} "
                    f"FROM ({src}) AS src ORDER BY {order} LIMIT {params.n}"
                )
            rank_alias = _unique_alias("__dbda_rank", out_cols)
            inner = f"SELECT src.*, RANK() OVER (ORDER BY {key} {direction}) AS {_q(rank_alias)} FROM ({src}) AS src"
            return (
                f"SELECT {', '.join(f'r.{_q(c)}' for c in out_cols)} FROM ({inner}) r "
                f"WHERE r.{_q(rank_alias)} <= {params.n} ORDER BY {order.replace('src.', 'r.')}"
            )

        outcome = self._route(
            local_result=local_result,
            local_block_reason=reason,
            bundles=bundles,
            ctx=ctx,
            allow_pushdown=allow_pushdown,
            run_pushdown=run_pushdown,
            build_sql=build_sql,
        )
        if outcome.ok and outcome.executed_sql is None and local_result is not None:
            comparable = all(
                bundle.family(c["name"]) in _ORD_OK for c in bundle.meta.columns
            )
            if not comparable:
                local_result.warnings.append("排序键在边界并列，选中记录不确定")
        return outcome

    # ------------------------------------------------------------ share

    def _share(self, params: ShareParams, bundles, ctx, allow_pushdown, run_pushdown):
        if len(bundles) != 1:
            raise AnalysisError("invalid_argument", "share 只接受一个输入结果")
        bundle = bundles[0]
        local_result = None
        reason = _share_local_ok(params, bundle)
        if reason is None:
            local_result = _share_local(params, bundle)

        def build_sql(bs: list[InputBundle]) -> str:
            b = bs[0]
            src = (b.meta.executed_sql or "").rstrip().rstrip(";")
            x = f"src.{_q(params.value_column)}"
            partition = (
                f" PARTITION BY {', '.join(f'src.{_q(g)}' for g in params.group_by)}"
                if params.group_by
                else ""
            )
            denom = f"NULLIF(SUM({x}::numeric) OVER ({partition.strip()}), 0)"
            expr = f"{x}::numeric / {denom}"
            if params.round_digits is not None:
                expr = f"round({expr}::numeric, {params.round_digits})"
            parts = [f"src.{_q(g)}" for g in params.group_by] + [
                f"{expr} AS {_q('share')}"
            ]
            return f"SELECT {', '.join(parts)} FROM ({src}) AS src"

        return self._route(
            local_result=local_result,
            local_block_reason=reason,
            bundles=bundles,
            ctx=ctx,
            allow_pushdown=allow_pushdown,
            run_pushdown=run_pushdown,
            build_sql=build_sql,
        )

    # ------------------------------------------------------------ distinct_values

    def _distinct_values(
        self, params: DistinctValuesParams, bundles, ctx, allow_pushdown, run_pushdown
    ):
        if len(bundles) != 1:
            raise AnalysisError(
                "invalid_argument", "distinct_values 只接受一个输入结果"
            )
        bundle = bundles[0]
        local_result = None
        reason = _distinct_local_ok(params, bundle)
        if reason is None:
            local_result = _distinct_local(params, bundle)

        def build_sql(bs: list[InputBundle]) -> str:
            b = bs[0]
            src = (b.meta.executed_sql or "").rstrip().rstrip(";")
            col = f"src.{_q(params.column)}"
            return (
                f"SELECT DISTINCT {col} FROM ({src}) AS src "
                f"ORDER BY 1 LIMIT {params.max_values + 1}"
            )

        return self._route(
            local_result=local_result,
            local_block_reason=reason,
            bundles=bundles,
            ctx=ctx,
            allow_pushdown=allow_pushdown,
            run_pushdown=run_pushdown,
            build_sql=build_sql,
            # SQL 读 max_values+1 判截断；row_cap 让管线只存 max_values 行
            # 并置 source_truncated（§7.10.1 与探针同构）
            row_cap=params.max_values,
        )


# ---------------------------------------------------------------------------
# 本地路径可行性（返回 None=可行，否则阻断原因）
# ---------------------------------------------------------------------------


def _check_columns(
    bundle: InputBundle,
    eq_columns: list[tuple[str, str]],
    ord_columns: list[tuple[str, str]],
    arith_columns: list[tuple[str, str]],
) -> str | None:
    for column, role in eq_columns:
        if bundle.family(column) not in _EQ_OK:
            return f"{role}列 {column} 类型不支持本地判等（仅下推）"
    for column, role in ord_columns:
        if bundle.family(column) not in _ORD_OK:
            return f"{role}列 {column} 类型不支持本地排序（仅下推）"
    for column, role in arith_columns:
        if bundle.family(column) not in _ARITH_OK:
            return f"{role}列 {column} 算术仅支持下推"
    for column, role in [*eq_columns, *ord_columns, *arith_columns]:
        pos = bundle.pos(column)
        if any(_nonfinite(bundle.family(column), row[pos]) for row in bundle.rows):
            return f"{role}列 {column} 含非有限值（仅下推）"
    return None


def _aggregate_local_ok(params: AggregateParams, bundle: InputBundle) -> str | None:
    eq = [(g, "分组") for g in params.group_by]
    ord_cols: list[tuple[str, str]] = []
    arith: list[tuple[str, str]] = []
    for m in params.metrics:
        if m.func in ("count", "count_distinct"):
            eq.append((m.column, "计数"))
        elif m.func in ("min", "max"):
            ord_cols.append((m.column, "聚合"))
        else:
            arith.append((m.column, "聚合"))
    return _check_columns(bundle, eq, ord_cols, arith)


def _top_n_local_ok(params: TopNParams, bundle: InputBundle) -> str | None:
    return _check_columns(bundle, [], [(params.order_by, "排序")], [])


def _share_local_ok(params: ShareParams, bundle: InputBundle) -> str | None:
    return _check_columns(
        bundle,
        [(g, "分组") for g in params.group_by],
        [],
        [(params.value_column, "占比")],
    )


def _distinct_local_ok(params: DistinctValuesParams, bundle: InputBundle) -> str | None:
    # 截断选择依赖排序：文本族等不可排序类型整族仅下推（doc 7 §1.2）
    family = bundle.family(params.column)
    if family not in _ORD_OK:
        return f"列 {params.column} 类型不支持本地排序截断（仅下推）"
    pos = bundle.pos(params.column)
    if any(_nonfinite(family, row[pos]) for row in bundle.rows):
        return f"列 {params.column} 含非有限值（仅下推）"
    return None


# ---------------------------------------------------------------------------
# 本地计算实现
# ---------------------------------------------------------------------------


def _grouped_rows(
    bundle: InputBundle, group_by: list[str]
) -> tuple[list[tuple[Any, ...]], dict[tuple[Any, ...], list[list[Any]]]]:
    groups: dict[tuple[Any, ...], list[list[Any]]] = {}
    order: list[tuple[Any, ...]] = []
    positions = [bundle.pos(g) for g in group_by]
    for row in bundle.rows:
        key = tuple(
            _restore(bundle.family(g), row[p]) for g, p in zip(group_by, positions)
        )
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(row)
    if not group_by:
        groups[()] = list(bundle.rows)
        order = [()]
    return order, groups


def _aggregate_local(params: AggregateParams, bundle: InputBundle) -> AnalysisOutcome:
    """镜像 PG：无分组键空输入→1 行（count=0、sum/avg/min/max=NULL）。"""
    order, groups = _grouped_rows(bundle, params.group_by)
    # 分组键与 min/max 保留输入列类型（数值键标成 text 会让后续本地分析误拒）
    columns = [
        ColumnMeta(name=g, pg_type=bundle.meta.columns[bundle.pos(g)]["pg_type"])
        for g in params.group_by
    ]
    for m in params.metrics:
        if m.func in ("sum", "avg"):
            out_type = "numeric"
        elif m.func in ("count", "count_distinct"):
            out_type = "int8"
        else:  # min/max：沿用输入列类型
            out_type = bundle.meta.columns[bundle.pos(m.column)]["pg_type"]
        columns.append(ColumnMeta(name=f"{m.func}_{m.column}", pg_type=out_type))
    rows: list[list[Any]] = []
    for key in order:
        rows_in = groups.get(key) or []
        out: list[Any] = list(key)
        for m in params.metrics:
            pos = bundle.pos(m.column)
            family = bundle.family(m.column)
            values = [row[pos] for row in rows_in if row[pos] is not None]
            if m.func == "count":
                out.append(len(values))
            elif m.func == "count_distinct":
                out.append(len({_restore(family, v) for v in values}))
            elif m.func in ("sum", "avg"):
                if not values:
                    out.append(None)
                    continue
                total = exact_sum([_as_decimal(v) for v in values])
                out.append(
                    total
                    if m.func == "sum"
                    else divide(total, Decimal(len(values)), 10)
                )
            else:  # min/max（键含 NULL 位次，PG 语义 NULL 不参与聚合）
                usable = [v for v in values if v is not None]
                if not usable:
                    out.append(None)
                    continue
                keys = [_sort_key(family, v, False) for v in usable]
                out.append((min if m.func == "min" else max)(keys)[1])
        rows.append(out)
    return AnalysisOutcome(ok=True, rows=rows, columns=columns)


def _top_n_local(params: TopNParams, bundle: InputBundle) -> AnalysisOutcome:
    import functools

    family = bundle.family(params.order_by)
    pos = bundle.pos(params.order_by)
    out_cols = [c["name"] for c in bundle.meta.columns]
    comparable = all(bundle.family(c) in _ORD_OK for c in out_cols)
    tie_pos = [bundle.pos(c) for c in out_cols] if comparable else []

    def _cmp_value(a: Any, b: Any, descending: bool, fam: TypeFamily) -> int:
        """值比较；None 位次按 PG 默认（ASC→LAST，DESC→FIRST）。"""
        if a is None and b is None:
            return 0
        if a is None:
            return -1 if descending else 1
        if b is None:
            return 1 if descending else -1
        ka, kb = _restore(fam, a), _restore(fam, b)
        if ka == kb:
            return 0
        result = 1 if ka > kb else -1
        return -result if descending else result

    def row_cmp(a: list[Any], b: list[Any]) -> int:
        primary = _cmp_value(a[pos], b[pos], params.descending, family)
        if primary != 0:
            return primary
        for i, p in enumerate(tie_pos):  # 次键：升序、可比较列（含 NULL 位次）
            tie = _cmp_value(a[p], b[p], False, bundle.family(out_cols[i]))
            if tie != 0:
                return tie
        return 0

    ordered = sorted(bundle.rows, key=functools.cmp_to_key(row_cmp))
    if params.ties == "strict":
        chosen = ordered[: params.n]
    else:
        chosen = list(ordered[: params.n])
        if ordered and len(ordered) > params.n:
            boundary = _restore(family, ordered[params.n - 1][pos])
            i = params.n
            while i < len(ordered) and _restore(family, ordered[i][pos]) == boundary:
                chosen.append(ordered[i])
                i += 1
    columns = [
        ColumnMeta(name=c["name"], pg_type=c["pg_type"]) for c in bundle.meta.columns
    ]
    return AnalysisOutcome(ok=True, rows=chosen, columns=columns)


def _share_local(params: ShareParams, bundle: InputBundle) -> AnalysisOutcome:
    x_pos = bundle.pos(params.value_column)
    order, groups = _grouped_rows(bundle, params.group_by)
    columns = [
        ColumnMeta(name=g, pg_type=bundle.meta.columns[bundle.pos(g)]["pg_type"])
        for g in params.group_by
    ]
    columns.append(ColumnMeta(name="share", pg_type="numeric"))
    rows: list[list[Any]] = []
    warned = False
    for key in order:
        rows_in = groups.get(key) or []
        values = [row[x_pos] for row in rows_in if row[x_pos] is not None]
        total = exact_sum([_as_decimal(v) for v in values]) if values else None
        if not values or total == 0:
            warned = True
            for row in rows_in:
                out = list(key) + [None]
                rows.append(out)
            continue
        for row in rows_in:
            value = row[x_pos]
            if value is None:
                rows.append(list(key) + [None])
                continue
            share = divide(_as_decimal(value), total, params.round_digits or 10)
            if params.round_digits is not None:
                share = _round_half_up(share, params.round_digits)
            rows.append(list(key) + [share])
    warnings: list[str] = []
    if warned:
        warnings.append("分母为零或无有效值，占比无定义")
    return AnalysisOutcome(ok=True, rows=rows, columns=columns, warnings=warnings)


def _distinct_local(
    params: DistinctValuesParams, bundle: InputBundle
) -> AnalysisOutcome:
    family = bundle.family(params.column)
    pos = bundle.pos(params.column)
    # 恢复值作键判等（Decimal("1.0")==Decimal("1.00") 哈希相同）
    seen: dict[Any, Any] = {}
    for row in bundle.rows:
        value = row[pos]
        if value is None:
            seen.setdefault(None, None)
            continue
        restored = _restore(family, value)
        seen.setdefault(restored, restored)
    keys = sorted(
        seen.items(),
        key=lambda kv: (kv[1] is None, kv[1] if kv[1] is not None else 0),
    )
    truncated = len(keys) > params.max_values
    chosen = keys[: params.max_values]
    return AnalysisOutcome(
        ok=True,
        rows=[[v] for _k, v in chosen],
        columns=[
            ColumnMeta(name=params.column, pg_type=bundle.meta.columns[pos]["pg_type"])
        ],
        warnings=["取值多于 limit，只有列出的值有证据"] if truncated else [],
        source_truncated=truncated,
    )


def _unique_alias(base: str, existing: list[str]) -> str:
    """rank 别名冲突时递增后缀（doc 7 §1.4）。"""
    alias = base
    i = 2
    while alias in existing:
        alias = f"{base}_{i}"
        i += 1
    return alias
