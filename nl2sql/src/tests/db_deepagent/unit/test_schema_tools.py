"""检索工具单元测试（docs/v4/5 §7.2–§7.4、§18.2）

覆盖：可见 schema 与严格参数（min_score 拒绝）、服务端固定阈值、维度证据分组
与未命中提示、Command 状态写入、inspect_tables 逐表隔离、plan_joins 三态与
桥接表。
"""

from __future__ import annotations

import json

import pytest
from langgraph.prebuilt import ToolNode

from src.modules.db_deepagent.services import DbAgentServices
from src.modules.db_deepagent.services.join_graph import JoinPathOutcome
from src.modules.db_deepagent.tools import build_business_tools
from src.tests.db_deepagent.conftest import invoke_tool_node

pytestmark = pytest.mark.unit


@pytest.fixture()
def tools(fake_services: DbAgentServices):
    return build_business_tools(fake_services)


@pytest.fixture()
def node(tools):
    return ToolNode(tools)


def _dim_hit(dim_table: str, dim_col: str, matched: str, score: float) -> dict:
    return {
        "query_value": matched,
        "dim_table": dim_table,
        "dim_col": dim_col,
        "matched_text": matched,
        "score": score,
    }


# ============================================================================
# search_schema：参数契约
# ============================================================================


class TestSearchSchemaSchema:
    def test_visible_schema_has_all_business_fields_and_no_min_score(self, tools):
        """模型可见 schema 含全部业务字段；无 runtime / min_score（§7.2）。"""
        schema = tools[0].tool_call_schema.model_json_schema()
        props = schema["properties"]
        assert set(props) == {"query", "kinds", "value_texts", "top_k"}
        assert "min_score" not in props  # 阈值参数不存在（服务端固定）

    def test_top_k_upper_bound_from_config(self, tools, db_agent_config):
        """top_k 上限动态来自 retrieval.max_top_k。"""
        schema = tools[0].tool_call_schema.model_json_schema()
        top_k = json.dumps(schema["properties"]["top_k"])
        assert str(db_agent_config.retrieval.max_top_k) in top_k

    def test_min_score_rejected_by_framework(self, node, db_agent_context):
        """min_score 为未声明字段：extra=forbid → invalid_argument ToolMessage(status=error)。"""
        _, msg = invoke_tool_node(
            node,
            [("search_schema", {"query": "x", "min_score": 0.9}, "c1")],
            db_agent_context,
        )
        assert msg.status == "error"
        assert msg.content.startswith("invalid_argument")
        assert "min_score" in msg.content
        # 不回显输入值
        assert "0.9" not in msg.content

    def test_top_k_over_max_rejected(self, node, db_agent_context, db_agent_config):
        _, msg = invoke_tool_node(
            node,
            [
                (
                    "search_schema",
                    {"query": "x", "top_k": db_agent_config.retrieval.max_top_k + 1},
                    "c1",
                )
            ],
            db_agent_context,
        )
        assert msg.status == "error"
        assert "top_k" in msg.content


# ============================================================================
# search_schema：检索行为
# ============================================================================


