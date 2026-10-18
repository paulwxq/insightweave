"""SQL 表引用 schema 范围检查测试（doc 4 §4.7）

登记 selected_schemas 之外的表引用在校验入口（EXPLAIN 与执行之前）被
拒绝——包括不读列信息、绕过 get_columns 的限定引用
（`SELECT COUNT(*) FROM private.secret`），以及经 pg_catalog 隐式解析的
裸名（`FROM pg_class`）。标识符按 PG 规则归一：未加引号折叠小写、
加双引号保留原样。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.modules.db_deepagent.services.sql_validation import DbSqlValidator

pytestmark = pytest.mark.unit


class _ScopedDb:
    """带 allowed_schemas、目录核对与 EXPLAIN 调用记录的假业务库。

    located: ``{(name, schema)}`` 命中集合——locate_table_schemas 据此
    返回 ``名字 -> [schema, ...]``；None 表示目录查询失败。
    """

    def __init__(self, allowed, located=None, locate_fails=False):
        self.allowed_schemas = allowed
        self.located = located or set()
        self.locate_fails = locate_fails
        self.explain_calls: list[str] = []
        self.locate_calls: list[tuple[list[str], list[str]]] = []

    def locate_table_schemas(self, table_names, schemas):
        if self.locate_fails:
            raise RuntimeError("目录不可用")
        self.locate_calls.append((list(table_names), list(schemas)))
        out: dict[str, list[str]] = {}
        for name, schema in self.located:
            if name in table_names and schema in schemas:
                out.setdefault(name, []).append(schema)
        return out

    def explain(self, sql, timeout_s=None):
        self.explain_calls.append(sql)
        return []


@pytest.fixture()
def validator(db_agent_config):
    db = _ScopedDb(["public"], located={("orders", "public")})
    return DbSqlValidator(db_agent_config.validation, db, explain_timeout_s=1.0), db


class TestQualifiedReference:
    def test_out_of_scope_qualified_table_rejected_before_explain(self, validator):
        """限定越界引用在 EXPLAIN 前拒绝（不读列信息也拦）。"""
        v, db = validator
        outcome = v.validate("SELECT COUNT(*) AS n FROM private.secret")
        assert outcome.valid is False
        assert outcome.layer == "security"
        assert "schema_out_of_scope" in outcome.error_codes
        assert "private.secret" in outcome.errors[0]
        assert db.explain_calls == []  # 未进 EXPLAIN

    def test_in_scope_qualified_table_passes(self, validator):
        v, db = validator
        outcome = v.validate("SELECT COUNT(*) AS n FROM public.orders")
        assert outcome.valid is True
        assert db.explain_calls == ["SELECT COUNT(*) AS n FROM public.orders"]

    def test_three_part_reference_uses_middle_segment(self, validator):
        v, db = validator
        outcome = v.validate("SELECT 1 AS x FROM mydb.private.t")
        assert outcome.valid is False
        assert "mydb.private.t" in outcome.errors[0]
        assert db.explain_calls == []

    def test_join_with_mixed_schemas_lists_violation(self, validator):
        v, _ = validator
        outcome = v.validate(
            "SELECT a.id FROM public.orders a JOIN private.secret b ON a.id = b.id"
        )
        assert outcome.valid is False
        assert "private.secret" in outcome.errors[0]
        assert "public.orders" not in outcome.errors[0]

    def test_subquery_reference_checked(self, validator):
        v, db = validator
        outcome = v.validate(
            "SELECT (SELECT COUNT(*) FROM private.secret) AS n FROM public.orders"
        )
        assert outcome.valid is False
        assert db.explain_calls == []


class TestIdentifierCaseFolding:
    """PG 规则：未加引号折叠小写比较，加双引号保留原样。"""

    def test_unquoted_uppercase_schema_folds_to_registration(self, validator):
        """登记 public 时 `FROM PUBLIC.orders` 可正常解释，不得误判越界。"""
        v, db = validator
        outcome = v.validate("SELECT COUNT(*) AS n FROM PUBLIC.orders")
        assert outcome.valid is True
        assert db.explain_calls

    def test_quoted_schema_kept_verbatim(self, db_agent_config):
        db = _ScopedDb(["Sales"], located={("x", "Sales")})
        v = DbSqlValidator(db_agent_config.validation, db, explain_timeout_s=1.0)
        assert v.validate('SELECT 1 AS x FROM "Sales".x').valid is True
        # 未加引号 Sales 折叠为 sales，登记 "Sales" 精确名不匹配 → 拒绝
        assert v.validate("SELECT 1 AS x FROM Sales.x").valid is False

    def test_quoted_out_of_scope_kept_verbatim(self, validator):
        v, _ = validator
        outcome = v.validate('SELECT 1 AS x FROM "Private".t')
        assert outcome.valid is False
        assert '"Private".t' in outcome.errors[0] or "Private.t" in outcome.errors[0]


class TestUnqualifiedBareName:
    def test_bare_name_resolving_into_registration_passes(self, validator):
        """裸名在登记范围内可解析（目录命中 public）→ 放行。"""
        v, db = validator
        outcome = v.validate("SELECT COUNT(*) AS n FROM orders")
        assert outcome.valid is True
        assert db.locate_calls  # 核对确实发生

    def test_bare_system_table_resolving_to_pg_catalog_rejected(self, db_agent_config):
        """用户复现场景：`FROM pg_class` 经隐式 pg_catalog 解析，越界拒绝。"""
        db = _ScopedDb(["public"], located={("pg_class", "pg_catalog")})
        v = DbSqlValidator(db_agent_config.validation, db, explain_timeout_s=1.0)
        outcome = v.validate("SELECT COUNT(*) AS n FROM pg_class")
        assert outcome.valid is False
        assert "schema_out_of_scope" in outcome.error_codes
        assert "pg_class" in outcome.errors[0]
        assert "pg_catalog" in outcome.errors[0]
        assert db.explain_calls == []  # EXPLAIN 前拦截

    def test_bare_name_in_both_registration_and_pg_catalog_rejected(
        self, db_agent_config
    ):
        """两边同名：有效搜索路径是 pg_catalog, <登记清单>，系统目录在前。

        用户复现：登记 schema 存在同名表时不得因登记命中而放行——
        裸名实际解析到系统表（真实 PG 已确认解析优先级）。
        """
        db = _ScopedDb(
            ["public"],
            located={("pg_class", "public"), ("pg_class", "pg_catalog")},
        )
        v = DbSqlValidator(db_agent_config.validation, db, explain_timeout_s=1.0)
        outcome = v.validate("SELECT COUNT(*) AS n FROM pg_class")
        assert outcome.valid is False
        assert "schema_out_of_scope" in outcome.error_codes
        assert "pg_catalog" in outcome.errors[0]
        assert db.explain_calls == []  # EXPLAIN 前拦截

    def test_bare_unknown_table_passes_to_explain(self, validator):
        """两处皆无（表不存在）：放行，由 EXPLAIN 报 relation 不存在兜底。"""
        v, db = validator
        outcome = v.validate("SELECT COUNT(*) AS n FROM no_such_table")
        assert outcome.valid is True
        assert db.explain_calls  # 进入 EXPLAIN 由数据库报错

    def test_cte_alias_excluded_from_bare_name_resolution(self, validator):
        """CTE 别名不是物理表，不参与目录核对。"""
        v, db = validator
        outcome = v.validate("WITH x AS (SELECT 1 AS id) SELECT COUNT(*) AS n FROM x")
        assert outcome.valid is True
        assert db.locate_calls == []  # CTE 名未进目录核对

    def test_shadowing_cte_body_reference_still_checked(self, db_agent_config):
        """用户复现：CTE 与系统表同名时，**定义体内**的引用指向物理表。

        `WITH pg_class AS (... FROM pg_class) SELECT n FROM pg_class`——
        内层 FROM pg_class 不在该 CTE 的可见范围（非递归 CTE 对自身不可见），
        仍需目录核对并拒绝；外层引用才是 CTE。
        """
        db = _ScopedDb(["public"], located={("pg_class", "pg_catalog")})
        v = DbSqlValidator(db_agent_config.validation, db, explain_timeout_s=1.0)
        outcome = v.validate(
            "WITH pg_class AS (SELECT COUNT(*) AS n FROM pg_class) "
            "SELECT n FROM pg_class"
        )
        assert outcome.valid is False
        assert "schema_out_of_scope" in outcome.error_codes
        assert "pg_class" in outcome.errors[0]
        assert db.explain_calls == []

    def test_nested_cte_inner_reference_visible(self, validator):
        """嵌套 CTE：a 定义体内的 b 引用可见（b 在其定义体外），不查目录。"""
        v, db = validator
        outcome = v.validate(
            "WITH a AS (WITH b AS (SELECT 1 AS v) SELECT v FROM b) "
            "SELECT COUNT(*) AS n FROM a"
        )
        assert outcome.valid is True
        assert db.locate_calls == []

    def test_nested_cte_outer_name_not_visible_inside_its_body(self, db_agent_config):
        """嵌套 CTE：b 定义体（=a 定义体内更深一层）中引用 a 不可见。"""
        db = _ScopedDb(["public"], located={("a", "pg_catalog")})
        v = DbSqlValidator(db_agent_config.validation, db, explain_timeout_s=1.0)
        outcome = v.validate(
            "WITH a AS (WITH b AS (SELECT 1 AS v) "
            "SELECT COUNT(*) AS n FROM a) SELECT n FROM a"
        )
        # b 定义体外的 a 引用（在 a 自身定义体内）指向物理表 → 拒绝；
        # 最外层的 a 是 CTE 引用
        assert outcome.valid is False
        assert "a" in outcome.errors[0]

    def test_later_declared_cte_not_visible_to_earlier_body(self, db_agent_config):
        """用户复现：非递归 WITH 中后声明的同名 CTE 对前面定义体不可见。

        `WITH x AS (... FROM pg_class), pg_class AS (...) SELECT n FROM x`
        ——x 体内的 pg_class 指向实际系统表，必须检查并拒绝。
        """
        db = _ScopedDb(["public"], located={("pg_class", "pg_catalog")})
        v = DbSqlValidator(db_agent_config.validation, db, explain_timeout_s=1.0)
        outcome = v.validate(
            "WITH x AS (SELECT COUNT(*) AS n FROM pg_class), "
            "pg_class AS (SELECT 1 AS n) "
            "SELECT n FROM x"
        )
        assert outcome.valid is False
        assert "schema_out_of_scope" in outcome.error_codes
        assert "pg_class" in outcome.errors[0]
        assert db.explain_calls == []

    def test_earlier_declared_cte_visible_to_later_body(self, validator):
        """前向引用（后声明者引用更早声明者）是合法 CTE 引用，不查目录。"""
        v, db = validator
        outcome = v.validate(
            "WITH a AS (SELECT 1 AS n), b AS (SELECT n FROM a) "
            "SELECT COUNT(*) AS n FROM b"
        )
        assert outcome.valid is True
        assert db.locate_calls == []

    def test_recursive_cte_self_reference_treated_as_cte(self, validator):
        """递归 CTE 定义体内的自引用按 CTE 处理（PG 语义），不查目录。"""
        v, db = validator
        outcome = v.validate(
            "WITH RECURSIVE t(n) AS "
            "(SELECT 1 AS n UNION ALL SELECT n + 1 FROM t WHERE n < 3) "
            "SELECT COUNT(*) AS n FROM t"
        )
        assert outcome.valid is True
        assert db.locate_calls == []

    def test_catalog_lookup_failure_fails_closed(self, db_agent_config):
        db = _ScopedDb(["public"], locate_fails=True)
        v = DbSqlValidator(db_agent_config.validation, db, explain_timeout_s=1.0)
        outcome = v.validate("SELECT COUNT(*) AS n FROM orders")
        assert outcome.valid is False
        assert "schema_scope_lookup_failed" in outcome.error_codes
        assert db.explain_calls == []

    def test_uppercase_cte_declaration_matched_after_folding(self, db_agent_config):
        """用户复现：CTE 声明 PG_CLASS、引用 pg_class——统一折叠后按
        CTE 排除，不再误判为读取系统表。"""
        db = _ScopedDb(["public"], located={("pg_class", "pg_catalog")})
        v = DbSqlValidator(db_agent_config.validation, db, explain_timeout_s=1.0)
        outcome = v.validate(
            "WITH PG_CLASS AS (SELECT 1 AS n) SELECT COUNT(*) AS n FROM pg_class"
        )
        assert outcome.valid is True
        assert db.locate_calls == []  # 折叠后按 CTE 排除，未进目录核对

    def test_quoted_cte_not_matched_by_unquoted_reference(self, db_agent_config):
        """加引号的 CTE 名保留原样：未引号引用折叠后不等 → 物理表
        pg_class → 解析到 pg_catalog，越界拒绝（PG 语义）。"""
        db = _ScopedDb(["public"], located={("pg_class", "pg_catalog")})
        v = DbSqlValidator(db_agent_config.validation, db, explain_timeout_s=1.0)
        outcome = v.validate(
            'WITH "PG_CLASS" AS (SELECT 1 AS n) SELECT COUNT(*) AS n FROM pg_class'
        )
        assert outcome.valid is False
        assert "schema_out_of_scope" in outcome.error_codes


class TestNoRestriction:
    def test_no_allowed_schemas_does_not_restrict(self, db_agent_config):
        db = _ScopedDb(None)
        v = DbSqlValidator(db_agent_config.validation, db, explain_timeout_s=1.0)
        outcome = v.validate("SELECT COUNT(*) AS n FROM private.secret")
        assert outcome.valid is True

    def test_db_without_attribute_does_not_restrict(self, db_agent_config):
        """旧假件/未装配 allowed_schemas 的服务：保持不限制（既有行为）。"""
        db = SimpleNamespace(explain=lambda sql, timeout_s=None: [])
        v = DbSqlValidator(db_agent_config.validation, db, explain_timeout_s=1.0)
        outcome = v.validate("SELECT COUNT(*) AS n FROM private.secret")
        assert outcome.valid is True
