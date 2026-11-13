"""db_deepagent 测试公共 fixture（docs/v4/5 §18.1）"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.runtime import Runtime
from langgraph.types import Command
from pydantic import Field

from src.modules.db_deepagent.config import DbAgentConfig, load_db_agent_config
from src.modules.db_deepagent.context import DbAgentContext
from src.modules.db_deepagent.schemas import DimEvidence, TimeEvidence
from src.modules.db_deepagent.services import DbAgentServices
from src.modules.db_deepagent.services.business_db import ReadonlyResult
from src.modules.db_deepagent.services.dimension_probe import DimensionProbeBuilder
from src.modules.db_deepagent.services.result_store import ResultStore
from src.modules.db_deepagent.services.run_budget import RunBudgetLedger
from src.modules.db_deepagent.services.sql_guard import SqlEvidenceGuard
from src.modules.db_deepagent.services.sql_pipeline import SqlPipeline
from src.modules.db_deepagent.services.sql_validation import DbSqlValidator
from src.modules.db_deepagent.services.table_catalog import TableCatalog
from src.modules.db_deepagent.services.time_resolver import TimeResolver

# 测试统一使用的来源绑定（doc 5.1 §1 短 ID：服务/会话绑定 source_id 的
# 假数据源 A；双源用例另用 TEST_SOURCE_ID_B 区分同名对象）
TEST_SOURCE_ID = "db_src_1"
TEST_SOURCE_ID_B = "db_src_2"


@pytest.fixture(scope="session")
def db_agent_config() -> DbAgentConfig:
    """加载真实 §4.1 配置（依赖根 .env 提供 DB_* 环境变量）。"""
    return load_db_agent_config()


@pytest.fixture()
def config_dict(db_agent_config: DbAgentConfig) -> dict:
    """可变配置快照（model_dump 后按用例改写再 model_validate）。"""
    return db_agent_config.model_dump(mode="json")


# ============================================================================
# 假服务（unit：不触碰真实 PG/Milvus/Neo4j）
# ============================================================================


class FakeEmbedding:
    """记录输入并返回固定向量的 Embedding 假实现"""

    def __init__(self) -> None:
        self.queries: list[str] = []

    def embed_query(self, text: str) -> list[float]:
        self.queries.append(text)
        return [0.1, 0.2, 0.3]


class FakeMilvus:
    """按需配置命中结果的 Milvus 假实现；记录每次检索的 top_k 与阈值"""

    def __init__(self) -> None:
        self.table_hits: list[dict[str, Any]] = []
        self.column_hits: list[dict[str, Any]] = []
        self.dim_hits_by_value: dict[str, list[dict[str, Any]]] = {}
        self.sql_hits: list[dict[str, Any]] = []
        self.cards: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[str, int | None, float | None]] = []

    def search_tables(self, embedding, top_k: int, threshold: float):
        self.calls.append(("tables", top_k, threshold))
        return [dict(h) for h in self.table_hits]

    def search_columns(self, embedding, top_k: int, threshold: float):
        self.calls.append(("columns", top_k, threshold))
        return [dict(h) for h in self.column_hits]

    def search_dim_values(self, query_value: str, top_k: int, min_score: float):
        self.calls.append(("dim_values", top_k, min_score))
        return [dict(h) for h in self.dim_hits_by_value.get(query_value, [])]

    def search_similar_sqls(self, embedding, top_k: int, threshold: float):
        self.calls.append(("sql_examples", top_k, threshold))
        return [dict(h) for h in self.sql_hits]

    def fetch_table_cards(self, table_names: list[str]) -> dict[str, dict[str, Any]]:
        return {n: dict(self.cards[n]) for n in table_names if n in self.cards}

    def close(self) -> None:
        pass


@dataclass
class FakeColumn:
    """business_db.get_columns 返回的列描述（字段名对齐 ColumnInfo）"""

    column_name: str
    data_type: str
    is_nullable: bool = True


class FakeBusinessDb:
    """返回固定列清单的业务库假实现"""

    def __init__(self, columns: list[FakeColumn] | None = None) -> None:
        self.columns = columns or []
        self.queries: list[tuple[str, str]] = []

    def get_columns(self, schema: str, table: str):
        self.queries.append((schema, table))
        return list(self.columns)

    def close(self) -> None:
        pass


class FakeJoinGraph:
    """按 (base, target) 返回预设 JoinPathOutcome 的假实现"""

    def __init__(self, outcomes: dict[tuple[str, str], Any] | None = None) -> None:
        self.outcomes = outcomes or {}
        self.calls: list[dict[str, Any]] = []

    def find_join_path(self, base: str, target: str, **kwargs):
        self.calls.append({"base": base, "target": target, **kwargs})
        outcome = self.outcomes[(base, target)]
        return outcome() if callable(outcome) else outcome

    def close(self) -> None:
        pass


@pytest.fixture()
def fake_embedding() -> FakeEmbedding:
    return FakeEmbedding()


@pytest.fixture()
def fake_milvus() -> FakeMilvus:
    return FakeMilvus()


@pytest.fixture()
def fake_business_db() -> FakeBusinessDb:
    return FakeBusinessDb(
        [
            FakeColumn("order_id", "integer", False),
            FakeColumn("customer_id", "integer"),
            FakeColumn("amount", "numeric"),
        ]
    )


@pytest.fixture()
def fake_services(
    db_agent_config: DbAgentConfig,
    fake_milvus: FakeMilvus,
    fake_embedding: FakeEmbedding,
    fake_business_db: FakeBusinessDb,
) -> DbAgentServices:
    """注入假后端的服务容器（真实 TableCatalog：normalize/classify/describe 纯逻辑）"""
    return DbAgentServices(
        config=db_agent_config,
        business_db=fake_business_db,
        embedding=fake_embedding,
        milvus=fake_milvus,
        catalog=TableCatalog(db_agent_config, fake_milvus, fake_business_db),
        join_graph=FakeJoinGraph(),
        embedding_release={},
        source_id=TEST_SOURCE_ID,
    )


@pytest.fixture()
def db_agent_context():
    """固定字段的运行上下文（时钟对齐 §7.5 固定时钟 2026-10-13 12:00+08）。

    doc 6.3 §6.5：附带进程内会话守卫租约（生产 ctx 由 Runner 构造时注入；
    teardown 释放，不跨用例泄漏）。
    """
    from src.modules.db_deepagent.persistence.session_guard import (
        acquire_memory_lease,
        release_memory_lease,
    )

    lease = acquire_memory_lease("s-test")
    assert lease is not None, "测试会话锁被前一用例占用"
    yield DbAgentContext(
        session_id="s-test",
        thread_id="t-test",
        request_id="req-test",
        run_id="run-test",
        datasource_id="orders",
        validation_fingerprint="fp-test",
        now=datetime(2026, 10, 13, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
        timezone="Asia/Shanghai",
        user_texts=("测试问题",),
        carried_evidence=(),
        deadline_monotonic=0.0,
        session_guard_lease=lease,
    )
    release_memory_lease(lease)


# ============================================================================
# 校验执行闭环假件（步骤 3）
# ============================================================================


class FakeExecDb:
    """校验执行闭环假业务库：public.orders 列结构 + EXPLAIN + 可配置只读结果"""

    ORDERS_COLUMNS = ["id", "status", "created_at", "customer_id", "amount"]

    def __init__(
        self,
        rows: list[tuple[Any, ...]] | None = None,
        source_truncated: bool = False,
        probe_rows: list[tuple[Any, ...]] | None = None,
    ) -> None:
        default_rows = [
            (1, "已支付", Decimal("99.50"), "C001", date(2026, 9, 5)),
            (2, "已支付", Decimal("10.00"), "C002", date(2026, 9, 6)),
        ]
        self.rows = rows if rows is not None else default_rows
        self.source_truncated = source_truncated
        # 探针形态（SELECT DISTINCT ... AS value）的应答行
        self.probe_rows = probe_rows if probe_rows is not None else [("已支付",)]
        self.executed: list[tuple[str, float, int]] = []
        self.explained: list[str] = []

    def get_columns(self, schema: str, table: str):
        if (schema, table) != ("public", "orders"):
            return []
        types = {
            "id": "integer",
            "status": "varchar",
            "created_at": "date",
            "customer_id": "varchar",
            "amount": "numeric",
        }
        return [FakeColumn(n, types[n]) for n in self.ORDERS_COLUMNS]

    def column_type(self, schema: str, table: str, column: str) -> str | None:
        if (schema, table) != ("public", "orders") or column not in self.ORDERS_COLUMNS:
            return None
        if column in ("status", "customer_id"):
            return "varchar"
        return "integer"

    def explain(self, sql: str, timeout_s: float):
        self.explained.append(sql)
        return [("Seq Scan on orders  (cost=0.00..1.01 rows=5 width=4)",)]

    def run_readonly(self, sql: str, *, timeout_s: float, max_rows: int):
        self.executed.append((sql, timeout_s, max_rows))
        if sql.startswith("SELECT DISTINCT"):
            # 探针形态：单列 value
            fetched = self.probe_rows[: max_rows + 1]
            return ReadonlyResult(
                columns=["value"],
                column_types=["varchar"],
                rows=list(fetched[:max_rows]),
                source_truncated=len(fetched) > max_rows,
                elapsed_ms=3,
            )
        fetched = self.rows[: max_rows + 1]  # 与真实实现一致：读 max_rows+1 判截断
        return ReadonlyResult(
            columns=["id", "status", "amount", "customer_id", "created_at"],
            column_types=["integer", "varchar", "numeric", "varchar", "date"],
            rows=list(fetched[:max_rows]),
            source_truncated=len(fetched) > max_rows or self.source_truncated,
            elapsed_ms=12,
        )

    def close(self) -> None:
        pass


def make_time_evidence(
    evidence_id: str,
    start: str | None,
    end: str | None,
    *,
    run_id: str = "run-test",
    source_text: str = "2026年9月",
    grain: str = "month",
    datasource_id: str = "orders",
) -> TimeEvidence:
    return TimeEvidence(
        time_evidence_id=evidence_id,
        run_id=run_id,
        source_text=source_text,
        grain=grain,
        start=start,
        end=end,
        timezone="Asia/Shanghai",
        datasource_id=datasource_id,
        inherited_from=None,
    )


def make_dim_evidence(
    table: str,
    column: str,
    values: list[str],
    *,
    evidence_id: str | None = None,
    run_id: str = "run-test",
    source: str = "milvus_dim_hit",
    patterns: list[str] | None = None,
    datasource_id: str = "orders",
) -> DimEvidence:
    return DimEvidence(
        dim_evidence_id=evidence_id or f"d-{table}.{column}",
        run_id=run_id,
        source=source,  # type: ignore[typeddict-item]
        table=table,
        column=column,
        values=values,
        patterns=patterns or [],
        query_text=None,
        ref=None,
        datasource_id=datasource_id,
        inherited_from=None,
    )


@pytest.fixture()
def pipeline_stack(db_agent_config: DbAgentConfig, tmp_path):
    """步骤 3 服务栈：假业务库 + 临时工作区；返回 (pipeline, db, results, budget, cfg)"""
    cfg = db_agent_config.model_copy(deep=True)
    cfg.workspace.root = str(tmp_path)
    db = FakeExecDb()
    results = ResultStore(tmp_path)
    budget = RunBudgetLedger()
    validator = DbSqlValidator(
        cfg.validation, db, explain_timeout_s=cfg.business_db.explain_timeout_seconds
    )
    guard = SqlEvidenceGuard(cfg.validation.guard, db, DimensionProbeBuilder())
    pipeline = SqlPipeline(
        config=cfg,
        validator=validator,
        guard=guard,
        db=db,
        results=results,
        budget=budget,
    )
    return pipeline, db, results, budget, cfg


# ============================================================================
# 任务账本构造器（doc 6.3 测试共用）
# ============================================================================


def make_task_record(
    run_id: str,
    sub_task_id: str,
    revision: int,
    *,
    status: str = "pending",
    content: str = "业务任务",
    dependencies: list[dict] | None = None,
    evidence_ids: list[str] | None = None,
    reason_code: str | None = None,
    reason: str | None = None,
    clarification_question: str | None = None,
    session_id: str = "s-test",
    thread_id: str = "t-test",
    request_id: str = "req-test",
    extra: dict | None = None,
) -> dict:
    """构造 SubTaskRecord 测试副本（键由 make_task_state 统一编码）。"""
    record: dict = {
        "session_id": session_id,
        "thread_id": thread_id,
        "request_id": request_id,
        "run_id": run_id,
        "sub_task_id": sub_task_id,
        "revision": revision,
        "content": content,
        "dependencies": list(dependencies or []),
        "status": status,
        "evidence_ids": list(evidence_ids or []),
        "creation_operation": {
            "op": "add",
            "sub_task_id": sub_task_id,
            "content": content,
            "dependencies": list(dependencies or []),
        },
        "failure_refs": [],
        "decision_source": "agent",
        "created_at": "2026-11-13T10:00:00+08:00",
        "updated_at": "2026-11-13T10:00:00+08:00",
        "tool_call_id": "tc-task",
    }
    if status in ("failed", "needs_clarification"):
        record["reason_code"] = reason_code
        record["reason"] = reason
    if status == "needs_clarification":
        record["clarification_question"] = clarification_question
    if extra:
        record.update(extra)
    return record


def make_task_state(
    records: list[dict],
    *,
    plan_version: int = 1,
) -> tuple[dict, dict]:
    """任务记录列表 → (db_sub_tasks, db_task_plans) 状态段。

    run_id 取自首条记录（空列表返回空段）。
    """
    from src.modules.db_deepagent.state import task_ledger_key

    sub_tasks = {
        task_ledger_key(r["run_id"], r["sub_task_id"], r["revision"]): r
        for r in records
    }
    plans: dict = {}
    if records and plan_version:
        first = records[0]
        plans[first["run_id"]] = {
            "session_id": first["session_id"],
            "thread_id": first["thread_id"],
            "request_id": first["request_id"],
            "run_id": first["run_id"],
            "plan_version": plan_version,
            "updated_at": "2026-11-13T10:00:00+08:00",
            "tool_call_id": "tc-task",
        }
    return {"db_sub_tasks": sub_tasks, "db_task_plans": plans}


@pytest.fixture()
def guard_services(pipeline_stack, db_agent_config: DbAgentConfig) -> DbAgentServices:
    """带步骤 3 服务栈的工具容器（embedding/milvus/catalog 未用，置 None）"""
    pipeline, _db, results, budget, _cfg = pipeline_stack
    return DbAgentServices(
        config=db_agent_config,
        business_db=_db,
        embedding=None,  # type: ignore[arg-type]
        milvus=None,  # type: ignore[arg-type]
        catalog=None,  # type: ignore[arg-type]
        join_graph=None,  # type: ignore[arg-type]
        embedding_release={},
        source_id=TEST_SOURCE_ID,
        guard=SqlEvidenceGuard(
            db_agent_config.validation.guard, _db, DimensionProbeBuilder()
        ),
        pipeline=pipeline,
        time_resolver=TimeResolver(),
        probe=DimensionProbeBuilder(),
        results=results,
        analysis=_result_analysis(db_agent_config, results),
        budget=budget,
    )


def _result_analysis(cfg, results):
    from src.modules.db_deepagent.services.result_analysis import ResultAnalysisService

    return ResultAnalysisService(cfg, results)


# ============================================================================
# 步骤 4 假件（装配／Runner 测试）
# ============================================================================


class ScriptedChatModel(BaseChatModel):
    """受控假模型：顺序返回脚本响应；支持 bind_tools（返回自身）。

    不做真实模型调用；供真实 SDK 中间件栈（Summarization／ModelRefusal）
    消费，驱动真实图路径（doc 1 §4.4／§6.4 专项）。
    """

    model_name: str = "scripted-fake"
    responses: list = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "scripted-fake"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        index = min(getattr(self, "_cursor", 0), len(self.responses) - 1)
        response = self.responses[index]
        object.__setattr__(self, "_cursor", getattr(self, "_cursor", 0) + 1)
        if isinstance(response, str):
            response = AIMessage(content=response)
        return ChatResult(generations=[ChatGeneration(message=response)])


class FakeAgent:
    """可控的 agent 替身：invoke/get_state 按预设脚本执行（Runner 测试用）"""

    def __init__(self, state_values: dict[str, Any] | None = None):
        self.state_values = state_values or {"messages": []}
        self.invoke_calls: list[dict[str, Any]] = []
        self.invoke_side_effect: Any = None
        self.get_state_side_effect: Any = None
        self.fail_get_state_after_invoke: BaseException | None = None
        self.get_state_calls = 0

    def invoke(self, inputs, config=None, context=None):
        self.invoke_calls.append(
            {"inputs": inputs, "config": config, "context": context}
        )
        if self.invoke_side_effect is not None:
            raise self.invoke_side_effect
        if context is not None:
            self.state_values = _remap_run_id(
                self.state_values, context.run_id, context.request_id
            )
        return {"messages": self.state_values.get("messages", [])}

    def get_state(self, config):
        self.get_state_calls += 1
        if self.get_state_side_effect is not None:
            raise self.get_state_side_effect
        if self.fail_get_state_after_invoke is not None and self.invoke_calls:
            raise self.fail_get_state_after_invoke
        values = self.state_values
        by_thread = getattr(self, "states_by_thread", None)
        if by_thread:
            tid = (config.get("configurable") or {}).get("thread_id")
            values = by_thread.get(tid, self.state_values)

        class _Snapshot:
            pass

        snapshot = _Snapshot()
        snapshot.values = values
        snapshot.next = ()
        snapshot.created_at = None
        return snapshot


def _remap_run_id(
    state: dict[str, Any], run_id: str, request_id: str | None = None
) -> dict[str, Any]:
    """把假状态里的占位 run_id（run-x）改写为实际运行，账本键随之对齐。

    request_id 一并改写（doc 6.2 §7.2.1 运行归属校验）：模拟正常提交工具
    用 ctx.run_id/ctx.request_id 写入的记录；不传时保持原值（用于构造归属
    不符的用例）。doc 6.3：任务账本键 (run_id, sub_task_id, revision) 与
    计划键同步重编码。
    """
    import copy

    from src.modules.db_deepagent.state import (
        parse_task_ledger_key,
        task_ledger_key,
    )

    values = copy.deepcopy(state)
    for key in ("db_executions", "db_analyses"):
        for record in values.get(key) or []:
            if record.get("run_id") == "run-x":
                record["run_id"] = run_id
            # 结构化输入血缘/直接来源中的同轮身份由服务端按实际 run_id 写入；
            # 占位 run-x 的引用需同步改写，跨 run 历史来源（其他 run_id）
            # 保持原值（R2-02：完整身份寻址，不猜测归属）。
            for entry in record.get("input_result_refs") or []:
                if not isinstance(entry, dict):
                    continue
                for sub in ("evidence_ref", "source_task_ref"):
                    ref = entry.get(sub)
                    if isinstance(ref, dict) and ref.get("run_id") == "run-x":
                        ref["run_id"] = run_id
            direct = record.get("direct_reuse_from")
            if isinstance(direct, dict):
                for sub in ("evidence_ref", "source_task_ref"):
                    ref = direct.get(sub)
                    if isinstance(ref, dict) and ref.get("run_id") == "run-x":
                        ref["run_id"] = run_id
    answers = values.get("db_final_answers") or {}
    if "run-x" in answers:
        answers[run_id] = answers.pop("run-x")
        answers[run_id]["run_id"] = run_id
        if request_id is not None:
            answers[run_id]["request_id"] = request_id
    sub_tasks = values.get("db_sub_tasks") or {}
    if sub_tasks:
        remapped = {}
        for key, record in sub_tasks.items():
            parsed = parse_task_ledger_key(key)
            if parsed is not None and parsed[0] == "run-x":
                record = {**record, "run_id": run_id}
                if request_id is not None:
                    record = {**record, "request_id": request_id}
                key = task_ledger_key(run_id, parsed[1], parsed[2])
            remapped[key] = record
        values["db_sub_tasks"] = remapped
    plans = values.get("db_task_plans") or {}
    if "run-x" in plans:
        record = {**plans["run-x"], "run_id": run_id}
        if request_id is not None:
            record = {**record, "request_id": request_id}
        plans[run_id] = record
        del plans["run-x"]
    return values


class FakeStore:
    """BaseStore 替身：记录 put；可注入 get/put 异常（P0/P3 注入用）。

    另支持前缀 search／delete／条目 created_at（§13.5 清理测试用）；
    created_at 默认当前时刻，可用 put(..., created_at=...) 注入历史时间。
    """

    def __init__(self):
        self.items: dict[tuple[tuple[str, ...], str], dict[str, Any]] = {}
        self.created: dict[tuple[tuple[str, ...], str], Any] = {}
        self.deleted: list[tuple[tuple[str, ...], str]] = []
        self.get_side_effect: Any = None
        self.put_side_effect: Any = None

    def put(self, namespace, key, value, created_at=None):
        if self.put_side_effect is not None:
            raise self.put_side_effect
        self.items[(namespace, key)] = dict(value)
        self.created[(namespace, key)] = created_at or datetime.now().astimezone()

    def get(self, namespace, key):
        if self.get_side_effect is not None:
            raise self.get_side_effect
        item = self.items.get((namespace, key))
        return SimpleNamespace(value=dict(item)) if item is not None else None

    def search(self, namespace_prefix, limit=10, offset=0):
        prefix = tuple(namespace_prefix)
        matched = [
            SimpleNamespace(
                key=key,
                value=dict(value),
                namespace=ns,
                created_at=self.created.get((ns, key)),
            )
            for (ns, key), value in sorted(
                self.items.items(), key=lambda kv: (kv[0], kv[1])
            )
            if ns[: len(prefix)] == prefix
        ]
        return matched[offset : offset + limit]

    def delete(self, namespace, key):
        self.deleted.append((tuple(namespace), key))
        self.items.pop((tuple(namespace), key), None)


@pytest.fixture()
def assembly_services(
    db_agent_config: DbAgentConfig, tmp_path, monkeypatch
) -> DbAgentServices:
    """装配测试服务栈：假后端 + 临时工作区 + 步骤 3 服务（内存持久化）。

    doc 5.1 §5.1：离线装配一律注入假摘要模型（每次新建、名称不同于假主
    模型），避免现有离线测试构造/调用真实 Flash。仅作用于本 fixture 的
    使用者，不设全局 autouse，不影响 --run-llm 真实模型集成测试。
    """
    import src.modules.db_deepagent.agent as agent_mod

    def _fake_build_summarizer_model(_cfg):
        return ScriptedChatModel(
            model_name="fake-flash-summarizer",
            responses=["历史业务口径摘要（假摘要模型）：时间范围与统计口径已概括。"],
            metadata={"dbda_call_kind": "summarization"},
        )

    monkeypatch.setattr(
        agent_mod, "build_summarizer_model", _fake_build_summarizer_model
    )

    from src.modules.db_deepagent.services.sql_validation import DbSqlValidator

    cfg = db_agent_config.model_copy(deep=True)
    cfg.workspace.root = str(tmp_path)
    cfg.persistence.mode = "memory"
    db = FakeExecDb()
    db.datasource_id = "pg:test"
    results = ResultStore(tmp_path)
    budget = RunBudgetLedger()
    validator = DbSqlValidator(
        cfg.validation, db, explain_timeout_s=cfg.business_db.explain_timeout_seconds
    )
    guard = SqlEvidenceGuard(cfg.validation.guard, db, DimensionProbeBuilder())
    pipeline = SqlPipeline(
        config=cfg,
        validator=validator,
        guard=guard,
        db=db,
        results=results,
        budget=budget,
    )
    from src.modules.db_deepagent.services.result_analysis import ResultAnalysisService

    return DbAgentServices(
        config=cfg,
        business_db=db,
        embedding=FakeEmbedding(),  # type: ignore[arg-type]
        milvus=FakeMilvus(),  # type: ignore[arg-type]
        catalog=TableCatalog(cfg, FakeMilvus(), db),
        join_graph=FakeJoinGraph(),  # type: ignore[arg-type]
        embedding_release={},
        source_id=TEST_SOURCE_ID,
        validator=validator,
        guard=guard,
        pipeline=pipeline,
        time_resolver=TimeResolver(),
        probe=DimensionProbeBuilder(),
        results=results,
        analysis=ResultAnalysisService(cfg, results),
        budget=budget,
    )


# ============================================================================
# ToolNode 调用辅助
# ============================================================================


def invoke_tool_node(
    node,
    tool_calls: list[tuple[str, dict, str]],
    context: DbAgentContext,
    extra_state: dict[str, Any] | None = None,
):
    """以完整 ToolCall 走 ToolNode（含 runtime 注入），返回 (合并 update, 末条 ToolMessage)。

    tool_calls 为 (name, args, call_id) 三元组列表；extra_state 并入图状态
    （如 db_time_evidence，供工具经 runtime.state 读取）。ToolNode 的返回形态：
    全非 Command 时为 {"messages": [...]}；含 Command 时为
    [Command | {"messages": [...]}] 列表，此处统一抽取。
    """
    state = {
        **(extra_state or {}),
        "messages": [
            AIMessage(
                content="",
                tool_calls=[
                    {"name": name, "args": args, "id": call_id, "type": "tool_call"}
                    for name, args, call_id in tool_calls
                ],
            )
        ],
    }
    out = node.invoke(
        state, {"configurable": {"__pregel_runtime": Runtime(context=context)}}
    )
    updates: dict[str, Any] = {}
    messages: list[ToolMessage] = []
    for item in out if isinstance(out, list) else [out]:
        if isinstance(item, Command):
            update = item.update or {}
            updates.update(update)
            messages += [
                m for m in update.get("messages", []) if isinstance(m, ToolMessage)
            ]
        elif isinstance(item, dict):
            messages += [
                m for m in item.get("messages", []) if isinstance(m, ToolMessage)
            ]
    return updates, messages[-1]
