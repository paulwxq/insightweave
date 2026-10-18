"""DbSqlValidator：三层校验（docs/v4/5 §6.7）

复制 src/tools/validation/sql_validation.py 的三层检查逻辑与配置键，改动：
- 构造注入 BusinessDbService 与 ValidationConfig，语义层调用 db.explain(...)
- 返回 ValidationOutcome，error_codes 为机器可读错误码便于断言
- 继承旧安全检查的已知局限：关键字匹配作用于整条 SQL 的大写文本，字符串字面量
  中出现 DELETE 等词也会被拒绝（第一阶段不放宽；只读事务与只读角色兜底）
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

import sqlparse

from src.modules.db_deepagent.config import ValidationConfig
from src.modules.db_deepagent.errors import DbExecutionError
from src.modules.db_deepagent.services.business_db import BusinessDbService
from src.utils.logger import get_module_logger

logger = get_module_logger("nl2sql.db_deepagent.sql_validation")


@dataclass(frozen=True)
class ValidationOutcome:
    """三层校验结果（§6.7）"""

    valid: bool
    layer: str  # syntax / security / semantic / all_passed
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    explain_plan: Optional[str] = None
    error_codes: list[str] = field(default_factory=list)


class DbSqlValidator:
    """SQL 三层校验（语法 / 安全 / EXPLAIN 语义）"""

    def __init__(
        self,
        config: ValidationConfig,
        db: BusinessDbService,
        *,
        explain_timeout_s: float,
    ):
        self._cfg = config
        self._db = db
        self._explain_timeout_s = explain_timeout_s
        security = config.security
        self._allowed_types = [t.upper() for t in security.allowed_statement_types]
        self._keywords = [kw.upper() for kw in security.forbidden_keywords]
        self._allow_comments = security.allow_comments
        self._allow_multiple = config.syntax.allow_multiple_statements
        warnings_cfg = config.semantic.warnings
        self._seq_scan_warn = warnings_cfg.seq_scan_warn
        self._nested_loop_warn = warnings_cfg.nested_loop_warn
        self._estimated_rows_threshold = warnings_cfg.estimated_rows_threshold

    def validate(self, sql: str) -> ValidationOutcome:
        """依次执行三层检查，任一层失败即返回（§6.7 与旧实现一致）。

        语法层之后、EXPLAIN 之前追加表引用 schema 范围检查（doc 4 §4.7：
        登记 selected_schemas 之外的对象不得进入校验与执行链路——
        `SELECT ... FROM private.secret` 这类不读列信息的语句也在此拦截）。
        """
        if config_enabled(self._cfg.enable_syntax_check):
            outcome = self._check_syntax(sql)
            if not outcome.valid:
                return outcome
        else:
            outcome = ValidationOutcome(valid=True, layer="syntax")

        scope = self._check_schema_scope(sql)
        if not scope.valid:
            return scope

        if config_enabled(self._cfg.enable_security_check):
            security = self._check_security(sql)
            if not security.valid:
                return security
        if config_enabled(self._cfg.enable_semantic_check):
            semantic = self._check_semantics(sql)
            if not semantic.valid:
                return semantic
            # 语义层通过时带上它累积的警告与计划
            return ValidationOutcome(
                valid=True,
                layer="all_passed",
                errors=[],
                warnings=semantic.warnings,
                explain_plan=semantic.explain_plan,
            )
        return ValidationOutcome(valid=True, layer="all_passed")

    # ---------------------------------------------------------------- 语法

    def _check_syntax(self, sql: str) -> ValidationOutcome:
        errors: list[str] = []
        codes: list[str] = []
        warnings: list[str] = []
        try:
            parsed = sqlparse.parse(sql)
            if not parsed:
                errors.append("SQL 解析失败：无法识别的 SQL 语句")
                codes.append("unparseable")
                return ValidationOutcome(False, "syntax", errors, warnings, None, codes)
            if len(parsed) > 1:
                if not self._allow_multiple:
                    errors.append("不允许执行多条 SQL 语句")
                    codes.append("multiple_statements")
                    return ValidationOutcome(
                        False, "syntax", errors, warnings, None, codes
                    )
                warnings.append("检测到多条 SQL 语句")
            if not parsed[0].get_type():
                errors.append("无法识别的 SQL 类型，可能不是合法的 SQL 语句")
                codes.append("unknown_statement_type")
        except Exception as exc:  # noqa: BLE001（语法层异常统一按失败上报）
            errors.append(f"语法解析错误：{exc}")
            codes.append("unparseable")
        return (
            ValidationOutcome(False, "syntax", errors, warnings, None, codes)
            if errors
            else ValidationOutcome(True, "syntax", [], warnings)
        )

    # ---------------------------------------------------------------- 安全

    @staticmethod
    def _identifier_text(identifier) -> tuple[str, bool]:
        """(文本, 是否加引号)；未加引号的标识符按 PG 规则折叠小写。"""
        if identifier is None:
            return "", False
        raw = identifier.name if hasattr(identifier, "name") else str(identifier)
        quoted = bool(getattr(identifier, "quoted", False))
        return (raw if quoted else raw.lower()), quoted

    @staticmethod
    def _visible_cte_names(table) -> set:
        """该表引用处按 PG 作用域规则**可见**的 CTE 名（非全局排除）。

        自内向外的父链：进入 exp.CTE（处于其定义体内）时记 pending；遇到
        某查询层的 WITH 时——pending 命中表示引用位于该 CTE 定义体内：

        - 非递归 WITH：PG 规则为"CTE 定义体只能引用**同层更早声明**的
          CTE"（自身与后声明者不可见——`WITH x AS (... pg_class),
          pg_class AS (...)` 中 x 体内的 pg_class 指物理表）；
        - 递归 WITH：同层全部 CTE 互相可见（含自引用）。

        引用不在任何定义体内时本层全部可见；随后清空 pending（该 WITH
        已消化，更外层 WITH 不受影响）。嵌套 CTE（定义体内再 WITH）
        天然正确：内层 WITH 先于外层 CTE 节点出现在父链上。
        """
        from sqlglot import exp

        names: set = set()
        pending = None
        node = table.parent
        while node is not None:
            if isinstance(node, exp.CTE):
                pending = node
            # sqlglot 30.x：WITH 挂在 args["with_"]（带下划线）
            with_expr = (
                node.args.get("with_") if isinstance(node, exp.Expression) else None
            )
            if with_expr is not None:
                recursive = bool(with_expr.args.get("recursive"))
                exprs = list(with_expr.expressions)
                pending_index = None
                if pending is not None:
                    for index, cte in enumerate(exprs):
                        if cte is pending:
                            pending_index = index
                            break
                for index, cte in enumerate(exprs):
                    if pending_index is None:
                        names.add(cte.alias_or_name)  # 不在定义体内：全部可见
                    elif recursive:
                        names.add(cte.alias_or_name)  # 递归：互相可见
                    elif index < pending_index:
                        names.add(cte.alias_or_name)  # 非递归：仅更早声明者
                pending = None
            node = node.parent
        return names

    def _check_schema_scope(self, sql: str) -> ValidationOutcome:
        """表引用 schema 范围检查（doc 4 §4.7，EXPLAIN/执行之前）。

        复用 sqlglot 解析（与守卫同一解析器）提取全部表引用，按 PostgreSQL
        标识符规则归一（未加引号折叠小写，加双引号保留原样）：

        - 限定名（schema.table / db.schema.table）：schema 段必须 ∈
          allowed_schemas，否则拒绝；
        - 非限定名（排除**该引用处可见的** CTE 名，按作用域判定而非全局
          排除——`WITH pg_class AS (... FROM pg_class)` 定义体内的引用
          指向物理表，仍需检查；CTE 名与引用经 normalize_identifiers
          统一折叠后匹配，`WITH PG_CLASS ... FROM pg_class` 也是 CTE）：
          连接的有效 search_path 为
          ``pg_catalog, <登记清单>``，pg_catalog 在前，裸名会隐式解析到
          登记范围外。对裸名查目录核对实际解析（按有效搜索路径顺序，
          pg_catalog 在登记清单之前——**两边同名时实际解析到系统表**，
          登记命中不能放行）：pg_catalog 命中 → 拒绝；否则登记范围内
          可解析 → 放行；两处皆无 → 放行（表不存在由 EXPLAIN 报错兜底）。
        allowed_schemas 为 None（未按登记装配）时不限制；解析失败跳过
        （语法层与守卫的 unparseable 拦截兜底）。
        """
        allowed = getattr(self._db, "allowed_schemas", None)
        if not allowed:
            return ValidationOutcome(True, "syntax")
        try:
            from sqlglot import exp, parse
            from sqlglot.optimizer.normalize_identifiers import normalize_identifiers

            # 分析 AST 统一按 PG 规则折叠未引号标识符（含 CTE 名/列名/
            # 别名），双引号保留——`WITH PG_CLASS ... FROM pg_class` 折叠后
            # 声明与引用一致，按 CTE 排除，不再误判为读取系统表
            statements = [
                normalize_identifiers(s, dialect="postgres") if s else s
                for s in parse(sql, read="postgres")
            ]
        except Exception:  # noqa: BLE001（解析失败交由语法层/守卫拦截）
            return ValidationOutcome(True, "syntax")

        violations: list[str] = []
        unqualified: list[str] = []  # 裸名（已按 PG 规则归一）
        for statement in statements:
            for table in statement.find_all(exp.Table) if statement else []:
                catalog, _ = self._identifier_text(table.args.get("catalog"))
                schema, _ = self._identifier_text(table.args.get("db"))
                name, _ = self._identifier_text(table.args.get("this"))
                if catalog and not schema:
                    # 三段引用（db.schema.table）：PG 无跨库，schema 取中段
                    schema = catalog
                if schema:
                    if schema not in allowed:
                        violations.append(
                            f"{catalog + '.' if catalog else ''}{schema}.{name}"
                        )
                elif name not in self._visible_cte_names(table):
                    unqualified.append(name)

        if unqualified:
            resolved = self._locate_tables(sorted(set(unqualified)), allowed)
            if resolved is None:
                return ValidationOutcome(
                    False,
                    "security",
                    ["目录查询失败，无法核对非限定表名的解析 schema"],
                    [],
                    None,
                    ["schema_scope_lookup_failed"],
                )
            for bare in sorted(set(unqualified)):
                if resolved.get(f"pg_catalog::{bare}"):
                    # 有效搜索路径 pg_catalog 在登记清单之前：两边同名时
                    # 实际解析到系统表，登记命中不能放行
                    violations.append(
                        f"{bare}（非限定名实际解析到 pg_catalog，不在登记范围）"
                    )
                elif resolved.get(bare):
                    continue  # pg_catalog 未命中，登记范围内可解析
                # 两处皆无：表不存在，交给 EXPLAIN 报错

        if violations:
            return ValidationOutcome(
                False,
                "security",
                [
                    "SQL 引用了登记 selected_schemas 之外的表: "
                    + ", ".join(sorted(set(violations)))
                    + f"（本源登记范围: {', '.join(allowed)}）"
                ],
                [],
                None,
                ["schema_out_of_scope"],
            )
        return ValidationOutcome(True, "syntax")

    def _locate_tables(
        self, names: list[str], allowed: list[str]
    ) -> dict[str, list[str]] | None:
        """查 information_schema.tables 核对裸名实际解析：返回
        ``名字 -> [schema, ...]``（pg_catalog 命中以 ``pg_catalog::名`` 键）。

        目录查询失败返回 None（fail closed：无法核对即拒绝校验）。
        """
        try:
            located = self._db.locate_table_schemas(names, [*allowed, "pg_catalog"])
        except Exception as exc:  # noqa: BLE001（后端故障按校验失败上报）
            logger.warning("schema 范围目录查询失败: %s", exc)
            return None
        result: dict[str, list[str]] = {}
        for name, schemas in located.items():
            for schema in schemas:
                key = f"pg_catalog::{name}" if schema == "pg_catalog" else name
                result.setdefault(key, []).append(schema)
        return result

    def _check_security(self, sql: str) -> ValidationOutcome:
        errors: list[str] = []
        codes: list[str] = []
        sql_upper = sql.upper()
        try:
            parsed = sqlparse.parse(sql)
            if parsed:
                stmt_type = parsed[0].get_type()
                if stmt_type and stmt_type not in self._allowed_types:
                    errors.append(
                        f"不允许的 SQL 类型：{stmt_type}，只允许：{', '.join(self._allowed_types)}"
                    )
                    codes.append(f"disallowed_type:{stmt_type}")
        except Exception as exc:  # noqa: BLE001
            logger.warning("SQL 类型检查失败: %s", exc)
        for keyword in self._keywords:
            if re.search(rf"\b{re.escape(keyword)}\b", sql_upper):
                errors.append(f"禁止使用关键字：{keyword}")
                codes.append(f"forbidden_keyword:{keyword}")
        if not self._allow_comments and ("--" in sql or "/*" in sql):
            errors.append("SQL 中不允许包含注释（防止注入）")
            codes.append("comment_not_allowed")
        return (
            ValidationOutcome(False, "security", errors, [], None, codes)
            if errors
            else ValidationOutcome(True, "security")
        )

    # ---------------------------------------------------------------- 语义

    def _check_semantics(self, sql: str) -> ValidationOutcome:
        errors: list[str] = []
        warnings: list[str] = []
        try:
            rows = self._db.explain(sql, timeout_s=self._explain_timeout_s)
        except DbExecutionError as exc:
            errors.append(exc.message)
            codes = [f"explain_failed:{exc.error_type}"]
            return ValidationOutcome(False, "semantic", errors, warnings, None, codes)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"语义验证失败：{type(exc).__name__}")
            return ValidationOutcome(
                False, "semantic", errors, warnings, None, ["explain_failed"]
            )
        plan = "\n".join(str(row[0]) for row in rows)
        plan_upper = plan.upper()
        if self._seq_scan_warn and "SEQ SCAN" in plan_upper:
            warnings.append("检测到顺序扫描（Seq Scan），可能影响性能")
        if (
            self._nested_loop_warn
            and "NESTED LOOP" in plan_upper
            and "JOIN" not in sql.upper()
        ):
            warnings.append("可能存在笛卡尔积，请检查 JOIN 条件")
        if self._estimated_rows_threshold:
            for match in re.finditer(r"rows=(\d+)", plan):
                estimated = int(match.group(1))
                if estimated > self._estimated_rows_threshold:
                    warnings.append(
                        f"预估返回行数过多：{estimated} 行（阈值：{self._estimated_rows_threshold}）"
                    )
                    break
        return ValidationOutcome(True, "semantic", [], warnings, plan)


def config_enabled(flag: bool) -> bool:
    """配置开关（显式书写便于阅读）。"""
    return flag
