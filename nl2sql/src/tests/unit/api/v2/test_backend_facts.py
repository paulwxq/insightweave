"""DefaultReadinessBackend 三库读取字段的直接单测（doc 12.2 §1.1）。

FakeBackend 用例只覆盖 readiness 判定逻辑；本文件直接驱动真实
DefaultReadinessBackend，断言 Milvus 枚举请求的 output_fields 与
Neo4j Cypher 读取的属性名均为 publish_run_id（doc 12.1 写侧对应字段），
并核对记录值正确进入 MilvusFacts/Neo4jFacts 对应集合——字段名写错
（如回退为 publish_id）时现有 FakeBackend 用例不会失败，此处才会。
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest

from src.api.v2.backend import DefaultReadinessBackend
from src.api.v2.readiness import RegistrationLookup
from src.tests.unit.api.v2.fakes import make_registration

pytestmark = pytest.mark.unit

SOURCE = "db_src_1"
DB = "ordersdb"
RUN_TS = "11111111-2222-4333-8444-555555555555"
RUN_REL = "aaaaaaa1-bbbb-4ccc-8ddd-eeeeeeeeeeee"


class _FakeMilvus:
    """iter_source_records 按 kind 返回预设记录，并捕获请求的输出字段。"""

    def __init__(self, records_by_kind: dict[str, list[dict]]):
        self._records = records_by_kind
        self.requests: list[tuple[str, list[str]]] = []

    def iter_source_records(self, kind: str, output_fields: list[str]):
        self.requests.append((kind, list(output_fields)))
        for record in self._records.get(kind, []):
            yield dict(record)

    def close(self) -> None:
        pass


class _FakeSession:
    """按 Cypher 文本路由返回记录；捕获全部查询文本（neo4j Record ≈ dict.get）。"""

    def __init__(self):
        self.queries: list[str] = []

    def run(self, query: str, **params):
        self.queries.append(query)
        if "MATCH (n:Table)" in query:
            return [
                {
                    "entity_id": f"{SOURCE}:{DB}.public.orders",
                    "id": f"{DB}.public.orders",
                    "publish_run_id": RUN_REL,
                }
            ]
        if "MATCH (n:Column)" in query:
            return [
                {
                    "entity_id": f"{SOURCE}:{DB}.public.orders.id",
                    "id": f"{DB}.public.orders.id",
                    "publish_run_id": RUN_REL,
                }
            ]
        # 先判 type(r)：该查询文本含 IN ['HAS_COLUMN', 'JOIN_ON']，
        # 不能先按 HAS_COLUMN 命中（会错走边端点分支返回无 pid 记录）
        if "type(r)" in query:
            return [{"pid": RUN_REL}]
        if "HAS_COLUMN" in query:
            return [
                {
                    "te": f"{SOURCE}:{DB}.public.orders",
                    "ti": f"{DB}.public.orders",
                    "tsid": SOURCE,
                    "ce": f"{SOURCE}:{DB}.public.orders.id",
                    "ci": f"{DB}.public.orders.id",
                    "csid": SOURCE,
                }
            ]
        raise AssertionError(f"未预期的查询: {query}")


class _FakeManager:
    def __init__(self):
        self.session = _FakeSession()

    @contextmanager
    def get_session(self):
        yield self.session

    def close(self) -> None:
        pass


@pytest.fixture()
def backend(monkeypatch):
    """登记恒 ok 的真实 backend 实例（按用例再注入 fake Milvus/Neo4j）。"""
    real = DefaultReadinessBackend()
    lookup = RegistrationLookup(
        status="ok", registration=make_registration(SOURCE, db_name=DB)
    )
    monkeypatch.setattr(real, "lookup_registration", lambda source_id: lookup)
    return real


class TestLoadMilvusFacts:
    def _install(self, monkeypatch, backend, records) -> _FakeMilvus:
        milvus = _FakeMilvus(records)
        monkeypatch.setattr(backend, "_open_milvus", lambda source_id: milvus)
        return milvus

    def test_output_fields_request_publish_run_id(self, monkeypatch, backend):
        """三集合枚举请求 publish_run_id 输出字段，不请求旧 publish_id。"""
        milvus = self._install(monkeypatch, backend, {})
        backend.load_milvus_facts(SOURCE)
        requested = dict(milvus.requests)
        for kind in ("table_schema", "dim_value", "sql_example"):
            assert "publish_run_id" in requested[kind], (kind, requested[kind])
            assert "publish_id" not in requested[kind], (kind, requested[kind])

    def test_publish_run_ids_fill_facts_sets(self, monkeypatch, backend):
        """记录的 publish_run_id 值进入三个实际版本集合。"""
        self._install(
            monkeypatch,
            backend,
            {
                "table_schema": [
                    {
                        "object_id": f"{DB}.public.orders",
                        "parent_id": f"{DB}.public.orders",
                        "object_type": "table",
                        "publish_run_id": RUN_TS,
                    }
                ],
                "dim_value": [{"col_id": f"{SOURCE}:x", "publish_run_id": RUN_TS}],
                "sql_example": [
                    {"example_id": f"{SOURCE}:e1", "publish_run_id": RUN_TS}
                ],
            },
        )
        facts = backend.load_milvus_facts(SOURCE)
        assert facts.table_publish_run_ids == {RUN_TS}
        assert facts.dim_value_publish_run_ids == {RUN_TS}
        assert facts.sql_example_publish_run_ids == {RUN_TS}
        assert facts.table_keys == {f"{DB}.public.orders"}
        assert facts.dim_value_count == 1
        assert facts.sql_example_count == 1


class TestLoadNeo4jFacts:
    def test_cypher_reads_publish_run_id(self, monkeypatch, backend):
        """节点与关系 Cypher 均读取 publish_run_id，不读旧 publish_id。"""
        import src.modules.db_deepagent.services as services_mod

        manager = _FakeManager()
        monkeypatch.setattr(services_mod, "_neo4j_manager", lambda cfg: manager)
        facts = backend.load_neo4j_facts(SOURCE)
        joined = "\n".join(manager.session.queries)
        assert "n.publish_run_id AS publish_run_id" in joined
        assert "r.publish_run_id AS pid" in joined
        # 旧属性名不得再出现（排除 publish_run_id 的子串匹配）
        assert "publish_id" not in joined.replace("publish_run_id", "")
        assert facts.publish_run_ids == {RUN_REL}
        assert facts.table_keys == {f"{DB}.public.orders"}
        assert facts.column_keys == {(f"{DB}.public.orders", "id")}
        assert facts.has_column_pairs == {(f"{DB}.public.orders", "id")}
        assert facts.legacy_two_segment == 0
        assert facts.unparseable == 0
