"""Runner 持久化生命周期单元测试（docs/v4/5 §14.3、§18.2）

FakeAgent／FakeStore 注入 P0–P4 各阶段异常；状态优先级、primary 标记、
归档条件 a/b、本地记录。摘要事件读取与轮换见 test_summary_context。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import psycopg
import pytest

from src.modules.db_deepagent.agent import DbAgentHandle
from src.modules.db_deepagent.persistence.archive import QaArchive
from src.modules.db_deepagent.runner import DbAgentRunner, is_persistence_error
from src.tests.db_deepagent.conftest import (
    TEST_SOURCE_ID,
    FakeAgent,
    FakeStore,
    _remap_run_id,
)

pytestmark = pytest.mark.unit

ANSWER = {
    "status": "completed",
    "answer": "9 月订单 100 单",
    "findings": [
        {
            "text": "订单数",
            "finding_id": "F-1",
            "task_refs": [{"sub_task_id": "T1", "revision": 1}],
            "evidence_ids": ["E-run00001"],
        }
    ],
    "metric_definitions": [],
    "limitations": [],
    "open_questions": [],
    "unresolved_task_refs": [],
}


def _exec(evidence_id="E-run00001", run_id="run-x", reused=False):
    return {
        "evidence_id": evidence_id,
        "tool_call_id": "tc-e",
        "run_id": run_id,
        "request_id": "req-x",
        "purpose": "query",
        "datasource_id": "orders",
        "sql_template": "SELECT ...",
        "executed_sql": "SELECT ...",
        "executed_sql_sha256": "h",
        "result_ref": "res_" + "1" * 32,
        "columns": [],
        "saved_row_count": 1,
        "source_truncated": False,
        "elapsed_ms": 9,
        "warnings": [],
        "sub_task_id": "T1",
        "sub_task_revision": 1,
        "input_result_refs": [],
        **(
            {
                "reused_from": {
                    "evidence_id": "E-old",
                    "thread_id": "",
                    "request_id": "",
                    "run_id": "run-old",
                    "datasource_id": "orders",
                    "user_text": None,
                }
            }
            if reused
            else {}
        ),
        "result_generated_at": "2026-10-01T00:00:00+08:00",
    }


def _task_ledger(
    run_id="run-x",
    *,
    status="completed",
    evidence_ids=("E-run00001",),
    sub_task_id="T1",
    extra_task=None,
):
    """doc 6.3：合法提交所需的最小任务账本（T1@1 completed 选定成果）。"""
    from src.tests.db_deepagent.conftest import make_task_record, make_task_state

    task = make_task_record(
        run_id,
        sub_task_id,
        1,
        status=status,
        evidence_ids=list(evidence_ids),
        content="统计 9 月订单",
    )
    if extra_task is not None:
        return make_task_state([task, extra_task])
    return make_task_state([task])


def _state(answer=ANSWER, executions=None, run_id="run-x", ledger=True):
    answers = {"run-x": {"run_id": "run-x", "answer": answer}} if answer else {}
    state = {
        "messages": [],
        "db_executions": (
            executions if executions is not None else [_exec(run_id=run_id)]
        ),
        "db_analyses": [],
        "db_validations": {},
        "db_final_answers": answers,
    }
    if ledger:
        # 无 answer 的失败注入路径也保留账本：状态判定不依赖提交存在
        state.update(_task_ledger(run_id))
    return state


def _runner(services, agent, store, state):
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
    runner._fixed_now = None  # 用实时时钟
    return runner


def _run_ok(services, store, state):
    agent = FakeAgent(state)
    result = _runner(services, agent, store, state).run("问", "s1", request_id="req-p")
    return result, agent


class TestHappyPath:
    def test_completed_result_with_evidence(self, assembly_services):
        store = FakeStore()
        result, agent = _run_ok(assembly_services, store, _state())
        assert result.status == "completed"
        assert result.thread_id and result.thread_id.startswith("dbda:s1:")
        assert result.answer.startswith(ANSWER["answer"])  # R1-13：附加分项段
        assert [e.evidence_id for e in result.evidence] == ["E-run00001"]
        assert result.archived is True
        assert (
            agent.invoke_calls
            and agent.invoke_calls[0]["context"].run_id == result.run_id
        )
        # qa 命名空间写入
        qa_keys = [k for (ns, k) in store.items if ns[1] == "qa"]
        assert qa_keys == ["req-p"]
        # P4 本地记录
        local = (
            Path(assembly_services.config.workspace.root)
            / "sessions"
            / "s1"
            / "runs"
            / "req-p.json"
        )
        assert local.exists()
        payload = json.loads(local.read_text(encoding="utf-8"))
        assert payload["status"] == "completed" and payload["failures"] == []

    def test_no_final_answer_failed(self, assembly_services):
        store = FakeStore()
        result, _ = _run_ok(assembly_services, store, _state(answer=None))
        assert result.status == "failed"
        assert any(e.kind == "no_final_answer" and e.primary for e in result.errors)
        # 无成功证据？（有真实执行 → qa 仍写，status=failed）
        assert result.archived is True


class TestFailureInjection:
    def test_p0_persistence_skips_invoke(self, assembly_services):
        store = FakeStore()
        store.get_side_effect = psycopg.OperationalError("down")
        agent = FakeAgent(_state())
        result = _runner(assembly_services, agent, store, _state()).run("问", "s1")
        assert result.status == "persistence_error"
        assert result.thread_id is None
        assert not agent.invoke_calls
        assert result.stats.model_calls == 0
        assert result.archived is False
        assert any(
            e.phase == "P0" and e.kind == "persistence" and e.primary
            for e in result.errors
        )

    def test_p1_runtime_reads_diagnostic_only(self, assembly_services):
        """P1 runtime 异常：P2 只为诊断读取，不读取最终答案；兜底说明非空。"""
        store = FakeStore()
        agent = FakeAgent(_state())
        agent.invoke_side_effect = RuntimeError("model boom")
        result = _runner(assembly_services, agent, store, _state()).run("问", "s1")
        assert result.status == "failed"
        # doc 6.2：已开始调用的空答案运行生成服务端兜底（不读原提交记录）
        assert result.answer.startswith("系统兜底说明：")
        assert "未取得可确认的查询或分析结果" in result.answer
        assert any("服务端兜底" in w for w in result.warnings)
        assert not result.findings
        assert any(
            e.phase == "P1" and e.kind == "runtime" and e.primary for e in result.errors
        )

    def test_p1_persistence_skips_get_state(self, assembly_services):
        store = FakeStore()
        agent = FakeAgent(_state())
        agent.invoke_side_effect = psycopg.OperationalError("checkpoint down")
        agent.fail_get_state_after_invoke = psycopg.OperationalError("state down")
        result = _runner(assembly_services, agent, store, _state()).run("问", "s1")
        assert result.status == "persistence_error"
        # P0 读过一次线程状态；P1 持久化异常后 P2 不再 get_state
        assert agent.get_state_calls == 1
        assert any(
            e.phase == "P1" and e.kind == "persistence" and e.primary
            for e in result.errors
        )

    def test_p2_persistence_marks_primary_there(self, assembly_services):
        """P1 runtime 后 P2 persistence：状态 persistence_error，primary 在 P2 条目。"""
        store = FakeStore()
        agent = FakeAgent(_state())
        agent.invoke_side_effect = RuntimeError("boom")
        agent.fail_get_state_after_invoke = psycopg.OperationalError("state down")
        result = _runner(assembly_services, agent, store, _state()).run("问", "s1")
        assert result.status == "persistence_error"
        primary = [e for e in result.errors if e.primary]
        assert primary and primary[0].phase == "P2" and primary[0].kind == "persistence"

    def test_p3_archive_failure_changes_status(self, assembly_services):
        store = FakeStore()
        store.put_side_effect = psycopg.OperationalError("store down")
        result, _ = _run_ok(assembly_services, store, _state())
        assert result.status == "persistence_error"
        assert result.archived is False
        assert "未归档" in "".join(result.warnings)
        assert result.answer.startswith(
            ANSWER["answer"]
        )  # R1-13：附加分项段  # 答案仍在结果中

    def test_budget_exhausted_status(self, assembly_services):
        """无执行无答案（FakeAgent 直接返回）→ failed；预算路径由真实图触发。"""
        store = FakeStore()
        agent = FakeAgent(_state(answer=None, executions=[]))
        result = _runner(assembly_services, agent, store, _state()).run("问", "s1")
        # FakeAgent 正常返回、无答案且无执行 → failed（no_final_answer）；
        # 预算路径由 middleware 在真实图内触发，此处验证状态判定接受 budget 快照
        assert result.status in ("failed", "budget_exhausted")


class TestArchiveConditions:
    def test_condition_a_real_execution_archives_qa(self, assembly_services):
        store = FakeStore()
        result, _ = _run_ok(assembly_services, store, _state())
        assert any(ns[1] == "qa" for (ns, _k) in store.items)

    def test_condition_a_pushdown_execution_archives_qa(self, assembly_services):
        """纯下推分析（无 query 执行）满足条件 a → qa（§13.3：query 或 pushdown）。

        修复前 evidence 只含 query 执行，纯下推轮次被误归 run_errors。
        doc 6.3：分析 A- 为任务 T1 的选定成果（含结构化输入血缘）。
        """
        store = FakeStore()
        pushdown = _exec(evidence_id="E-push", run_id="run-x")
        pushdown["purpose"] = "pushdown"
        analysis = {
            "evidence_id": "A-1",
            "tool_call_id": "tc-a",
            "run_id": "run-x",
            "request_id": "req-x",
            "pushdown_execution_id": "E-push",
            "operation": {"name": "aggregate"},
            "input_result_refs": [
                # R2-02：结构化输入血缘必须能解析到真实原始记录；此处
                # 指向同轮的下推执行 E-push（同任务输入），不再引用一个
                # 状态中不存在的裸 evidence_id。
                {
                    "result_ref": "res_" + "1" * 32,
                    "evidence_ref": {
                        "session_id": "s1",
                        "thread_id": "dbda:s1:1",
                        "run_id": "run-x",
                        "evidence_id": "E-push",
                    },
                    "source_task_ref": {
                        "session_id": "s1",
                        "thread_id": "dbda:s1:1",
                        "run_id": "run-x",
                        "sub_task_id": "T1",
                        "revision": 1,
                    },
                }
            ],
            "output_result_ref": "res_" + "2" * 32,
            "executed_sql": "SELECT ...",
            "saved_row_count": 1,
            "source_truncated": False,
            "warnings": [],
            "result_generated_at": "2026-10-01T00:00:00+08:00",
            "sub_task_id": "T1",
            "sub_task_revision": 1,
        }
        answer = {
            **ANSWER,
            "findings": [
                {
                    "text": "订单数",
                    "finding_id": "F-1",
                    "task_refs": [{"sub_task_id": "T1", "revision": 1}],
                    "evidence_ids": ["A-1"],
                }
            ],
        }
        state = _state(
            answer=answer,
            executions=[pushdown],
            ledger=False,
        )
        state["db_analyses"] = [analysis]
        state.update(_task_ledger(evidence_ids=("A-1",)))
        result, _ = _run_ok(assembly_services, store, state)
        assert result.status == "completed"
        assert any(
            ns[1] == "qa" for (ns, _k) in store.items
        ), "纯下推执行的成功轮次应进 qa 而非 run_errors"
        assert not any(ns[1] == "run_errors" for (ns, _k) in store.items)

    def test_archives_include_question(self, assembly_services):
        """qa 与 run_errors 记录都保存本轮用户问题（checkpoint 清理后可还原问答）。"""
        store = FakeStore()
        result, _ = _run_ok(assembly_services, store, _state())
        qa_records = [v for key, v in store.items.items() if key[0][1] == "qa"]
        assert qa_records and qa_records[0]["question"] == "问"
        # run_errors 路径：无执行无答案
        store2 = FakeStore()
        _runner(assembly_services, FakeAgent({}), store2, _state()).run(
            "失败问题", "s2", request_id="req-q"
        )
        err_records = [
            v for key, v in store2.items.items() if key[0][1] == "run_errors"
        ]
        assert err_records and err_records[0]["question"] == "失败问题"

    def test_sql_success_then_model_failure_archives_qa(self, assembly_services):
        """SQL 成功后模型调用失败：状态 failed 但按条件 a 进 qa（§13.3）。

        修复前 P1 runtime 异常使整个 values 被替换为 None，已执行账本
        被丢弃、请求误入 run_errors。doc 6.2：空答案断言改为非空系统兜底。
        """

        class SqlThenModelFail(FakeAgent):
            """真实顺序：工具先以本次 run_id 写账本，随后的模型调用才失败。"""

            def invoke(self, inputs, config=None, context=None):
                if context is not None:
                    self.state_values = _remap_run_id(
                        self.state_values, context.run_id, context.request_id
                    )
                raise RuntimeError("model boom after sql")

        store = FakeStore()
        agent = SqlThenModelFail(_state())  # 账本含本轮成功 query 执行
        result = _runner(assembly_services, agent, store, _state()).run(
            "问", "s1", request_id="req-sql-ok-model-fail"
        )
        assert result.status == "failed"
        assert result.answer.startswith("系统兜底说明：")  # 服务端兜底非空
        assert "已记录 1 行结果" in result.answer  # 可靠行数来自本轮账本
        assert any("服务端兜底" in w for w in result.warnings)
        assert any(e.evidence_id == "E-run00001" for e in result.evidence)
        qa_records = [v for key, v in store.items.items() if key[0][1] == "qa"]
        assert qa_records, "已执行 SQL 的轮次应进 qa 而非 run_errors"
        assert qa_records[0]["status"] == "failed"  # 保留真实状态
        assert qa_records[0]["evidence"], "执行轨迹须进入归档"
        # 归档契约 v2（doc 6.2 §7.4）：原 answer 为空提交视图、交付内容另存
        assert qa_records[0]["archive_schema_version"] == 2
        assert qa_records[0]["answer"] == {}  # 无提交记录 → 缺失约定值
        assert qa_records[0]["answer_source"] == "system_fallback"
        assert (
            qa_records[0]["delivery_answer"]["answer"] == result.answer
        ), "归档交付正文与实时返回一致"
        assert not any(key[0][1] == "run_errors" for key in store.items)

    def test_condition_b_reuse_only_archives_qa(self, assembly_services):
        """纯沿用（无真实执行）+ 被接受答案引用沿用证据 → qa。"""
        store = FakeStore()
        state = _state(executions=[_exec(reused=True)], run_id="run-x")
        result, _ = _run_ok(assembly_services, store, state)
        assert all(e.reused_from is not None for e in result.evidence)
        assert result.status == "completed"
        assert any(ns[1] == "qa" for (ns, _k) in store.items)

    def test_time_dim_reuse_only_goes_run_errors(self, assembly_services):
        """仅时间／维度沿用（无执行、无答案）→ run_errors。"""
        store = FakeStore()
        result, _ = _run_ok(
            assembly_services, store, _state(answer=None, executions=[])
        )
        assert any(ns[1] == "run_errors" for (ns, _k) in store.items)

    def test_budget_exhausted_with_execution_keeps_real_status(self, assembly_services):
        """有真实执行但答案失败：qa 保存真实状态（failed），不改写。"""
        store = FakeStore()
        result, _ = _run_ok(assembly_services, store, _state(answer=None))
        assert result.status == "failed"
        qa_records = [v for key, v in store.items.items() if key[0][1] == "qa"]
        assert qa_records and qa_records[0]["status"] == "failed"


class TestPersistenceClassifier:
    def test_classifier(self):
        assert is_persistence_error(psycopg.OperationalError("x"))
        assert is_persistence_error(RuntimeError("x")) is False


class TestStep5Gates:
    """doc 7 §2 折入：入口归属校验、会话 ID 字符集、轮换编号不复用。"""

    def test_cross_session_thread_id_rejected(self, assembly_services):
        from src.modules.db_deepagent.agent import DbAgentHandle

        store = FakeStore()
        agent = FakeAgent(_state())
        runner = DbAgentRunner(
            services=assembly_services,
            handle=DbAgentHandle(
                agent=agent,
                backend=None,
                permissions=[],
                harness_key="k",
                session_dir=Path("."),
            ),
            archive=QaArchive(store),
        )
        result = runner.run("问", "s1", thread_id="dbda:other:1")
        assert result.status == "failed"
        assert any("不属于本会话" in e.message for e in result.errors)

    def test_session_id_with_colon_rejected(self, assembly_services):
        from src.modules.db_deepagent.agent import DbAgentHandle

        store = FakeStore()
        agent = FakeAgent(_state())
        runner = DbAgentRunner(
            services=assembly_services,
            handle=DbAgentHandle(
                agent=agent,
                backend=None,
                permissions=[],
                harness_key="k",
                session_dir=Path("."),
            ),
            archive=QaArchive(store),
        )
        # doc 2 §4.2：ID 校验先于一切副作用（目录/锁/本地记录），直接拒绝
        import pytest as _pytest

        with _pytest.raises(ValueError, match="session_id 非法"):
            runner.run("问", "a:b")

    def test_rotation_does_not_reuse_existing_seq(self, assembly_services):
        """恢复线程 1 后轮换：新编号=会话最大序号+1，不复用已有线程 2。"""
        from src.modules.db_deepagent.agent import DbAgentHandle

        store = FakeStore()
        store.put(
            ("db_deepagent", "sessions"),
            "s1",
            {
                "session_id": "s1",
                "current_thread_id": "dbda:s1:1",
                "thread_seq": 2,
                "source_id": TEST_SOURCE_ID,
            },
        )
        # 线程 1（当前，消息超阈值触发轮换）、线程 2（已存在）、线程 3（空）
        agent = FakeAgent({})
        agent.states_by_thread = {
            "dbda:s1:1": {"messages": [object(), object(), object()]},
            "dbda:s1:2": {"messages": [object()]},
            "dbda:s1:3": {"messages": []},
        }
        cfg = assembly_services.config.model_copy(deep=True)
        cfg.thread.rotate_after_messages = 2
        services = assembly_services
        services.config = cfg
        runner = DbAgentRunner(
            services=services,
            handle=DbAgentHandle(
                agent=agent,
                backend=None,
                permissions=[],
                harness_key="k",
                session_dir=Path("."),
            ),
            archive=QaArchive(store),
        )
        result = runner.run("问", "s1", thread_id="dbda:s1:1", request_id="req-rot")
        assert result.thread_id == "dbda:s1:3", result.thread_id  # 不复用 2
        assert store.items[(("db_deepagent", "sessions"), "s1")]["thread_seq"] >= 3
        thread_records = [
            k for k in store.items if k[0] == ("db_deepagent", "threads", "s1")
        ]
        assert thread_records  # threads 轨迹已落库

    def test_p3_writes_session_and_threads(self, assembly_services):
        """成功运行后 P3 落库 sessions 与 threads 轨迹（doc 7 §2.1）。"""
        from src.modules.db_deepagent.agent import DbAgentHandle

        store = FakeStore()
        agent = FakeAgent(_state())
        runner = DbAgentRunner(
            services=assembly_services,
            handle=DbAgentHandle(
                agent=agent,
                backend=None,
                permissions=[],
                harness_key="k",
                session_dir=Path("."),
            ),
            archive=QaArchive(store),
        )
        result = runner.run("问", "s1", request_id="req-track")
        assert result.status == "completed"
        session = store.items[(("db_deepagent", "sessions"), "s1")]
        assert session["current_thread_id"] == result.thread_id
        tracks = [
            v
            for (ns, _k), v in store.items.items()
            if ns == ("db_deepagent", "threads", "s1")
        ]
        assert tracks and tracks[0]["last_active_at"]


class TestCarriedEvidencePersistence:
    """§13.4/§13.3：P3 落库的 carried_evidence 不得被清空。"""

    def _rotate_runner(self, services, store, agent):
        from src.modules.db_deepagent.agent import DbAgentHandle

        cfg = services.config.model_copy(deep=True)
        cfg.thread.rotate_after_messages = 2
        services.config = cfg
        return DbAgentRunner(
            services=services,
            handle=DbAgentHandle(
                agent=agent,
                backend=None,
                permissions=[],
                harness_key="k",
                session_dir=Path("."),
            ),
            archive=QaArchive(store),
        )

    def test_rotation_persists_carried_evidence(self, assembly_services):
        """轮换运行后 sessions.carried_evidence 保存实际携带清单（时间+执行）。"""
        store = FakeStore()
        store.put(
            ("db_deepagent", "sessions"),
            "s1",
            {
                "session_id": "s1",
                "current_thread_id": "dbda:s1:1",
                "thread_seq": 1,
                "source_id": TEST_SOURCE_ID,
            },
        )
        old_state = {
            "messages": [object(), object()],
            "db_executions": [_exec()],
            "db_analyses": [],
            "db_validations": {
                "v1": {
                    "status": "valid",
                    "guard_evidence": {
                        "time": [
                            {
                                "time_evidence_id": "T-old",
                                "run_id": "run-x",
                                "source_text": "9 月",
                                "grain": "month",
                                "start": "2026-09-01",
                                "end": "2026-10-01",
                                "timezone": "Asia/Shanghai",
                                "datasource_id": "orders",
                                "inherited_from": None,
                            }
                        ],
                        "dim": [],
                    },
                }
            },
        }
        agent = FakeAgent({})
        agent.states_by_thread = {
            "dbda:s1:1": old_state,
            "dbda:s1:2": {"messages": []},
        }
        runner = self._rotate_runner(assembly_services, store, agent)
        result = runner.run("问", "s1", request_id="req-carried")
        assert result.thread_id == "dbda:s1:2"  # 轮换到新线程
        session = store.items[(("db_deepagent", "sessions"), "s1")]
        kinds = {item.get("kind") for item in session["carried_evidence"]}
        assert "time" in kinds and "execution" in kinds, session["carried_evidence"]

    def test_normal_run_preserves_existing_carried(self, assembly_services):
        """普通运行不覆盖既有携带清单（修复前固定写 [] 清空）。"""
        store = FakeStore()
        seeded = [
            {
                "kind": "time",
                "record": {
                    "time_evidence_id": "T-old",
                    "run_id": "run-old",
                    "source_text": "9 月",
                    "grain": "month",
                    "start": "2026-09-01",
                    "end": "2026-10-01",
                    "timezone": "Asia/Shanghai",
                    "datasource_id": "orders",
                    "inherited_from": None,
                },
                "origin": None,
            }
        ]
        store.put(
            ("db_deepagent", "sessions"),
            "s1",
            {
                "session_id": "s1",
                "current_thread_id": "dbda:s1:1",
                "thread_seq": 1,
                "source_id": TEST_SOURCE_ID,
                "carried_evidence": seeded,
            },
        )
        agent = FakeAgent(_state())  # 空消息：不触发轮换
        result = _runner(assembly_services, agent, store, _state()).run(
            "问", "s1", request_id="req-keep"
        )
        assert result.status == "completed" and result.thread_id == "dbda:s1:1"
        session = store.items[(("db_deepagent", "sessions"), "s1")]
        assert session["carried_evidence"] == seeded

    def test_force_rotate_below_threshold(self, assembly_services):
        """force_rotate=True 在消息数未达阈值时也轮换（/rotate 接线）。"""
        store = FakeStore()
        store.put(
            ("db_deepagent", "sessions"),
            "s1",
            {
                "session_id": "s1",
                "current_thread_id": "dbda:s1:1",
                "thread_seq": 1,
                "source_id": TEST_SOURCE_ID,
            },
        )
        agent = FakeAgent({})
        agent.states_by_thread = {
            "dbda:s1:1": {"messages": [object()]},  # 仅 1 条 < 阈值
            "dbda:s1:2": {"messages": []},
        }
        runner = self._rotate_runner(assembly_services, store, agent)
        # _rotate_runner 把阈值调到 2；1 条消息不达阈值，靠 force 触发
        result = runner.run("问", "s1", request_id="req-force", force_rotate=True)
        assert result.thread_id == "dbda:s1:2"

    def test_force_rotate_empty_thread_no_rotation(self, assembly_services):
        """空线程强制轮换无意义：仍留在当前线程。"""
        store = FakeStore()
        agent = FakeAgent({})
        agent.states_by_thread = {"dbda:s1:1": {"messages": []}}
        runner = self._rotate_runner(assembly_services, store, agent)
        result = runner.run("问", "s1", request_id="req-force-empty", force_rotate=True)
        assert result.thread_id == "dbda:s1:1"

    def test_rotation_probe_read_failure_fails_fast(self, assembly_services):
        """候选编号探测读取失败不再按“存在”处理——交 P0 报错，不无限递增。"""
        from src.modules.db_deepagent.agent import DbAgentHandle

        store = FakeStore()
        store.put(
            ("db_deepagent", "sessions"),
            "s1",
            {
                "session_id": "s1",
                "current_thread_id": "dbda:s1:1",
                "thread_seq": 1,
                "source_id": TEST_SOURCE_ID,
            },
        )
        agent = FakeAgent({})
        agent.states_by_thread = {
            "dbda:s1:1": {"messages": [object(), object()]},
        }
        probed: list[str] = []

        def _get_state(config):
            from types import SimpleNamespace

            tid = (config.get("configurable") or {}).get("thread_id")
            probed.append(tid)
            if tid in agent.states_by_thread:
                return SimpleNamespace(
                    values=agent.states_by_thread[tid], next=(), created_at=None
                )
            raise psycopg.OperationalError("platform down")  # 候选编号读取持续失败

        agent.get_state = _get_state
        cfg = assembly_services.config.model_copy(deep=True)
        cfg.thread.rotate_after_messages = 2
        assembly_services.config = cfg
        runner = DbAgentRunner(
            services=assembly_services,
            handle=DbAgentHandle(
                agent=agent,
                backend=None,
                permissions=[],
                harness_key="k",
                session_dir=Path("."),
            ),
            archive=QaArchive(store),
        )
        result = runner.run("问", "s1", request_id="req-probe-fail")
        assert result.status == "persistence_error"
        assert any(e.phase == "P0" and e.kind == "persistence" for e in result.errors)
        # 未无限递增：探到线程 2 读取失败即终止，不会继续 3、4……
        assert "dbda:s1:3" not in probed


class TestCarriedDedup:
    """§13.4：去重键含取值集合，同键优先保留最近使用。"""

    def _dim(self, values, dim_id="d1"):
        return {
            "dim_evidence_id": dim_id,
            "run_id": "run-x",
            "source": "db_probe",
            "table": "public.orders",
            "column": "status",
            "values": values,
            "patterns": [],
            "query_text": None,
            "ref": None,
            "datasource_id": "orders",
            "inherited_from": None,
        }

    def test_same_column_different_values_both_carried(self, assembly_services):
        """同列不同取值口径不互相挤掉（修复前只留先登记的 paid）。"""
        from src.modules.db_deepagent.runner import _collect_carried

        old_values = {
            "db_validations": {
                "v1": {
                    "status": "valid",
                    "guard_evidence": {
                        "time": [],
                        "dim": [self._dim(["paid"], "d-paid")],
                    },
                },
                "v2": {
                    "status": "valid",
                    "guard_evidence": {
                        "time": [],
                        "dim": [self._dim(["cancelled"], "d-cancelled")],
                    },
                },
            }
        }
        carried = _collect_carried(old_values, (), assembly_services.config)
        values_sets = {
            tuple(item["record"]["values"]) for item in carried if item["kind"] == "dim"
        }
        assert ("paid",) in values_sets and ("cancelled",) in values_sets

    def test_same_key_most_recent_wins(self, assembly_services):
        """同键（同列同取值集合）后写覆盖先写——保留最近使用的口径。"""
        from src.modules.db_deepagent.runner import _collect_carried

        old_values = {
            "db_validations": {
                "v1": {
                    "status": "valid",
                    "guard_evidence": {
                        "time": [],
                        "dim": [self._dim(["paid"], "d-early")],
                    },
                },
                "v2": {
                    "status": "valid",
                    "guard_evidence": {
                        "time": [],
                        "dim": [self._dim(["paid"], "d-late")],
                    },
                },
            }
        }
        carried = _collect_carried(old_values, (), assembly_services.config)
        dim_items = [i for i in carried if i["kind"] == "dim"]
        assert len(dim_items) == 1
        assert dim_items[0]["record"]["dim_evidence_id"] == "d-late"

    def test_time_key_includes_timezone(self, assembly_services):
        """时间证据去重键含时区：同时区同区间才合并。"""
        from src.modules.db_deepagent.runner import _collect_carried

        def _time(tz, tid):
            return {
                "time_evidence_id": tid,
                "run_id": "run-x",
                "source_text": "9 月",
                "grain": "month",
                "start": "2026-09-01",
                "end": "2026-10-01",
                "timezone": tz,
                "datasource_id": "orders",
                "inherited_from": None,
            }

        old_values = {
            "db_validations": {
                "v1": {
                    "status": "valid",
                    "guard_evidence": {
                        "time": [_time("Asia/Shanghai", "t-sh")],
                        "dim": [],
                    },
                }
            }
        }
        prev = (
            {
                "kind": "time",
                "record": _time("UTC", "t-utc"),
                "origin": None,
            },
        )
        carried = _collect_carried(old_values, prev, assembly_services.config)
        tzs = {i["record"]["timezone"] for i in carried if i["kind"] == "time"}
        assert tzs == {"Asia/Shanghai", "UTC"}

    def test_cap_keeps_most_recently_used(self, assembly_services):
        """达到上限时按最近使用顺序截断：先 paid 后 cancelled，留 cancelled。"""
        from src.modules.db_deepagent.runner import _collect_carried

        old_values = {
            "db_validations": {
                "v1": {
                    "status": "valid",
                    "guard_evidence": {
                        "time": [],
                        "dim": [self._dim(["paid"], "d-early")],
                    },
                },
                "v2": {
                    "status": "valid",
                    "guard_evidence": {
                        "time": [],
                        "dim": [self._dim(["cancelled"], "d-late")],
                    },
                },
            }
        }
        cfg = assembly_services.config.model_copy(deep=True)
        cfg.thread.carry_max_evidence = 1
        carried = _collect_carried(old_values, (), cfg)
        dim_items = [i for i in carried if i["kind"] == "dim"]
        assert len(dim_items) == 1
        assert dim_items[0]["record"]["values"] == ["cancelled"]


class TestModelRefusalThroughMainGraph:
    """doc 1 §4.4／§6.4 专项：标准 refusal 块走完整主图到 Runner。

    受控假模型返回标准 refusal 内容块（非普通文本拒答），经真实
    ModelRefusalMiddleware（0.7.34 默认栈）→ 完整中间件栈 → agent.invoke()
    → Runner：确认 ModelRefusalError 传播为 P1 runtime、优先于
    "未提交最终答案"、预算条目在 finally 中 close_run。
    """

    def _build_handle(self, assembly_services, monkeypatch, responses):
        from langgraph.checkpoint.memory import InMemorySaver

        import src.modules.db_deepagent.agent as agent_mod
        from src.tests.db_deepagent.conftest import ScriptedChatModel

        fake = ScriptedChatModel(responses=responses)
        monkeypatch.setattr(agent_mod, "build_model", lambda _cfg: fake)
        return agent_mod.build_db_agent(
            assembly_services,
            session_id="t-refusal",
            checkpointer=InMemorySaver(),
        )

    def test_standard_refusal_propagates_as_p1_runtime(
        self, assembly_services, monkeypatch
    ):
        from langchain_core.messages import AIMessage

        from src.modules.db_deepagent.persistence.archive import QaArchive

        handle = self._build_handle(
            assembly_services,
            monkeypatch,
            responses=[
                AIMessage(
                    content=[{"type": "refusal", "refusal": "我不能协助这个请求。"}]
                ),
            ],
        )
        store = FakeStore()
        runner = DbAgentRunner(
            services=assembly_services,
            handle=handle,
            archive=QaArchive(store),
        )
        close_calls: list[str] = []
        original_close = assembly_services.budget.close_run

        def _spy_close(run_id):
            close_calls.append(run_id)
            return original_close(run_id)

        assembly_services.budget.close_run = _spy_close
        result = runner.run("敏感问题", "s-refusal", request_id="req-refusal")

        assert result.status == "failed"
        p1 = [f for f in result.errors if f.phase == "P1"]
        assert p1, f"必须记录 P1 失败: {result.errors}"
        assert any(
            "ModelRefusalError" in f.message for f in p1
        ), f"P1 应含 ModelRefusalError 传播: {[f.message for f in p1]}"
        assert any(f.kind == "runtime" for f in p1)
        # runtime 优先：不得同时判定"未提交最终答案"
        assert not any(f.kind == "no_final_answer" for f in result.errors)
        # 预算条目在 finally 中关闭
        assert close_calls == [
            result.run_id
        ], f"close_run 必须恰好对本 run 调用一次: {close_calls}"

    def test_plain_text_refusal_is_not_protocol_refusal(
        self, assembly_services, monkeypatch
    ):
        """对照：普通文本拒答不是标准 refusal 块，不抛 ModelRefusalError。

        运行自然结束，按既有协议判"未提交最终答案"（P2）。
        """
        from langchain_core.messages import AIMessage

        from src.modules.db_deepagent.persistence.archive import QaArchive

        handle = self._build_handle(
            assembly_services,
            monkeypatch,
            responses=[AIMessage(content="抱歉，这个问题我无法回答。")],
        )
        store = FakeStore()
        runner = DbAgentRunner(
            services=assembly_services,
            handle=handle,
            archive=QaArchive(store),
        )
        result = runner.run("普通问题", "s-plain", request_id="req-plain")
        assert result.status == "failed"
        assert not any(
            "ModelRefusalError" in f.message for f in result.errors
        ), "普通文本拒答不得触发 ModelRefusalError"
        assert any(f.kind == "no_final_answer" for f in result.errors)


# ============================================================================
# doc 6.2 §7：最终答案兜底——完整收尾、运行归属、一次归一、行数映射与归档 v2
# ============================================================================


def _qa_records(store):
    return [v for key, v in store.items.items() if key[0][1] == "qa"]


def _err_records(store):
    return [v for key, v in store.items.items() if key[0][1] == "run_errors"]


class SqlOkAgent(FakeAgent):
    """invoke 成功并按上下文重映射；随后可对答案记录做受控篡改（归属用例）。"""

    def __init__(self, state, answer_mutate=None):
        super().__init__(state)
        self._answer_mutate = answer_mutate

    def invoke(self, inputs, config=None, context=None):
        self.invoke_calls.append(
            {"inputs": inputs, "config": config, "context": context}
        )
        if context is not None:
            self.state_values = _remap_run_id(
                self.state_values, context.run_id, context.request_id
            )
            if self._answer_mutate is not None:
                answers = self.state_values.setdefault("db_final_answers", {})
                record = answers.get(context.run_id)
                if record is not None:
                    self._answer_mutate(record)
        return {"messages": self.state_values.get("messages", [])}


class TestFallbackFinalization:
    """doc 6.2 §7.8：完整 Runner 收尾（评估→兜底→P3→返回）与归档 v2。"""

    ILLEGAL_PAYLOADS = {
        "answer_none": {
            "status": "completed",
            "answer": None,
            "findings": [],
            "metric_definitions": [],
            "limitations": [],
            "open_questions": [],
        },
        "answer_empty_str": {
            "status": "completed",
            "answer": "",
            "findings": [],
            "metric_definitions": [],
            "limitations": [],
            "open_questions": [],
        },
        "answer_blank": {
            "status": "completed",
            "answer": " \n\t ",
            "findings": [],
            "metric_definitions": [],
            "limitations": [],
            "open_questions": [],
        },
        "answer_list": {
            "status": "completed",
            "answer": ["不是字符串"],
            "findings": [],
            "metric_definitions": [],
            "limitations": [],
            "open_questions": [],
        },
        "findings_str": {
            "status": "completed",
            "answer": "正文",
            "findings": "不是列表",
            "metric_definitions": [],
            "limitations": [],
            "open_questions": [],
        },
        "findings_nondict_items": {
            "status": "completed",
            "answer": "正文",
            "findings": [{"text": "x", "evidence_ids": ["E-run00001"]}, "混入字符串"],
            "metric_definitions": [],
            "limitations": [],
            "open_questions": [],
        },
    }

    @pytest.mark.parametrize("variant", sorted(ILLEGAL_PAYLOADS))
    @pytest.mark.parametrize("reused", [False, True], ids=["real-exec", "pure-reuse"])
    def test_illegal_payload_full_tail(self, assembly_services, variant, reused):
        """非法 payload 经完整收尾：failed + 系统兜底，无额外 P3 runtime。

        纯沿用不满足条件 b → run_errors；真实执行按条件 a → qa；原 answer
        值按约定精确保留；delivery_answer 与实时返回一致。
        """
        payload = self.ILLEGAL_PAYLOADS[variant]
        state = _state(answer=payload, executions=[_exec(reused=reused)])
        store = FakeStore()
        result = _runner(assembly_services, SqlOkAgent(state), store, state).run(
            "问", "s1", request_id="req-illegal"
        )
        assert result.status == "failed"  # 不得保留 completed
        assert result.answer.startswith("系统兜底说明：")
        assert any("服务端兜底" in w for w in result.warnings)
        assert not result.findings
        # 不新增 P3 runtime / 归档异常
        assert all(e.phase != "P3" for e in result.errors)
        assert any(e.kind == "no_final_answer" and e.primary for e in result.errors)
        # 分流：真实执行 → qa；纯沿用 → run_errors
        if reused:
            assert not _qa_records(store) and _err_records(store)
            record = _err_records(store)[0]
        else:
            assert _qa_records(store) and not _err_records(store)
            record = _qa_records(store)[0]
        # 原值按类型精确保留（answer 键存在 → 原样；不作 {} 替换）
        assert record["answer"] == payload
        assert record["answer_source"] == "system_fallback"
        assert record["archive_schema_version"] == 2
        assert record["delivery_answer"]["answer"] == result.answer
        assert record["delivery_answer"]["findings"] == []

    def test_blank_completed_never_completed_plus_fallback(self, assembly_services):
        """空白 completed：判 failed + 兜底，绝不出现 completed + system_fallback。"""
        payload = dict(self.ILLEGAL_PAYLOADS["answer_blank"])
        payload["findings"] = [{"text": "发现", "evidence_ids": ["E-run00001"]}]
        state = _state(answer=payload)
        store = FakeStore()
        result = _runner(assembly_services, SqlOkAgent(state), store, state).run(
            "问", "s1", request_id="req-blank"
        )
        assert result.status == "failed"
        assert result.answer.startswith("系统兜底说明：")

    def test_normal_answer_not_overridden_with_v2_fields(self, assembly_services):
        """正常有效答案：不覆盖；qa 记录 v2 字段齐备，原 answer 保留模型提交。

        doc 17：成功交付不再向正文追加分项审计段（结论 F-/已完成/已作废），
        交付正文即模型原文；失败/澄清场景的追加见 doc 17 精简模式测试。
        """
        store = FakeStore()
        result, _ = _run_ok(assembly_services, store, _state())
        assert result.status == "completed"
        assert result.answer == ANSWER["answer"]  # doc 17：无追加审计段
        for token in ("结论 F-", "已完成：", "已作废："):
            assert token not in result.answer
        assert not any("服务端兜底" in w for w in result.warnings)
        qa = _qa_records(store)[0]
        assert qa["archive_schema_version"] == 2
        assert qa["answer_source"] == "model"
        assert qa["answer"] == ANSWER  # 原模型提交 payload 原样
        # R1-13：交付正文与归档一致；原始 payload 原样
        assert qa["delivery_answer"]["answer"] == result.answer
        assert qa["delivery_answer"]["status"] == "completed"

    def test_no_submission_with_evidence_falls_back(self, assembly_services):
        """已开始调用但未提交 + 有真实执行：failed + 兜底，仍按条件 a 进 qa。"""
        state = _state(answer=None)
        store = FakeStore()
        result = _runner(assembly_services, SqlOkAgent(state), store, state).run(
            "问", "s1", request_id="req-nosub"
        )
        assert result.status == "failed"
        assert result.answer.startswith("系统兜底说明：")
        assert _qa_records(store)

    def test_started_without_evidence_fallback_no_result_claim(self, assembly_services):
        """已开始调用但无证据：failed + 兜底说明未取得结果，不声称查询成功。"""
        state = _state(answer=None, executions=[])
        store = FakeStore()
        result = _runner(assembly_services, SqlOkAgent(state), store, state).run(
            "问", "s1", request_id="req-noev"
        )
        assert result.status == "failed"
        assert "未取得可确认的查询或分析结果" in result.answer
        assert "查询成功" not in result.answer

    def test_pre_rejection_keeps_empty_answer_no_fallback(self, assembly_services):
        """前置拒绝（request_id 冲突）：维持原空答案行为，无兜底与兜底 warning。"""
        store = FakeStore()
        state = _state()
        runner = _runner(assembly_services, SqlOkAgent(state), store, state)
        first = runner.run("问", "s1", request_id="req-dup")
        assert first.status == "completed"
        second = runner.run("问", "s1", request_id="req-dup")
        assert second.status == "failed"
        assert second.answer == ""  # 未调用 Agent：不生成兜底
        assert not any("服务端兜底" in w for w in second.warnings)
        assert any("已存在本地运行记录" in e.message for e in second.errors)


class TestOwnershipChecks:
    """doc 6.2 §7.2.1：外层 run_id/request_id 严格归属（字典键属于本轮）。"""

    @pytest.mark.parametrize(
        "mutate",
        [
            pytest.param(
                lambda r: r.__setitem__("run_id", "run-other"), id="run-id-mismatch"
            ),
            pytest.param(
                lambda r: r.__setitem__("request_id", "req-other"),
                id="request-id-mismatch",
            ),
            pytest.param(lambda r: r.pop("run_id"), id="run-id-missing"),
            pytest.param(lambda r: r.pop("request_id"), id="request-id-missing"),
        ],
    )
    @pytest.mark.parametrize("reused", [False, True], ids=["real-exec", "pure-reuse"])
    def test_mismatch_is_invalid_submission(self, assembly_services, mutate, reused):
        state = _state(answer=ANSWER, executions=[_exec(reused=reused)])
        store = FakeStore()
        result = _runner(
            assembly_services, SqlOkAgent(state, answer_mutate=mutate), store, state
        ).run("问", "s1", request_id="req-own")
        assert result.status == "failed"
        assert result.answer.startswith("系统兜底说明：")
        assert any(e.kind == "no_final_answer" and e.primary for e in result.errors)
        assert all(e.phase != "P3" for e in result.errors)
        if reused:
            assert _err_records(store) and not _qa_records(store)
            record = _err_records(store)[0]
        else:
            assert _qa_records(store) and not _err_records(store)
            record = _qa_records(store)[0]
        # 原 payload（含被篡改的标识）保留审计，不因归属不符改写
        assert record["answer"] == ANSWER
        assert record["answer_source"] == "system_fallback"

    def test_valid_identifiers_positive_control(self, assembly_services):
        """正向对照：正常工具写入的标识一致记录仍有效（completed）。"""
        store = FakeStore()
        result, _ = _run_ok(assembly_services, store, _state())
        assert result.status == "completed"


class TestSingleNormalization:
    """doc 6.2 §7.3 注 9：收尾入口只归一一次；归属不符不解析正文。"""

    def test_valid_submission_single_validate(self, assembly_services, monkeypatch):
        from src.modules.db_deepagent import schemas as schemas_mod

        calls = {"n": 0}
        original = schemas_mod.DbAgentFinalAnswer.model_validate

        def counting(payload):
            calls["n"] += 1
            return original(payload)

        monkeypatch.setattr(schemas_mod.DbAgentFinalAnswer, "model_validate", counting)
        store = FakeStore()
        result, _ = _run_ok(assembly_services, store, _state())
        assert result.status == "completed"
        assert calls["n"] == 1  # Runner 本次收尾恰好一次 Pydantic 解析

    def test_ownership_failure_skips_payload_parsing(
        self, assembly_services, monkeypatch
    ):
        from src.modules.db_deepagent import schemas as schemas_mod

        calls = {"n": 0}
        original = schemas_mod.DbAgentFinalAnswer.model_validate

        def counting(payload):
            calls["n"] += 1
            return original(payload)

        monkeypatch.setattr(schemas_mod.DbAgentFinalAnswer, "model_validate", counting)
        state = _state(answer=ANSWER)
        store = FakeStore()
        result = _runner(
            assembly_services,
            SqlOkAgent(
                state, answer_mutate=lambda r: r.__setitem__("run_id", "run-other")
            ),
            store,
            state,
        ).run("问", "s1", request_id="req-own2")
        assert result.status == "failed"
        assert calls["n"] == 0  # 归属不符：不解析正文


class TestKnownRowCountsAndTemplate:
    """doc 6.2 §7.2.2/§7.2.3：可靠行数映射与简版兜底模板。"""

    @staticmethod
    def _counts_map(records_by_eid):
        """构造 db_executions 记录并直接调用映射函数。"""
        from src.modules.db_deepagent.runner import DbAgentRunner

        values = {
            "db_executions": [
                {"run_id": "run-k", "purpose": "query", "evidence_id": eid, **rest}
                for eid, rest in records_by_eid.items()
            ],
            "db_analyses": [],
        }
        return DbAgentRunner._known_saved_row_counts(values, "run-k")

    def test_known_zero_is_zero(self):
        counts = self._counts_map({"E1": {"saved_row_count": 0}})
        assert counts == {"E1": 0}

    @pytest.mark.parametrize(
        "raw",
        [None, True, False, -1, "5", 5.0],
        ids=["none", "true", "false", "neg", "str", "float"],
    )
    def test_illegal_or_missing_is_unknown(self, raw):
        rest = {} if raw == "MISSING" else {"saved_row_count": raw}
        counts = self._counts_map({"E1": rest})
        assert counts == {"E1": None}

    def test_missing_key_is_unknown(self):
        counts = self._counts_map({"E1": {}})
        assert counts == {"E1": None}

    def test_conflicting_duplicates_unknown(self):
        values = {
            "db_executions": [
                {
                    "run_id": "run-k",
                    "purpose": "query",
                    "evidence_id": "E1",
                    "saved_row_count": 3,
                },
                {
                    "run_id": "run-k",
                    "purpose": "query",
                    "evidence_id": "E1",
                    "saved_row_count": 4,
                },
            ],
            "db_analyses": [],
        }
        from src.modules.db_deepagent.runner import DbAgentRunner

        assert DbAgentRunner._known_saved_row_counts(values, "run-k") == {"E1": None}

    def test_other_run_and_probe_excluded(self):
        values = {
            "db_executions": [
                {
                    "run_id": "run-other",
                    "purpose": "query",
                    "evidence_id": "E-other",
                    "saved_row_count": 7,
                },
                {
                    "run_id": "run-k",
                    "purpose": "probe",
                    "evidence_id": "E-probe",
                    "saved_row_count": 7,
                },
            ],
            "db_analyses": [
                {"run_id": "run-k", "evidence_id": "A-1", "saved_row_count": 2},
            ],
        }
        from src.modules.db_deepagent.runner import DbAgentRunner

        counts = DbAgentRunner._known_saved_row_counts(values, "run-k")
        assert counts == {"A-1": 2}  # 探针与他轮记录不进入映射

    def test_template_omits_unknown_count_and_ids(self):
        from src.modules.db_deepagent.runner import DbAgentRunner
        from src.modules.db_deepagent.schemas import EvidenceItem

        ev = EvidenceItem(
            evidence_id="E-internal01",
            kind="sql_query",
            tool_call_id="t",
            source="orders",
            sql="SELECT 1",
            result_ref="res_x",
            row_count=0,  # 兼容投影零（真实未知）
            preview_truncated=False,
            source_truncated=False,
        )
        text = DbAgentRunner._build_fallback_answer(
            "failed", [ev], {"E-internal01": None}
        )
        assert not re.search(r"已记录 \d+ 行", text)  # 未知行数整句省略
        assert "未知" not in text
        assert "E-internal01" not in text  # 不展示内部证据编号
        assert "账本" not in text

    def test_template_known_counts_and_boundaries(self):
        from src.modules.db_deepagent.runner import DbAgentRunner
        from src.modules.db_deepagent.schemas import EvidenceItem

        origin = {
            "evidence_id": "E-old",
            "thread_id": "",
            "request_id": "",
            "run_id": "run-old",
            "datasource_id": "orders",
            "user_text": None,
        }

        def _ev(eid, kind="sql_query", reused=False, truncated=False):
            return EvidenceItem(
                evidence_id=eid,
                kind=kind,
                tool_call_id="t",
                source="orders",
                sql="SELECT 1" if kind == "sql_query" else None,
                result_ref="res_x",
                input_result_refs=[] if kind == "sql_query" else ["res_y"],
                operation=None if kind == "sql_query" else {"name": "aggregate"},
                row_count=0,
                preview_truncated=False,
                source_truncated=truncated,
                reused_from=dict(origin) if reused else None,
            )

        evs = [
            _ev("E1"),
            _ev("E2", kind="result_analysis", reused=True),
            _ev("E3", truncated=True),
        ] + [
            _ev(f"Ex{i}") for i in range(4, 8)
        ]  # 共 7 条 → 逐项 5 条 + “另有 2 份”
        counts = {"E1": 0, "E2": 12, "E3": None}
        text = DbAgentRunner._build_fallback_answer("budget_exhausted", evs, counts)
        assert "已达到运行次数或时间限制" in text
        assert "该查询没有返回记录" in text  # M1：查询零行专用文案
        assert "该分析没有输出记录" not in text  # E1 是查询，不是分析
        assert "已记录 0 行" not in text  # 零行不再写“已记录 0 行”
        assert "已记录 12 行结果" in text
        assert "结果 2：" in text and "来自历史记录" in text  # 沿用说明
        assert "结果仅包含部分数据" in text  # 截断说明
        assert "另有 2 份结果记录" in text
        assert "完整答案尚未生成" in text
        # 未提及行数的 E3 不出现“已记录 … 行”，也不写“未知”
        assert not re.search(
            r"已记录 \d+ 行",
            text.replace("已记录 12 行结果", ""),
        )

    def test_analysis_zero_row_copy(self):
        """M1：分析零行写“该分析没有输出记录。”，与查询零行区分。"""
        from src.modules.db_deepagent.runner import DbAgentRunner
        from src.modules.db_deepagent.schemas import EvidenceItem

        ev = EvidenceItem(
            evidence_id="A-zero",
            kind="result_analysis",
            tool_call_id="t",
            source="orders",
            sql=None,
            result_ref="res_y",
            input_result_refs=["res_x"],
            operation={"name": "aggregate"},
            row_count=0,
            preview_truncated=False,
            source_truncated=False,
        )
        text = DbAgentRunner._build_fallback_answer("failed", [ev], {"A-zero": 0})
        assert "该分析没有输出记录" in text
        assert "该查询没有返回记录" not in text

    def test_run_with_missing_row_count_keeps_evidence_compatible(
        self, assembly_services
    ):
        """账本缺 saved_row_count：EvidenceItem.row_count 兼容为 0，模板省略行数句。"""
        exec_rec = _exec()
        exec_rec.pop("saved_row_count")
        state = _state(answer=None, executions=[exec_rec])
        store = FakeStore()
        result = _runner(assembly_services, SqlOkAgent(state), store, state).run(
            "问", "s1", request_id="req-nocount"
        )
        assert result.status == "failed"
        assert result.evidence[0].row_count == 0  # 外部字段兼容投影
        assert not re.search(r"已记录 \d+ 行", result.answer)  # 不把兼容零当真实零


class TestArchiveV2Read:
    """doc 6.2 §7.4：read_delivery_answer 版本路由与 v1 status 改名。"""

    def test_v1_status_renamed_to_submitted_status(self):
        record = {
            "status": "failed",
            "answer": {"status": "completed", "answer": "旧答案"},
        }
        view = QaArchive.read_delivery_answer(record)
        assert "status" not in view
        assert view["submitted_status"] == "completed"
        assert view["answer"] == "旧答案"
        assert record["answer"]["status"] == "completed"  # 原档不改写

    def test_v1_missing_status_not_fabricated(self):
        view = QaArchive.read_delivery_answer({"answer": {"answer": "x"}})
        assert "submitted_status" not in view and "status" not in view

    def test_v1_conflicting_submitted_status_uses_original(self):
        view = QaArchive.read_delivery_answer(
            {"answer": {"status": "partial", "submitted_status": "fake"}}
        )
        assert view["submitted_status"] == "partial"

    def test_v1_illegal_answer_returns_empty_view(self):
        assert QaArchive.read_delivery_answer({"answer": "corrupt"}) == {}
        assert QaArchive.read_delivery_answer({}) == {}

    def test_v2_returns_delivery_copy(self):
        record = {
            "archive_schema_version": 2,
            "status": "failed",
            "delivery_answer": {"status": "failed", "answer": "兜底"},
        }
        view = QaArchive.read_delivery_answer(record)
        assert view["status"] == "failed" and view["answer"] == "兜底"
        view["answer"] = "mutated"
        assert record["delivery_answer"]["answer"] == "兜底"  # 浅拷贝隔离

    @pytest.mark.parametrize(
        "record",
        [
            pytest.param({"archive_schema_version": 2}, id="missing-delivery"),
            pytest.param(
                {"archive_schema_version": 2, "delivery_answer": "x"}, id="non-dict"
            ),
            pytest.param({"archive_schema_version": 3}, id="unknown-version"),
            pytest.param({"archive_schema_version": True}, id="bool-version"),
            pytest.param({"archive_schema_version": "2"}, id="str-version"),
        ],
    )
    def test_contract_violations_raise(self, record):
        with pytest.raises(ValueError):
            QaArchive.read_delivery_answer(record)

    def test_written_v2_records_roundtrip(self, assembly_services):
        """Runner 写出的 v2 qa 记录可直接经 read_delivery_answer 读回交付内容。"""
        store = FakeStore()
        result, _ = _run_ok(assembly_services, store, _state())
        qa = _qa_records(store)[0]
        view = QaArchive.read_delivery_answer(qa)
        assert view["answer"] == result.answer
        # 跨版本统计读顶层 status（两版本均为 Runner 状态）
        assert qa["status"] == "completed"


def _raw_state(raw_answer, executions=None, omit_answer_key=False):
    """构造外层记录合法（run_id/request_id 重映射后一致）、answer 原值任意的状态。

    与 _state() 不同：raw_answer 允许任意类型（None/空串/列表/False/0），
    记录始终存在；omit_answer_key=True 时构造缺失 answer 键的对照。
    账本合法（T1 completed）：非法原始 payload 属结构归一失败 →
    no_final_answer（doc 6.3 §7.5：不因无账本误判为门控拒绝）。
    """
    record = {"run_id": "run-x", "request_id": "req-x"}
    if not omit_answer_key:
        record["answer"] = raw_answer
    return {
        "messages": [],
        "db_executions": (executions if executions is not None else [_exec()]),
        "db_analyses": [],
        "db_validations": {},
        "db_final_answers": {"run-x": record},
        **_task_ledger(),
    }


class TestRawPayloadPreservation:
    """doc 6.2 §7.4：FinalAnswerRecord.answer 原值为非法类型时的完整收尾。

    R3：此前矩阵只覆盖字典内字段非法；这里直接对原始 payload 类型（含
    假值）与缺失 answer 键做对照，验证“键存在按原值保留、缺失才写 {}”。
    """

    RAW_VARIANTS = {
        "payload_str": "不是字典的正文",
        "payload_list": ["不是字典"],
        "payload_none": None,
        "payload_empty_str": "",
        "payload_empty_list": [],
        "payload_false": False,
        "payload_zero": 0,
        "findings_dict": {
            "status": "completed",
            "answer": "正文",
            "findings": {"text": "x"},
            "metric_definitions": [],
            "limitations": [],
            "open_questions": [],
        },
    }

    @pytest.mark.parametrize("variant", sorted(RAW_VARIANTS))
    @pytest.mark.parametrize("reused", [False, True], ids=["real-exec", "pure-reuse"])
    def test_raw_illegal_payload_full_tail(self, assembly_services, variant, reused):
        raw = self.RAW_VARIANTS[variant]
        state = _raw_state(raw, executions=[_exec(reused=reused)])
        store = FakeStore()
        result = _runner(assembly_services, SqlOkAgent(state), store, state).run(
            "问", "s1", request_id="req-raw"
        )
        assert result.status == "failed"
        assert result.answer.startswith("系统兜底说明：")
        assert not result.findings
        assert all(e.phase != "P3" for e in result.errors)  # 不新增 P3 runtime
        assert any(e.kind == "no_final_answer" and e.primary for e in result.errors)
        record = _err_records(store)[0] if reused else _qa_records(store)[0]
        assert (bool(_qa_records(store))) is (not reused)  # 分流不因兜底改变
        # 原值按类型与值精确保留：先精确类型再值（False==0 之类不放过，R2-02）
        assert type(record["answer"]) is type(raw)
        assert record["answer"] == raw
        assert record["answer_source"] == "system_fallback"
        assert record["delivery_answer"]["answer"] == result.answer

    def test_missing_answer_key_archives_empty_dict(self, assembly_services):
        """answer 键缺失（外层记录在）→ 归档按缺失约定值 {}。"""
        state = _raw_state(None, omit_answer_key=True)
        store = FakeStore()
        result = _runner(assembly_services, SqlOkAgent(state), store, state).run(
            "问", "s1", request_id="req-nokey"
        )
        assert result.status == "failed"
        assert result.answer.startswith("系统兜底说明：")
        qa = _qa_records(store)[0]  # 真实执行按条件 a 进 qa
        assert qa["answer"] == {}
        assert qa["answer_source"] == "system_fallback"


class TestTaskGateFallbackIntegration:
    """doc 6.3 §7.5：资格结论贯穿评估、正文与归档条件 b。"""

    def test_structurally_valid_semantically_rejected(self, assembly_services):
        """非空结构合法但语义非法（漏覆盖）→ failed+system_fallback；
        原正文/findings/口径/补充问题不交付，原始 payload 保真。"""
        store = FakeStore()
        answer = {
            **ANSWER,
            "findings": [],  # 漏覆盖 completed 任务
        }
        result, _ = _run_ok(assembly_services, store, _state(answer=answer))
        assert result.status == "failed"
        assert result.answer.startswith("系统兜底说明：")
        assert not result.findings
        assert any(
            e.kind == "task_completion_rejected" and e.primary for e in result.errors
        )
        qa = _qa_records(store)[0]
        assert qa["answer_source"] == "system_fallback"
        # 原始 payload 保真（未因资格为空丢失）
        assert qa["answer"]["findings"] == []
        assert qa["answer"]["answer"] == answer["answer"]
        # 无资格提交的补充问题不进入交付
        assert qa["delivery_answer"]["findings"] == []

    def test_legitimate_business_failed_keeps_model_source(self, assembly_services):
        """合法业务 failed（任务登记 failed+unresolved 恰覆盖）→ model 来源。"""
        from src.tests.db_deepagent.conftest import make_task_record

        task = make_task_record(
            "run-x",
            "T1",
            1,
            status="failed",
            reason_code="agent_abandoned",
            reason="无法推进",
        )
        answer = {
            "status": "failed",
            "answer": "本轮未能完成查询。",
            "findings": [],
            "metric_definitions": [],
            "limitations": [],
            "open_questions": [],
            "unresolved_task_refs": [{"sub_task_id": "T1", "revision": 1}],
        }
        state = _state(answer=answer, executions=[], ledger=False)
        state.update(_task_ledger(status="failed", evidence_ids=[], extra_task=task))
        store = FakeStore()
        result, _ = _run_ok(assembly_services, store, state)
        assert result.status == "failed"
        # R1-13：合法交付附加确定性分项段；正文以模型原文开头
        assert result.answer.startswith("本轮未能完成查询。")
        assert "未完成：业务任务（原因：无法推进）" in result.answer
        assert "系统兜底说明：" not in result.answer  # 非空合法正文不兜底
        assert any(e.kind == "no_final_answer" for e in result.errors) is False

    def test_p0_rejection_no_fallback(self, assembly_services, tmp_path):
        """未调用 Agent 的前置拒绝（请求 ID 冲突）：answer_source=none，
        不触发系统兜底（§7.5：invoke_started=False 不进兜底）。"""
        store = FakeStore()
        runner = _runner(assembly_services, FakeAgent(_state()), store, _state())
        # 预置同 request_id 本地记录 → P0 锁内拒绝（不执行 Agent）
        marker = runner._local_record_path("s-p0", "req-p0")
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("{}", encoding="utf-8")
        result = runner.run("问", "s-p0", request_id="req-p0")
        assert result.status == "failed"
        assert result.answer.startswith("系统兜底说明：") is False
        qa_or_err = [*_qa_records(store), *_err_records(store)]
        assert all(r["answer_source"] == "none" for r in qa_or_err)

    def test_pure_reuse_without_qualification_goes_run_errors(self, assembly_services):
        """纯沿用 + 无资格提交 → 条件 b 为假 → run_errors（真实执行仍可 qa）。"""
        store = FakeStore()
        reused = _exec(evidence_id="E-reused1", reused=True)
        answer = {
            **ANSWER,
            "findings": [
                {
                    "text": "沿用结果",
                    "finding_id": "F-1",
                    "task_refs": [{"sub_task_id": "T1", "revision": 1}],
                    "evidence_ids": ["E-reused1"],
                }
            ],
        }
        state = _state(answer=answer, executions=[reused])
        # 任务未选定沿用证据 → 漏覆盖 → 拒绝
        state["db_sub_tasks"][[k for k in state["db_sub_tasks"] if '"T1"' in k][0]][
            "evidence_ids"
        ] = []
        result, _ = _run_ok(assembly_services, store, state)
        assert result.status == "failed"
        assert not _qa_records(store)
        assert _err_records(store)

    def test_delivery_open_questions_merged_list(self, assembly_services):
        """§8.3：交付 open_questions 为合并列表（权威在前），非原提交拷贝。"""
        from src.tests.db_deepagent.conftest import (
            make_task_record,
            make_task_state,
        )

        t1 = make_task_record(
            "run-x",
            "T1",
            1,
            status="needs_clarification",
            reason="口径不明",
            clarification_question="统计含退款吗？",
        )
        t1["clarification_question"] = "统计含退款吗？"
        answer = {
            "status": "needs_clarification",
            "answer": "需要澄清统计口径。",
            "findings": [],
            "metric_definitions": [],
            "limitations": [],
            "open_questions": ["补充：是否按渠道拆分？", "统计含退款吗？"],
            "unresolved_task_refs": [{"sub_task_id": "T1", "revision": 1}],
        }
        state = _state(answer=answer, executions=[], ledger=False)
        ledger_state = make_task_state([t1])
        state.update(ledger_state)
        store = FakeStore()
        result, _ = _run_ok(assembly_services, store, state)
        assert result.status == "needs_clarification"
        # 权威问题在前；重复补充去重保留首次
        assert result.open_questions == [
            "统计含退款吗？",
            "补充：是否按渠道拆分？",
        ]
        # 无真实执行且状态为 needs_clarification（非 completed/partial）
        # → 条件 b 为假 → run_errors（保存同一交付列表）
        err = _err_records(store)[0]
        assert err["delivery_answer"]["open_questions"] == result.open_questions
        # 原提交列表保真
        assert err["answer"]["open_questions"] == [
            "补充：是否按渠道拆分？",
            "统计含退款吗？",
        ]

    def test_run_snapshot_sealed_and_referenced(self, assembly_services):
        """§8.2：QA/run_errors 引用运行快照；快照含全部任务与覆盖结论。"""
        import json as _json

        store = FakeStore()
        result, _ = _run_ok(assembly_services, store, _state())
        assert result.status == "completed"
        qa = _qa_records(store)[0]
        ref = qa["snapshot_ref"]
        assert ref["run_id"] == result.run_id
        snapshot = None
        for (ns, key), value in store.items.items():
            if ns[1] == "run_snapshots":
                snapshot = value
        assert snapshot is not None
        assert snapshot["run_snapshot_schema_version"] == 1
        assert snapshot["run_status_at_seal"] == "completed"
        assert [t["sub_task_id"] for t in snapshot["tasks"]] == ["T1"]
        assert snapshot["coverage"]["accepted"] is True
        assert snapshot["interrupted_tasks"] == []
        _json.dumps(snapshot)  # 可 JSON 序列化
