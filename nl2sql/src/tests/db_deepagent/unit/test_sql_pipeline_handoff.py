"""SqlPipeline 校验→执行闭环单元测试（docs/v4/5 §8.2–§8.4、§18.2）

FakeExecDb 提供 public.orders；时间证据 T-sep=[09-01,10-01)。
"""

from __future__ import annotations

import pytest

from src.modules.db_deepagent.services.dimension_probe import (
    DimensionProbeBuilder,
    ProbeSpec,
)
from src.modules.db_deepagent.services.sql_pipeline import LedgerView

from src.tests.db_deepagent.conftest import (
    FakeExecDb,
    make_dim_evidence,
    make_time_evidence,
)

pytestmark = pytest.mark.unit

SEP = make_time_evidence("T-sep", "2026-09-01", "2026-10-01")
QUERY_SQL = (
    "SELECT id, status, amount FROM public.orders "
    "WHERE created_at >= '2026-09-01' AND created_at < '2026-10-01'"
)


def ledger_with(ev=None, validations=None) -> LedgerView:
    return LedgerView(
        {
            "db_time_evidence": ev if ev is not None else [SEP],
            "db_dim_evidence": [],
            "db_validations": validations or {},
        }
    )


class TestValidate:
    def test_valid_records_authorization_and_guard_snapshot(
        self, pipeline_stack, db_agent_context
    ):
        pipeline, *_ = pipeline_stack
        outcome, record, update = pipeline.validate(
            QUERY_SQL, db_agent_context, ledger_with(), tool_call_id="tc1"
        )
        assert outcome.valid, outcome.errors
        assert record is not None
        assert record["status"] == "valid"
        assert record["normalized_sql"] == QUERY_SQL
        assert [e["time_evidence_id"] for e in record["guard_evidence"]["time"]] == [
            "T-sep"
        ]
        assert update.validations[record["key"]] is record

    def test_guard_failure_no_record(self, pipeline_stack, db_agent_context):
        """时间字面量无证据 → guard 失败，无授权记录。"""
        pipeline, *_ = pipeline_stack
        outcome, record, update = pipeline.validate(
            QUERY_SQL, db_agent_context, LedgerView({"db_time_evidence": []})
        )
        assert not outcome.valid
        assert outcome.layer == "guard"
        assert record is None and not update.validations

    def test_failure_revokes_existing_valid_record(
        self, pipeline_stack, db_agent_context
    ):
        """同键已有 valid 记录，证据消失后再次校验失败 → 撤销原记录。"""
        pipeline, *_ = pipeline_stack
        _, record, _ = pipeline.validate(QUERY_SQL, db_agent_context, ledger_with())
        assert record is not None
        outcome, record2, update = pipeline.validate(
            QUERY_SQL,
            db_agent_context,
            ledger_with(validations={record["key"]: record}, ev=[]),
        )
        assert not outcome.valid
        assert record2 is None
        assert update.validations[record["key"]]["status"] == "revoked"

    def test_duplicate_output_columns(self, pipeline_stack, db_agent_context):
        pipeline, *_ = pipeline_stack
        sql = "SELECT id, id FROM public.orders WHERE created_at >= '2026-09-01' AND created_at < '2026-10-01'"
        outcome, *_ = pipeline.validate(sql, db_agent_context, ledger_with())
        assert not outcome.valid
        assert "duplicate_column_names" in (outcome.error_codes or [])

    def test_empty_sql(self, pipeline_stack, db_agent_context):
        pipeline, *_ = pipeline_stack
        outcome, record, _ = pipeline.validate("  ", db_agent_context, ledger_with())
        assert not outcome.valid and record is None


