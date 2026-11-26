"""真实模型读取结果类工具的 SDK 转存大输出（doc 9 受控验收，需 --run-llm）。

生产 build_db_agent（真实主 LLM、真实校验执行管线、ResultStore、Command
账本、SDK backend/permissions/filesystem 中间件、最终答案门控）＋临时工作
区；仅业务数据源为受控假件（public.orders 固定 20 行）。

目标：execute_sql 返回 >80000 字符被 SDK 转存后，真实模型自行定位并分窗
口回读头尾预览之外的 id=12 note 原文值（随机后缀，不出现在问题/预览），
并用本 run 真实 evidence_id 通过门控提交最终答案。随机值、路径、行号、
引用 ID 均不写入问题或脚本化工具调用；读取参数由模型自行决定。
不访问真实业务数据库、不修改生产配置；证据写入 pytest 临时目录，设置
环境变量 DOC9_REAL_EVIDENCE_DIR 时另存快照。轮数/费用由预算账本控制
（模型调用上限 12、deadline 约 300 秒）。
"""

from __future__ import annotations

import json
import os
import re
import secrets
import time
from dataclasses import asdict
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, ClassVar

import pytest
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.types import Command
from src.modules.db_deepagent.config import load_db_agent_config
from src.modules.db_deepagent.context import DbAgentContext
from src.modules.db_deepagent.middleware.model_observer import ModelCallObserver
from src.modules.db_deepagent.services import DbAgentServices
from src.modules.db_deepagent.services.business_db import ReadonlyResult
from src.modules.db_deepagent.services.dimension_probe import DimensionProbeBuilder
from src.modules.db_deepagent.services.result_analysis import ResultAnalysisService
from src.modules.db_deepagent.services.result_store import ResultStore
from src.modules.db_deepagent.services.run_budget import BudgetLimits, RunBudgetLedger
from src.modules.db_deepagent.services.sql_guard import SqlEvidenceGuard
from src.modules.db_deepagent.services.sql_pipeline import SqlPipeline
from src.modules.db_deepagent.services.sql_validation import DbSqlValidator
from src.modules.db_deepagent.services.table_catalog import TableCatalog
from src.modules.db_deepagent.services.time_resolver import TimeResolver
from src.tests.db_deepagent.conftest import (
    TEST_SOURCE_ID,
    FakeColumn,
    FakeEmbedding,
    FakeJoinGraph,
    FakeMilvus,
)

pytestmark = [pytest.mark.integration, pytest.mark.requires_llm]

_PATH = re.compile(r"/artifacts/large_tool_results/[^\s]+")
_RUN_ID = "run-real-doc9"
_REQUEST_ID = "req-real-doc9"
_SESSION_ID = "t-real-doc9"
_MODEL_CALL_LIMIT = 12
_DEADLINE_SECONDS = 300.0


class _OrdersFakeDb:
    """受控业务数据假件：public.orders 固定列与 20 行；只读执行返回同一全集。

    真实校验/执行管线消费该假件（结构查询、EXPLAIN、只读执行全部路由到
    这里），不触碰真实业务库。目标行（id=12）的每个标量都远短于读取预算；
    其余行携带 ~4900 字符宽字段把 execute 预览推过 80000 字符。
    """

    COLUMNS: ClassVar[list[tuple[str, str]]] = [
        ("id", "integer"),
        ("status", "varchar"),
        ("note", "varchar"),
        ("payload", "varchar"),
        ("customer_id", "varchar"),
        ("created_at", "date"),
    ]

    def __init__(self, rows: list[tuple]) -> None:
        self.rows = rows
        self.executed: list[tuple[str, float, int]] = []
        self.explained: list[str] = []
        self.datasource_id = "orders"

    def get_columns(self, schema: str, table: str) -> list[FakeColumn]:
        if (schema, table) != ("public", "orders"):
            return []
        return [FakeColumn(n, t) for n, t in self.COLUMNS]

    def column_type(self, schema: str, table: str, column: str) -> str | None:
        if (schema, table) != ("public", "orders"):
            return None
        return dict(self.COLUMNS).get(column)

    def explain(self, sql: str, timeout_s: float):
        self.explained.append(sql)
        return [("Seq Scan on orders  (cost=0.00..1.01 rows=20 width=4)",)]

    def run_readonly(self, sql: str, *, timeout_s: float, max_rows: int):
        self.executed.append((sql, timeout_s, max_rows))
        return ReadonlyResult(
            columns=[n for n, _ in self.COLUMNS],
            column_types=[t for _, t in self.COLUMNS],
            rows=list(self.rows[:max_rows]),
            source_truncated=False,
            elapsed_ms=12,
        )

    def close(self) -> None:
        pass


