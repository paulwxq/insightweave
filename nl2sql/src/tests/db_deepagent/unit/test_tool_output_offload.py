"""SDK 大输出转存与回读验证（doc 7 步骤 4/5）

使用真实 SDK 公开 wrapper 与生产 backend/permissions/filesystem 中间件；
覆盖边界（80000/80001）、原文件逐字相等、JSON 完整性、消息身份保留、
既有权限回读、Command 账本保留（同步/异步）、写入失败语义与完整生产图
（动态假模型从实际 ToolMessage 提取转存路径再回读）。全部离线。
"""

from __future__ import annotations

import asyncio
import json
import re
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.types import Command

from src.modules.db_deepagent.agent import (
    build_backend,
    build_db_agent,
    build_filesystem_middleware,
    build_permissions,
    build_session_dir,
)
from src.modules.db_deepagent.services import DbAgentServices
from src.modules.db_deepagent.services.table_catalog import TableCatalog
from src.modules.db_deepagent.tools import build_business_tools
from src.tests.db_deepagent.conftest import (
    TEST_SOURCE_ID,
    FakeBusinessDb,
    FakeColumn,
    FakeEmbedding,
    FakeJoinGraph,
    FakeMilvus,
    ScriptedChatModel,
    invoke_tool_node,
)

pytestmark = pytest.mark.unit

# 大输出假件规模（doc 7 步骤 4）：10 表 × 100 列 × ~50 字符列名
_BIG_TABLES = [f"public.t{i:02d}" for i in range(1, 11)]
_MARKER = "ZQ_END_MARKER_COLUMN_ZQ"
_OFFLOAD_PATH_RE = re.compile(r"/artifacts/large_tool_results/\S+")


def _big_columns(table_index: int, count: int = 100, name_len: int = 50):
    """生产实际字段形态（name/pg_type/nullable；无 comment 字段凑长度）。"""
    columns = []
    for i in range(count):
        prefix = f"c{table_index:02d}_{i:03d}_"
        columns.append(
            FakeColumn(prefix + "x" * (name_len - len(prefix)), "varchar", i % 2 == 0)
        )
    if table_index == len(_BIG_TABLES):
        columns[-1] = FakeColumn(_MARKER, "varchar", False)
    return columns


def _big_services(config) -> DbAgentServices:
    """10 表 × 100 列的假服务栈：序列化后 > 80000 字符，末表末列带标记。"""
    db = FakeBusinessDb([])
    db.columns = []  # 按表返回不同列：覆写 get_columns 语义
    per_table = {t: _big_columns(i + 1) for i, t in enumerate(_BIG_TABLES)}

    def _get_columns(schema, table):
        db.queries.append((schema, table))
        return list(per_table.get(f"{schema}.{table}", []))

    db.get_columns = _get_columns
    milvus = FakeMilvus()
    for t in _BIG_TABLES:
        milvus.cards[f"orders.{t}"] = {
            "object_id": f"orders.{t}",
            "text_raw": f"# {t} [table]\n- 说明",
            "grain_hint": None,
            "time_col_hint": None,
            "table_category": "",
        }
    return DbAgentServices(
        config=config,
        business_db=db,
        embedding=FakeEmbedding(),
        milvus=milvus,
        catalog=TableCatalog(config, milvus, db),
        join_graph=FakeJoinGraph(),
        embedding_release={},
        source_id=TEST_SOURCE_ID,
    )


@pytest.fixture()
def prod_stack(db_agent_config, tmp_path):
    """生产 backend/permissions/filesystem 中间件 + 临时会话目录。"""
    cfg = db_agent_config.model_copy(deep=True)
    cfg.workspace.root = str(tmp_path)
    session_dir = build_session_dir(cfg, "t-offload")
    backend = build_backend(session_dir, cfg)
    permissions = build_permissions(cfg)
    file_mw = build_filesystem_middleware(backend, permissions, cfg)
    offload_dir = session_dir / "artifacts" / "large_tool_results"
    return SimpleNamespace(
        cfg=cfg,
        session_dir=session_dir,
        backend=backend,
        file_mw=file_mw,
        offload_dir=offload_dir,
    )


