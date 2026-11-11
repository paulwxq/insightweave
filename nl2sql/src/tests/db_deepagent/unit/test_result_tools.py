"""result_tools 单元测试（docs/v4/5 §7.9/§7.10；doc 9 §4 步骤 1）

read_result 分页字段与大页 JSON 完整性；analyze_result 本地路径的
AnalysisRecord 登记（evidence_id、input/output refs、executed_sql=None、
Command 合并）与大预览不删行/不裁列。
"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest
from langgraph.prebuilt import ToolNode

from src.modules.db_deepagent.context import DbAgentContext
from src.modules.db_deepagent.services import DbAgentServices
from src.modules.db_deepagent.tools.result_tools import (
    make_analyze_result_tool,
    make_read_result_tool,
)

from src.tests.db_deepagent.conftest import invoke_tool_node

pytestmark = pytest.mark.unit


def _reject_constant(value: str):
    raise ValueError(f"非标准 JSON 常量: {value}")


def _strict_loads(text: str):
    return json.loads(text, parse_constant=_reject_constant)


@pytest.fixture()
def node(guard_services: DbAgentServices) -> ToolNode:
    return ToolNode(
        [
            make_read_result_tool(guard_services),
            make_analyze_result_tool(guard_services),
        ]
    )


def _save_input(services: DbAgentServices, ctx: DbAgentContext, *, datasource_id=None):
    return services.results.save(
        ctx=ctx,
        purpose="query",
        sql_template="SELECT k, x FROM t",
        executed_sql="SELECT k, x FROM t",
        columns=[
            {"name": "k", "pg_type": "int4"},
            {"name": "x", "pg_type": "numeric"},
        ],
        rows=[[1, Decimal("1.5")], [1, Decimal("2.5")], [2, None]],
        source_truncated=False,
        derived_from=[],
        guard_evidence={"time": [], "dim": []},
        datasource_id=datasource_id,
    )


class TestReadResult:
    def test_pagination_fields(self, node, guard_services, db_agent_context):
        meta = _save_input(guard_services, db_agent_context)
        _u, msg = invoke_tool_node(
            node,
            [
                (
                    "read_result",
                    {"result_ref": meta.result_ref, "offset": 0, "limit": 2},
                    "tc-r1",
                )
            ],
            db_agent_context,
        )
        payload = json.loads(msg.content)
        assert payload["status"] == "ok"
        data = payload["data"]
        assert data["saved_row_count"] == 3
        assert data["next_offset"] == 2 and data["preview_truncated"] is True
        assert data["source_truncated"] is False
        assert data["rows"][0] == [1, {"$dec": "1.5"}]  # §6.8 编码

    def test_session_denied(self, node, guard_services, db_agent_context):
        from dataclasses import replace

        meta = _save_input(guard_services, db_agent_context)
        other = replace(db_agent_context, session_id="s-other")
        _u, msg = invoke_tool_node(
            node, [("read_result", {"result_ref": meta.result_ref}, "tc-r2")], other
        )
        payload = json.loads(msg.content)
        assert payload["error"]["error_type"] == "result_access_denied"

    def test_wide_page_columns_rows_aligned(
        self, node, guard_services, db_agent_context
    ):
        """doc 9 §4 步骤 1：140 列 × 2 行的真实大页。

        旧 finalize_output 把 columns 裁到 5 而 rows 保留 140 个单元格——
        JSON 仍合法但行列错位；本用例必须失败于列数/对应值断言（不是
        JSONDecodeError）。修复后逐项比较原列顺序、行宽与位置值。
        """
        n_cols = 140
        names = [f"c{i:03d}_" + "x" * 50 for i in range(n_cols)]
        rows = [[f"v{r}{c:03d}" for c in range(n_cols)] for r in range(2)]
        meta = guard_services.results.save(
            ctx=db_agent_context,
            purpose="query",
            sql_template="SELECT * FROM wide",
            executed_sql="SELECT * FROM wide",
            columns=[{"name": n, "pg_type": "varchar"} for n in names],
            rows=rows,
            source_truncated=False,
            derived_from=[],
            guard_evidence={"time": [], "dim": []},
        )
        _u, msg = invoke_tool_node(
            node,
            [("read_result", {"result_ref": meta.result_ref}, "tc-w1")],
            db_agent_context,
        )
        # 数据前置条件（独立于生产代码）：完整载荷的首次序列化即超过旧预算，
        # 旧函数因此进入裁列分支
        expected_data = {
            "rows": rows,
            "columns": [{"name": n, "pg_type": "varchar"} for n in names],
            "offset": 0,
            "next_offset": None,
            "saved_row_count": 2,
            "preview_truncated": False,
            "source_truncated": False,
        }
        assert (
            len(
                json.dumps(
                    {"status": "ok", "data": expected_data, "warnings": []},
                    ensure_ascii=False,
                )
            )
            > 12000
        )
        payload = _strict_loads(msg.content)
        data = payload["data"]
        expected_columns = [{"name": n, "pg_type": "varchar"} for n in names]
        # 旧实现此处 columns 为 5 项而 rows 保留 140 个单元格（合法 JSON 但错位）
        assert data["columns"] == expected_columns
        assert data["rows"] == rows
        assert all(len(r) == n_cols for r in data["rows"])  # 行宽与列数一致
        assert data["next_offset"] is None and data["saved_row_count"] == 2
        assert len(msg.content) > 12000  # 修复后实测长度越过旧预算

    def test_single_huge_cell_intact(self, node, guard_services, db_agent_context):
        """单行单格 20000 字符：旧实现整体截断产出非法 JSON；修复后整格保留。"""
        blob = "B" * 20000
        meta = guard_services.results.save(
            ctx=db_agent_context,
            purpose="query",
            sql_template="SELECT k, blob FROM t",
            executed_sql="SELECT k, blob FROM t",
            columns=[
                {"name": "k", "pg_type": "int4"},
                {"name": "blob", "pg_type": "text"},
            ],
            rows=[[1, blob], [2, Decimal("2.5")]],
            source_truncated=False,
            derived_from=[],
            guard_evidence={"time": [], "dim": []},
        )
        _u, msg = invoke_tool_node(
            node,
            [("read_result", {"result_ref": meta.result_ref}, "tc-w2")],
            db_agent_context,
        )
        payload = _strict_loads(msg.content)  # 旧实现此处 JSONDecodeError
        data = payload["data"]
        assert data["columns"] == [
            {"name": "k", "pg_type": "int4"},
            {"name": "blob", "pg_type": "text"},
        ]
        assert data["rows"][0][1] == blob  # 超长文本不改写为截短标记
        assert data["rows"][1] == [2, {"$dec": "2.5"}]  # §6.8 类型信封完整

    def test_continuous_pagination_no_skip_or_repeat(
        self, node, guard_services, db_agent_context
    ):
        """按原 next_offset 连续分页：拼回全部已保存行，无跳行/重复；末页 None。

        source_truncated 标志每页原样保留（不做消息层删行或游标修正）。
        """
        all_rows = [[i, Decimal("1.5")] for i in range(7)]
        collected_flags = []
        for truncated in (False, True):
            meta = guard_services.results.save(
                ctx=db_agent_context,
                purpose="query",
                sql_template="SELECT k, x FROM t",
                executed_sql="SELECT k, x FROM t",
                columns=[
                    {"name": "k", "pg_type": "int4"},
                    {"name": "x", "pg_type": "numeric"},
                ],
                rows=all_rows,
                source_truncated=truncated,
                derived_from=[],
                guard_evidence={"time": [], "dim": []},
            )
            collected = []
            offset = 0
            for _ in range(5):  # 上限保护，末页必须在此内到达
                _u, msg = invoke_tool_node(
                    node,
                    [
                        (
                            "read_result",
                            {
                                "result_ref": meta.result_ref,
                                "offset": offset,
                                "limit": 3,
                            },
                            f"tc-p-{truncated}-{offset}",
                        )
                    ],
                    db_agent_context,
                )
                data = _strict_loads(msg.content)["data"]
                assert data["offset"] == offset
                collected.extend(data["rows"])
                collected_flags.append(data["source_truncated"] is truncated)
                if data["next_offset"] is None:
                    break
                offset = data["next_offset"]
            assert collected == [[i, {"$dec": "1.5"}] for i in range(7)]
        assert all(collected_flags)


class TestAnalyzeResult:
    def test_local_aggregate_registers_analysis(
        self, node, guard_services, db_agent_context
    ):
        meta = _save_input(guard_services, db_agent_context)
        updates, msg = invoke_tool_node(
            node,
            [
                (
                    "analyze_result",
                    {
                        "operation": "aggregate",
                        "inputs": [meta.result_ref],
                        "params": {
                            "group_by": ["k"],
                            "metrics": [{"column": "x", "func": "sum"}],
                        },
                        "allow_pushdown": True,
                    },
                    "tc-a1",
                )
            ],
            db_agent_context,
        )
        payload = json.loads(msg.content)
        assert payload["status"] == "ok", payload
        record = updates["db_analyses"][0]
        assert record["evidence_id"].startswith("A-")
        assert record["executed_sql"] is None  # 本地计算不伪造 SQL
        assert record["input_result_refs"] == [meta.result_ref]
        assert record["output_result_ref"]
        assert record["pushdown_execution_id"] is None
        assert payload["data"]["evidence_id"] == record["evidence_id"]

    def test_invalid_params_reported(self, node, guard_services, db_agent_context):
        meta = _save_input(guard_services, db_agent_context)
        _u, msg = invoke_tool_node(
            node,
            [
                (
                    "analyze_result",
                    {
                        "operation": "aggregate",
                        "inputs": [meta.result_ref],
                        "params": {"group_by": ["k"]},  # 缺 metrics
                        "allow_pushdown": True,
                    },
                    "tc-a2",
                )
            ],
            db_agent_context,
        )
        payload = json.loads(msg.content)
        assert payload["error"]["error_type"] == "invalid_argument"

    def test_stale_input_ref_returns_error_envelope(
        self, node, guard_services, db_agent_context
    ):
        """失效输入引用返回结构化错误包（Agent 可换引用重试），不抛异常。"""
        _u, msg = invoke_tool_node(
            node,
            [
                (
                    "analyze_result",
                    {
                        "operation": "aggregate",
                        "inputs": ["res_" + "0" * 32],  # 不存在
                        "params": {
                            "group_by": [],
                            "metrics": [{"column": "x", "func": "sum"}],
                        },
                        "allow_pushdown": True,
                    },
                    "tc-a3",
                )
            ],
            db_agent_context,
        )
        payload = json.loads(msg.content)
        assert payload["status"] == "error", payload
        assert payload["error"]["error_type"] == "result_not_found"
        assert payload["error"]["retryable"] is False

    def test_distinct_truncated_flags_persisted(
        self, node, guard_services, db_agent_context
    ):
        """distinct_values 超 max_values：结果文件、分析记录与返回都带截断标记。

        修复前固定 source_truncated=False，后续 count 会把截断清单当完整集合。
        """
        meta = _save_input(guard_services, db_agent_context)
        updates, msg = invoke_tool_node(
            node,
            [
                (
                    "analyze_result",
                    {
                        "operation": "distinct_values",
                        "inputs": [meta.result_ref],
                        "params": {"column": "k", "max_values": 1},
                        "allow_pushdown": True,
                    },
                    "tc-d1",
                )
            ],
            db_agent_context,
        )
        payload = json.loads(msg.content)
        assert payload["status"] == "ok", payload
        assert payload["data"]["source_truncated"] is True
        record = updates["db_analyses"][0]
        assert record["source_truncated"] is True
        saved = guard_services.results.load_meta(
            record["output_result_ref"], session_id=db_agent_context.session_id
        )
        assert saved.source_truncated is True
        assert saved.saved_row_count == 1

    def test_local_output_keeps_input_datasource(
        self, node, guard_services, db_agent_context
    ):
        """分析其他数据源的历史结果：本地输出沿用输入数据源（不改写为当前）。"""
        meta = _save_input(guard_services, db_agent_context, datasource_id="ds-history")
        _updates, msg = invoke_tool_node(
            node,
            [
                (
                    "analyze_result",
                    {
                        "operation": "aggregate",
                        "inputs": [meta.result_ref],
                        "params": {
                            "group_by": [],
                            "metrics": [{"column": "x", "func": "sum"}],
                        },
                        "allow_pushdown": True,
                    },
                    "tc-d2",
                )
            ],
            db_agent_context,
        )
        payload = json.loads(msg.content)
        assert payload["status"] == "ok", payload
        saved = guard_services.results.load_meta(
            payload["data"]["result_ref"], session_id=db_agent_context.session_id
        )
        assert saved.datasource_id == "ds-history"

    def test_aggregate_output_types_preserved(
        self, node, guard_services, db_agent_context
    ):
        """数值分组键输出保留 int4（修复前被标 text）。"""
        meta = _save_input(guard_services, db_agent_context)
        _updates, msg = invoke_tool_node(
            node,
            [
                (
                    "analyze_result",
                    {
                        "operation": "aggregate",
                        "inputs": [meta.result_ref],
                        "params": {
                            "group_by": ["k"],
                            "metrics": [{"column": "x", "func": "sum"}],
                        },
                        "allow_pushdown": True,
                    },
                    "tc-d3",
                )
            ],
            db_agent_context,
        )
        payload = json.loads(msg.content)
        assert payload["status"] == "ok", payload
        saved = guard_services.results.load_meta(
            payload["data"]["result_ref"], session_id=db_agent_context.session_id
        )
        types = {c["name"]: c["pg_type"] for c in saved.columns}
        assert types["k"] == "int4"

    def test_large_preview_not_reduced(self, node, guard_services, db_agent_context):
        """doc 9 §5：真实 analyze_result 大预览不删行/不裁列。

        本地 top_n（int4 排序，40 行 × 700 字符文本列）→ 预览 20 行
        越过旧 12000 预算（旧实现整体截断产出非法 JSON）。账本、结果
        存储与返回引用一致。
        """
        n_rows = 40
        rows = [[i, f"g{i:03d}_" + "k" * 695] for i in range(n_rows)]
        meta = guard_services.results.save(
            ctx=db_agent_context,
            purpose="query",
            sql_template="SELECT rank, payload FROM t",
            executed_sql="SELECT rank, payload FROM t",
            columns=[
                {"name": "rank", "pg_type": "int4"},
                {"name": "payload", "pg_type": "varchar"},
            ],
            rows=rows,
            source_truncated=False,
            derived_from=[],
            guard_evidence={"time": [], "dim": []},
        )
        updates, msg = invoke_tool_node(
            node,
            [
                (
                    "analyze_result",
                    {
                        "operation": "top_n",
                        "inputs": [meta.result_ref],
                        "params": {
                            "order_by": "rank",
                            "descending": False,
                            "n": 40,
                            "ties": "strict",
                        },
                        "allow_pushdown": True,
                    },
                    "tc-d4",
                )
            ],
            db_agent_context,
        )
        payload = _strict_loads(msg.content)  # 旧实现此处 JSONDecodeError
        data = payload["data"]
        assert payload["status"] == "ok", payload
        # 本地计算无下推警告；文本列不可比较 → 仅保留既定的并列位次提示
        assert payload["warnings"] == ["排序键在边界并列，选中记录不确定"]
        preview_rows = data["preview_rows"]
        assert len(preview_rows) == 20  # results.preview_rows 业务限制原样生效
        assert data["preview_truncated"] is True  # 40 > 20：预览不完整标记保留
        assert data["row_count"] == 40
        assert preview_rows[0] == [0, rows[0][1]]  # 长文本列不被改写/裁剪
        assert preview_rows[19] == [19, rows[19][1]]
        assert len(msg.content) > 12000  # 修复后实测长度越过旧预算
        # 账本与结果存储一致；完整 40 行可从 ResultStore 读回
        record = updates["db_analyses"][0]
        assert data["evidence_id"] == record["evidence_id"]
        assert data["result_ref"] == record["output_result_ref"]
        assert record["executed_sql"] is None  # 本地计算不伪造 SQL
        assert record["saved_row_count"] == 40
        page = guard_services.results.read_rows(
            record["output_result_ref"],
            session_id=db_agent_context.session_id,
            offset=20,
            limit=20,
        )
        assert page.rows[0] == rows[20]  # 预览之外的第 21 行仍可读取