def _build_rows(target_note: str) -> list[tuple]:
    """20 行受控数据：id=12 为短标量目标行，其余行宽字段撑出 >80000 正文。"""
    rows = []
    for i in range(1, 21):
        note = target_note if i == 12 else f"note-{i:03d}"
        payload = "P" * 80 if i == 12 else "P" * 4900
        rows.append((i, "已支付", note, payload, f"C{i:03d}", date(2026, 9, 5)))
    return rows


def _build_scenario(tmp_path: Path) -> dict[str, Any]:
    """受控场景装配：真实配置/管线/装配 + 假业务数据 + 临时工作区。"""
    target_note = "NOTE12-" + secrets.token_hex(4)
    cfg = load_db_agent_config().model_copy(deep=True)
    cfg.workspace.root = str(tmp_path)
    cfg.persistence.mode = "memory"
    db = _OrdersFakeDb(_build_rows(target_note))
    results = ResultStore(tmp_path / "results")
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
    milvus = FakeMilvus()
    milvus.cards["orders.public.orders"] = {
        "object_id": "orders.public.orders",
        "text_raw": "# public.orders [table]\n- 订单事实表（受控验收模拟卡片）",
        "grain_hint": None,
        "time_col_hint": "created_at",
        "table_category": "",
    }
    services = DbAgentServices(
        config=cfg,
        business_db=db,
        embedding=FakeEmbedding(),  # type: ignore[arg-type]
        milvus=milvus,  # type: ignore[arg-type]
        catalog=TableCatalog(cfg, milvus, db),
        join_graph=FakeJoinGraph(),  # type: ignore[arg-type]
        embedding_release={},
        source_id=TEST_SOURCE_ID,
        guard=guard,
        pipeline=pipeline,
        time_resolver=TimeResolver(),
        probe=DimensionProbeBuilder(),
        results=results,
        analysis=ResultAnalysisService(cfg, results),
        budget=budget,
    )
    question = (
        "请对 public.orders 做一次全量核验：用一条 SQL 取回全部 20 行的完整明细"
        "（所有列、不加过滤条件），执行成功后在返回的结果正文中核对 id=12 那一行"
        "的 note 字段原文值（若工具提示结果已保存为文件，按系统提示用文件工具"
        "定位并分窗口读取核对，不要凭预览猜测）。核对完成后单独调用 "
        "submit_final_answer 提交最终答案：answer 中写明 id=12 的 note 原文值，"
        "findings 引用 execute_sql 返回的 evidence_id。"
    )
    assert target_note not in question
    return {
        "target_note": target_note,
        "question": question,
        "services": services,
        "cfg": cfg,
        "db": db,
        "results": results,
        "budget": budget,
    }