class TestSearchSchemaBehavior:
    def test_thresholds_fixed_from_config(
        self, node, db_agent_context, fake_milvus, db_agent_config
    ):
        """阈值全部取服务端配置，且在返回中回显 effective_min_scores。"""
        fake_milvus.table_hits = [
            {
                "object_id": "orders.public.orders",
                "object_type": "table",
                "similarity": 0.9,
                "grain_hint": None,
                "time_col_hint": "",
                "table_category": "订单事实表",
            },
        ]
        updates, msg = invoke_tool_node(
            node, [("search_schema", {"query": "订单"}, "c1")], db_agent_context
        )
        payload = json.loads(msg.content)
        r = db_agent_config.retrieval
        assert payload["data"]["effective_min_scores"] == {
            "tables": r.table_min_score,
            "columns": r.column_min_score,
        }
        assert fake_milvus.calls[0] == ("tables", r.table_top_k, r.table_min_score)
        assert fake_milvus.calls[1] == ("columns", r.column_top_k, r.column_min_score)
        assert "db_dim_evidence" not in updates  # 无 dim 检索不写证据

    def test_table_hits_enriched_with_display_and_category(
        self, node, db_agent_context, fake_milvus, db_agent_config
    ):
        fact_category = db_agent_config.retrieval.table_category_mapping["fact"][0]
        fake_milvus.table_hits = [
            {
                "object_id": "orders.public.orders",
                "object_type": "table",
                "similarity": 0.9,
                "grain_hint": None,
                "time_col_hint": "",
                "table_category": fact_category,
            },
        ]
        _, msg = invoke_tool_node(
            node,
            [("search_schema", {"query": "订单", "kinds": ["tables"]}, "c1")],
            db_agent_context,
        )
        payload = json.loads(msg.content)
        table = payload["data"]["tables"][0]
        assert table["display"] == "public.orders"
        assert table["category_group"] == "fact"  # 映射自 table_category_mapping

    def test_dim_values_grouping_evidence_and_unmatched(
        self, node, db_agent_context, fake_milvus
    ):
        """命中按 (dim_table, dim_col) 分组写 DimEvidence；未命中进 unmatched 并提示。"""
        fake_milvus.dim_hits_by_value = {
            "商品07": [
                _dim_hit("orders.public.products", "product_name", "商品07", 1.0),
                _dim_hit("orders.public.products", "product_name", "商品08", 0.9),
                _dim_hit(
                    "orders.public.categories", "category_name", "商品07近似", 0.8
                ),
            ],
            "编造值zzz": [],
        }
        updates, msg = invoke_tool_node(
            node,
            [
                (
                    "search_schema",
                    {
                        "query": "商品",
                        "kinds": ["dim_values"],
                        "value_texts": ["商品07", "编造值zzz"],
                    },
                    "c1",
                )
            ],
            db_agent_context,
        )
        payload = json.loads(msg.content)
        # 分组：products 一组（合并两个 query 值的命中），categories 一组
        dim_entries = {
            d["dim_table"] + "." + d["dim_col"]: d
            for d in payload["data"]["dim_values"]
        }
        assert dim_entries["public.products.product_name"]["matched_values"] == [
            "商品07",
            "商品08",
        ]
        assert dim_entries["public.products.product_name"]["best_score"] == 1.0
        assert payload["data"]["unmatched_values"] == ["编造值zzz"]
        assert any("probe_dimension_values" in w for w in payload["warnings"])
        # 证据写入：两段表名、来源 milvus_dim_hit、run_id 归属
        evidence = updates["db_dim_evidence"]
        by_table = {e["table"] + "." + e["column"]: e for e in evidence}
        assert by_table["public.products.product_name"]["values"] == [
            "商品07",
            "商品08",
        ]
        assert by_table["public.products.product_name"]["source"] == "milvus_dim_hit"
        assert all(e["run_id"] == "run-test" for e in evidence)
        assert all(e["table"].count(".") == 1 for e in evidence)

    def test_dim_values_without_value_texts_is_invalid_argument(
        self, node, db_agent_context
    ):
        _, msg = invoke_tool_node(
            node,
            [("search_schema", {"query": "x", "kinds": ["dim_values"]}, "c1")],
            db_agent_context,
        )
        payload = json.loads(msg.content)
        assert payload["status"] == "error"
        assert payload["error"]["error_type"] == "invalid_argument"
        assert payload["error"]["retryable"] is True

    def test_sql_examples_truncated_to_limit(self, node, db_agent_context, fake_milvus):
        fake_milvus.sql_hits = [
            {
                "question": "q",
                "sql": "SELECT " + "1" * 2000,
                "similarity": 0.9,
                "example_id": "e1",
                "domain": "d",
            },
        ]
        _, msg = invoke_tool_node(
            node,
            [("search_schema", {"query": "x", "kinds": ["sql_examples"]}, "c1")],
            db_agent_context,
        )
        payload = json.loads(msg.content)
        sql = payload["data"]["sql_examples"][0]["sql"]
        assert len(sql) <= 1500 + len("…(截断)")
        assert sql.endswith("…(截断)")

    def test_all_empty_returns_status_empty(self, node, db_agent_context, fake_milvus):
        _, msg = invoke_tool_node(
            node, [("search_schema", {"query": "x"}, "c1")], db_agent_context
        )
        payload = json.loads(msg.content)
        assert payload["status"] == "empty"
        assert payload["data"]["tables"] == []
        assert payload["data"]["columns"] == []

    def test_large_hits_not_trimmed(self, node, db_agent_context, fake_milvus):
        """doc 9 §4 步骤 1：检索列表大结果不被裁到 5 项。

        9 表 + 9 列命中（各 ~700 字符）越过旧 12000 预算；旧实现把
        tables/columns 各裁到 5 项（JSON 合法但丢检索结果）。修复后
        全部命中与顺序保留。
        """
        fake_milvus.table_hits = [
            {
                "object_id": f"orders.public.t{i:02d}",
                "object_type": "table",
                "similarity": 0.9,
                "grain_hint": "g" * 650,
                "time_col_hint": None,
                "table_category": "",
            }
            for i in range(1, 10)
        ]
        fake_milvus.column_hits = [
            {
                "object_id": f"orders.public.orders.c{i:02d}",
                "parent_id": "orders.public.orders",
                "similarity": 0.9,
                "comment": "c" * 650,
            }
            for i in range(1, 10)
        ]
        _, msg = invoke_tool_node(
            node, [("search_schema", {"query": "大结果"}, "c1")], db_agent_context
        )
        # 数据前置条件（独立于生产代码）：完整命中列表的首次序列化即超旧预算
        assert (
            len(
                json.dumps(
                    fake_milvus.table_hits + fake_milvus.column_hits, ensure_ascii=False
                )
            )
            > 12000
        )
        payload = json.loads(msg.content)
        data = payload["data"]
        assert len(data["tables"]) == 9  # 旧实现此处为 5（合法 JSON 但丢命中）
        assert len(data["columns"]) == 9  # 旧实现此处为 5
        assert [t["display"] for t in data["tables"]] == [
            f"public.t{i:02d}" for i in range(1, 10)
        ]
        assert data["tables"][0]["grain_hint"] == "g" * 650  # 长字段原样保留
        assert payload["warnings"] == []
        assert len(msg.content) > 12000  # 修复后实测长度越过旧预算


