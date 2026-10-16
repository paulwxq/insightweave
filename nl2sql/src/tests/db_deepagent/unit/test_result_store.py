"""ResultStore 单元测试（docs/v4/5 §6.8、§18.2 P10/F11）"""

from __future__ import annotations

import json
import re
from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal

import pytest

from src.modules.db_deepagent.errors import ResultStoreError
from src.modules.db_deepagent.services.result_store import ResultStore

pytestmark = pytest.mark.unit

_COLUMNS = [
    {"name": "id", "pg_type": "integer"},
    {"name": "amount", "pg_type": "numeric"},
    {"name": "created_at", "pg_type": "date"},
]


@pytest.fixture()
def store(tmp_path) -> ResultStore:
    return ResultStore(tmp_path)


def _save(store, ctx, rows, *, columns=None, source_truncated=False):
    return store.save(
        ctx=ctx,
        purpose="query",
        sql_template="SELECT ...",
        executed_sql="SELECT ...",
        columns=columns or _COLUMNS,
        rows=rows,
        source_truncated=source_truncated,
        derived_from=[],
        guard_evidence={"time": [], "dim": []},
    )


class TestSaveAndRead:
    def test_result_ref_format(self, store, db_agent_context):
        meta = _save(store, db_agent_context, [(1, Decimal("9.9"), date(2026, 9, 1))])
        assert re.fullmatch(r"res_[0-9a-f]{32}", meta.result_ref)

    def test_roundtrip_decimal_date(self, store, db_agent_context):
        """Decimal 解码还原；date 以 ISO 字符串返回（§6.8 编码）。"""
        meta = _save(
            store,
            db_agent_context,
            [
                (1, Decimal("99.50"), date(2026, 9, 5)),
                (2, Decimal("10.00"), date(2026, 9, 6)),
            ],
        )
        page = store.read_rows(meta.result_ref, session_id=db_agent_context.session_id)
        assert page.saved_row_count == 2
        assert page.rows[0][1] == Decimal("99.50")
        assert page.rows[0][2] == "2026-09-05"

    def test_files_layout(self, store, db_agent_context, tmp_path):
        """目录结构 <workspace>/results/<ref>/{rows.jsonl,meta.json}。"""
        meta = _save(store, db_agent_context, [(1, Decimal("1"), date(2026, 9, 1))])
        d = tmp_path / "results" / meta.result_ref
        assert (d / "rows.jsonl").exists() and (d / "meta.json").exists()

    def test_meta_carries_all_fields(self, store, db_agent_context):
        meta = _save(store, db_agent_context, [(1, Decimal("1"), date(2026, 9, 1))])
        assert meta.session_id == db_agent_context.session_id
        assert meta.thread_id == db_agent_context.thread_id
        assert meta.run_id == db_agent_context.run_id
        assert meta.datasource_id == db_agent_context.datasource_id
        assert meta.purpose == "query"
        assert meta.saved_row_count == 1
        assert meta.rows_sha256
        assert meta.guard_evidence == {"time": [], "dim": []}


class TestSessionAndIntegrity:
    def test_cross_session_denied(self, store, db_agent_context):
        meta = _save(store, db_agent_context, [(1, Decimal("1"), date(2026, 9, 1))])
        other = replace(db_agent_context, session_id="s-other")
        with pytest.raises(ResultStoreError) as e:
            store.load_meta(meta.result_ref, session_id=other.session_id)
        assert e.value.error_type == "result_access_denied"

    def test_invalid_ref_rejected(self, store, db_agent_context):
        with pytest.raises(ResultStoreError) as e:
            store.load_meta("res_zzz", session_id=db_agent_context.session_id)
        assert e.value.error_type == "invalid_result_ref"

    def test_not_found(self, store, db_agent_context):
        with pytest.raises(ResultStoreError) as e:
            store.load_meta("res_" + "a" * 32, session_id=db_agent_context.session_id)
        assert e.value.error_type == "result_not_found"

    def test_tampered_rows_detected(self, store, db_agent_context, tmp_path):
        meta = _save(store, db_agent_context, [(1, Decimal("1"), date(2026, 9, 1))])
        rows_path = tmp_path / "results" / meta.result_ref / "rows.jsonl"
        rows_path.write_text('["tampered"]\n', encoding="utf-8")
        with pytest.raises(ResultStoreError) as e:
            store.read_rows(meta.result_ref, session_id=db_agent_context.session_id)
        assert e.value.error_type == "result_integrity_error"


class TestColumnValues:
    def test_projection(self, store, db_agent_context):
        meta = _save(
            store,
            db_agent_context,
            [(1, Decimal("1"), date(2026, 9, 1)), (2, Decimal("2"), date(2026, 9, 2))],
        )
        vals = store.column_values(
            meta.result_ref, session_id=db_agent_context.session_id, column="id"
        )
        assert vals == [1, 2]

    def test_unknown_column(self, store, db_agent_context):
        meta = _save(store, db_agent_context, [(1, Decimal("1"), date(2026, 9, 1))])
        with pytest.raises(ResultStoreError) as e:
            store.column_values(
                meta.result_ref, session_id=db_agent_context.session_id, column="nope"
            )
        assert e.value.error_type == "invalid_argument"


class TestMaintenance:
    def test_delete(self, store, db_agent_context):
        meta = _save(store, db_agent_context, [(1, Decimal("1"), date(2026, 9, 1))])
        store.delete(meta.result_ref, session_id=db_agent_context.session_id)
        with pytest.raises(ResultStoreError):
            store.load_meta(meta.result_ref, session_id=db_agent_context.session_id)

    def test_purge_expired(self, store, db_agent_context, tmp_path):
        meta = _save(store, db_agent_context, [(1, Decimal("1"), date(2026, 9, 1))])
        meta_path = tmp_path / "results" / meta.result_ref / "meta.json"
        data = json.loads(meta_path.read_text(encoding="utf-8"))
        data["created_at"] = "2020-01-01T00:00:00+08:00"
        meta_path.write_text(json.dumps(data), encoding="utf-8")
        removed = store.purge_expired(
            retention_days=7, now=datetime(2026, 10, 13, 12, 0)
        )
        assert removed == 1