class _ModelInputs(BaseCallbackHandler):
    """按调用顺序记录真实模型每轮收到的工具消息与模型身份。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def on_chat_model_start(
        self, serialized, messages, *, metadata=None, **kwargs
    ) -> None:
        md = metadata or {}
        self.calls.append(
            {
                "model": md.get("ls_model_name"),
                "provider": md.get("ls_provider"),
                "call_kind": md.get("dbda_call_kind"),
                "tools_seen": [
                    {
                        "name": msg.name,
                        "tool_call_id": msg.tool_call_id,
                        "content": msg.content,
                    }
                    for batch in messages
                    for msg in batch
                    if isinstance(msg, ToolMessage)
                ],
            }
        )


def _write_evidence(tmp_path: Path, report: dict[str, Any]) -> None:
    payload = json.dumps(report, ensure_ascii=False, indent=2)
    (tmp_path / "evidence.json").write_text(payload, encoding="utf-8")
    audit_dir = os.environ.get("DOC9_REAL_EVIDENCE_DIR")
    if audit_dir:
        Path(audit_dir).mkdir(parents=True, exist_ok=True)
        (Path(audit_dir) / "evidence.json").write_text(payload, encoding="utf-8")


def test_real_model_reads_offloaded_execute_sql_and_submits(tmp_path, monkeypatch):
    """主路径：validate_sql → execute_sql(>80000 转存) → read_file → submit。"""
    import src.modules.db_deepagent.agent as agent_mod
    from langgraph.checkpoint.memory import InMemorySaver
    from src.modules.db_deepagent.agent import build_db_agent, build_session_dir

    scenario = _build_scenario(tmp_path)
    services = scenario["services"]
    cfg = scenario["cfg"]
    target_note = scenario["target_note"]

    # 证据仪表：SDK 转存前截获每次工具调用原正文（记录用，不改变行为）
    captured_raw: dict[str, list[str]] = {}
    real_build_fmw = agent_mod.build_filesystem_middleware

    def _recording_fmw(backend, permissions, cfg):
        mw = real_build_fmw(backend, permissions, cfg)
        real_wrap = mw.wrap_tool_call

        def wrap(request, handler):
            def _capture(req):
                result = handler(req)
                msgs = (
                    result.update.get("messages", [])
                    if isinstance(result, Command)
                    else [result]
                )
                for m in msgs:
                    if isinstance(m, ToolMessage) and isinstance(m.content, str):
                        captured_raw.setdefault(request.tool_call.get("id"), []).append(
                            m.content
                        )
                return result

            return real_wrap(request, _capture)

        mw.wrap_tool_call = wrap
        return mw

    monkeypatch.setattr(agent_mod, "build_filesystem_middleware", _recording_fmw)

    deadline = time.monotonic() + _DEADLINE_SECONDS
    ctx = DbAgentContext(
        session_id=_SESSION_ID,
        thread_id="th-real-doc9",
        request_id=_REQUEST_ID,
        run_id=_RUN_ID,
        datasource_id="orders",
        validation_fingerprint="fp-real-doc9",
        now=datetime.now(UTC),
        timezone="Asia/Shanghai",
        user_texts=(scenario["question"],),
        carried_evidence=(),
        deadline_monotonic=deadline,
    )
    services.budget.open_run(
        _RUN_ID,
        BudgetLimits(model_call=_MODEL_CALL_LIMIT, tool_call=30, db_execute=10),
        deadline_monotonic=deadline,
    )
    capture = _ModelInputs()
    observer = ModelCallObserver(_REQUEST_ID, _RUN_ID)
    handle = build_db_agent(
        services, session_id=_SESSION_ID, checkpointer=InMemorySaver()
    )

    report: dict[str, Any] = {
        "scope": "real LLM + production graph, controlled fake business data",
        "provider": cfg.model.provider,
        "llm_name": cfg.model.llm_name,
        "question": scenario["question"],
        "target_note_kind": "random-suffix, middle of multi-line JSON",
        "model_call_limit": _MODEL_CALL_LIMIT,
        "deadline_seconds": _DEADLINE_SECONDS,
        "evidence_path": str(tmp_path / "evidence.json"),
        "passed": False,
    }
    started = time.monotonic()
    try:
        result = handle.agent.invoke(
            {"messages": [HumanMessage(content=scenario["question"])]},
            config={
                "configurable": {"thread_id": "th-real-doc9"},
                "callbacks": [capture, observer],
                "metadata": {"request_id": _REQUEST_ID, "dbda_call_kind": "agent"},
                "recursion_limit": 80,
            },
            context=ctx,
        )
        messages = result["messages"]
        calls = [
            call
            for msg in messages
            if isinstance(msg, AIMessage)
            for call in msg.tool_calls
        ]
        names = [call["name"] for call in calls]
        report["tool_calls"] = calls
        report["tool_results"] = [
            {"name": m.name, "status": m.status, "content": m.content}
            for m in messages
            if isinstance(m, ToolMessage)
        ]

        # 1) 主路径顺序：validate → execute → (grep)? → read_file → submit
        def _first_idx(pred, start=0):
            return next(
                (i for i, call in enumerate(calls[start:], start) if pred(call)),
                None,
            )

        i_validate = _first_idx(lambda c: c["name"] == "validate_sql")
        i_execute = _first_idx(
            lambda c: c["name"] == "execute_sql", (i_validate or 0) + 1
        )
        i_read = _first_idx(
            lambda c: c["name"] in ("grep", "read_file"), (i_execute or 0) + 1
        )
        i_submit = _first_idx(
            lambda c: c["name"] == "submit_final_answer", (i_read or 0) + 1
        )
        assert None not in (i_validate, i_execute, i_read, i_submit), names
        assert any(c["name"] == "read_file" for c in calls), names
        # 校验与执行文本逐字相同（授权链路未被绕过）
        assert calls[i_validate]["args"].get("sql") == calls[i_execute]["args"].get(
            "sql"
        )
        report["tool_sequence"] = names

        # 2) execute 原正文规模、转存与逐字相等
        execute_call_id = calls[i_execute]["id"]
        execute_msgs = [m for m in messages if m.name == "execute_sql"]
        preview_msg = next(m for m in execute_msgs if "large_tool_results" in m.content)
        (raw_content,) = captured_raw[execute_call_id]
        report["execute_raw_characters"] = len(raw_content)
        report["execute_offload_preview_characters"] = len(preview_msg.content)
        assert len(raw_content) > 80000
        assert target_note not in preview_msg.content  # 预览不泄露目标
        virtual_path = _PATH.search(preview_msg.content).group(0)
        file_path = (
            build_session_dir(cfg, _SESSION_ID)
            / "artifacts"
            / virtual_path.removeprefix("/artifacts/")
        )
        report["offloaded_file"] = str(file_path)
        assert file_path.read_text(encoding="utf-8") == raw_content  # 逐字相等
        target_offset = raw_content.index(target_note)
        report["target_character_offset"] = target_offset
        raw_lines = raw_content.splitlines()
        target_line = next(i for i, ln in enumerate(raw_lines) if target_note in ln)
        report["target_line_index"] = target_line
        report["raw_total_lines"] = len(raw_lines)
        # doc9 规格：目标位于多行 JSON 中部且 SDK 头尾预览（各 5 行）不可见
        assert 5 < target_line < len(raw_lines) - 5
        assert target_note not in "\n".join(raw_lines[:5] + raw_lines[-5:])

        # 3) 模型输入证据：目标只在回读后可见；完整正文从未直接注入
        first_seen_call = next(
            (
                i
                for i, call in enumerate(capture.calls)
                if any(target_note in t["content"] for t in call["tools_seen"])
            ),
            None,
        )
        assert first_seen_call is not None, "目标值从未进入模型输入"
        for call in capture.calls[:first_seen_call]:
            assert all(
                target_note not in t["content"] for t in call["tools_seen"]
            ), "目标在回读前泄露给模型"
        seen_call = capture.calls[first_seen_call]
        assert any(
            t["name"] == "read_file" and target_note in t["content"]
            for t in seen_call["tools_seen"]
        ), "目标值不是经 read_file 回读进入模型输入"
        for call in capture.calls:
            for t in call["tools_seen"]:
                assert t["content"] != raw_content, "完整大输出直接进入模型上下文"
                assert len(t["content"]) < 80000
        readback = next(
            t
            for call in capture.calls[first_seen_call:]
            for t in call["tools_seen"]
            if t["name"] == "read_file" and target_note in t["content"]
        )
        report["readback_window_characters"] = len(readback["content"])

        # 4) 提交内容与本 run 账本一致；门控通过
        values = handle.agent.get_state(
            {"configurable": {"thread_id": "th-real-doc9"}}
        ).values
        answers = values.get("db_final_answers") or {}
        assert _RUN_ID in answers, "最终答案未通过门控登记"
        record = answers[_RUN_ID]
        report["final_answer_record"] = {
            "status": record["answer"]["status"],
            "answer": record["answer"]["answer"],
            "findings": record["answer"]["findings"],
        }
        assert target_note in record["answer"]["answer"]
        exec_records = [
            r
            for r in (values.get("db_executions") or [])
            if r.get("run_id") == _RUN_ID and r.get("purpose") == "query"
        ]
        assert exec_records, "本 run 无 query 执行账本"
        cited = {eid for f in record["answer"]["findings"] for eid in f["evidence_ids"]}
        assert cited and cited <= {r["evidence_id"] for r in exec_records}
        exec_record = next(r for r in exec_records if r["result_ref"])
        report["ledger_evidence_id"] = exec_record["evidence_id"]
        report["ledger_result_ref"] = exec_record["result_ref"]
        # ResultStore 后端真值：目标行仍在结果文件中
        page = services.results.read_rows(
            exec_record["result_ref"],
            session_id=ctx.session_id,
            offset=11,
            limit=1,
        )
        assert page.rows[0][2] == target_note

        # 5) 观测：调用次数受控、无摘要、token 完整
        snapshot = observer.finalize()
        usage = asdict(snapshot)
        report["usage"] = usage
        assert snapshot.token_usage_complete and snapshot.anomalies == []
        assert snapshot.total_model_calls <= _MODEL_CALL_LIMIT
        report["summary_model_calls"] = snapshot.summary_model_calls
        report["passed"] = True
    finally:
        report["elapsed_ms"] = int((time.monotonic() - started) * 1000)
        report["model_inputs"] = capture.calls
        report["captured_raw_sizes"] = {
            k: [len(c) for c in v] for k, v in captured_raw.items()
        }
        _write_evidence(tmp_path, report)
