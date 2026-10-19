"""v2 Web API 单测共用的 fake 基础设施。

FakeStore 实现 BaseStore 的 get/put/delete/search 子集；FakeBackend 按
readiness Protocol 提供可控结果；make_result 构造合法 DbAgentResult。
所有 fake 不连任何真实依赖。
"""

from __future__ import annotations

from typing import Any, Optional

from src.modules.db_deepagent.schemas import (
    DbAgentResult,
    EvidenceItem,
    RunStats,
)
from src.modules.db_deepagent.source_registry import SourceRegistration


class FakeItem:
    def __init__(self, namespace, key, value):
        self.namespace = tuple(namespace)
        self.key = key
        self.value = dict(value)


class FakeStore:
    """内存 Store：search 按 key 升序返回，模拟 PostgresStore 行为子集。"""

    def __init__(self):
        self._data: dict[tuple[str, ...], dict[str, dict]] = {}
        self.fail_writes = False
        self.fail_reads = False

    def _bucket(self, namespace):
        return self._data.setdefault(tuple(namespace), {})

    def get(self, namespace, key):
        if self.fail_reads:
            raise RuntimeError("store read failure (fake)")
        return (
            FakeItem(namespace, key, self._bucket(namespace)[key])
            if key in self._bucket(namespace)
            else None
        )

    def put(self, namespace, key, value):
        if self.fail_writes:
            raise RuntimeError("store write failure (fake)")
        self._bucket(namespace)[key] = dict(value)

    def delete(self, namespace, key):
        if self.fail_writes:
            raise RuntimeError("store delete failure (fake)")
        self._bucket(namespace).pop(key, None)

    def search(self, namespace, *, limit=10, offset=0, **_kwargs):
        if self.fail_reads:
            raise RuntimeError("store read failure (fake)")
        bucket = self._data.get(tuple(namespace))
        if not bucket:
            return []
        keys = sorted(bucket)
        page = keys[offset : offset + limit]
        return [FakeItem(namespace, k, bucket[k]) for k in page]


def make_registration(
    source_id: str = "11111111-1111-1111-1111-111111111111",
    db_name: str = "ordersdb",
) -> SourceRegistration:
    return SourceRegistration(
        source_id=source_id,
        db_name=db_name,
        host="db.example.com",
        port=5432,
        username="u",
        password="p",
        sslmode="prefer",
        selected_schemas=("public",),
        is_active=True,
    )


def _stats() -> RunStats:
    return RunStats(
        elapsed_ms=10,
        model_calls=1,
        summary_model_calls=0,
        total_model_calls=1,
        tool_calls=0,
        input_tokens=10,
        output_tokens=5,
        token_usage_complete=True,
    )


def make_result(
    *,
    session_id: str = "s_test",
    request_id: str = "req_test",
    run_id: str = "run_test",
    status: str = "completed",
    answer: str = "答案文本",
    evidence: Optional[list[EvidenceItem]] = None,
    errors: Optional[list[Any]] = None,
    archived: bool = True,
) -> DbAgentResult:
    from src.modules.db_deepagent.schemas import RunFailureRecord

    return DbAgentResult(
        session_id=session_id,
        thread_id=f"{session_id}-1",
        request_id=request_id,
        run_id=run_id,
        status=status,
        answer=answer,
        findings=[],
        evidence=evidence or [],
        metric_definitions=[],
        limitations=[],
        open_questions=[],
        errors=[
            e if isinstance(e, RunFailureRecord) else RunFailureRecord(**e)
            for e in (errors or [])
        ],
        warnings=[],
        archived=archived,
        stats=_stats(),
    )


def make_evidence(evidence_id: str = "ev_1", result_ref: str = "res_" + "a" * 32):
    return EvidenceItem(
        evidence_id=evidence_id,
        kind="sql_query",
        tool_call_id="call_1",
        source="pg:abc123",
        sql="SELECT 1",
        sql_template=None,
        result_ref=result_ref,
        input_result_refs=[],
        operation=None,
        row_count=1,
        preview_truncated=False,
        source_truncated=False,
        warnings=[],
        reused_from=None,
        result_generated_at="2026-10-01T00:00:00+00:00",
    )


