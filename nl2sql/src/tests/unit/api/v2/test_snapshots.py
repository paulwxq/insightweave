"""结果快照表读写单元测试（不连 PG）。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.api.v2.snapshots import SnapshotStore, SnapshotTableMissing

pytestmark = pytest.mark.unit


class _Cursor:
    def __init__(self, row=None, rowcount=0):
        self._row = row
        self.rowcount = rowcount

    def fetchone(self):
        return self._row


class _State:
    def __init__(self):
        self.table = True
        self.row = None
        self.queries: list[tuple[str, tuple]] = []


class _Conn:
    def __init__(self, state: _State):
        self._state = state

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, params=None):
        self._state.queries.append((sql, params or ()))
        if "to_regclass" in sql:
            return _Cursor({"reg": "result_snapshots" if self._state.table else None})
        if "INSERT INTO result_snapshots" in sql:
            return _Cursor(rowcount=1)
        return _Cursor(self._state.row)


class _Pool:
    def __init__(self, state: _State):
        self._state = state

    def connection(self):
        return _Conn(self._state)


class _Results:
    def __init__(self, *, fail_refs=()):
        self.fail_refs = set(fail_refs)

    def load_meta(self, ref, *, session_id):
        if ref in self.fail_refs:
            raise RuntimeError("meta readable failure")
        return SimpleNamespace(
            session_id=session_id,
            request_id="req_original",
            thread_id="dbda:s:1",
            saved_row_count=2,
            to_json=lambda: {"columns": [{"name": "id", "pg_type": "int4"}]},
        )

    def read_rows_cells(self, ref, *, session_id, offset, limit):
        return SimpleNamespace(rows=[[{"kind": "integer", "value": "1"}]])


def test_write_inserts_snapshot_and_uses_original_request_id():
    state = _State()
    store = SnapshotStore(_Pool(state), max_rows=200)
    store.write(
        session_id="s1",
        request_id="req_current",
        evidence=[{"result_ref": "res_" + "a" * 32}, {"result_ref": None}],
        results=_Results(),
    )
    inserts = [q for q in state.queries if "INSERT INTO result_snapshots" in q[0]]
    assert len(inserts) == 1
    params = inserts[0][1]
    assert params[0] == "res_" + "a" * 32
    assert params[1] == "s1"
    assert params[2] == "req_original"  # 不是当前触发写入的 request_id
    assert params[4] == 2  # saved_row_count
    assert params[6] == 1  # snapshot_row_count


def test_write_continues_after_single_item_failure():
    state = _State()
    store = SnapshotStore(_Pool(state), max_rows=200)
    store.write(
        session_id="s1",
        request_id="req",
        evidence=[
            {"result_ref": "res_" + "b" * 32},
            {"result_ref": "res_" + "c" * 32},
        ],
        results=_Results(fail_refs={"res_" + "b" * 32}),
    )
    inserts = [q for q in state.queries if "INSERT INTO result_snapshots" in q[0]]
    assert len(inserts) == 1
    assert inserts[0][1][0] == "res_" + "c" * 32


def test_snapshot_ddl_contains_table_and_indexes():
    from src.modules.db_deepagent.persistence.platform_pool import (
        SNAPSHOT_DDL,
        SNAPSHOT_TABLE,
    )

    sql = "\n".join(SNAPSHOT_DDL)
    assert f"CREATE TABLE IF NOT EXISTS {SNAPSHOT_TABLE}" in sql
    assert "idx_result_snapshots_lastref" in sql


def test_fetch_returns_row_and_missing_table_raises():
    state = _State()
    state.row = {
        "result_ref": "res_" + "d" * 32,
        "session_id": "s1",
        "rows_snapshot": [[{"kind": "integer", "value": "1"}]],
        "snapshot_row_count": 1,
        "saved_row_count": 1,
        "meta_jsonb": {"columns": []},
    }
    store = SnapshotStore(_Pool(state))
    assert store.fetch("s1", "res_" + "d" * 32)["snapshot_row_count"] == 1
    state.table = False
    with pytest.raises(SnapshotTableMissing):
        store.fetch("s1", "res_" + "d" * 32)
