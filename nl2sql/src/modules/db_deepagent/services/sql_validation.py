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
        """依次执行三层检查，任一层失败即返回（§6.7 与旧实现一致）。"""
        if config_enabled(self._cfg.enable_syntax_check):
            outcome = self._check_syntax(sql)
            if not outcome.valid:
                return outcome
        else:
            outcome = ValidationOutcome(valid=True, layer="syntax")

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