class TestExecute:
    def test_not_validated(self, pipeline_stack, db_agent_context):
        pipeline, *_ = pipeline_stack
        outcome, _ = pipeline.execute(QUERY_SQL, db_agent_context, ledger_with())
        assert not outcome.ok
        assert outcome.error_type == "not_validated"

    def test_execute_after_validate(self, pipeline_stack, db_agent_context):
        """授权记录来自账本；结果落盘并登记执行记录。"""
        pipeline, db, results, *_ = pipeline_stack
        _, record, _ = pipeline.validate(QUERY_SQL, db_agent_context, ledger_with())
        outcome, update = pipeline.execute(
            QUERY_SQL,
            db_agent_context,
            ledger_with(validations={record["key"]: record}),
            tool_call_id="tc2",
        )
        assert outcome.ok, outcome.error_type
        meta = outcome.meta
        assert meta.saved_row_count == 2
        assert len(outcome.preview_rows) == 2
        exec_record = update.executions[0]
        assert exec_record["evidence_id"].startswith("E-")
        assert exec_record["result_ref"] == meta.result_ref
        # 结果文件可读且会话匹配
        page = results.read_rows(
            meta.result_ref, session_id=db_agent_context.session_id
        )
        assert page.saved_row_count == 2
        assert db.executed and db.executed[0][0] == QUERY_SQL

    def test_internal_whitespace_change_needs_revalidation(
        self, pipeline_stack, db_agent_context
    ):
        """内部空白变化即新文本（规范化后不同）→ 原授权不可用；首尾空白则等价。"""
        pipeline, *_ = pipeline_stack
        _, record, _ = pipeline.validate(QUERY_SQL, db_agent_context, ledger_with())
        changed = QUERY_SQL.replace("WHERE", " WHERE")
        outcome, _ = pipeline.execute(
            changed,
            db_agent_context,
            ledger_with(validations={record["key"]: record}),
        )
        assert outcome.error_type == "not_validated"

    def test_budget_exhausted(self, pipeline_stack, db_agent_context):
        import time as _time

        from src.modules.db_deepagent.services.run_budget import BudgetLimits

        pipeline, *_db, _r, budget, _c = pipeline_stack
        budget.open_run(
            db_agent_context.run_id,
            BudgetLimits(model_call=10, tool_call=10, db_execute=0),
            deadline_monotonic=_time.monotonic() + 600,
        )
        _, record, _ = pipeline.validate(QUERY_SQL, db_agent_context, ledger_with())
        outcome, _ = pipeline.execute(
            QUERY_SQL, db_agent_context, ledger_with(), trusted_record=record
        )
        assert not outcome.ok
        assert outcome.error_type == "budget_exhausted"
        assert outcome.stop_reason == "execute_limit"


class TestValidateAndExecute:
    def test_probe_unified_entry(self, pipeline_stack, db_agent_context):
        """探针经统一入口：形态守卫通过、run_readonly 用 max_rows=n。"""
        pipeline, db, *_ = pipeline_stack
        spec = ProbeSpec(
            mode="exact",
            schema_name="public",
            table="orders",
            column="status",
            value="已支付",
            limit=5,
        )
        sql = DimensionProbeBuilder().sql_string(spec)
        (val_outcome, exec_outcome), update = pipeline.validate_and_execute(
            sql, db_agent_context, ledger_with(), purpose="probe", probe_spec=spec
        )
        assert val_outcome.valid, val_outcome.errors
        assert exec_outcome.ok, exec_outcome.error_type
        # §7.6：run_readonly 的 max_rows = n（读 n+1 判截断）
        assert db.executed[-1][2] == 5
        assert update.executions[0]["purpose"] == "probe"

    def test_pushdown_missing_snapshot(self, pipeline_stack, db_agent_context):
        """下推输入无 guard_evidence 快照 → 失败。"""
        pipeline, *_ = pipeline_stack
        (outcome, exec_outcome), _ = pipeline.validate_and_execute(
            "SELECT COUNT(*) FROM (" + QUERY_SQL + ") x",
            db_agent_context,
            ledger_with(),
            purpose="pushdown",
            source_metas=[{"guard_evidence": None}],
        )
        assert not outcome.valid
        assert "pushdown_evidence_missing" in outcome.error_codes
        assert not exec_outcome.ok

    def test_validation_failure_short_circuits(self, pipeline_stack, db_agent_context):
        pipeline, db, *_ = pipeline_stack
        (outcome, exec_outcome), update = pipeline.validate_and_execute(
            QUERY_SQL,
            db_agent_context,
            LedgerView({"db_time_evidence": []}),
            purpose="query",
        )
        assert not outcome.valid and not exec_outcome.ok
        assert not update.executions and not db.executed


# ============================================================================
# 登记范围装配下的表身份解析：前置校验与守卫同一口径（doc 4 §4.7）
# ============================================================================


class ScopedExecDb(FakeExecDb):
    """allowed_schemas + 裸名目录核对的闭环假库：列结构同 public.orders，
    可映射到任意登记 schema（如 sales）。located 为 {(表名, schema)} 命中集合。"""

    def __init__(self, schema: str = "public", located=None):
        super().__init__()
        self.allowed_schemas = [schema]
        self._schema = schema
        self._located = set(located if located is not None else {("orders", schema)})

    def get_columns(self, schema: str, table: str):
        # 列结构与闭环应答复用 public.orders 实现
        return super().get_columns(
            "public" if schema == self._schema else schema, table
        )

    def column_type(self, schema: str, table: str, column: str) -> str | None:
        return super().column_type(
            "public" if schema == self._schema else schema, table, column
        )

    def locate_table_schemas(self, table_names, schemas):
        out: dict[str, list[str]] = {}
        for name, schema in self._located:
            if name in table_names and schema in schemas:
                out.setdefault(name, []).append(schema)
        return out


