"""真实模型读取 SDK 转存的大工具输出（doc 7，需显式 --run-llm）。

使用生产 inspect_tables、文件后端和权限；仅表结构数据为模拟数据。
随机字段位于原文 80000 字符之后，且不在头尾预览或用户问题中。
模型必须实际调用 read_file，并准确返回该字段，才能通过。
不访问真实数据库、不修改生产配置；证据写入 pytest 临时目录。
"""

from __future__ import annotations

import json
import re
import secrets
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pytest
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from src.modules.db_deepagent.agent import (
    build_backend,
    build_filesystem_middleware,
    build_model,
    build_permissions,
    build_session_dir,
)
from src.modules.db_deepagent.middleware.model_observer import ModelCallObserver
from src.modules.db_deepagent.services.table_catalog import TableCatalog
from src.modules.db_deepagent.tools.schema_tools import make_inspect_tables_tool
from src.services.config_loader import get_config
from src.tests.db_deepagent.conftest import FakeColumn

pytestmark = [pytest.mark.integration, pytest.mark.requires_llm]

_TABLES = [f"public.t{i:02d}" for i in range(1, 11)]
_PATH = re.compile(r"/artifacts/large_tool_results/[^\s]+")


class _ModelInputs(BaseCallbackHandler):
    """记录真实模型实际收到的工具消息和模型身份。"""

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


