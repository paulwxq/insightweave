"""SDK 大输出转存与回读验证（doc 7 步骤 4/5；doc 9 §4 步骤 4）

使用真实 SDK 公开 wrapper 与生产 backend/permissions/filesystem 中间件；
覆盖边界（80000/80001）、原文件逐字相等、JSON 完整性、消息身份保留、
既有权限回读、Command 账本保留（同步/异步）、写入失败语义与完整生产图
（动态假模型从实际 ToolMessage 提取转存路径再回读）。全部离线。

doc 9 新增：真实 read_result / execute_sql 大输出的转存、逐字回读与
Command 账本一致性；完整生产图 validate_sql → execute_sql → read_file →
submit_final_answer 闭环（提交内容来自实际回读窗口，非头尾预览）。
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import date
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
    FakeExecDb,
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

# doc 9 结果类大输出假件（20 行 × 4600 字符单元格 ≈ 95000 字符 > 80000）
_RESULT_MARKER = "MK_READBACK_TAIL_ROW_12"
_WIDE_SQL = "SELECT id, status, amount, customer_id, created_at FROM public.orders"
# read_file 窗口内引号包裹的标记族单元格值（Kimi R1 K1-1：提交值来源）
_WINDOW_VALUE_RE = re.compile(r'"(MK_[A-Z0-9_]+)"')


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


# ============================================================================
# doc 9 步骤 4：结果类工具（read_result / execute_sql）大输出转存
# ============================================================================


@pytest.fixture()
def result_services(db_agent_config, tmp_path):
    """带真实校验执行管线 + ResultStore 的服务栈（FakeExecDb，临时工作区）。"""
    from src.modules.db_deepagent.services.dimension_probe import DimensionProbeBuilder
    from src.modules.db_deepagent.services.result_analysis import ResultAnalysisService
    from src.modules.db_deepagent.services.result_store import ResultStore
    from src.modules.db_deepagent.services.run_budget import RunBudgetLedger
    from src.modules.db_deepagent.services.sql_guard import SqlEvidenceGuard
    from src.modules.db_deepagent.services.sql_pipeline import SqlPipeline
    from src.modules.db_deepagent.services.sql_validation import DbSqlValidator
    from src.modules.db_deepagent.services.time_resolver import TimeResolver

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
    return SimpleNamespace(
        cfg=cfg,
        db=db,
        results=results,
        budget=budget,
        services=DbAgentServices(
            config=cfg,
            business_db=db,
            embedding=FakeEmbedding(),  # type: ignore[arg-type]
            milvus=FakeMilvus(),  # type: ignore[arg-type]
            catalog=TableCatalog(cfg, FakeMilvus(), db),
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
        ),
    )


def _tools_node_with_file_mw(prod_stack, tools, captured_raw=None):
    """生产 filesystem 中间件包住业务工具的 ToolNode（含 read_file 注册）。

    captured_raw（可选）：dict[str, list[str]]。提供时在 SDK 转存前截获
    每次工具调用的原始 ToolMessage.content（Kimi R1 K1-2：供转存文件与
    同一次调用原正文做逐字比较；不引入第二次执行）。
    """
    from deepagents import create_deep_agent
    from langgraph.checkpoint.memory import InMemorySaver

    if captured_raw is not None:
        real_wrap = prod_stack.file_mw.wrap_tool_call

        def _capturing_wrap(request, handler):
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

        prod_stack.file_mw.wrap_tool_call = _capturing_wrap
    agent = create_deep_agent(
        model=ScriptedChatModel(responses=["ok"]),
        tools=list(tools),
        middleware=[prod_stack.file_mw],
        backend=prod_stack.backend,
        checkpointer=InMemorySaver(),
    )
    return agent.nodes["tools"]


def _reject_constant(value: str):
    raise ValueError(f"非标准 JSON 常量: {value}")


def _strict_loads(text: str):
    return json.loads(text, parse_constant=_reject_constant)


class TestResultToolOffload:
    def test_read_result_large_output_offloaded_verbatim(
        self, result_services, prod_stack, db_agent_context
    ):
        """真实 read_result 200 行 × 500 字符页：文件逐字等于正文，可窗口回读。

        read_result 对同一 result_ref 的两次调用返回逐字相同正文，故裸
        ToolNode 输出可与转存文件做字节级比较（doc 9 §5 SDK 阈值行）。
        """
        from src.modules.db_deepagent.tools.result_tools import make_read_result_tool

        rows = [[i, "M" * 500, _RESULT_MARKER if i == 190 else ""] for i in range(200)]
        meta = result_services.results.save(
            ctx=db_agent_context,
            purpose="query",
            sql_template="SELECT k, blob, note FROM t",
            executed_sql="SELECT k, blob, note FROM t",
            columns=[
                {"name": "k", "pg_type": "int4"},
                {"name": "blob", "pg_type": "text"},
                {"name": "note", "pg_type": "varchar"},
            ],
            rows=rows,
            source_truncated=False,
            derived_from=[],
            guard_evidence={"time": [], "dim": []},
        )
        args = {"result_ref": meta.result_ref, "offset": 0, "limit": 200}

        from langgraph.prebuilt import ToolNode

        bare = ToolNode([make_read_result_tool(result_services.services)])
        _u, raw_msg = invoke_tool_node(
            bare, [("read_result", args, "r-raw")], db_agent_context
        )
        raw = raw_msg.content
        assert len(raw) > 80000  # 正文超过 SDK 转存阈值
        marker_line = raw[: raw.index(_RESULT_MARKER)].count("\n")
        assert marker_line > 10  # 标记不在文件头部

        node = _tools_node_with_file_mw(
            prod_stack, [make_read_result_tool(result_services.services)]
        )
        updates, msg = invoke_tool_node(
            node, [("read_result", args, "r-1")], db_agent_context
        )
        assert msg.content != raw
        assert "large_tool_results" in msg.content
        assert msg.tool_call_id == "r-1"
        assert msg.status == "success"
        files = _offloaded_files(prod_stack)
        assert len(files) == 1
        assert files[0].read_text(encoding="utf-8") == raw  # 逐字等于工具正文

        # 既有权限按窗口回读：标记行窗口（不整读大文件）
        file_path = _OFFLOAD_PATH_RE.search(msg.content).group(0).rstrip(".,)")
        _u2, readback = invoke_tool_node(
            node,
            [
                (
                    "read_file",
                    {"file_path": file_path, "offset": marker_line - 2, "limit": 6},
                    "r-2",
                )
            ],
            db_agent_context,
        )
        assert _RESULT_MARKER in readback.content
        assert "@@" in readback.content  # SDK 状态头/窗口提示
        assert len(readback.content) < 80000
        # 原始结果不受消息层处理影响：预览之外的行仍可从 ResultStore 读取
        page = result_services.results.read_rows(
            meta.result_ref,
            session_id=db_agent_context.session_id,
            offset=190,
            limit=5,
        )
        assert page.rows[0][2] == _RESULT_MARKER
        assert not {k for k in updates if k != "messages"}  # read_result 无账本更新

    def test_execute_sql_large_output_offloaded_with_command(
        self, result_services, prod_stack, db_agent_context
    ):
        """真实 execute_sql 大预览（>80000）：Command 账本保留、文件可回读。

        Kimi R1 K1-2：在 SDK 转存前截获同一次 execute_sql 调用的原始
        ToolMessage.content，断言转存文件与其逐字相等（不引入第二次执行
        生成另一个随机 result_ref）；账本与 ResultStore 断言保留。
        """
        from src.modules.db_deepagent.tools.sql_tools import (
            make_execute_sql_tool,
            make_validate_sql_tool,
        )

        wide = "G" * 4600
        result_services.db.rows = [
            (
                i,
                "已支付",
                wide,
                _RESULT_MARKER if i == 12 else f"C{i:03d}",
                date(2026, 9, 5),
            )
            for i in range(1, 21)
        ]
        captured_raw: dict[str, list[str]] = {}
        node = _tools_node_with_file_mw(
            prod_stack,
            [
                make_validate_sql_tool(result_services.services),
                make_execute_sql_tool(result_services.services),
            ],
            captured_raw=captured_raw,
        )
        u1, _v = invoke_tool_node(
            node, [("validate_sql", {"sql": _WIDE_SQL}, "e2e-v")], db_agent_context
        )
        updates, msg = invoke_tool_node(
            node,
            [("execute_sql", {"sql": _WIDE_SQL}, "e2e-x")],
            db_agent_context,
            extra_state={"db_validations": u1["db_validations"]},
        )
        assert "large_tool_results" in msg.content  # >80000 已转存替换
        assert msg.tool_call_id == "e2e-x"
        assert msg.status == "success"
        # Command 账本更新完整保留
        exec_record = updates["db_executions"][0]
        assert exec_record["purpose"] == "query"
        assert exec_record["evidence_id"].startswith("E-")
        assert exec_record["result_ref"].startswith("res_")
        # 同一次调用的原正文（转存前截获）与转存文件逐字相等
        (raw_content,) = captured_raw["e2e-x"]
        assert len(raw_content) > 80000
        files = _offloaded_files(prod_stack)
        assert len(files) == 1
        assert files[0].read_text(encoding="utf-8") == raw_content
        payload = _strict_loads(raw_content)
        data = payload["data"]
        assert data["evidence_id"] == exec_record["evidence_id"]
        assert data["result_ref"] == exec_record["result_ref"]
        assert len(data["preview_rows"]) == 20
        assert data["preview_rows"][11][3] == _RESULT_MARKER
        assert data["preview_rows"][0][2] == wide
        # 消息引用（预览头 5 行含 evidence_id/result_ref）与账本一致
        head = msg.content
        assert exec_record["evidence_id"] in head
        assert exec_record["result_ref"] in head
        # 原始结果可按窗口从 ResultStore 读取
        page = result_services.results.read_rows(
            exec_record["result_ref"],
            session_id=db_agent_context.session_id,
            offset=12,
            limit=3,
        )
        assert page.rows[0][3] == "C013"


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


class _ResultChainModel(ScriptedChatModel):
    """doc 9 §4 步骤 4 完整图假模型：validate → execute → read_file → submit。

    从替换消息的头 5 行预览提取转存路径与 evidence_id（合法引用可来自
    预览）；提交值必须由 read_file 实际窗口解析取得（Kimi R1 K1-1）：
    解析失败时如实说明未取得，不回退到预置 marker 字段——该字段仅供
    测试外层计算窗口位置与断言。逐轮记录模型请求。
    """

    marker: str = _RESULT_MARKER
    marker_offset: int = 0
    marker_limit: int = 20
    sql: str = _WIDE_SQL

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
            return self._wrap(
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "validate_sql",
                            "args": {"sql": self.sql},
                            "id": "call-validate",
                            "type": "tool_call",
                        }
                    ],
                )
            )
        latest = tool_msgs[-1]
        content = latest.content if isinstance(latest.content, str) else ""
        if latest.name == "validate_sql":
            payload = json.loads(content)
            assert payload["status"] == "ok", payload
            return self._wrap(
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "execute_sql",
                            "args": {"sql": self.sql},
                            "id": "call-execute",
                            "type": "tool_call",
                        }
                    ],
                )
            )
        if latest.name == "execute_sql":
            match = _OFFLOAD_PATH_RE.search(content)
            if match is None:
                return self._wrap(
                    AIMessage(content="未观察到转存路径（输出未超阈值或旧实现）")
                )
            evidence = re.search(r'"evidence_id": "(E-[0-9a-f]{8})"', content)
            if evidence is None:
                return self._wrap(AIMessage(content="预览未见 evidence_id"))
            object.__setattr__(self, "_evidence_id", evidence.group(1))
            return self._wrap(
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "read_file",
                            "args": {
                                "file_path": match.group(0).rstrip(".,)"),
                                "offset": self.marker_offset,
                                "limit": self.marker_limit,
                            },
                            "id": "call-read",
                            "type": "tool_call",
                        }
                    ],
                )
            )
        if latest.name == "read_file":
            # Kimi R1 K1-1：提交值由窗口实际解析取得；窗口缺目标值时如实
            # 说明，不回退到预置 marker（该字段只供测试外层断言使用）。
            evidence_id = getattr(self, "_evidence_id", "")
            parsed = _WINDOW_VALUE_RE.search(content)
            window_value = parsed.group(1) if parsed else None
            object.__setattr__(self, "_submitted_window_value", window_value)
            if window_value is None:
                answer = (
                    "read_file 窗口未包含目标单元格，未取得第 12 行标记值；"
                    f"引用执行证据 {evidence_id}。"
                )
                finding_text = (
                    "read_file 窗口未命中第 12 行 customer_id，本次未取得中部标记"
                )
            else:
                answer = (
                    f"第 12 行 customer_id 实际回读值 {window_value}；"
                    f"中部数据经 read_file 窗口取得，引用执行证据 {evidence_id}。"
                )
                finding_text = (
                    "转存文件中部行经 read_file 回读：" f"customer_id 值 {window_value}"
                )
            return self._wrap(
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "submit_final_answer",
                            "args": {
                                "status": "completed",
                                "answer": answer,
                                "findings": [
                                    {
                                        "text": finding_text,
                                        "evidence_ids": [evidence_id],
                                    }
                                ],
                                "metric_definitions": [],
                                "limitations": ["大输出被 SDK 转存，中部行按窗口回读"],
                                "open_questions": [],
                            },
                            "id": "call-submit",
                            "type": "tool_call",
                        }
                    ],
                )
            )
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


class TestProductionGraphResultChain:
    def test_validate_execute_readfile_submit_chain(
        self, assembly_services, monkeypatch
    ):
        """doc 9 §4 步骤 4：完整生产图离线业务闭环。

        validate_sql → execute_sql（>80000 转存）→ 从替换消息提取路径与
        预览中的合法引用 → read_file 读头尾预览之外的中部标记 →
        submit_final_answer 通过门控。提交内容使用实际回读数据；记录的
        模型请求证明完整原文从未直接进入模型上下文。该用例证明"读回
        后部数据并用合法引用提交"，不证明从文件恢复了引用。
        """
        from langgraph.checkpoint.memory import InMemorySaver
        from langgraph.prebuilt import ToolNode

        import src.modules.db_deepagent.agent as agent_mod
        from src.modules.db_deepagent.tools.sql_tools import (
            make_execute_sql_tool,
            make_validate_sql_tool,
        )

        wide = "G" * 4600
        assembly_services.business_db.rows = [
            (
                i,
                "已支付",
                wide,
                _RESULT_MARKER if i == 12 else f"C{i:03d}",
                date(2026, 9, 5),
            )
            for i in range(1, 21)
        ]

        # 预计算：直接执行真实工具取得正文布局（字段定长 → 行号稳定）
        bare = ToolNode(
            [
                make_validate_sql_tool(assembly_services),
                make_execute_sql_tool(assembly_services),
            ]
        )
        _uv, _mv = invoke_tool_node(
            bare,
            [("validate_sql", {"sql": _WIDE_SQL}, "pre-v")],
            db_agent_context_of("run-graph9"),
        )
        _ux, mx = invoke_tool_node(
            bare,
            [("execute_sql", {"sql": _WIDE_SQL}, "pre-x")],
            db_agent_context_of("run-graph9"),
            extra_state={"db_validations": _uv["db_validations"]},
        )
        raw = mx.content
        assert len(raw) > 80000  # 越过 SDK 转存阈值
        lines = raw.splitlines()
        head_tail = lines[:5] + lines[-5:]
        assert all(_RESULT_MARKER not in ln for ln in head_tail)  # 不在头尾预览
        marker_line = raw[: raw.index(_RESULT_MARKER)].count("\n")
        assert marker_line > 5

        model = _ResultChainModel(
            marker_offset=max(marker_line - 5, 0), marker_limit=20
        )
        monkeypatch.setattr(agent_mod, "build_model", lambda _cfg: model)

        handle = build_db_agent(
            assembly_services, session_id="t-graph9", checkpointer=InMemorySaver()
        )

        from datetime import UTC, datetime

        from src.modules.db_deepagent.context import DbAgentContext

        thread_id = "th-graph9"
        ctx = DbAgentContext(
            session_id="t-graph9",
            thread_id=thread_id,
            request_id="req-graph9",
            run_id="run-graph9",
            datasource_id="pg:test",
            validation_fingerprint="fp",
            now=datetime.now(UTC),
            timezone="Asia/Shanghai",
            user_texts=("读取后半部分数据并提交最终答案",),
            carried_evidence=(),
            deadline_monotonic=float("inf"),
        )
        config = {
            "configurable": {"thread_id": thread_id},
            "recursion_limit": 80,
            "metadata": {"request_id": "req-graph9", "dbda_call_kind": "agent"},
        }
        import time as _time

        from src.modules.db_deepagent.services.run_budget import limits_from_config

        assembly_services.budget.open_run(
            "run-graph9",
            limits_from_config(assembly_services.config.budget),
            deadline_monotonic=_time.monotonic() + 300,
        )
        handle.agent.invoke(
            {"messages": [HumanMessage(content="请查询全部订单并提交最终答案")]},
            config=config,
            context=ctx,
        )

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
        assert tool_call_names == [
            "validate_sql",
            "execute_sql",
            "read_file",
            "submit_final_answer",
        ]

        # 最终答案通过门控：登记记录存在，提交内容使用实际回读的标记
        answers = values.get("db_final_answers") or {}
        assert "run-graph9" in answers
        record = answers["run-graph9"]
        # Kimi R1 K1-1：提交值来自 read_file 窗口解析（模型记录的解析值
        # 非空且等于预定标记；因果对照见 TestResultChainModelWindowParsing）
        assert getattr(model, "_submitted_window_value", None) == _RESULT_MARKER
        assert _RESULT_MARKER in record["answer"]["answer"]
        assert record["answer"]["findings"][0]["evidence_ids"] == [
            getattr(model, "_evidence_id", "")
        ]
        # 引用与账本一致（引用取自预览头 5 行，属本运行 query 执行）
        exec_records = [
            r
            for r in (values.get("db_executions") or [])
            if r.get("run_id") == "run-graph9" and r.get("purpose") == "query"
        ]
        assert [r["evidence_id"] for r in exec_records] == [
            getattr(model, "_evidence_id", "")
        ]

        # 模型请求证据：完整原文从未直接进入任何一轮模型请求
        turns = getattr(model, "_seen_turns", [])
        assert len(turns) >= 4, [len(t) for t in turns]
        # 首个携带 read_file 结果的轮次（提交决策轮）之前的任何模型请求
        # 都不含标记：execute 转存预览（头尾 5 行）不含中部标记
        read_result_turn_idx = next(
            i
            for i, t in enumerate(turns)
            if any(getattr(m, "name", "") == "read_file" for m in t)
        )
        for i, turn in enumerate(turns):
            for m in turn:
                text = m.content if isinstance(m.content, str) else ""
                # 原始完整正文（>80000）从未整体进入任何模型请求；
                # read_file 窗口按行回读是合法的部分可见
                assert len(text) < 80000
                if i < read_result_turn_idx:
                    assert "G" * 1100 not in text  # 预览行裁剪至 1000 字符
                    assert _RESULT_MARKER not in text
        execute_turn = next(
            t for t in turns if any(getattr(m, "name", "") == "execute_sql" for m in t)
        )
        execute_msg = next(
            m for m in execute_turn if getattr(m, "name", "") == "execute_sql"
        )
        assert "large_tool_results" in execute_msg.content
        assert _RESULT_MARKER not in execute_msg.content
        assert len(execute_msg.content) < 80000
        read_msg = next(
            m
            for m in turns[read_result_turn_idx]
            if getattr(m, "name", "") == "read_file"
        )
        assert _RESULT_MARKER in read_msg.content  # 提交内容来源：实际回读窗口

        # 转存文件在图会话工作区，内容为完整序列化正文（>80000）
        offload_dir = (
            handle_session_dir(assembly_services.config, "t-graph9")
            / "artifacts"
            / "large_tool_results"
        )
        files = sorted(offload_dir.glob("*")) if offload_dir.exists() else []
        assert len(files) >= 1
        saved = files[-1].read_text(encoding="utf-8")
        assert len(saved) > 80000
        payload = _strict_loads(saved)
        assert payload["data"]["preview_rows"][11][3] == _RESULT_MARKER
        from src.modules.db_deepagent.tools._envelope import serialize_tool_output

        assert serialize_tool_output(payload) == saved


def db_agent_context_of(run_id: str):
    """图用例的固定上下文（预计算裸执行与图运行共用 run_id 布局）。"""
    from datetime import UTC, datetime

    from src.modules.db_deepagent.context import DbAgentContext

    return DbAgentContext(
        session_id="t-graph9",
        thread_id="th-graph9",
        request_id="req-graph9",
        run_id=run_id,
        datasource_id="pg:test",
        validation_fingerprint="fp",
        now=datetime.now(UTC),
        timezone="Asia/Shanghai",
        user_texts=("读取后半部分数据并提交最终答案",),
        carried_evidence=(),
        deadline_monotonic=float("inf"),
    )


def handle_session_dir(cfg, session_id):
    from src.modules.db_deepagent.agent import build_session_dir

    return build_session_dir(cfg, session_id)


class TestResultChainModelWindowParsing:
    """Kimi R1 K1-1 反向与因果对照：提交值必须来自 read_file 窗口解析。

    直接驱动假模型的 read_file 分支（同 Kimi 复现方式）：窗口含不同值
    时提交该窗口值而非预置 marker；窗口缺目标值时不提交预置 marker。
    """

    @staticmethod
    def _read_file_turn(window: str) -> list:
        return [
            HumanMessage(content="请查询全部订单"),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "read_file",
                        "args": {},
                        "id": "call-read",
                        "type": "tool_call",
                    }
                ],
            ),
            ToolMessage(content=window, name="read_file", tool_call_id="call-read"),
        ]

    @staticmethod
    def _submit_args(model, messages) -> dict:
        result = model._generate(list(messages))
        (call,) = result.generations[0].message.tool_calls
        assert call["name"] == "submit_final_answer"
        return call["args"]

    def _model(self) -> _ResultChainModel:
        model = _ResultChainModel()
        object.__setattr__(model, "_evidence_id", "E-direct01")
        return model

    def test_window_with_marker_submits_window_value(self):
        model = self._model()
        window = (
            "@@ lines 90-109 of 176 | next offset 109 @@\n"
            f' 96        "{_RESULT_MARKER}",'
        )
        args = self._submit_args(model, self._read_file_turn(window))
        assert _RESULT_MARKER in args["answer"]
        assert _RESULT_MARKER in args["findings"][0]["text"]

    def test_different_window_value_submitted_not_preset_marker(self):
        """因果对照：提交值取自窗口实际内容，而非模型预置字段。"""
        other = "MK_DIFFERENT_WINDOW_VALUE"
        model = self._model()
        window = f'@@ lines 1-5 of 5 @@\n 3      "{other}",'
        args = self._submit_args(model, self._read_file_turn(window))
        assert other in args["answer"] and other in args["findings"][0]["text"]
        assert _RESULT_MARKER not in args["answer"]
        assert _RESULT_MARKER not in args["findings"][0]["text"]

    def test_window_without_marker_does_not_submit_preset(self):
        """反向检查：窗口缺目标值时不得提交预置 marker（Kimi 复现场景）。"""
        model = self._model()
        window = '@@ lines 1-5 of 5 @@\n 3      "C011",\n 4      "C013",'
        args = self._submit_args(model, self._read_file_turn(window))
        combined = args["answer"] + args["findings"][0]["text"]
        assert _RESULT_MARKER not in combined
        assert "未取得" in args["answer"]  # 如实说明，不编造目标值
