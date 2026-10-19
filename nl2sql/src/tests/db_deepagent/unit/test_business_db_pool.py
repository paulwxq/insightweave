"""业务库连接池测试（docs/v4/5 §6.1；§19 步骤 1）

configure 回调用 tuple_row 连接替身验证（不依赖真实库）；真实池行为由
doctor --stage env 与集成测试覆盖。
"""

from __future__ import annotations

from contextlib import contextmanager

import psycopg.pq
import pytest

from src.modules.db_deepagent.errors import (
    DbExecutionError,
    DatasourceMismatchError,
    DuplicateColumnNamesError,
)
from src.modules.db_deepagent.services.business_db import BusinessDbService


class _FakeCursor:
    def __init__(self, row):
        self._row = row

    def fetchone(self):
        return self._row


class _FakeConn:
    """tuple_row 连接替身：只支持 configure 回调用到的 transaction/execute。"""

    def __init__(self, row):
        self._row = row
        self.in_transaction = False
        self.transaction_status = psycopg.pq.TransactionStatus.IDLE

    @contextmanager
    def transaction(self):
        self.in_transaction = True
        self.transaction_status = psycopg.pq.TransactionStatus.INTRANS
        try:
            yield
        finally:
            self.in_transaction = False
            self.transaction_status = psycopg.pq.TransactionStatus.IDLE

    def execute(self, sql):
        assert sql.startswith("SELECT current_database()"), sql
        return _FakeCursor(self._row)


def _service(expected: str) -> BusinessDbService:
    """绕过真实连接池构造：只测 configure 回调逻辑。"""
    svc = BusinessDbService.__new__(BusinessDbService)
    from src.modules.db_deepagent.config import BusinessDbConfig

    svc._cfg = BusinessDbConfig(
        host="h",
        port=5432,
        database="orders",
        user="u",
        password="p",
        expected_database=expected,
        table_key_prefix="orders",
        timezone="Asia/Shanghai",
        pool_min_size=1,
        pool_max_size=2,
        explain_timeout_seconds=5,
        statement_timeout_seconds=30,
        max_saved_result_rows=10000,
    )
    return svc


def test_configure_passes_and_returns_idle():
    conn = _FakeConn(("orders", "app_readonly", "public"))
    _service("orders")._configure(conn)
    assert conn.transaction_status == psycopg.pq.TransactionStatus.IDLE
    assert conn.in_transaction is False


def test_configure_rejects_wrong_database():
    conn = _FakeConn(("other_db", "app_readonly", "public"))
    with pytest.raises(DatasourceMismatchError, match="other_db"):
        _service("orders")._configure(conn)


def test_configure_checks_inside_transaction():
    """检查语句在显式事务内执行（C11：退出时提交、连接回 IDLE）。"""
    seen = []

    class _RecordingConn(_FakeConn):
        @contextmanager
        def transaction(self):
            seen.append("begin")
            with super().transaction():
                yield
            seen.append("end")

        def execute(self, sql):
            seen.append(("query-in-txn" if self.in_transaction else "query-outside",))
            return super().execute(sql)

    _service("orders")._configure(_RecordingConn(("orders", "r", "public")))
    assert seen == ["begin", ("query-in-txn",), "end"], seen


@pytest.mark.integration
@pytest.mark.requires_db
class TestRealBusinessDbPool:
    """真实池行为（dev 环境 orders 库）。"""

    def test_open_computes_datasource_id_and_connections_idle(self, db_agent_config):
        import psycopg

        svc = BusinessDbService(db_agent_config.business_db)
        try:
            svc.open()
            assert svc.datasource_id and svc.datasource_id.startswith("pg:")
            with svc._pool.connection() as conn:  # noqa: SLF001
                assert conn.info.transaction_status == psycopg.pq.TransactionStatus.IDLE
        finally:
            svc.close()

    def test_run_readonly_truncation_and_types(self, db_agent_config):
        svc = BusinessDbService(db_agent_config.business_db)
        try:
            svc.open()
            r = svc.run_readonly(
                "SELECT order_id FROM public.orders ORDER BY order_id",
                timeout_s=5,
                max_rows=3,
            )
            assert len(r.rows) == 3 and r.source_truncated is True
            assert r.column_types == ["int4"]
        finally:
            svc.close()

    def test_duplicate_columns_rejected_without_reading_rows(self, db_agent_config):
        svc = BusinessDbService(db_agent_config.business_db)
        try:
            svc.open()
            with pytest.raises(DuplicateColumnNamesError):
                svc.run_readonly("SELECT 1 AS a, 2 AS a", timeout_s=5, max_rows=10)
            with pytest.raises(DuplicateColumnNamesError, match=r"\?column\?"):
                svc.run_readonly("SELECT 1, 2", timeout_s=5, max_rows=10)
        finally:
            svc.close()

    def test_error_mapping_table(self, db_agent_config):
        svc = BusinessDbService(db_agent_config.business_db)
        try:
            svc.open()
            with pytest.raises(DbExecutionError) as ei:
                svc.run_readonly(
                    "SELECT * FROM public.no_such_table_zz", timeout_s=5, max_rows=1
                )
            assert ei.value.error_type == "db_semantic_error"
            assert ei.value.retryable is True
            assert ei.value.sqlstate.startswith("42")

            with pytest.raises(DbExecutionError) as ei:
                svc.run_readonly("SELECT date '2026-02-30'", timeout_s=5, max_rows=1)
            assert ei.value.error_type == "db_data_error"
            assert ei.value.sqlstate.startswith("22")

            with pytest.raises(DbExecutionError) as ei:
                svc.run_readonly("SELECT pg_sleep(3)", timeout_s=1, max_rows=1)
            assert ei.value.error_type == "timeout"
            assert ei.value.sqlstate == "57014"
        finally:
            svc.close()