def _request(tool_call_id: str, name: str = "inspect_tables") -> ToolCallRequest:
    return ToolCallRequest(
        tool_call={"name": name, "args": {}, "id": tool_call_id, "type": "tool_call"},
        tool=None,
        state={},
        runtime=None,
    )


def _inspect_tool_output(services: DbAgentServices, table_ids=None) -> str:
    """真实试点工具的序列化返回（不经中间件）。"""
    tools = {t.name: t for t in build_business_tools(services)}
    return tools["inspect_tables"].invoke(
        {"table_ids": table_ids or _BIG_TABLES, "include_live_columns": True}
    )


def _offloaded_files(stack) -> list:
    return sorted(stack.offload_dir.glob("*")) if stack.offload_dir.exists() else []


class TestOffloadBasics:
    def test_small_inspect_output_stays_inline(self, db_agent_config, prod_stack):
        """短输出原样返回、无转存文件（生产阈值 20000 → 80000 字符）。"""
        services = _big_services(db_agent_config)
        raw = _inspect_tool_output(services, table_ids=["public.t01"])
        assert len(raw) < 80000

        def handler(_req):
            return ToolMessage(content=raw, tool_call_id="c-small")

        out = prod_stack.file_mw.wrap_tool_call(_request("c-small"), handler)
        assert out.content == raw
        assert _offloaded_files(prod_stack) == []

    def test_threshold_boundary(self, prod_stack):
        """80000 字符不转存、80001 转存；边界测量的是消息文本。"""
        inline = "x" * 80000

        def handler_inline(_req):
            return ToolMessage(content=inline, tool_call_id="c-b1")

        out_inline = prod_stack.file_mw.wrap_tool_call(_request("c-b1"), handler_inline)
        assert out_inline.content == inline
        assert _offloaded_files(prod_stack) == []

        overflow = "x" * 80001

        def handler_over(_req):
            return ToolMessage(content=overflow, tool_call_id="c-b2")

        out_over = prod_stack.file_mw.wrap_tool_call(_request("c-b2"), handler_over)
        assert out_over.content != overflow
        assert "large_tool_results" in out_over.content
        assert len(_offloaded_files(prod_stack)) == 1
        assert _offloaded_files(prod_stack)[0].read_text(encoding="utf-8") == overflow

    def test_large_inspect_output_saved_without_loss(self, db_agent_config, prod_stack):
        """真实试点工具输出 > 80000：文件逐字相同、JSON 完整、身份保留。"""
        services = _big_services(db_agent_config)
        raw = _inspect_tool_output(services)
        # 规模口径：显式长度断言 + 标记位置在 80000 之后（doc 7 步骤 4）
        assert len(raw) > 80000
        assert raw.index(_MARKER) > 80000

        def handler(_req):
            return ToolMessage(content=raw, tool_call_id="c-big")

        out = prod_stack.file_mw.wrap_tool_call(_request("c-big"), handler)
        assert out.content != raw
        assert len(out.content) < len(raw)
        path_match = _OFFLOAD_PATH_RE.search(out.content)
        assert path_match, out.content[:200]
        assert out.tool_call_id == "c-big"
        assert out.status == "success"

        files = _offloaded_files(prod_stack)
        assert len(files) == 1
        saved = files[0].read_text(encoding="utf-8")
        assert saved == raw  # 转存文件原文与 handler 输入完全相同
        payload = json.loads(saved)
        tables = payload["data"]["tables"]
        assert [e["display"] for e in tables] == _BIG_TABLES
        assert sum(len(e["live_columns"]) for e in tables) == 1000
        assert _MARKER in saved