def test_real_model_reads_middle_of_offloaded_inspect_output(
    fake_services, tmp_path: Path, monkeypatch
) -> None:
    """长输出转存后，真实模型从中部读取未向其泄露的随机字段。"""
    from deepagents import create_deep_agent
    from langchain.agents.middleware import ModelCallLimitMiddleware
    from langgraph.checkpoint.memory import InMemorySaver

    cfg = fake_services.config.model_copy(deep=True)
    cfg.workspace.root = str(tmp_path)
    target_name = "lookup_probe_" + secrets.token_hex(6)
    target_type = "numeric"
    columns = {
        table: [
            FakeColumn(f"col_{table_index:02d}_{i:03d}_" + "x" * 39, "varchar", True)
            for i in range(100)
        ]
        for table_index, table in enumerate(_TABLES, 1)
    }
    columns["public.t06"][50] = FakeColumn(target_name, target_type, False)

    def get_columns(schema: str, table: str) -> list[FakeColumn]:
        return list(columns.get(f"{schema}.{table}", []))

    monkeypatch.setattr(fake_services.business_db, "get_columns", get_columns)
    fake_services.config = cfg
    fake_services.catalog = TableCatalog(
        cfg, fake_services.milvus, fake_services.business_db
    )
    for table in _TABLES:
        fake_services.milvus.cards[f"orders.{table}"] = {
            "object_id": f"orders.{table}",
            "text_raw": f"# {table} [table]\n- 模拟结构，仅用于转存测试",
            "grain_hint": None,
            "time_col_hint": None,
            "table_category": "",
        }

    inspect_tool = make_inspect_tables_tool(fake_services)
    raw = inspect_tool.invoke({"table_ids": _TABLES, "include_live_columns": True})
    target_offset = raw.index(target_name)
    assert len(raw) > 80000 and target_offset > 80000
    assert target_name not in "\n".join(raw.splitlines()[:5] + raw.splitlines()[-5:])

    session_dir = build_session_dir(cfg, "t-real-offload")
    backend = build_backend(session_dir, cfg)
    permissions = build_permissions(cfg)
    file_mw = build_filesystem_middleware(backend, permissions, cfg)
    model = build_model(cfg)
    model.metadata = {**(model.metadata or {}), "dbda_call_kind": "agent"}
    capture = _ModelInputs()
    observer = ModelCallObserver("req-real-offload", "run-real-offload")
    agent = create_deep_agent(
        model=model,
        tools=[inspect_tool],
        middleware=[
            file_mw,
            ModelCallLimitMiddleware(run_limit=10, exit_behavior="error"),
        ],
        backend=backend,
        permissions=permissions,
        subagents=[],
        checkpointer=InMemorySaver(),
        system_prompt=(
            "你执行一次表结构核对。先调用 inspect_tables 一次读取用户列出的全部表，"
            "include_live_columns=true。若工具提示内容已保存为文件，使用已有文件工具"
            "定位，并调用 read_file 核对目标字段所在段落；可以先 grep 定位，再读取"
            "该段，不要猜测或把预览当成完整内容。不要编写 SQL，不需要待办列表。"
            '最终只返回 JSON：{"column_name": "精确列名", "pg_type": "精确类型"}。'
        ),
    )
    question = (
        f"请先一次检查这些表的结构：{', '.join(_TABLES)}。"
        "然后找出 public.t06 中以 lookup_probe_ 开头的列，"
        "给出完整列名及 pg_type。必须读取该列所在的文件段落核对后再回答。"
    )
    assert target_name not in question
    report: dict[str, Any] = {
        "scope": "synthetic schema, real model and production tool/filesystem",
        "llm_profile": cfg.model.llm_profile,
        "raw_characters": len(raw),
        "target_character_offset": target_offset,
        "expected": {"column_name": target_name, "pg_type": target_type},
        "evidence_path": str(tmp_path / "evidence.json"),
        "passed": False,
    }
    started = time.monotonic()
    try:
        result = agent.invoke(
            {"messages": [HumanMessage(content=question)]},
            config={
                "configurable": {"thread_id": "real-offload"},
                "callbacks": [capture, observer],
                "metadata": {"dbda_call_kind": "agent"},
                "recursion_limit": 60,
            },
        )
        messages = result["messages"]
        calls = [
            call
            for msg in messages
            if isinstance(msg, AIMessage)
            for call in msg.tool_calls
        ]
        tool_messages = [msg for msg in messages if isinstance(msg, ToolMessage)]
        report["tool_calls"] = calls
        report["tool_results"] = [
            {"name": msg.name, "status": msg.status, "content": msg.content}
            for msg in tool_messages
        ]
        inspect_results = [msg for msg in tool_messages if msg.name == "inspect_tables"]
        assert len(inspect_results) == 1, "没有按要求一次读取全部表结构"
        preview = inspect_results[0].content
        match = _PATH.search(preview)
        assert match, "工具结果没有 SDK 转存路径"
        virtual_path = match.group(0)
        assert target_name not in preview, "预览泄露了目标，不能证明中部回读"
        file_path = session_dir / "artifacts" / virtual_path.removeprefix("/artifacts/")
        assert file_path.read_text(encoding="utf-8") == raw
        report["offloaded_file"] = str(file_path)
        report["preview_characters"] = len(preview)

        assert any(
            call["name"] == "read_file"
            and call["args"].get("file_path") == virtual_path
            for call in calls
        ), "真实模型没有调用 read_file 读取 SDK 返回的路径"
        assert any(
            msg.name == "read_file"
            and msg.status == "success"
            and target_name in msg.content
            for msg in tool_messages
        ), "read_file 未实际返回中部的目标字段"
        assert any(
            item["name"] == "read_file" and target_name in item["content"]
            for call in capture.calls
            for item in call["tools_seen"]
        ), "目标字段没有进入真实模型收到的回读消息"
        assert all(
            item["content"] != raw
            for call in capture.calls
            for item in call["tools_seen"]
        ), "完整大输出直接进入了模型上下文"
        final = next(msg for msg in reversed(messages) if isinstance(msg, AIMessage))
        report["final_answer"] = final.content
        answer = final.content.strip()
        if answer.startswith("```"):
            answer = "\n".join(answer.splitlines()[1:-1])
        assert json.loads(answer) == report["expected"]
        expected_model = get_config()["llm_profiles"][cfg.model.llm_profile]["model"]
        assert capture.calls and all(
            c["model"] == expected_model for c in capture.calls
        )
        snapshot = observer.finalize()
        assert snapshot.token_usage_complete and snapshot.anomalies == []
        report["passed"] = True
    finally:
        report["elapsed_ms"] = int((time.monotonic() - started) * 1000)
        report["model_inputs"] = capture.calls
        report["usage"] = asdict(observer.finalize())
        (tmp_path / "evidence.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
