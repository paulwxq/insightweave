"""readiness 严格键、指纹、快照与完整检查判定（doc 2 §5.5）。"""

from __future__ import annotations

import pytest

from src.api.v2.readiness import (
    ReadinessService,
    source_fingerprint,
    strict_column_key,
    strict_table_key,
)
from src.tests.unit.api.v2.fakes import FakeBackend, make_registration

pytestmark = pytest.mark.unit


class TestStrictKeys:
    def test_two_segment_completion(self):
        assert strict_table_key("public.orders", "ordersdb") == "ordersdb.public.orders"

    def test_three_segment_passthrough(self):
        assert (
            strict_table_key("ordersdb.public.orders", "ordersdb")
            == "ordersdb.public.orders"
        )

    def test_three_segment_wrong_db_rejected(self):
        assert strict_table_key("other.public.orders", "ordersdb") is None

    def test_no_case_folding_no_strip(self):
        # 大小写不折叠：作为独立键保留，而非拒绝
        assert strict_table_key("Public.Orders", "ordersdb") == "ordersdb.Public.Orders"
        assert strict_table_key(" public.orders", "ordersdb") is None

    def test_invalid_identifiers_rejected(self):
        assert strict_table_key("pub lic.orders", "ordersdb") is None
        assert strict_table_key("1abc.orders", "ordersdb") is None
        assert strict_table_key("a.b.c.d", "ordersdb") is None

    def test_db_prefix_with_internal_space_allowed(self):
        assert strict_table_key("public.orders", "my db") == "my db.public.orders"

    def test_column_key(self):
        assert strict_column_key("public.orders.id", "ordersdb") == (
            "ordersdb.public.orders",
            "id",
        )
        assert strict_column_key("ordersdb.public.o.id", "ordersdb") == (
            "ordersdb.public.o",
            "id",
        )
        assert strict_column_key("public.orders.1x", "ordersdb") is None
        assert strict_column_key("orders", "ordersdb") is None


class TestFingerprint:
    def test_stable_and_sensitive(self):
        base = source_fingerprint(make_registration())
        assert base == source_fingerprint(make_registration())
        changed = make_registration()
        object.__setattr__(changed, "port", 5433)
        assert source_fingerprint(changed) != base

    def test_sslmode_normalized(self):
        r1 = make_registration()
        object.__setattr__(r1, "sslmode", "PREFER ")
        r2 = make_registration()
        object.__setattr__(r2, "sslmode", "prefer")
        assert source_fingerprint(r1) == source_fingerprint(r2)

    def test_schema_order_matters(self):
        r1 = make_registration()
        object.__setattr__(r1, "selected_schemas", ("a", "b"))
        r2 = make_registration()
        object.__setattr__(r2, "selected_schemas", ("b", "a"))
        assert source_fingerprint(r1) != source_fingerprint(r2)

    def test_display_fields_not_included(self):
        r1 = make_registration()
        r2 = make_registration()
        object.__setattr__(r2, "db_name", "otherdb")
        assert source_fingerprint(r1) != source_fingerprint(r2)