# ============================================================================
# inspect_tables（§7.3）
# ============================================================================


class TestInspectTables:
    def test_valid_table_full_description(
        self, node, db_agent_context, fake_milvus, fake_business_db, db_agent_config
    ):
        """好表：卡片 + 实时列合并，display 为两段名。"""
        fake_milvus.cards["orders.public.orders"] = {
            "object_id": "orders.public.orders",
            "text_raw": "# public.orders [table]\n- order_id (integer) - 主键\n- amount (numeric) - 金额",
            "grain_hint": None,
            "time_col_hint": "created_at",
            "table_category": db_agent_config.retrieval.table_category_mapping["fact"][
                0
            ],
        }
        _, msg = invoke_tool_node(
            node,
            [("inspect_tables", {"table_ids": ["public.orders"]}, "c1")],
            db_agent_context,
        )
        payload = json.loads(msg.content)
        entry = payload["data"]["tables"][0]
        assert entry["status"] == "ok"
        assert entry["display"] == "public.orders"
        assert entry["category_group"] == "fact"
        assert entry["time_col_hint"] == "created_at"
        assert [c["name"] for c in entry["live_columns"]] == [
            "order_id",
            "customer_id",
            "amount",
        ]
        # 卡片列 order_id/amount 均在实时列中 → 无差异
        assert entry["card_only_columns"] == []
        assert entry["live_only_columns"] == ["customer_id"]
        assert fake_business_db.queries == [("public", "orders")]

    def test_invalid_table_isolated_per_table(
        self, node, db_agent_context, fake_milvus
    ):
        """非法表逐表报错，不影响其他表。"""
        fake_milvus.cards["orders.public.orders"] = {
            "object_id": "orders.public.orders",
            "text_raw": "",
            "grain_hint": None,
            "time_col_hint": None,
            "table_category": "",
        }
        _, msg = invoke_tool_node(
            node,
            [("inspect_tables", {"table_ids": ["public.orders", "bad-table!"]}, "c1")],
            db_agent_context,
        )
        payload = json.loads(msg.content)
        entries = payload["data"]["tables"]
        assert entries[0]["status"] == "ok"
        assert entries[1]["status"] == "error"
        assert entries[1]["error_type"] == "invalid_table_id"

    def test_no_card_returns_live_only_with_warnings(
        self, node, db_agent_context, fake_milvus
    ):
        _, msg = invoke_tool_node(
            node,
            [("inspect_tables", {"table_ids": ["public.orders"]}, "c1")],
            db_agent_context,
        )
        payload = json.loads(msg.content)
        entry = payload["data"]["tables"][0]
        assert entry["status"] == "ok"
        assert any("无该表卡片" in w for w in entry["warnings"])


