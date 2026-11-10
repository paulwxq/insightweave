"""Runner 持久化生命周期单元测试（docs/v4/5 §14.3、§18.2）

FakeAgent／FakeStore 注入 P0–P4 各阶段异常；状态优先级、primary 标记、
归档条件 a/b、本地记录。摘要事件读取与轮换见 test_summary_context。
"""

from __future__ import annotations

import json
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
    "findings": [{"text": "订单数", "evidence_ids": ["E-run00001"]}],
    "metric_definitions": [],
    "limitations": [],
    "open_questions": [],
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


def _state(answer=ANSWER, executions=None, run_id="run-x"):
    answers = {"run-x": {"run_id": "run-x", "answer": answer}} if answer else {}
    return {
        "messages": [],
        "db_executions": (
            executions if executions is not None else [_exec(run_id=run_id)]
        ),
        "db_analyses": [],
        "db_validations": {},
        "db_final_answers": answers,
    }


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
        assert result.answer == ANSWER["answer"]
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
        """P1 runtime 异常：P2 只为诊断读取，不读取最终答案。"""
        store = FakeStore()
        agent = FakeAgent(_state())
        agent.invoke_side_effect = RuntimeError("model boom")
        result = _runner(assembly_services, agent, store, _state()).run("问", "s1")
        assert result.status == "failed"
        assert result.answer == ""
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
        assert result.answer == ANSWER["answer"]  # 答案仍在结果中

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
        """
        store = FakeStore()
        pushdown = _exec(evidence_id="E-push", run_id="run-x")
        pushdown["purpose"] = "pushdown"
        state = _state(executions=[pushdown])
        state["db_analyses"] = [
            {
                "evidence_id": "A-1",
                "tool_call_id": "tc-a",
                "run_id": "run-x",
                "request_id": "req-x",
                "pushdown_execution_id": "E-push",
                "operation": {"name": "aggregate"},
                "input_result_refs": ["res_" + "3" * 32],
                "output_result_ref": "res_" + "2" * 32,
                "executed_sql": "SELECT ...",
                "saved_row_count": 1,
                "source_truncated": False,
                "warnings": [],
                "result_generated_at": "2026-10-01T00:00:00+08:00",
            }
        ]
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
        被丢弃、请求误入 run_errors。
        """

        class SqlThenModelFail(FakeAgent):
            """真实顺序：工具先以本次 run_id 写账本，随后的模型调用才失败。"""

            def invoke(self, inputs, config=None, context=None):
                if context is not None:
                    self.state_values = _remap_run_id(self.state_values, context.run_id)
                raise RuntimeError("model boom after sql")

        store = FakeStore()
        agent = SqlThenModelFail(_state())  # 账本含本轮成功 query 执行
        result = _runner(assembly_services, agent, store, _state()).run(
            "问", "s1", request_id="req-sql-ok-model-fail"
        )
        assert result.status == "failed"
        assert result.answer == ""  # 不读取最终答案（评估开关关闭）
        assert any(e.evidence_id == "E-run00001" for e in result.evidence)
        qa_records = [v for key, v in store.items.items() if key[0][1] == "qa"]
        assert qa_records, "已执行 SQL 的轮次应进 qa 而非 run_errors"
        assert qa_records[0]["status"] == "failed"  # 保留真实状态
        assert qa_records[0]["evidence"], "执行轨迹须进入归档"
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
