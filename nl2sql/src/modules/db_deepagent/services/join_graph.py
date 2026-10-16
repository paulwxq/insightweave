"""JoinGraphService：Neo4j JOIN 路径服务（docs/v4/5 §6.6）

复制 src/services/db/neo4j_client.py 的 find_join_path、_find_path_apoc_dijkstra、
_find_path_shortest、_extract_path_info、check_table_exists，改为：
- 注入显式配置的 Neo4jConnectionManager 实例（不使用 get_neo4j_manager() 单例）
- 每条 Cypher 以 neo4j.Query(text, timeout=...) 执行，超时按 error 处理
- 返回 JoinPathOutcome（三态区分：找到 / 无路径 / 失败）及所用策略
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Optional

from neo4j import Query
from neo4j.exceptions import Neo4jError
from src.services.db.neo4j_connection import Neo4jConnectionManager
from src.utils.logger import get_module_logger

logger = get_module_logger("nl2sql.db_deepagent.join_graph")


@dataclass
class JoinPathOutcome:
    """JOIN 路径查找结果（§6.6：status 区分无路径与失败）"""

    base: str
    target: str
    status: Literal["found", "no_path", "table_not_in_graph", "error"]
    strategy: Optional[Literal["apoc_dijkstra", "shortest_path"]] = None
    fallback_used: bool = False
    edges: list[dict] = field(default_factory=list)
    total_cost: Optional[float] = None
    error: Optional[str] = None


class JoinGraphService:
    """Neo4j JOIN 路径检索（显式配置，超时受控）"""

    def __init__(self, manager: Neo4jConnectionManager, *, join_max_hops: int = 5):
        self._manager = manager
        self._max_hops = join_max_hops

    def close(self) -> None:
        self._manager.close()

    def find_join_path(
        self,
        base_table: str,
        target_table: str,
        max_hops: Optional[int] = None,
        strategy: Literal["apoc_dijkstra", "shortest_path"] = "apoc_dijkstra",
        allow_shortest_path_fallback: bool = True,
        query_timeout_s: float = 15.0,
    ) -> JoinPathOutcome:
        """查找两表间 JOIN 路径（§6.6 判定规则）。

        先 check_table_exists 两端（缺失即 table_not_in_graph）；APOC 抛 Neo4jError
        时按 allow_shortest_path_fallback 决定回退（fallback_used=True）；
        shortestPath 抛错为 error；两种查询都正常但无结果为 no_path。
        """
        hops = max_hops or self._max_hops
        for table in (base_table, target_table):
            if not self.check_table_exists(table):
                return JoinPathOutcome(
                    base=base_table,
                    target=target_table,
                    status="table_not_in_graph",
                    error=f"表 {table!r} 不在 JOIN 图中",
                )

        if strategy == "apoc_dijkstra":
            outcome = self._find_path_apoc_dijkstra(
                base_table, target_table, query_timeout_s
            )
            if outcome.status == "found":
                return outcome
            if outcome.status == "error" and allow_shortest_path_fallback:
                logger.warning(
                    "APOC Dijkstra 失败（%s），回退 shortestPath: %s → %s",
                    outcome.error,
                    base_table,
                    target_table,
                )
                fallback = self._find_path_shortest(
                    base_table, target_table, hops, query_timeout_s
                )
                fallback.fallback_used = True
                return fallback
            return outcome
        return self._find_path_shortest(base_table, target_table, hops, query_timeout_s)

    # ---------------------------------------------------------------- 内部

    def _run(self, text: str, query_timeout_s: float, **params) -> Any:
        """单条 Cypher 执行（带超时）。"""
        with self._manager.get_session() as session:
            return session.run(Query(text, timeout=query_timeout_s), **params).single()

    def _find_path_apoc_dijkstra(
        self, base: str, target: str, query_timeout_s: float
    ) -> JoinPathOutcome:
        query = """
            MATCH (src:Table {id: $base}), (dst:Table {id: $target})
            CALL apoc.algo.dijkstra(src, dst, 'JOIN_ON', 'cost', 1.0)
            YIELD path, weight
            RETURN path, weight
            ORDER BY weight ASC
            LIMIT 1
        """
        try:
            record = self._run(query, query_timeout_s, base=base, target=target)
            if record and record.get("path"):
                return self._extract_path_info(
                    base,
                    target,
                    record["path"],
                    float(record["weight"]),
                    strategy="apoc_dijkstra",
                    status="found",
                )
            return JoinPathOutcome(base=base, target=target, status="no_path")
        except Neo4jError as exc:
            logger.warning("APOC Dijkstra 执行失败: %s", exc)
            return JoinPathOutcome(
                base=base,
                target=target,
                status="error",
                strategy="apoc_dijkstra",
                error=str(exc),
            )

    def _find_path_shortest(
        self, base: str, target: str, max_hops: int, query_timeout_s: float
    ) -> JoinPathOutcome:
        query = f"""
            MATCH path = shortestPath(
                (src:Table {{id: $base}})-[:JOIN_ON*..{max_hops}]-(dst:Table {{id: $target}})
            )
            RETURN path
            LIMIT 1
        """
        try:
            record = self._run(query, query_timeout_s, base=base, target=target)
            if record and record.get("path"):
                return self._extract_path_info(
                    base,
                    target,
                    record["path"],
                    strategy="shortest_path",
                    status="found",
                )
            return JoinPathOutcome(base=base, target=target, status="no_path")
        except Neo4jError as exc:
            logger.error("shortestPath 执行失败: %s", exc)
            return JoinPathOutcome(
                base=base,
                target=target,
                status="error",
                strategy="shortest_path",
                error=str(exc),
            )

    def _extract_path_info(
        self,
        base: str,
        target: str,
        path,
        total_cost: Optional[float] = None,
        *,
        strategy: str,
        status: str,
    ) -> JoinPathOutcome:
        """从 Neo4j Path 对象提取边信息（沿用旧实现字段）。"""
        edges = []
        for relationship in path.relationships:
            src_table = relationship.start_node.get(
                "id"
            ) or relationship.start_node.get("name")
            dst_table = relationship.end_node.get("id") or relationship.end_node.get(
                "name"
            )
            edges.append(
                {
                    "src_table": src_table,
                    "dst_table": dst_table,
                    "constraint_name": relationship.get("constraint_name"),
                    "join_type": relationship.get("join_type", "INNER JOIN"),
                    "cardinality": relationship.get("cardinality"),
                    "on": relationship.get("on_clause") or relationship.get("on"),
                    "cost": relationship.get("cost", 1.0),
                }
            )
        if total_cost is None:
            total_cost = float(sum(edge.get("cost", 1.0) for edge in edges))
        return JoinPathOutcome(
            base=base,
            target=target,
            status=status,  # type: ignore[arg-type]
            strategy=strategy,  # type: ignore[arg-type]
            edges=edges,
            total_cost=total_cost,
        )

    def check_table_exists(self, table_id: str) -> bool:
        """检查表节点是否存在（Table 节点 id 为三段表键）。"""
        query = "MATCH (t:Table {id: $table_id}) RETURN count(t) > 0 AS exists"
        record = self._run(query, 15.0, table_id=table_id)
        return bool(record["exists"]) if record else False