def _local_services(db_agent_config, business_db, milvus):
    """带自定义业务库/检索假件的服务容器（doc 7 步骤 1：假件局部放测试内）。"""
    from src.modules.db_deepagent.services import DbAgentServices
    from src.modules.db_deepagent.services.table_catalog import TableCatalog
    from src.tests.db_deepagent.conftest import (
        TEST_SOURCE_ID,
        FakeEmbedding,
        FakeJoinGraph,
    )

    return DbAgentServices(
        config=db_agent_config,
        business_db=business_db,
        embedding=FakeEmbedding(),
        milvus=milvus,
        catalog=TableCatalog(db_agent_config, milvus, business_db),
        join_graph=FakeJoinGraph(),
        embedding_release={},
        source_id=TEST_SOURCE_ID,
    )


def _fake_columns(count: int, name_len: int = 50, marker: str | None = None):
    """生产实际字段形态的实时列：长列名撑出真实序列化体积（doc 7 步骤 4 规模口径）。"""
    from src.tests.db_deepagent.conftest import FakeColumn

    columns = []
    for i in range(count):
        prefix = f"col_{i:03d}_"
        name = prefix + "x" * (name_len - len(prefix))
        columns.append(FakeColumn(name, "varchar", i % 2 == 0))
    if marker is not None:
        columns[-1] = FakeColumn(marker, "varchar", False)
    return columns


def _card(table: str) -> dict:
    return {
        "object_id": f"orders.{table}",
        "text_raw": f"# {table} [table]\n- 说明列",
        "grain_hint": None,
        "time_col_hint": None,
        "table_category": "",
    }


class TestInspectTablesLargeOutput:
    """doc 7 步骤 1：试点工具不再压缩/截断（RED 于旧 finalize_output 行为）。"""

    def test_large_description_keeps_all_columns(
        self, db_agent_config, db_agent_context
    ):
        """单表大量实时列（>12000 字符）：JSON 可解析、全部列与末列标记保留。"""
        from langgraph.prebuilt import ToolNode

        from src.tests.db_deepagent.conftest import FakeBusinessDb, FakeMilvus

        marker = "ZQ_LAST_COLUMN_MARKER_ZQ"
        db = FakeBusinessDb(_fake_columns(120, marker=marker))
        milvus = FakeMilvus()
        milvus.cards["orders.public.orders"] = _card("public.orders")
        node = ToolNode(
            build_business_tools(_local_services(db_agent_config, db, milvus))
        )

        _, msg = invoke_tool_node(
            node,
            [("inspect_tables", {"table_ids": ["public.orders"]}, "c1")],
            db_agent_context,
        )
        payload = json.loads(msg.content)
        entry = payload["data"]["tables"][0]
        assert entry["status"] == "ok"
        names = [c["name"] for c in entry["live_columns"]]
        assert len(names) == 120
        assert marker in names

    def test_six_table_descriptions_not_reduced_to_five(
        self, db_agent_config, db_agent_context
    ):
        """6 个合法表且总输出 >12000 字符：返回全部 6 表、顺序与逐表状态不变。"""
        from langgraph.prebuilt import ToolNode

        from src.tests.db_deepagent.conftest import FakeBusinessDb, FakeMilvus

        tables = [f"public.t{i:02d}" for i in range(1, 7)]
        db = FakeBusinessDb(_fake_columns(30))
        milvus = FakeMilvus()
        for t in tables:
            milvus.cards[f"orders.{t}"] = _card(t)
        node = ToolNode(
            build_business_tools(_local_services(db_agent_config, db, milvus))
        )

        _, msg = invoke_tool_node(
            node,
            [("inspect_tables", {"table_ids": tables}, "c1")],
            db_agent_context,
        )
        payload = json.loads(msg.content)
        entries = payload["data"]["tables"]
        assert len(entries) == 6
        assert [e["display"] for e in entries] == tables
        assert all(e["status"] == "ok" for e in entries)


# ============================================================================
# plan_joins（§7.4）
# ============================================================================