class TestReadbackWithPermissions:
    def test_saved_output_readable_with_existing_permissions(
        self, db_agent_config, prod_stack, db_agent_context
    ):
        """经生产中间件注册的 read_file 按行回读 80000 之后的末列标记。"""
        from deepagents import create_deep_agent
        from langgraph.checkpoint.memory import InMemorySaver

        services = _big_services(db_agent_config)
        raw = _inspect_tool_output(services)
        assert raw.index(_MARKER) > 80000

        def handler(_req):
            return ToolMessage(content=raw, tool_call_id="c-read")

        out = prod_stack.file_mw.wrap_tool_call(_request("c-read"), handler)
        file_path = _OFFLOAD_PATH_RE.search(out.content).group(0).rstrip(".,)")
        saved_files = _offloaded_files(prod_stack)
        assert len(saved_files) == 1
        assert saved_files[0].read_text(encoding="utf-8") == raw

        # 通过生产中间件注册的 read_file（create_deep_agent 装配后经工具节点）
        agent = create_deep_agent(
            model=ScriptedChatModel(responses=["ok"]),
            tools=[],
            middleware=[prod_stack.file_mw],
            backend=prod_stack.backend,
            checkpointer=InMemorySaver(),
        )
        node = agent.nodes["tools"]
        assert "read_file" in node.bound.tools_by_name

        marker_line = raw[: raw.index(_MARKER)].count("\n")

        # 首部窗口
        _, head = invoke_tool_node(
            node,
            [("read_file", {"file_path": file_path, "offset": 0, "limit": 15}, "r1")],
            db_agent_context,
        )
        assert "@@" in head.content  # SDK 状态头/窗口提示
        assert "public.t01" in head.content

        # 末列标记窗口（标记行前的多行窗口，不整读大文件）
        _, tail = invoke_tool_node(
            node,
            [
                (
                    "read_file",
                    {"file_path": file_path, "offset": marker_line - 10, "limit": 30},
                    "r2",
                )
            ],
            db_agent_context,
        )
        assert _MARKER in tail.content
        assert len(tail.content) < 80000  # 未把整个大文件塞回上下文


class TestCommandAndFailures:
    def test_command_updates_survive_offload(self, prod_stack):
        """含长 ToolMessage 的 Command：账本更新保留，消息转存并保留身份。"""
        long_content = "y" * 80001
        dim = [{"dim_evidence_id": "D-sentinel"}]
        executions = [{"run_id": "run-x", "sql": "SELECT 1"}]

        def handler(_req):
            return Command(
                update={
                    "db_dim_evidence": dim,
                    "db_executions": executions,
                    "messages": [
                        ToolMessage(content=long_content, tool_call_id="c-cmd")
                    ],
                }
            )

        out = prod_stack.file_mw.wrap_tool_call(_request("c-cmd"), handler)
        assert isinstance(out, Command)
        assert out.update["db_dim_evidence"] == dim
        assert out.update["db_executions"] == executions
        (msg,) = out.update["messages"]
        assert isinstance(msg, ToolMessage)
        assert msg.tool_call_id == "c-cmd"
        assert msg.content != long_content
        assert "large_tool_results" in msg.content
        assert len(_offloaded_files(prod_stack)) == 1

    def test_write_error_preserves_original_message(self, prod_stack):
        """WriteResult(error=...) 保留原消息；OSError 异常不被伪装为成功。"""
        from deepagents.backends.protocol import WriteResult

        long_content = "z" * 80001
        real_backend = prod_stack.backend

        class _ErrBackend:
            def __getattr__(self, item):
                return getattr(real_backend, item)

            def write(self, path, content):
                return WriteResult(error="disk full (test)")

        mw_err = build_filesystem_middleware(
            _ErrBackend(), build_permissions(prod_stack.cfg), prod_stack.cfg
        )

        def handler(_req):
            return ToolMessage(content=long_content, tool_call_id="c-err")

        out = mw_err.wrap_tool_call(_request("c-err"), handler)
        assert out.content == long_content  # 原消息保留
        assert "large_tool_results" not in out.content  # 无假文件引用

        class _RaiseBackend:
            def __getattr__(self, item):
                return getattr(real_backend, item)

            def write(self, path, content):
                raise OSError("backend unavailable (test)")

        mw_raise = build_filesystem_middleware(
            _RaiseBackend(), build_permissions(prod_stack.cfg), prod_stack.cfg
        )
        with pytest.raises(OSError):
            mw_raise.wrap_tool_call(_request("c-err"), handler)

    def test_async_offload_and_command_updates(self, prod_stack):
        """公开异步 wrapper：转存、回读文件与账本保留。"""
        long_content = "w" * 80001
        dim = [{"dim_evidence_id": "D-async"}]

        async def handler(_req):
            return Command(
                update={
                    "db_dim_evidence": dim,
                    "messages": [
                        ToolMessage(content=long_content, tool_call_id="c-async")
                    ],
                }
            )

        out = asyncio.run(
            prod_stack.file_mw.awrap_tool_call(_request("c-async"), handler)
        )
        assert isinstance(out, Command)
        assert out.update["db_dim_evidence"] == dim
        (msg,) = out.update["messages"]
        assert msg.tool_call_id == "c-async"
        assert "large_tool_results" in msg.content
        files = _offloaded_files(prod_stack)
        assert len(files) == 1
        assert files[0].read_text(encoding="utf-8") == long_content