class TestFullCheck:
    def _service(self, backend):
        return ReadinessService(backend)

    def test_ready_outcome_snapshot(self):
        svc = self._service(FakeBackend())
        outcome = svc.full_check("11111111-1111-1111-1111-111111111111")
        assert outcome.ready and outcome.compatible is True
        assert outcome.fingerprint
        snap = outcome.knowledge_snapshot
        assert snap["table_schema"] == ["job-1", "pub-ts-1"]
        assert snap["rel"] == ["job-1", "pub-rel-1"]
        names = [c["name"] for c in outcome.checks]
        assert {
            "registry",
            "business_db.target",
            "publish_records",
            "table_schema",
            "rel_graph",
            "knowledge_consistency",
            "embedding",
        } <= set(names)

    def test_full_check_cached_30s(self):
        backend = FakeBackend()
        milvus_calls = {"n": 0}
        orig = backend.load_milvus_facts

        def counting(source_id):
            milvus_calls["n"] += 1
            return orig(source_id)

        backend.load_milvus_facts = counting
        svc = self._service(backend)
        svc.full_check("11111111-1111-1111-1111-111111111111")
        svc.full_check("11111111-1111-1111-1111-111111111111")
        # 命中缓存：Milvus/Neo4j/embedding 等重检查不重跑；键复核只读
        # 登记与发布记录两条轻查询（doc 2 §5.5.2）
        assert milvus_calls["n"] == 1

    def test_cache_hit_rechecked_against_registration(self):
        backend = FakeBackend()
        svc = self._service(backend)
        first = svc.full_check("11111111-1111-1111-1111-111111111111")
        assert first.ready
        backend.lookup_status = "inactive"  # 登记变化 = 缓存键变化
        second = svc.full_check("11111111-1111-1111-1111-111111111111")
        assert second is not first and not second.ready

    def test_cache_hit_rechecked_against_publish_version(self):
        backend = FakeBackend()
        svc = self._service(backend)
        first = svc.full_check("11111111-1111-1111-1111-111111111111")
        assert first.ready
        backend.job_id = "job-new"  # current 发布版本变化 = 缓存键变化
        second = svc.full_check("11111111-1111-1111-1111-111111111111")
        assert second is not first and second.ready
        assert svc.full_check("11111111-1111-1111-1111-111111111111") is second

    def test_failure_not_cached(self):
        backend = FakeBackend()
        backend.business_fail = True
        svc = self._service(backend)
        o1 = svc.full_check("11111111-1111-1111-1111-111111111111")
        assert not o1.ready
        backend.business_fail = False
        o2 = svc.full_check("11111111-1111-1111-1111-111111111111")
        assert o2.ready

    def test_not_found(self):
        backend = FakeBackend()
        backend.lookup_status = "not_found"
        outcome = self._service(backend).full_check("x")
        assert outcome.terminal_error_code == "source_not_found"
        assert not outcome.ready and outcome.compatible is None

    def test_inactive(self):
        backend = FakeBackend()
        backend.lookup_status = "inactive"
        outcome = self._service(backend).full_check("x")
        assert outcome.terminal_error_code == "source_inactive"

    def test_unsupported_compatible_false(self):
        backend = FakeBackend()
        backend.lookup_status = "unsupported"
        outcome = self._service(backend).full_check("x")
        assert outcome.compatible is False
        assert outcome.terminal_error_code == "source_unsupported"

    def test_business_fail_blocks_ready(self):
        backend = FakeBackend()
        backend.business_fail = True
        outcome = self._service(backend).full_check(
            "11111111-1111-1111-1111-111111111111"
        )
        assert not outcome.ready
        assert outcome.terminal_error_code == "business_target_mismatch"

    def test_missing_rel_current(self):
        backend = FakeBackend()
        backend.publish_id_rel = None
        rows = [r for r in backend._rows() if r["step_name"] != "rel"]
        backend._rows = lambda: rows
        outcome = self._service(backend).full_check(
            "11111111-1111-1111-1111-111111111111"
        )
        assert outcome.terminal_error_code == "knowledge_missing"

    def test_job_id_mismatch(self):
        backend = FakeBackend()
        backend.job_id = "job-2"
        rows = []
        for r in backend._rows():
            r = dict(r)
            if r["step_name"] == "rel":
                r["job_id"] = "job-1"
            rows.append(r)
        backend._rows = lambda: rows
        outcome = self._service(backend).full_check(
            "11111111-1111-1111-1111-111111111111"
        )
        assert outcome.terminal_error_code == "knowledge_inconsistent"

    def test_milvus_empty_tables(self):
        backend = FakeBackend()
        backend.milvus.table_keys = set()
        backend.milvus.column_keys = set()
        backend.milvus.table_publish_ids = set()
        outcome = self._service(backend).full_check(
            "11111111-1111-1111-1111-111111111111"
        )
        assert outcome.terminal_error_code == "knowledge_missing"

    def test_cross_store_table_mismatch(self):
        backend = FakeBackend()
        backend.neo4j.table_keys = {
            "ordersdb.public.orders",
            "ordersdb.public.users",
        }
        outcome = self._service(backend).full_check(
            "11111111-1111-1111-1111-111111111111"
        )
        assert outcome.terminal_error_code == "knowledge_inconsistent"

    def test_milvus_column_not_in_graph(self):
        backend = FakeBackend()
        backend.milvus.column_keys = {
            ("ordersdb.public.orders", "id"),
            ("ordersdb.public.orders", "extra"),
        }
        outcome = self._service(backend).full_check(
            "11111111-1111-1111-1111-111111111111"
        )
        assert outcome.terminal_error_code == "knowledge_inconsistent"

    def test_publish_id_residual_rejected(self):
        backend = FakeBackend()
        backend.milvus.table_publish_ids = {"pub-ts-1", "pub-old"}
        outcome = self._service(backend).full_check(
            "11111111-1111-1111-1111-111111111111"
        )
        assert outcome.terminal_error_code == "knowledge_inconsistent"

    def test_neo4j_legacy_two_segment(self):
        backend = FakeBackend()
        backend.neo4j.legacy_two_segment = 3
        outcome = self._service(backend).full_check(
            "11111111-1111-1111-1111-111111111111"
        )
        assert outcome.terminal_error_code == "knowledge_inconsistent"

    def test_registration_scope_change_blocks_ready(self):
        # 登记范围改为 sales 而知识仍是 public.orders：不得就绪
        backend = FakeBackend()
        object.__setattr__(backend.registration, "selected_schemas", ("sales",))
        outcome = self._service(backend).full_check(
            "11111111-1111-1111-1111-111111111111"
        )
        assert not outcome.ready
        # error_code 用闭集内 knowledge_inconsistent；检查名承载范围语义
        assert outcome.terminal_error_code == "knowledge_inconsistent"
        scope = [c for c in outcome.checks if c["name"] == "knowledge_scope"]
        assert scope and scope[0]["state"] == "fail"
        assert scope[0]["error_code"] == "knowledge_inconsistent"
        assert scope[0]["counts"] == {"out_of_scope_schemas": 1}

    def test_milvus_parent_mismatch_blocks_ready(self):
        backend = FakeBackend()
        backend.milvus.column_parent_mismatch = 1
        outcome = self._service(backend).full_check(
            "11111111-1111-1111-1111-111111111111"
        )
        assert not outcome.ready
        assert outcome.terminal_error_code == "knowledge_inconsistent"

    def test_missing_has_column_edges_blocks_ready(self):
        # 两边都有列但图中没有有效 Table→Column 边：不得就绪（doc 2 §5.5）
        backend = FakeBackend()
        backend.neo4j.has_column_pairs = set()
        backend.neo4j.has_column_count = 0
        outcome = self._service(backend).full_check(
            "11111111-1111-1111-1111-111111111111"
        )
        assert not outcome.ready
        assert outcome.terminal_error_code == "knowledge_inconsistent"
        failed = [
            c
            for c in outcome.checks
            if c["state"] == "fail" and "Table→Column" in c["message"]
        ]
        assert failed and failed[0]["counts"]["missing_edges"] == 1

    def test_invalid_edge_endpoints_block_ready(self):
        # 边存在但端点归属无效（表键与列键父表不符）
        backend = FakeBackend()
        backend.neo4j.has_column_invalid = 2
        backend.neo4j.has_column_count = 3
        outcome = self._service(backend).full_check(
            "11111111-1111-1111-1111-111111111111"
        )
        assert not outcome.ready
        assert outcome.terminal_error_code == "knowledge_inconsistent"

    def test_optional_empty_warn_not_fail(self):
        backend = FakeBackend()
        outcome = self._service(backend).full_check(
            "11111111-1111-1111-1111-111111111111"
        )
        dims = [c for c in outcome.checks if c["name"] == "dim_value"]
        assert dims and dims[0]["state"] == "warn"
        assert dims[0]["error_code"] == "optional_knowledge_empty"

    def test_optional_residual_blocks_ready(self):
        backend = FakeBackend()
        backend.milvus.dim_value_count = 2
        backend.milvus.dim_value_publish_ids = {"pub-dim-old"}
        outcome = self._service(backend).full_check(
            "11111111-1111-1111-1111-111111111111"
        )
        dims = [c for c in outcome.checks if c["name"] == "dim_value"]
        assert dims and dims[0]["state"] == "fail"
        assert dims[0]["required"] is False
        assert not outcome.ready  # 残留/混批阻止使用（doc 2 §5.5）

    def test_optional_data_without_publish_ids_fails(self):
        backend = FakeBackend()
        backend.milvus.dim_value_count = 2
        backend.milvus.dim_value_publish_ids = set()  # 有数据但缺 publish_id
        outcome = self._service(backend).full_check(
            "11111111-1111-1111-1111-111111111111"
        )
        dims = [c for c in outcome.checks if c["name"] == "dim_value"]
        assert dims and dims[0]["state"] == "fail"
        assert dims[0]["error_code"] == "knowledge_inconsistent"
        assert not outcome.ready

    def test_embedding_failure_dependency(self):
        backend = FakeBackend()
        backend.embedding_fail = True
        outcome = self._service(backend).full_check(
            "11111111-1111-1111-1111-111111111111"
        )
        assert not outcome.ready
        assert outcome.terminal_error_code == "dependency_unavailable"

    def test_embedding_probe_cached_independently(self):
        backend = FakeBackend()
        calls = {"n": 0}
        orig = backend.embedding_probe

        def counting(source_id):
            calls["n"] += 1
            orig(source_id)

        backend.embedding_probe = counting
        # full_ttl=0 让两次完整检查都重跑，探针缓存仍应把后端调用压到一次
        svc = ReadinessService(backend, full_ttl=0.0)
        svc.full_check("11111111-1111-1111-1111-111111111111")
        svc.full_check("11111111-1111-1111-1111-111111111111")
        assert calls["n"] == 1

    def test_probe_cache_invalidated_by_publish_change(self):
        # 发布版本变化后探针键随之变化：300 秒窗口内的旧通过结果不沿用
        backend = FakeBackend()
        svc = self._service(backend)
        assert svc.full_check("11111111-1111-1111-1111-111111111111").ready
        backend.embedding_fail = True
        backend.job_id = "job-new"  # current 发布版本变化
        outcome = svc.full_check("11111111-1111-1111-1111-111111111111")
        assert not outcome.ready
        assert outcome.terminal_error_code == "dependency_unavailable"