def _scoped_pipeline(db_agent_config, tmp_path, db):
    """与 conftest.pipeline_stack 同构，但业务库带登记范围装配。"""
    from src.modules.db_deepagent.services.result_store import ResultStore
    from src.modules.db_deepagent.services.run_budget import RunBudgetLedger
    from src.modules.db_deepagent.services.sql_guard import SqlEvidenceGuard
    from src.modules.db_deepagent.services.sql_pipeline import SqlPipeline
    from src.modules.db_deepagent.services.sql_validation import DbSqlValidator

    cfg = db_agent_config.model_copy(deep=True)
    cfg.workspace.root = str(tmp_path)
    return SqlPipeline(
        config=cfg,
        validator=DbSqlValidator(
            cfg.validation,
            db,
            explain_timeout_s=cfg.business_db.explain_timeout_seconds,
        ),
        guard=SqlEvidenceGuard(cfg.validation.guard, db, DimensionProbeBuilder()),
        db=db,
        results=ResultStore(tmp_path),
        budget=RunBudgetLedger(),
    )


class TestScopedTableIdentity:
    def test_uppercase_unquoted_schema_full_pipeline(
        self, db_agent_config, db_agent_context, tmp_path
    ):
        """用户复现：PUBLIC.orders 的列过滤查询走完整校验管线——前置
        校验按 PG 规则折叠放行后，守卫同规则解析归属，不再
        column_owner_unresolved。"""
        db = ScopedExecDb("public")
        pipeline = _scoped_pipeline(db_agent_config, tmp_path, db)
        sql = (
            "SELECT id, status, amount FROM PUBLIC.orders "
            "WHERE created_at >= '2026-09-01' AND created_at < '2026-10-01'"
        )
        outcome, record, _ = pipeline.validate(sql, db_agent_context, ledger_with())
        assert outcome.valid, outcome.errors
        assert record is not None
        assert db.explained  # EXPLAIN 语义层确实发生

    def test_bare_name_non_public_schema_full_pipeline(
        self, db_agent_config, db_agent_context, tmp_path
    ):
        """用户复现：登记 sales 时裸名 orders 的列过滤查询——前置校验按
        目录核对放行，守卫按有效搜索路径解析到 sales.orders，口径一致。"""
        db = ScopedExecDb("sales", located={("orders", "sales")})
        pipeline = _scoped_pipeline(db_agent_config, tmp_path, db)
        sql = (
            "SELECT id, status, amount FROM orders "
            "WHERE status = '已支付' "
            "AND created_at >= '2026-09-01' AND created_at < '2026-10-01'"
        )
        ledger = LedgerView(
            {
                "db_time_evidence": [SEP],
                "db_dim_evidence": [
                    make_dim_evidence("sales.orders", "status", ["已支付"])
                ],
                "db_validations": {},
            }
        )
        outcome, record, _ = pipeline.validate(sql, db_agent_context, ledger)
        assert outcome.valid, outcome.errors
        assert record is not None

    def test_case_normalized_identity_full_pipeline(
        self, db_agent_config, db_agent_context, tmp_path
    ):
        """用户复现四场景并集：未引号大写列名（O.STATUS）、别名声明 O2
        引用 o2、CTE 声明 BASE 引用 base、schema PUBLIC——分析 AST 统一
        折叠，前置校验与守卫同口径；原始 SQL 字符串继续用于授权。"""
        db = ScopedExecDb("public")
        pipeline = _scoped_pipeline(db_agent_config, tmp_path, db)
        sql = (
            "WITH BASE AS (SELECT O.STATUS AS status, O.CREATED_AT AS created_at "
            "FROM PUBLIC.orders O) "
            "SELECT status FROM base O2 "
            "WHERE o2.status = '已支付' "
            "AND o2.created_at >= '2026-09-01' AND o2.created_at < '2026-10-01'"
        )
        ledger = LedgerView(
            {
                "db_time_evidence": [SEP],
                "db_dim_evidence": [
                    make_dim_evidence("public.orders", "status", ["已支付"])
                ],
                "db_validations": {},
            }
        )
        outcome, record, _ = pipeline.validate(sql, db_agent_context, ledger)
        assert outcome.valid, outcome.errors
        assert record is not None

    def test_uppercase_cte_pg_class_full_pipeline(
        self, db_agent_config, db_agent_context, tmp_path
    ):
        """用户复现场景 4：CTE 声明 PG_CLASS、引用 pg_class——折叠后按
        CTE 排除，前置校验不再误报 schema_out_of_scope，守卫经投影
        透传解析到 public.orders。"""
        db = ScopedExecDb("public")
        pipeline = _scoped_pipeline(db_agent_config, tmp_path, db)
        sql = (
            "WITH PG_CLASS AS (SELECT status, created_at FROM public.orders) "
            "SELECT status FROM pg_class "
            "WHERE status = '已支付' "
            "AND created_at >= '2026-09-01' AND created_at < '2026-10-01'"
        )
        ledger = LedgerView(
            {
                "db_time_evidence": [SEP],
                "db_dim_evidence": [
                    make_dim_evidence("public.orders", "status", ["已支付"])
                ],
                "db_validations": {},
            }
        )
        outcome, record, _ = pipeline.validate(sql, db_agent_context, ledger)
        assert outcome.valid, outcome.errors
        assert record is not None