class _OffloadFollowingModel(ScriptedChatModel):
    """按消息内容分支的动态假模型（doc 7 步骤 5）。

    第 1 轮请求 inspect_tables；第 2 轮从实际 ToolMessage 提取转存路径并
    请求 read_file 读含末列标记的窗口；第 3 轮普通文本；收到
    dbda_kind=final_answer_reminder 后再回一轮普通文本。逐轮记录收到的
    消息，供测试断言模型可见内容。
    """

    marker_offset: int = 0
    marker_limit: int = 30

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self._record(messages)
        tool_msgs = [m for m in messages if isinstance(m, ToolMessage)]
        last = messages[-1] if messages else None
        is_reminder = isinstance(last, HumanMessage) and (
            (last.additional_kwargs or {}).get("dbda_kind") == "final_answer_reminder"
        )
        if is_reminder:
            return self._wrap(AIMessage(content="收到提醒，本轮普通文本结束。"))
        if not tool_msgs:
            call = {
                "name": "inspect_tables",
                "args": {"table_ids": list(_BIG_TABLES)},
                "id": "call-inspect",
                "type": "tool_call",
            }
            return self._wrap(AIMessage(content="", tool_calls=[call]))
        latest = tool_msgs[-1]
        content = latest.content if isinstance(latest.content, str) else ""
        if latest.name == "inspect_tables":
            match = _OFFLOAD_PATH_RE.search(content)
            if match is None:
                return self._wrap(AIMessage(content="未观察到转存路径（旧实现？）"))
            file_path = match.group(0).rstrip(".,)")
            call = {
                "name": "read_file",
                "args": {
                    "file_path": file_path,
                    "offset": self.marker_offset,
                    "limit": self.marker_limit,
                },
                "id": "call-read",
                "type": "tool_call",
            }
            return self._wrap(AIMessage(content="", tool_calls=[call]))
        if latest.name == "read_file":
            seen = _MARKER in content
            return self._wrap(AIMessage(content=f"read_file 回读完成，标记可见={seen}"))
        return self._wrap(AIMessage(content="非预期工具响应"))

    def _record(self, messages):
        turns = getattr(self, "_seen_turns", None)
        if turns is None:
            turns = []
            object.__setattr__(self, "_seen_turns", turns)
        turns.append(list(messages))

    def _wrap(self, message):
        from langchain_core.outputs import ChatGeneration, ChatResult

        return ChatResult(generations=[ChatGeneration(message=message)])