class TestEdgeEndpointKeys:
    """HAS_COLUMN 端点键解析（backend._edge_endpoint_keys）：跨源端点
    不回退业务 id，守住源隔离边界（doc 2 §5.5）。"""

    SRC = "11111111-1111-1111-1111-111111111111"
    OTHER = "22222222-2222-2222-2222-222222222222"

    def _resolve(self, *, tsid=SRC, csid=SRC, te=None, ti=None, ce=None, ci=None):
        from src.api.v2.backend import _edge_endpoint_keys

        return _edge_endpoint_keys(
            te if te is not None else f"{self.SRC}:public.orders",
            ti if ti is not None else "public.orders",
            tsid,
            ce if ce is not None else f"{self.SRC}:public.orders.id",
            ci if ci is not None else "public.orders.id",
            csid,
            self.SRC,
            "ordersdb",
        )

    def test_same_source_valid_pair(self):
        assert self._resolve() == (
            "ordersdb.public.orders",
            ("ordersdb.public.orders", "id"),
        )

    def test_legacy_fallback_within_source(self):
        # 同源节点缺 entity_id 前缀：回退业务 id（旧格式边仍可核对）
        assert self._resolve(te="", ce="") == (
            "ordersdb.public.orders",
            ("ordersdb.public.orders", "id"),
        )

    def test_cross_source_table_rejected(self):
        assert self._resolve(tsid=self.OTHER) is None

    def test_cross_source_column_rejected(self):
        assert self._resolve(csid=self.OTHER) is None

    def test_parent_mismatch_rejected(self):
        assert (
            self._resolve(ce=f"{self.SRC}:public.users.id", ci="public.users.id")
            is None
        )


class TestLightRecheck:
    def test_ok_returns_fingerprint_snapshot(self):
        backend = FakeBackend()
        lookup, fingerprint, snapshot = ReadinessService(backend).light_recheck(
            "11111111-1111-1111-1111-111111111111"
        )
        assert lookup.status == "ok"
        assert fingerprint == source_fingerprint(backend.registration)
        assert snapshot["rel"] == ["job-1", "pub-rel-1"]
        assert "backend_summary" not in snapshot  # 快照对只含版本对

    def test_not_ok_empty_values(self):
        backend = FakeBackend()
        backend.lookup_status = "inactive"
        lookup, fingerprint, snapshot = ReadinessService(backend).light_recheck("x")
        assert lookup.status == "inactive"
        assert fingerprint == "" and snapshot == {}
