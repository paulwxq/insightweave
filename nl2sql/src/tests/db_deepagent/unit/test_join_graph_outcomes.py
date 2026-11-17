"""JoinGraphService 单元测试（docs/v4/5 §6.6 + doc 4 §4.7）

fake manager 按 Cypher 文本路由返回；覆盖四态（found / no_path /
table_not_in_graph / error）与 APOC 失败回退 shortestPath。

doc 4 改造后追加：
- 存在性检查与两种寻路均按 ``Table {entity_id: $entity_id}`` 定位
  （entity_id = source_id:逻辑id）；
- 双源同名表：其他源存在同逻辑 id 不算存在、不参与寻路；
- 构造缺 source_id / 非法 source_id 拒绝，不回退为按 id 匹配。
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest
from neo4j.exceptions import Neo4jError

from src.modules.db_deepagent.services.join_graph import JoinGraphService

pytestmark = pytest.mark.unit

# doc 5.1 §1 短 ID 夹具（doc 12.2 N-1：合法来源为 db_src_N；UUID 输入非法）
SOURCE_A = "db_src_1"
SOURCE_B = "db_src_2"
UUID_SOURCE = "11111111-1111-4111-8111-111111111111"


class _FakeNode:
    def __init__(self, table_id: str):
        self._id = table_id

    def get(self, key: str):
        return self._id if key in ("id", "name") else None


class _FakeRel:
    def __init__(self, src: str, dst: str, **props):
        self.start_node = _FakeNode(src)
        self.end_node = _FakeNode(dst)
        self._props = props

    def get(self, key: str, default=None):
        return self._props.get(key, default)


class _FakePath:
    def __init__(self, rels: list[_FakeRel]):
        self.relationships = rels


class _FakeResult:
    def __init__(self, record: dict | None):
        self._record = record

    def single(self):
        return self._record


class _FakeSession:
    def __init__(self, state: dict):
        self.state = state
        self.queries: list = []

    def run(self, query, **params):
        self.queries.append((query, params))
        text = query.text
        if "count(t) > 0" in text:
            entity = params["entity_id"]
            return _FakeResult({"exists": entity in self.state.get("tables", set())})
        if "apoc.algo.dijkstra" in text:
            action = self.state.get("dijkstra")
        elif "shortestPath" in text:
            action = self.state.get("shortest")
        else:
            raise AssertionError(f"未预期的查询: {text}")
        if isinstance(action, Exception):
            raise action
        return _FakeResult(action)


class FakeNeo4jManager:
    """按查询路由的 Neo4j 连接管理器假实现

    state:
        tables: 存在的表 **entity_id** 集合（source_id:逻辑id）
        dijkstra / shortest: "record"（默认路径记录）、None（无结果）或待抛异常
    """

    def __init__(self, state: dict | None = None):
        self.state = state or {}
        self.session: _FakeSession | None = None

    @contextmanager
    def get_session(self, database=None):
        self.session = _FakeSession(self.state)
        yield self.session

    def close(self):
        pass


def _orders_products_path() -> _FakePath:
    return _FakePath(
        [
            _FakeRel(
                "orders.public.orders",
                "orders.public.order_items",
                join_type="INNER JOIN",
                cardinality="1:N",
                on_clause="SRC.id = DST.order_id",
                cost=1.5,
            ),
            _FakeRel(
                "orders.public.order_items",
                "orders.public.products",
                join_type="INNER JOIN",
                cardinality="N:1",
                on_clause="SRC.product_id = DST.id",
                cost=1.0,
            ),
        ]
    )


def _service(mgr, source_id: str = SOURCE_A, **kwargs) -> JoinGraphService:
    return JoinGraphService(mgr, source_id=source_id, **kwargs)


class TestFourOutcomes:
    def test_found_via_apoc_dijkstra(self):
        mgr = FakeNeo4jManager(
            {
                "tables": {
                    f"{SOURCE_A}:orders.public.orders",
                    f"{SOURCE_A}:orders.public.products",
                },
                "dijkstra": {"path": _orders_products_path(), "weight": 2.5},
            }
        )
        outcome = _service(mgr).find_join_path(
            "orders.public.orders", "orders.public.products"
        )
        assert outcome.status == "found"
        assert outcome.strategy == "apoc_dijkstra"
        assert outcome.fallback_used is False
        assert outcome.total_cost == 2.5
        assert [e["src_table"] for e in outcome.edges] == [
            "orders.public.orders",
            "orders.public.order_items",
        ]
        assert outcome.edges[0]["on"] == "SRC.id = DST.order_id"
        assert outcome.edges[0]["cost"] == 1.5

    def test_no_path_when_query_returns_nothing(self):
        mgr = FakeNeo4jManager(
            {"tables": {f"{SOURCE_A}:a.x", f"{SOURCE_A}:a.y"}, "dijkstra": None}
        )
        outcome = _service(mgr).find_join_path("a.x", "a.y")
        assert outcome.status == "no_path"
        assert outcome.edges == []

    def test_table_not_in_graph(self):
        mgr = FakeNeo4jManager({"tables": {f"{SOURCE_A}:a.x"}})
        outcome = _service(mgr).find_join_path("a.x", "a.missing")
        assert outcome.status == "table_not_in_graph"
        assert "a.missing" in outcome.error

    def test_error_when_both_queries_fail(self):
        mgr = FakeNeo4jManager(
            {
                "tables": {f"{SOURCE_A}:a.x", f"{SOURCE_A}:a.y"},
                "dijkstra": Neo4jError("apoc 不可用"),
                "shortest": Neo4jError("超时"),
            }
        )
        outcome = _service(mgr).find_join_path("a.x", "a.y")
        assert outcome.status == "error"
        assert outcome.fallback_used is True  # 曾尝试回退
        assert outcome.strategy == "shortest_path"


class TestFallback:
    def test_dijkstra_error_falls_back_to_shortest_path(self):
        mgr = FakeNeo4jManager(
            {
                "tables": {f"{SOURCE_A}:a.x", f"{SOURCE_A}:a.y"},
                "dijkstra": Neo4jError("apoc 不可用"),
                "shortest": {"path": _FakePath([_FakeRel("a.x", "a.y", cost=1.0)])},
            }
        )
        outcome = _service(mgr).find_join_path("a.x", "a.y")
        assert outcome.status == "found"
        assert outcome.strategy == "shortest_path"
        assert outcome.fallback_used is True
        assert outcome.total_cost == 1.0  # 无 weight 时按边 cost 求和

    def test_fallback_disabled_keeps_error(self):
        mgr = FakeNeo4jManager(
            {
                "tables": {f"{SOURCE_A}:a.x", f"{SOURCE_A}:a.y"},
                "dijkstra": Neo4jError("apoc 不可用"),
            }
        )
        outcome = _service(mgr).find_join_path(
            "a.x", "a.y", allow_shortest_path_fallback=False
        )
        assert outcome.status == "error"
        assert outcome.strategy == "apoc_dijkstra"
        assert outcome.fallback_used is False

    def test_strategy_shortest_path_direct(self):
        mgr = FakeNeo4jManager(
            {
                "tables": {f"{SOURCE_A}:a.x", f"{SOURCE_A}:a.y"},
                "shortest": {"path": _FakePath([_FakeRel("a.x", "a.y")])},
            }
        )
        outcome = _service(mgr).find_join_path("a.x", "a.y", strategy="shortest_path")
        assert outcome.status == "found"
        assert outcome.strategy == "shortest_path"
        assert outcome.fallback_used is False


class TestQueryExecution:
    def test_queries_carry_timeout(self):
        mgr = FakeNeo4jManager(
            {"tables": {f"{SOURCE_A}:a.x", f"{SOURCE_A}:a.y"}, "dijkstra": None}
        )
        _service(mgr).find_join_path(
            "a.x", "a.y", query_timeout_s=2.5, allow_shortest_path_fallback=False
        )
        timeouts = {q.timeout for q, _ in mgr.session.queries}  # type: ignore[union-attr]
        assert timeouts == {2.5}

    def test_hops_default_from_service(self):
        mgr = FakeNeo4jManager(
            {
                "tables": {f"{SOURCE_A}:a.x", f"{SOURCE_A}:a.y"},
                "dijkstra": Neo4jError("boom"),
            }
        )
        _service(mgr, join_max_hops=3).find_join_path("a.x", "a.y")
        shortest = [q for q, _ in mgr.session.queries if "shortestPath" in q.text]  # type: ignore[union-attr]
        assert "..3]" in shortest[0].text

    def test_queries_match_on_entity_id_not_logical_id(self):
        """doc 4 §4.7：存在性/寻路查询按 entity_id 定位，不按裸逻辑 id。"""
        mgr = FakeNeo4jManager({"tables": {f"{SOURCE_A}:a.x"}, "dijkstra": None})
        _service(mgr).find_join_path("a.x", "a.x", allow_shortest_path_fallback=False)
        for query, params in mgr.session.queries:  # type: ignore[union-attr]
            text = query.text
            assert "{id:" not in text
            if "count(t) > 0" in text:
                assert "{entity_id: $entity_id}" in text
                assert params["entity_id"] == f"{SOURCE_A}:a.x"
            else:  # 寻路查询：两端均按 entity_id 参数绑定
                assert "{entity_id: $base}" in text or "src:Table {entity_id:" in text
                assert params["base"] == f"{SOURCE_A}:a.x"
                assert params["target"] == f"{SOURCE_A}:a.x"


class TestSourceScoping:
    def test_same_name_table_in_other_source_not_visible(self):
        """B 源存在同逻辑 id 的表时，A 会话存在性检查返回 False（doc 4 §4.7）。"""
        mgr = FakeNeo4jManager({"tables": {f"{SOURCE_B}:orders.public.orders"}})
        outcome = _service(mgr).find_join_path(
            "orders.public.orders", "orders.public.orders"
        )
        assert outcome.status == "table_not_in_graph"

    def test_path_from_other_source_not_returned(self):
        """当前源无路径时不返回其他源内部的路径（同键不同源不匹配）。"""
        mgr = FakeNeo4jManager(
            {
                "tables": {f"{SOURCE_A}:a.x", f"{SOURCE_A}:a.y"},
                # 只有 B 源的两表之间存在路径记录
                "dijkstra": {
                    "path": _FakePath([_FakeRel("a.x", "a.y")]),
                    "weight": 1.0,
                },
            }
        )
        outcome = _service(mgr).find_join_path("a.x", "a.y")
        assert outcome.status == "found"
        # 匹配按 A 源 entity_id 发出（B 源节点不会被 MATCH 到——真实图中
        # 该查询返回空；fake 直接验证参数绑定正确即可）
        dijkstra_calls = [
            (q, p)
            for q, p in mgr.session.queries  # type: ignore[union-attr]
            if "dijkstra" in q.text
        ]
        assert dijkstra_calls
        for _q, params in dijkstra_calls:
            assert params["base"] == f"{SOURCE_A}:a.x"
            assert params["target"] == f"{SOURCE_A}:a.y"

    def test_missing_source_id_rejected(self):
        mgr = FakeNeo4jManager({"tables": set()})
        with pytest.raises(ValueError, match="source_id"):
            JoinGraphService(mgr)

    def test_invalid_source_id_rejected(self):
        mgr = FakeNeo4jManager({"tables": set()})
        with pytest.raises(ValueError, match="短 ID"):
            JoinGraphService(mgr, source_id="not-a-short-id")

    def test_uuid_source_id_rejected(self):
        """doc 12.2 N-1：UUID 属非法来源（合法身份为 db_src_N 短 ID）。"""
        mgr = FakeNeo4jManager({"tables": set()})
        with pytest.raises(ValueError, match="短 ID"):
            JoinGraphService(mgr, source_id=UUID_SOURCE)