class FakeBackend:
    """ReadinessBackend 可控 fake（默认全就绪）。"""

    def __init__(self, source_id: str = "11111111-1111-1111-1111-111111111111"):
        from src.api.v2.readiness import MilvusFacts, Neo4jFacts

        self.source_id = source_id
        self.registration = make_registration(source_id)
        self.lookup_status = "ok"
        self.lookup_detail = ""
        self.job_id = "job-1"
        self.publish_id_ts: str = "pub-ts-1"
        self.publish_id_rel: Optional[str] = "pub-rel-1"
        self.optional_steps: dict[str, Optional[str]] = {}
        self.milvus = MilvusFacts(
            table_keys={"ordersdb.public.orders"},
            column_keys={("ordersdb.public.orders", "id")},
            table_publish_ids={self.publish_id_ts},
        )
        self.neo4j = Neo4jFacts(
            table_keys={"ordersdb.public.orders"},
            column_keys={("ordersdb.public.orders", "id")},
            publish_ids={self.publish_id_rel},
            has_column_pairs={("ordersdb.public.orders", "id")},
            has_column_count=1,
        )
        self.business_fail = False
        self.dependency_fail = False
        self.embedding_fail = False

    def _rows(self):
        rows = [
            {
                "step_name": "table_schema",
                "status": "success",
                "is_current_publish": True,
                "publish_id": self.publish_id_ts,
                "job_id": self.job_id,
                "created_at": "2026-10-01T00:00:00+00:00",
            },
            {
                "step_name": "rel",
                "status": "success",
                "is_current_publish": True,
                "publish_id": self.publish_id_rel,
                "job_id": self.job_id,
                "created_at": "2026-10-01T00:00:00+00:00",
            },
        ]
        for step, publish_id in self.optional_steps.items():
            if publish_id is None:
                continue
            rows.append(
                {
                    "step_name": step,
                    "status": "success",
                    "is_current_publish": True,
                    "publish_id": publish_id,
                    "job_id": self.job_id,
                    "created_at": "2026-10-01T00:00:00+00:00",
                }
            )
        return rows

    # ---------------------------------------------------------- Protocol

    def lookup_registration(self, source_id: str):
        from src.api.v2.readiness import RegistrationLookup

        if self.lookup_status == "ok":
            return RegistrationLookup(status="ok", registration=self.registration)
        return RegistrationLookup(status=self.lookup_status, detail=self.lookup_detail)

    def load_publish_records(self, source_id: str):
        from src.api.v2.readiness import PublishRecords

        return PublishRecords(rows=self._rows())

    def load_milvus_facts(self, source_id: str):
        return self.milvus

    def load_neo4j_facts(self, source_id: str):
        return self.neo4j

    def check_business_db(self, registration):
        if self.dependency_fail:
            from src.api.v2.readiness import ReadinessError

            raise ReadinessError("业务库检查未能完成 (fake)")
        if self.business_fail:
            return [
                {
                    "name": "business_db.target",
                    "required": True,
                    "state": "fail",
                    "error_code": "business_target_mismatch",
                    "message": "目标库不一致",
                    "counts": None,
                }
            ]
        return [
            {
                "name": name,
                "required": True,
                "state": "pass",
                "error_code": None,
                "message": "ok",
                "counts": None,
            }
            for name in (
                "business_db.target",
                "business_db.readonly",
                "business_db.timeout",
                "business_db.idle",
            )
        ]

    def embedding_probe(self, source_id: str) -> None:
        if self.embedding_fail:
            from src.api.v2.readiness import ReadinessError

            raise ReadinessError("embedding probe failed (fake)")


class FakeLockPool:
    """会话锁 fake：记录占用状态，可注入忙碌/后端异常。"""

    def __init__(self):
        self.held: set[str] = set()
        self.busy: set[str] = set()
        self.backend_error: set[str] = set()

    def factory(self, session_id: str):
        from src.api.v2.runtime import SessionLockHandle
        from src.modules.db_deepagent.persistence.checkpoint import LockBackendError

        pool = self

        def _acquire() -> bool:
            if session_id in pool.backend_error:
                raise LockBackendError("lock backend failure (fake)")
            if session_id in pool.busy or session_id in pool.held:
                return False
            pool.held.add(session_id)
            return True

        def _release() -> None:
            pool.held.discard(session_id)

        return SessionLockHandle(_acquire, _release)