class TestProductionGraphOffload:
    def test_production_graph_reads_offloaded_inspect_output(
        self, assembly_services, monkeypatch
    ):
        """完整生产图：大输出被替换为预览/路径，动态假模型回读到末列标记。"""
        from langgraph.checkpoint.memory import InMemorySaver

        import src.modules.db_deepagent.agent as agent_mod

        # 完整图需要 budget/pipeline 等步骤 3 服务：仅替换检索/业务库三类假件
        big = _big_services(assembly_services.config)
        services = DbAgentServices(
            **{
                **assembly_services.__dict__,
                "business_db": big.business_db,
                "milvus": big.milvus,
                "catalog": big.catalog,
            }
        )
        raw = _inspect_tool_output(services)
        assert len(raw) > 80000
        assert raw.index(_MARKER) > 80000
        marker_line = raw[: raw.index(_MARKER)].count("\n")

        model = _OffloadFollowingModel(
            marker_offset=max(marker_line - 10, 0), marker_limit=30
        )
        monkeypatch.setattr(agent_mod, "build_model", lambda _cfg: model)

        handle = build_db_agent(
            services, session_id="t-offload-graph", checkpointer=InMemorySaver()
        )

        from datetime import UTC, datetime

        from src.modules.db_deepagent.context import DbAgentContext

        thread_id = "th-offload"
        ctx = DbAgentContext(
            session_id="t-offload-graph",
            thread_id=thread_id,
            request_id="req-offload",
            run_id="run-offload",
            datasource_id="pg:test",
            validation_fingerprint="fp",
            now=datetime.now(UTC),
            timezone="Asia/Shanghai",
            user_texts=("统计各表明细",),
            carried_evidence=(),
            deadline_monotonic=float("inf"),
        )
        config = {
            "configurable": {"thread_id": thread_id},
            "recursion_limit": 60,
            "metadata": {"request_id": "req-offload", "dbda_call_kind": "agent"},
        }
        # 与生产 Runner 一致：先注册预算运行，预占才会计数（未注册时放行不计数）
        import time as _time

        from src.modules.db_deepagent.services.run_budget import limits_from_config

        services.budget.open_run(
            "run-offload",
            limits_from_config(services.config.budget),
            deadline_monotonic=_time.monotonic() + 300,
        )
        handle.agent.invoke(
            {"messages": [HumanMessage(content="请检查全部表结构")]},
            config=config,
            context=ctx,
        )

        turns = getattr(model, "_seen_turns", [])
        assert len(turns) >= 4, [len(t) for t in turns]

        # 模型可见内容：inspect 轮看到预览/路径且无整段原文；read 轮看到标记
        inspect_turn = next(
            t
            for t in turns
            if any(getattr(m, "name", "") == "inspect_tables" for m in t)
        )
        inspect_msg = next(
            m for m in inspect_turn if getattr(m, "name", "") == "inspect_tables"
        )
        assert "large_tool_results" in inspect_msg.content
        assert len(inspect_msg.content) < 80000  # 原始大输出未直接进入该轮请求
        read_turn = next(
            t for t in turns if any(getattr(m, "name", "") == "read_file" for m in t)
        )
        read_msg = next(m for m in read_turn if getattr(m, "name", "") == "read_file")
        assert _MARKER in read_msg.content

        # 业务状态（doc 7 步骤 5 / C1-1 收紧）：工具调用序列恰为
        # inspect_tables → read_file 各一次；预算 tool_call 恰为 2；提醒恰
        # 一次且提醒后的回复不带工具（不因提醒重复 inspect/read）
        values = handle.agent.get_state(
            {"configurable": {"thread_id": thread_id}}
        ).values
        final_messages = values.get("messages", [])
        tool_call_names = [
            call["name"]
            for m in final_messages
            if isinstance(m, AIMessage)
            for call in (m.tool_calls or [])
        ]
        assert tool_call_names == ["inspect_tables", "read_file"]
        status = (values.get("db_run_status") or {}).get("run-offload") or {}
        assert status.get("counters", {}).get("tool_call", 0) == 2
        assert status.get("final_answer_reminders") == 1
        assert status.get("stop_reason") is None
        reminder_idx = next(
            i
            for i, m in enumerate(final_messages)
            if isinstance(m, HumanMessage)
            and (m.additional_kwargs or {}).get("dbda_kind") == "final_answer_reminder"
        )
        post_reminder_ai = [
            m for m in final_messages[reminder_idx + 1 :] if isinstance(m, AIMessage)
        ]
        assert post_reminder_ai, "提醒后模型未回复"
        assert all(
            not (m.tool_calls or []) for m in post_reminder_ai
        ), "提醒后的回复不得再带工具调用"
        bound = handle.agent.nodes["tools"].bound.tools_by_name
        from src.modules.db_deepagent.agent import stage_expected_tool_names

        assert set(bound) == stage_expected_tool_names(services.config)
        assert len(bound) == 15
