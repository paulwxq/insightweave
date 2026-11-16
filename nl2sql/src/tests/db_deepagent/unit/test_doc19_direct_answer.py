"""doc 19：非取数回答直接提交（零任务分支）+ doc 17：交付正文精简。

覆盖（doc 19 §10 / doc 17 §4-§7）：
- 共享门控零任务直接分支（§3）：completed/澄清放行、findings/任务引用
  拒绝、failed/partial/no_data 不开放、None 不进分支、仅作废仍拒；
- submit 工具联通（§7）：直接提交登记、两个拒绝码 hint 分别措辞、
  clarification_question 字段参数层拒绝；
- Runner（§6 / doc 17 §4）：直接回答归档 QA（condition_c）、交付正文
  无追加审计段、DEBUG 保留完整分项诊断；
- build_delivery_answer_section 精简模式（doc 17 §4.2）；
- 提示词/Skill/收卷提醒文本同步（§4/§5，文本断言防漂移）。
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from langgraph.prebuilt import ToolNode
from pydantic import ValidationError

from src.modules.db_deepagent.schemas import DbAgentFinalAnswer
from src.modules.db_deepagent.services.task_completion import (
    TASK_COMPLETION_RULES_VERSION,
    CompletionDecision,
    TaskOutcomeSummary,
    build_delivery_answer_section,
    evaluate_submission,
)
from src.modules.db_deepagent.tools.final_answer import make_submit_final_answer_tool
from src.tests.db_deepagent.conftest import (
    FakeAgent,
    FakeStore,
    invoke_tool_node,
    make_task_record,
    make_task_state,
)

pytestmark = pytest.mark.unit

_SRC = Path(__file__).resolve().parents[3]


# ------------------------------------------------------------ 共享门控分支


def _snap(tasks=None, plan_version=0):
    from src.modules.db_deepagent.services.task_completion import CompletionSnapshot

    return CompletionSnapshot(
        run_id="run-test",
        tasks=tasks or {},
        executions=[],
        analyses=[],
        errors=[],
        known_counts={},
        proven_untruncated={},
        reuse_failures={},
        all_task_records={},
        plan_version=plan_version,
    )


def _sub(
    status="completed",
    findings=None,
    unresolved=None,
    open_questions=None,
    answer="大前天是 2026年11月12日。",
):
    return {
        "status": status,
        "answer": answer,
        "findings": findings or [],
        "metric_definitions": [],
        "limitations": [],
        "open_questions": open_questions or [],
        "unresolved_task_refs": unresolved or [],
    }


_FINDING = [
    {
        "text": "发现一",
        "finding_id": "F-1",
        "task_refs": [{"sub_task_id": "T1", "revision": 1}],
        "evidence_ids": ["E-abc12345"],
    }
]
_UNRESOLVED = [{"sub_task_id": "T1", "revision": 1}]
_VOIDED_TASKS = {
    "T1": {
        "sub_task_id": "T1",
        "revision": 1,
        "status": "voided",
        "content": "确认日期",
    }
}


class TestDirectBranchGate:
    def test_zero_task_completed_accepted(self):
        decision = evaluate_submission(_sub(), _snap())
        assert decision.accepted
        assert decision.expected_status == "completed"
        assert decision.finding_summaries == []
        assert decision.coverage_map == {}
        assert decision.outcome_summaries == []
        assert decision.counts == {}  # doc 19 §3.3：直接回答无任务视图计数
        assert decision.errors == []
        # 完成规则语义变化（doc 19 §3.1）：版本递增为 2
        assert TASK_COMPLETION_RULES_VERSION == 2

    def test_zero_task_clarification_accepted(self):
        decision = evaluate_submission(
            _sub(
                status="needs_clarification",
                answer="请确认需要查询哪个月份。",
                open_questions=["需要查询哪个月份？"],
            ),
            _snap(),
        )
        assert decision.accepted
        assert decision.expected_status == "needs_clarification"

    def test_zero_task_findings_rejected(self):
        decision = evaluate_submission(_sub(findings=_FINDING), _snap())
        assert not decision.accepted
        assert decision.primary_error.code == "direct_answer_task_refs_not_allowed"

    def test_zero_task_unresolved_rejected(self):
        decision = evaluate_submission(_sub(unresolved=_UNRESOLVED), _snap())
        assert not decision.accepted
        assert decision.primary_error.code == "direct_answer_task_refs_not_allowed"

    @pytest.mark.parametrize("status", ["failed", "partial", "no_data"])
    def test_zero_task_closed_statuses_not_direct(self, status):
        """零任务的 failed/partial/no_data 不走直接分支，维持任务视图拒绝。"""
        decision = evaluate_submission(_sub(status=status), _snap())
        assert not decision.accepted
        assert decision.primary_error.code == "task_not_registered"

    def test_zero_task_none_submission_keeps_current_behavior(self):
        """Runner 无有效提交（None）不进直接分支，维持未提交收尾（§3.1）。"""
        decision = evaluate_submission(None, _snap())
        assert not decision.accepted
        assert decision.primary_error.code == "task_not_registered"

    def test_voided_only_still_rejected(self):
        """登记后全部作废：任务视图非空 → 维持 task_not_registered（v1 边界）。"""
        decision = evaluate_submission(_sub(), _snap(tasks=_VOIDED_TASKS))
        assert not decision.accepted
        assert decision.primary_error.code == "task_not_registered"


# ------------------------------------------------------------ submit 工具


def _direct_args(
    status="completed", findings=None, unresolved=None, open_questions=None
):
    return {
        "status": status,
        "answer": "大前天是 2026年11月12日。",
        "findings": findings or [],
        "metric_definitions": [],
        "limitations": [],
        "open_questions": open_questions or [],
        "unresolved_task_refs": unresolved or [],
    }


def _bare_state():
    return {
        "db_time_evidence": [],
        "db_executions": [],
        "db_analyses": [],
        "db_validations": {},
        "db_final_answers": {},
    }


@pytest.fixture()
def node() -> ToolNode:
    return ToolNode([make_submit_final_answer_tool()])


class TestDirectSubmitTool:
    def test_direct_answer_registers_record(self, node, db_agent_context):
        updates, msg = invoke_tool_node(
            node,
            [("submit_final_answer", _direct_args(), "tc-sub")],
            db_agent_context,
            extra_state=_bare_state(),
        )
        assert msg.content == "已接收最终答案"
        record = updates["db_final_answers"][db_agent_context.run_id]
        assert record["answer"]["status"] == "completed"
        assert record["completion_rules_version"] == 2
        assert record["coverage_map"] == {}

    def test_direct_answer_findings_rejected_with_clear_hint(
        self, node, db_agent_context
    ):
        updates, msg = invoke_tool_node(
            node,
            [("submit_final_answer", _direct_args(findings=_FINDING), "tc-sub")],
            db_agent_context,
            extra_state=_bare_state(),
        )
        assert msg.content != "已接收最终答案"
        payload = updates["messages"][-1].content if "messages" in updates else msg
        text = payload if isinstance(payload, str) else str(payload)
        assert "direct_answer_task_refs_not_allowed" in text
        assert "清空 findings" in text

    def test_voided_only_hint_mentions_re_register(self, node, db_agent_context):
        ledger = make_task_state(
            [make_task_record("run-test", "T1", 1, status="voided")]
        )
        _updates, msg = invoke_tool_node(
            node,
            [("submit_final_answer", _direct_args(), "tc-sub")],
            db_agent_context,
            extra_state={**_bare_state(), **ledger},
        )
        assert msg.content != "已接收最终答案"
        text = str(msg.content)
        assert "task_not_registered" in text
        # §7：作废形态不得承诺"清空 findings 可恢复"，应引导重新登记
        assert "重新登记真实任务" in text
        assert "清空 findings" not in text

    def test_clarification_question_field_forbidden(self):
        """提交 schema 无 clarification_question（extra=forbid），携带即参数层拒绝。"""
        with pytest.raises(ValidationError):
            DbAgentFinalAnswer(
                **_sub(status="needs_clarification"),
                clarification_question="需要查询哪个月份？",
            )


# ------------------------------------------------------------ Runner


def _runner(services, agent, store):
    from src.modules.db_deepagent.agent import DbAgentHandle
    from src.modules.db_deepagent.persistence.archive import QaArchive
    from src.modules.db_deepagent.runner import DbAgentRunner

    handle = DbAgentHandle(
        agent=agent,
        backend=None,
        permissions=[],
        harness_key="k",
        session_dir=Path("."),
    )
    runner = DbAgentRunner(
        services=services, handle=handle, archive=QaArchive(store), fixed_now=None
    )
    return runner


_DIRECT_ANSWER = {
    "status": "completed",
    "answer": "大前天是 2026年11月12日。",
    "findings": [],
    "metric_definitions": [],
    "limitations": [],
    "open_questions": [],
    "unresolved_task_refs": [],
}


class TestRunnerDirectAnswer:
    def test_direct_answer_archives_qa_without_tail(self, assembly_services):
        """零任务零执行直接回答：completed、归档 QA（condition_c）、无追加段。"""
        store = FakeStore()
        agent = FakeAgent(
            {
                "messages": [],
                "db_executions": [],
                "db_analyses": [],
                "db_validations": {},
                "db_final_answers": {
                    "run-x": {"run_id": "run-x", "answer": _DIRECT_ANSWER}
                },
            }
        )
        result = _runner(assembly_services, agent, store).run(
            "请问大前天是哪一天", "s1", request_id="req-p"
        )
        assert result.status == "completed"
        assert result.answer == "大前天是 2026年11月12日。"
        for token in ("结论 F-", "已完成：", "已作废：", "未完成："):
            assert token not in result.answer
        assert any(ns[1] == "qa" for (ns, _k) in store.items), "直接回答应归档 QA"
        assert not any(
            ns[1] == "run_errors" for (ns, _k) in store.items
        ), "合法直接回答不得写 run_errors"
        qa_values = [v for (ns, _k), v in store.items.items() if ns[1] == "qa"]
        assert qa_values[0]["delivery_answer"]["answer"] == result.answer
        assert qa_values[0]["answer"]["findings"] == []

    def test_data_run_delivery_lean_and_debug_full(self, assembly_services, caplog):
        """doc 17：数据查询成功交付不再追加审计段；DEBUG 保留完整分项诊断；
        非空 metric_definitions 在 result/QA 原提交/QA 交付三处一致（§7.1）。"""
        from copy import deepcopy

        from src.tests.db_deepagent.unit.test_runner_persistence import _state

        metrics = ["统计 public.orders 表订单记录数，聚合方式为 COUNT(*)。"]
        state = _state()
        state["db_final_answers"]["run-x"]["answer"] = {
            **state["db_final_answers"]["run-x"]["answer"],
            "metric_definitions": list(metrics),
        }
        store = FakeStore()
        agent = FakeAgent(state)
        with caplog.at_level(logging.DEBUG, logger="nl2sql.db_deepagent.runner"):
            result = _runner(assembly_services, agent, store).run(
                "9 月订单多少", "s1", request_id="req-q"
            )
        assert result.status == "completed"
        assert result.answer == "9 月订单 100 单"
        for token in ("结论 F-", "已完成：", "已作废："):
            assert token not in result.answer
        assert result.metric_definitions == metrics
        qa_values = [v for (ns, _k), v in store.items.items() if ns[1] == "qa"]
        raw = qa_values[0]["answer"]
        delivered = qa_values[0]["delivery_answer"]
        assert raw["metric_definitions"] == metrics  # 原提交保真
        assert delivered["metric_definitions"] == metrics  # 交付透传
        assert deepcopy(raw["findings"]) == delivered["findings"]
        assert any(
            "结论 F-1" in r.message for r in caplog.records
        ), "DEBUG 级别应记录完整分项诊断段落"


# ------------------------------------------------- doc 17 精简模式（纯函数）


def _rich_decision() -> CompletionDecision:
    return CompletionDecision(
        accepted=True,
        expected_status="completed",
        finding_summaries=[
            {
                "finding_id": "F-1",
                "text": "订单数 100",
                "task_refs": [{"sub_task_id": "T1", "revision": 1}],
            }
        ],
        outcome_summaries=[
            TaskOutcomeSummary("T1", 1, "completed", "统计 9 月订单"),
            TaskOutcomeSummary("T2", 1, "failed", "按渠道统计", reason="SQL 失败"),
            TaskOutcomeSummary(
                "T3",
                1,
                "needs_clarification",
                "口径确认",
                clarification_question="含退款吗？",
            ),
            TaskOutcomeSummary("T4", 1, "voided", "多余任务", reason="计划调整"),
        ],
        authoritative_questions=["需要哪个年份？"],
    )


class TestDeliverySectionLean:
    def test_full_mode_default_unchanged(self):
        section = build_delivery_answer_section(_rich_decision())
        assert "结论 F-1（T1@1）：订单数 100" in section
        assert "已完成：统计 9 月订单" in section
        assert "已作废：多余任务" in section
        assert "未完成：按渠道统计" in section

    def test_lean_mode_skips_success_keeps_failures(self):
        section = build_delivery_answer_section(
            _rich_decision(), include_audit_details=False
        )
        assert "结论 F-" not in section
        assert "已完成：" not in section
        assert "已作废：" not in section
        # 失败/澄清/权威问题保留（doc 17 §4.2）
        assert "未完成：按渠道统计" in section
        assert "待澄清" in section
        assert "需要澄清：需要哪个年份？" in section

    def test_lean_mode_all_success_empty(self):
        decision = CompletionDecision(
            accepted=True,
            expected_status="completed",
            finding_summaries=[
                {"finding_id": "F-1", "text": "订单数 100", "task_refs": []}
            ],
            outcome_summaries=[TaskOutcomeSummary("T1", 1, "completed", "统计")],
        )
        assert (
            build_delivery_answer_section(decision, include_audit_details=False) == ""
        )


# ------------------------------------------------------------ 文本同步断言


class TestPromptSync:
    def test_prompts_registration_narrowed(self):
        text = (_SRC / "modules/db_deepagent/config/prompts.yaml").read_text(
            encoding="utf-8"
        )
        assert "回答任何问题前先用 record_sub_tasks" not in text
        assert "简单问题也登记一项" not in text
        assert "需要取数、结果分析或沿用数据成果的问题" in text
        assert "无需登记任务" in text

    def test_prompts_scoping_and_antiskip(self):
        text = (_SRC / "modules/db_deepagent/config/prompts.yaml").read_text(
            encoding="utf-8"
        )
        assert "已登记任务" in text  # :48/:77 全部任务终态限定
        assert "不得直接猜测数据数值" in text
        assert "零任务直接澄清" in text  # open_questions 例外

    def test_prompts_doc17_metric_division(self):
        text = (_SRC / "modules/db_deepagent/config/prompts.yaml").read_text(
            encoding="utf-8"
        )
        assert "metric_definitions" in text
        assert (
            "不在 answer 展开技术口径" in text or "默认不在 answer 展开技术口径" in text
        )

    def test_query_planning_skill_synced(self):
        text = (_SRC / "modules/db_deepagent/skills/query-planning/SKILL.md").read_text(
            encoding="utf-8"
        )
        assert "简单问题也登记一项" not in text
        assert "无需登记任务" in text

    def test_result_analysis_skill_synced(self):
        text = (
            _SRC / "modules/db_deepagent/skills/result-analysis/SKILL.md"
        ).read_text(encoding="utf-8")
        assert "metric_definitions" in text

    def test_final_answer_reminder_updated(self):
        text = (
            _SRC / "modules/db_deepagent/middleware/final_answer_gate.py"
        ).read_text(encoding="utf-8")
        assert "并引用已有 evidence_id" not in text
        assert "已登记数据任务时在 findings 中引用对应 evidence_id" in text

    def test_prompts_time_challenge_requires_reresolve(self):
        """doc 20 §5：时间节改为"注入基准 + 工具复核"分工（2026-11-16 生产
        观察：追问轮模型曾编造"服务端当前日期"为旧错区间辩护）。旧规则整条
        移除，新三段落位，相邻时间规则保留。断言前把 YAML 续行空白拼回
        逻辑行，避免换行导致漏配或假绿。"""
        import re

        raw = (_SRC / "modules/db_deepagent/config/prompts.yaml").read_text(
            encoding="utf-8"
        )
        text = re.sub(r"\n[ \t]+", "", raw)
        # 新三段关键语义
        assert "以系统注入的本轮北京时间为准" in text
        assert "无需为获取今天日期单独调用工具" in text
        assert "不得从历史对话或模型记忆猜测当前日期" in text
        assert "时间计算结论被质疑时，调用 resolve_time_range" in text
        assert "不能仅因两轮参考日期不同" in text
        # 旧规则无残留（仅旧规则独有的片段；source_text 为新旧共用词不断言）
        assert "时间结论被质疑或需要当前日期基准时" not in text
        assert "必须重新调用 resolve_time_range 核实" not in text
        assert "传用户原话中的时间表达）" not in text
        assert "不得自行推断或编造" not in text
        # 相邻时间规则保留
        assert "不使用 CURRENT_DATE、now() 等实时时钟" in text
        assert ">= start AND < end" in text
