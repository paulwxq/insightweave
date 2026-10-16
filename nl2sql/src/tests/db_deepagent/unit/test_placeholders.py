"""结果占位符单元测试（docs/v4/5 §8.3、§18.2 F08）

前序结果经 ResultStore 保存；占位符展开与各子码经 SqlPipeline.validate /
expand_placeholders 验证。
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from src.modules.db_deepagent.context import DbAgentContext
from src.modules.db_deepagent.services.dimension_probe import DimensionProbeBuilder
from src.modules.db_deepagent.services.sql_guard import SqlEvidenceGuard
from src.modules.db_deepagent.services.sql_pipeline import (
    LedgerView,
    PipelineError,
    expand_placeholders,
)

from src.tests.db_deepagent.conftest import make_time_evidence

pytestmark = pytest.mark.unit

SEP = make_time_evidence("T-sep", "2026-09-01", "2026-10-01")


def _save_prior(
    results, ctx: DbAgentContext, rows, *, source_truncated=False, datasource_id=None
):
    return results.save(
        ctx=ctx,
        purpose="query",
        sql_template="SELECT customer_id ...",
        executed_sql="SELECT customer_id ...",
        columns=[{"name": "customer_id", "pg_type": "varchar"}],
        rows=rows,
        source_truncated=source_truncated,
        derived_from=[],
        guard_evidence={"time": [], "dim": []},
        datasource_id=datasource_id,
    )


def _ledger() -> LedgerView:
    return LedgerView(
        {"db_time_evidence": [SEP], "db_dim_evidence": [], "db_validations": {}}
    )


def _ph_sql(ref: str, column: str = "customer_id") -> str:
    return (
        "SELECT id FROM public.orders "
        f"WHERE customer_id IN {{{{result:{ref}:{column}}}}} "
        "AND created_at >= '2026-09-01' AND created_at < '2026-10-01'"
    )


@pytest.fixture()
def prior(pipeline_stack, db_agent_context):
    """保存一个两值（含重复）客户编号结果。"""
    _pipeline, _db, results, *_ = pipeline_stack
    return _save_prior(results, db_agent_context, [("C001",), ("C002",), ("C001",)])


class TestHappyPath:
    def test_validate_binds_refs_and_evidence(
        self, pipeline_stack, db_agent_context, prior
    ):
        pipeline, *_ = pipeline_stack
        outcome, record, _ = pipeline.validate(
            _ph_sql(prior.result_ref), db_agent_context, _ledger()
        )
        assert outcome.valid, outcome.errors
        assert record["placeholder_refs"] == [prior.result_ref]
        ph_dim = [
            d for d in record["guard_evidence"]["dim"] if d["source"] == "prior_result"
        ]
        assert len(ph_dim) == 1
        assert ph_dim[0]["values"] == ["C001", "C002"]  # 去重保序
        assert ph_dim[0]["table"] == "public.orders"

    def test_execute_expands_deduped_values(
        self, pipeline_stack, db_agent_context, prior
    ):
        pipeline, db, *_ = pipeline_stack
        _, record, _ = pipeline.validate(
            _ph_sql(prior.result_ref), db_agent_context, _ledger()
        )
        outcome, _ = pipeline.execute(
            _ph_sql(prior.result_ref),
            db_agent_context,
            LedgerView(
                {
                    "db_time_evidence": [SEP],
                    "db_dim_evidence": [],
                    "db_validations": {record["key"]: record},
                }
            ),
        )
        assert outcome.ok, outcome.error_type
        executed = db.executed[-1][0]
        assert "IN ('C001', 'C002')" in executed
        assert "C001', 'C002', 'C001" not in executed

    def test_template_keeps_placeholder_text(
        self, pipeline_stack, db_agent_context, prior
    ):
        """授权文本保留占位符原文；展开文本只在执行时出现。"""
        pipeline, _db, _results, *_ = pipeline_stack
        _, record, _ = pipeline.validate(
            _ph_sql(prior.result_ref), db_agent_context, _ledger()
        )
        assert "{{result:" in record["normalized_sql"]


class TestSubcodes:
    def test_malformed(self, pipeline_stack, db_agent_context, prior):
        """ref 非法 → placeholder_malformed。"""
        pipeline, *_ = pipeline_stack
        sql = _ph_sql("res_not_a_ref").replace("res_not_a_ref", "bad_ref")
        outcome, record, _ = pipeline.validate(sql, db_agent_context, _ledger())
        assert not outcome.valid
        assert "placeholder_malformed" in outcome.error_codes
        assert record is None

    def test_handwritten_sentinel_rejected(self, pipeline_stack, db_agent_context):
        pipeline, *_ = pipeline_stack
        sql = "SELECT id FROM public.orders WHERE customer_id IN (__dbda_ph_0__)"
        outcome, *_ = pipeline.validate(sql, db_agent_context, _ledger())
        assert "placeholder_malformed" in (outcome.error_codes or [])

    def test_position_invalid(self, pipeline_stack, db_agent_context, prior):
        """占位符在 = 右侧 → placeholder_position_invalid。"""
        pipeline, *_ = pipeline_stack
        sql = _ph_sql(prior.result_ref).replace("customer_id IN", "customer_id =")
        outcome, *_ = pipeline.validate(sql, db_agent_context, _ledger())
        assert "placeholder_position_invalid" in (outcome.error_codes or [])

    def test_datasource_mismatch(self, pipeline_stack, db_agent_context):
        pipeline, _db, results, *_ = pipeline_stack
        other = replace(db_agent_context, datasource_id="pg:other")
        prior = _save_prior(results, other, [("C001",)], datasource_id="pg:other")
        outcome, *_ = pipeline.validate(
            _ph_sql(prior.result_ref), db_agent_context, _ledger()
        )
        assert "placeholder_datasource_mismatch" in (outcome.error_codes or [])

    def test_source_truncated(self, pipeline_stack, db_agent_context):
        pipeline, _db, results, *_ = pipeline_stack
        prior = _save_prior(
            results, db_agent_context, [("C001",)], source_truncated=True
        )
        outcome, *_ = pipeline.validate(
            _ph_sql(prior.result_ref), db_agent_context, _ledger()
        )
        assert "placeholder_source_truncated" in (outcome.error_codes or [])

    def test_empty_values(self, pipeline_stack, db_agent_context):
        pipeline, _db, results, *_ = pipeline_stack
        prior = _save_prior(results, db_agent_context, [])
        outcome, *_ = pipeline.validate(
            _ph_sql(prior.result_ref), db_agent_context, _ledger()
        )
        assert "placeholder_empty" in (outcome.error_codes or [])

    def test_too_many_values(self, pipeline_stack, db_agent_context):
        pipeline, db, results, _budget, cfg = pipeline_stack
        prior = _save_prior(
            results, db_agent_context, [(f"C{i:04d}",) for i in range(5)]
        )
        guard = SqlEvidenceGuard(cfg.validation.guard, db, DimensionProbeBuilder())
        with pytest.raises(PipelineError) as e:
            expand_placeholders(
                _ph_sql(prior.result_ref),
                db_agent_context,
                guard=guard,
                results=results,
                placeholder_max_values=2,
            )
        assert e.value.error_type == "placeholder_too_many_values"

    def test_unknown_column(self, pipeline_stack, db_agent_context, prior):
        pipeline, *_ = pipeline_stack
        outcome, *_ = pipeline.validate(
            _ph_sql(prior.result_ref, column="nope"), db_agent_context, _ledger()
        )
        assert not outcome.valid  # 展开失败（结果无该列）