class TestPlanJoins:
    def test_found_path_with_bridge_tables(self, node, db_agent_context, fake_services):
        """找到路径：边转两段名；路径中未经输入的表进 bridge_tables。"""
        fake_services.join_graph.outcomes = {
            ("orders.public.orders", "orders.public.products"): JoinPathOutcome(
                base="orders.public.orders",
                target="orders.public.products",
                status="found",
                strategy="apoc_dijkstra",
                edges=[
                    {
                        "src_table": "orders.public.orders",
                        "dst_table": "orders.public.order_items",
                        "join_type": "INNER JOIN",
                        "cardinality": "1:N",
                        "on": "SRC.id = DST.order_id",
                        "cost": 1.0,
                    },
                    {
                        "src_table": "orders.public.order_items",
                        "dst_table": "orders.public.products",
                        "join_type": "INNER JOIN",
                        "cardinality": "N:1",
                        "on": "SRC.product_id = DST.id",
                        "cost": 1.0,
                    },
                ],
                total_cost=2.0,
            )
        }
        _, msg = invoke_tool_node(
            node,
            [
                (
                    "plan_joins",
                    {
                        "base_table": "public.orders",
                        "target_tables": ["public.products"],
                    },
                    "c1",
                )
            ],
            db_agent_context,
        )
        payload = json.loads(msg.content)
        assert payload["status"] == "ok"
        assert payload["data"]["base"] == "public.orders"
        assert payload["data"]["targets"] == ["public.products"]
        assert payload["data"]["bridge_tables"] == ["public.order_items"]
        plan = payload["data"]["plans"][0]
        assert plan["status"] == "found"
        assert plan["strategy"] == "apoc_dijkstra"
        assert plan["total_cost"] == 2.0
        assert plan["edges"][0]["src_table"] == "public.orders"
        assert plan["edges"][0]["dst_table"] == "public.order_items"
        assert any("桥接表" in w for w in payload["warnings"])

    def test_no_path_reports_message(self, node, db_agent_context, fake_services):
        fake_services.join_graph.outcomes = {
            ("orders.public.orders", "orders.public.users"): JoinPathOutcome(
                base="orders.public.orders",
                target="orders.public.users",
                status="no_path",
            )
        }
        _, msg = invoke_tool_node(
            node,
            [
                (
                    "plan_joins",
                    {"base_table": "public.orders", "target_tables": ["public.users"]},
                    "c1",
                )
            ],
            db_agent_context,
        )
        payload = json.loads(msg.content)
        plan = payload["data"]["plans"][0]
        assert plan["status"] == "no_path"
        assert "没有 JOIN 路径" in plan["message"]
        assert payload["status"] == "ok"  # 单目标无路径不是整体错误

    def test_all_targets_error_yields_status_error(
        self, node, db_agent_context, fake_services
    ):
        fake_services.join_graph.outcomes = {
            ("orders.public.orders", "orders.public.users"): JoinPathOutcome(
                base="orders.public.orders",
                target="orders.public.users",
                status="error",
                strategy="apoc_dijkstra",
                error="Neo4j 超时",
            )
        }
        _, msg = invoke_tool_node(
            node,
            [
                (
                    "plan_joins",
                    {"base_table": "public.orders", "target_tables": ["public.users"]},
                    "c1",
                )
            ],
            db_agent_context,
        )
        payload = json.loads(msg.content)
        assert payload["status"] == "error"
        assert payload["error"]["error_type"] == "graph_backend_error"
        assert payload["data"]["plans"][0]["error_type"] == "graph_backend_error"

    def test_invalid_base_table_is_error_envelope(self, node, db_agent_context):
        _, msg = invoke_tool_node(
            node,
            [
                (
                    "plan_joins",
                    {"base_table": "nope", "target_tables": ["public.users"]},
                    "c1",
                )
            ],
            db_agent_context,
        )
        payload = json.loads(msg.content)
        assert payload["status"] == "error"
        assert payload["error"]["error_type"] == "invalid_table_id"

    def test_max_hops_passed_through(self, node, db_agent_context, fake_services):
        fake_services.join_graph.outcomes = {
            ("orders.public.orders", "orders.public.products"): JoinPathOutcome(
                base="orders.public.orders",
                target="orders.public.products",
                status="no_path",
            )
        }
        invoke_tool_node(
            node,
            [
                (
                    "plan_joins",
                    {
                        "base_table": "public.orders",
                        "target_tables": ["public.products"],
                        "max_hops": 3,
                    },
                    "c1",
                )
            ],
            db_agent_context,
        )
        call = fake_services.join_graph.calls[0]
        assert call["max_hops"] == 3
        assert call["strategy"] == fake_services.config.neo4j.join_strategy
        assert (
            call["query_timeout_s"] == fake_services.config.neo4j.query_timeout_seconds
        )

    def test_all_targets_complete(self, node, db_agent_context, fake_services):
        """doc 9 §5：多目标（found/no_path/error/非法表混合）逐目标完整。

        8 个目标的 plans 顺序与输入一致、逐目标状态正确；整体 status
        不因部分失败变 error；无路径与桥接表 warning 均保留。
        """

        def found(target, *, with_bridge=False):
            edges = [
                {
                    "src_table": "orders.public.orders",
                    "dst_table": (
                        "orders.public.order_items"
                        if with_bridge
                        else f"orders.{target}"
                    ),
                    "join_type": "INNER JOIN",
                    "cardinality": "1:N",
                    "on": "SRC.id = DST.order_id",
                    "cost": 1.0,
                }
            ]
            if with_bridge:
                edges.append(
                    {
                        "src_table": "orders.public.order_items",
                        "dst_table": f"orders.{target}",
                        "join_type": "INNER JOIN",
                        "cardinality": "N:1",
                        "on": "SRC.order_id = DST.id",
                        "cost": 1.0,
                    }
                )
            return JoinPathOutcome(
                base="orders.public.orders",
                target=f"orders.{target}",
                status="found",
                strategy="apoc_dijkstra",
                edges=edges,
                total_cost=float(len(edges)),
            )

        fake_services.join_graph.outcomes = {
            ("orders.public.orders", "orders.public.t01"): found("public.t01"),
            ("orders.public.orders", "orders.public.t02"): found(
                "public.t02", with_bridge=True
            ),
            ("orders.public.orders", "orders.public.t03"): found("public.t03"),
            ("orders.public.orders", "orders.public.u01"): JoinPathOutcome(
                base="orders.public.orders",
                target="orders.public.u01",
                status="no_path",
            ),
            ("orders.public.orders", "orders.public.u02"): JoinPathOutcome(
                base="orders.public.orders",
                target="orders.public.u02",
                status="no_path",
            ),
            ("orders.public.orders", "orders.public.e01"): JoinPathOutcome(
                base="orders.public.orders",
                target="orders.public.e01",
                status="error",
                strategy="apoc_dijkstra",
                error="Neo4j 超时",
            ),
            ("orders.public.orders", "orders.public.e02"): JoinPathOutcome(
                base="orders.public.orders",
                target="orders.public.e02",
                status="error",
                strategy="apoc_dijkstra",
                error="Neo4j 超时",
            ),
        }
        targets = [
            "public.t01",
            "public.u01",
            "bad-table!",
            "public.t02",
            "public.e01",
            "public.t03",
            "public.u02",
            "public.e02",
        ]
        _, msg = invoke_tool_node(
            node,
            [
                (
                    "plan_joins",
                    {"base_table": "public.orders", "target_tables": targets},
                    "c1",
                )
            ],
            db_agent_context,
        )
        payload = json.loads(msg.content)
        assert payload["status"] == "ok"  # 部分失败不是整体错误
        plans = payload["data"]["plans"]
        assert [p["target"] for p in plans] == targets  # 顺序与输入一致、逐目标齐全
        assert [p["status"] for p in plans] == [
            "found",
            "no_path",
            "error",
            "found",
            "error",
            "found",
            "no_path",
            "error",
        ]
        assert len(plans[3]["edges"]) == 2  # 桥接路径两条边完整
        assert plans[3]["edges"][0]["dst_table"] == "public.order_items"
        assert payload["data"]["targets"] == [
            "public.t01",
            "public.t02",
            "public.t03",
        ]
        assert payload["data"]["bridge_tables"] == ["public.order_items"]
        warnings_text = "\n".join(payload["warnings"])
        assert "无路径: public.u01, public.u02" in warnings_text
        assert "桥接表" in warnings_text
