"""ResultStore 单元测试（docs/v4/5 §6.8、§18.2 P10/F11）"""

from __future__ import annotations

import json
import re
from dataclasses import replace
from datetime import date, datetime, time as dt_time, timedelta
from decimal import Decimal
from uuid import UUID

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
    """doc 6.3 §6.5：删除入口收口——须持有效 SessionGuardLease。"""

    @staticmethod
    def _lease_for(db_agent_context):
        from src.modules.db_deepagent.persistence.session_guard import (
            validate_lease,
        )

        return validate_lease(
            db_agent_context.session_guard_lease, db_agent_context.session_id
        )

    def test_delete_without_lease_rejected(self, store, db_agent_context):
        meta = _save(store, db_agent_context, [(1, Decimal("1"), date(2026, 9, 1))])
        with pytest.raises(Exception):
            store.delete(meta.result_ref, session_id=db_agent_context.session_id)
        # 有凭据可删
        store.delete(
            meta.result_ref,
            session_id=db_agent_context.session_id,
            lease=self._lease_for(db_agent_context),
        )
        with pytest.raises(ResultStoreError):
            store.load_meta(meta.result_ref, session_id=db_agent_context.session_id)

    def test_delete_wrong_session_lease_rejected(self, store, db_agent_context):
        from src.modules.db_deepagent.persistence.session_guard import (
            SessionGuardLease,
            validate_lease,
        )

        meta = _save(store, db_agent_context, [(1, Decimal("1"), date(2026, 9, 1))])
        other = SessionGuardLease(session_id="s-other", backend="memory", token=999999)
        with pytest.raises(Exception):
            store.delete(
                meta.result_ref,
                session_id=db_agent_context.session_id,
                lease=other,
            )

    def test_purge_expired_requires_lease_and_session(
        self, store, db_agent_context, tmp_path
    ):
        meta = _save(store, db_agent_context, [(1, Decimal("1"), date(2026, 9, 1))])
        meta_path = tmp_path / "results" / meta.result_ref / "meta.json"
        data = json.loads(meta_path.read_text(encoding="utf-8"))
        data["created_at"] = "2020-01-01T00:00:00+08:00"
        meta_path.write_text(json.dumps(data), encoding="utf-8")
        # 无 lease/session_ids：不删除任何目录（无全局绕过入口）
        assert (
            store.purge_expired(retention_days=7, now=datetime(2026, 10, 13, 12, 0))
            == 0
        )
        removed = store.purge_expired(
            retention_days=7,
            now=datetime(2026, 10, 13, 12, 0),
            lease=self._lease_for(db_agent_context),
            session_ids=[db_agent_context.session_id],
        )
        assert removed == 1


class TestV2Cells:
    """v2 cell 信封（doc 2 §4.5）：read_rows_cells 原样返回存储信封。"""

    def test_cells_envelopes(self, store, db_agent_context):
        big = 2**60
        cols = [
            {"name": "n", "pg_type": "numeric"},
            {"name": "i", "pg_type": "bigint"},
            {"name": "t", "pg_type": "text"},
            {"name": "b", "pg_type": "bytea"},
            {"name": "u", "pg_type": "uuid"},
            {"name": "iv", "pg_type": "interval"},
            {"name": "tm", "pg_type": "time"},
        ]
        meta = _save(
            store,
            db_agent_context,
            [
                (
                    Decimal("1.23"),
                    big,
                    "文本",
                    b"\x00\x01",
                    UUID("12345678-1234-5678-1234-567812345678"),
                    timedelta(days=1, seconds=1),
                    dt_time(12, 30, 45),
                )
            ],
            columns=cols,
        )
        page = store.read_rows_cells(
            meta.result_ref, session_id=db_agent_context.session_id
        )
        row = page.rows[0]
        assert row[0] == {"kind": "decimal", "value": "1.23"}
        assert row[1] == {"kind": "integer", "value": str(big)}
        assert row[2] == {"kind": "text", "value": "文本"}
        assert row[3] == {
            "kind": "binary",
            "value": {"length": 2, "content_saved": False},
        }
        assert row[4] == {
            "kind": "uuid",
            "value": "12345678-1234-5678-1234-567812345678",
        }
        assert row[5] == {"kind": "interval", "value": "86401000000"}
        assert row[6] == {"kind": "time", "value": "12:30:45"}

    def test_cells_float_special_values(self, store, db_agent_context):
        cols = [{"name": "f", "pg_type": "float8"}]
        meta = _save(
            store,
            db_agent_context,
            [(float("nan"),), (float("inf"),), (float("-inf"),)],
            columns=cols,
        )
        page = store.read_rows_cells(
            meta.result_ref, session_id=db_agent_context.session_id
        )
        assert [r[0]["value"] for r in page.rows] == ["NaN", "Infinity", "-Infinity"]
        assert all(r[0]["kind"] == "float" for r in page.rows)

    def test_cells_json_column(self, store, db_agent_context):
        cols = [{"name": "j", "pg_type": "jsonb"}]
        meta = _save(
            store,
            db_agent_context,
            [({"$dec": [1, "a"]},), ([1, 2],)],
            columns=cols,
        )
        page = store.read_rows_cells(
            meta.result_ref, session_id=db_agent_context.session_id
        )
        assert page.rows[0][0]["kind"] == "json"
        assert json.loads(page.rows[0][0]["value"]) == {"$dec": [1, "a"]}
        assert page.rows[1][0]["kind"] == "json"

    def test_cells_array_nested(self, store, db_agent_context):
        cols = [{"name": "a", "pg_type": "numeric[]"}]
        meta = _save(
            store,
            db_agent_context,
            [([Decimal("1.5"), None],)],
            columns=cols,
        )
        page = store.read_rows_cells(
            meta.result_ref, session_id=db_agent_context.session_id
        )
        assert page.rows[0][0] == {
            "kind": "array",
            "value": [
                {"kind": "decimal", "value": "1.5"},
                {"kind": "null", "value": None},
            ],
        }

    def test_cells_json_array_column(self, store, db_agent_context):
        """jsonb[]（pg_type.typname 为 "_jsonb"）按元素编码为 json cell
        （doc 2 §4.5：JSON/JSONB 必须先识别为业务 JSON）。"""
        cols = [{"name": "ja", "pg_type": "_jsonb"}]
        meta = _save(
            store,
            db_agent_context,
            [([{"k": 1}, {"k": 2}],)],
            columns=cols,
        )
        page = store.read_rows_cells(
            meta.result_ref, session_id=db_agent_context.session_id
        )
        cell = page.rows[0][0]
        assert cell["kind"] == "array"
        assert [c["kind"] for c in cell["value"]] == ["json", "json"]
        assert json.loads(cell["value"][0]["value"]) == {"k": 1}

    def _rewrite_rows(self, tmp_path, meta, lines, *, v1=False):
        import hashlib

        result_dir = tmp_path / "results" / meta.result_ref
        payload = "\n".join(lines)
        (result_dir / "rows.jsonl").write_text(payload + "\n", encoding="utf-8")
        meta_path = result_dir / "meta.json"
        data = json.loads(meta_path.read_text(encoding="utf-8"))
        if v1:
            data.pop("format_version", None)
        data["rows_sha256"] = hashlib.sha256(payload.encode()).hexdigest()
        meta_path.write_text(json.dumps(data), encoding="utf-8")

    def test_invalid_nested_cell_rejected(self, store, db_agent_context, tmp_path):
        """数组元素按同规则递归校验：嵌套未知 kind / 非法 decimal 均
        result_encoding_error（doc 2 §4.5 "同规则的 cell 数组"）。"""
        cols = [{"name": "a", "pg_type": "text[]"}]
        base = _save(store, db_agent_context, [([[""]],)], columns=cols)
        for bad in (
            {"kind": "bogus", "value": 1},
            {"kind": "decimal", "value": "abc"},
            {"kind": "null", "value": 0},
            "not-a-cell",
        ):
            self._rewrite_rows(
                tmp_path,
                base,
                [json.dumps([[{"kind": "array", "value": [bad]}]])],
            )
            with pytest.raises(ResultStoreError) as e:
                store.read_rows_cells(
                    base.result_ref, session_id=db_agent_context.session_id
                )
            assert e.value.error_type == "result_encoding_error"

    def test_nested_depth_capped(self, store, db_agent_context, tmp_path):
        """损坏文件的过深嵌套显式报编码错误，不触发递归崩溃。"""
        cols = [{"name": "a", "pg_type": "text[]"}]
        base = _save(store, db_agent_context, [([[""]],)], columns=cols)
        cell = {"kind": "text", "value": "x"}
        for _ in range(20):
            cell = {"kind": "array", "value": [cell]}
        self._rewrite_rows(tmp_path, base, [json.dumps([[cell]])])
        with pytest.raises(ResultStoreError) as e:
            store.read_rows_cells(
                base.result_ref, session_id=db_agent_context.session_id
            )
        assert e.value.error_type == "result_encoding_error"

    def test_unserializable_value_rejected(self, store, db_agent_context):
        cols = [{"name": "x", "pg_type": "text"}]
        with pytest.raises(ResultStoreError) as e:
            _save(store, db_agent_context, [(object(),)], columns=cols)
        assert e.value.error_type == "result_encoding_error"

    def test_v1_file_compatible_read(self, store, db_agent_context, tmp_path):
        """v1（无 format_version）文件按列 pg_type 转成 cell 读取（Q2）。

        手工构造真实 v1 文件：raw JSON 值 + $dec 数字信封。
        """
        import hashlib

        cols = [
            {"name": "i", "pg_type": "integer"},
            {"name": "d", "pg_type": "numeric"},
            {"name": "t", "pg_type": "text"},
            {"name": "dt", "pg_type": "date"},
        ]
        meta = _save(
            store,
            db_agent_context,
            [(1, Decimal("1"), "x", date(2026, 1, 1))],
            columns=cols,
        )
        result_dir = tmp_path / "results" / meta.result_ref
        lines = ['[1, {"$dec": "2.50"}, "x", "2026-09-05"]']
        (result_dir / "rows.jsonl").write_text(
            "\n".join(lines) + "\n", encoding="utf-8"
        )
        meta_path = result_dir / "meta.json"
        data = json.loads(meta_path.read_text(encoding="utf-8"))
        data.pop("format_version")
        data["rows_sha256"] = hashlib.sha256("\n".join(lines).encode()).hexdigest()
        meta_path.write_text(json.dumps(data), encoding="utf-8")
        page = store.read_rows_cells(
            meta.result_ref, session_id=db_agent_context.session_id
        )
        assert page.rows[0][0] == {"kind": "integer", "value": "1"}
        assert page.rows[0][1] == {"kind": "decimal", "value": "2.50"}
        assert page.rows[0][2] == {"kind": "text", "value": "x"}
        assert page.rows[0][3] == {"kind": "date", "value": "2026-09-05"}

    def test_v1_json_column_keeps_business_json(
        self, store, db_agent_context, tmp_path
    ):
        """v1 JSONB 列：数组值与 $bin/$dec 同名键保持业务 JSON，不解释为
        SQL 数组或二进制/数字占位（doc 2 §4.5）。"""
        cols = [{"name": "j", "pg_type": "jsonb"}]
        meta = _save(store, db_agent_context, [(None,)], columns=cols)
        self._rewrite_rows(
            tmp_path,
            meta,
            ["[[1, 2]]", '[{"$bin": 7}]', '[{"$dec": "1.5"}]'],
            v1=True,
        )
        page = store.read_rows_cells(
            meta.result_ref, session_id=db_agent_context.session_id
        )
        assert page.rows[0][0]["kind"] == "json"
        assert json.loads(page.rows[0][0]["value"]) == [1, 2]
        assert page.rows[1][0]["kind"] == "json"
        assert json.loads(page.rows[1][0]["value"]) == {"$bin": 7}
        assert page.rows[2][0]["kind"] == "json"
        assert json.loads(page.rows[2][0]["value"]) == {"$dec": "1.5"}

    def test_v1_json_string_serialized_as_json_text(
        self, store, db_agent_context, tmp_path
    ):
        """v1 JSONB 标量字符串按 kind=json 契约序列化为 JSON 文本：
        "hello" → '"hello"'，"true" 不被误读为布尔；数组内字符串元素
        同理。"""
        cols = [{"name": "j", "pg_type": "jsonb"}, {"name": "ja", "pg_type": "_jsonb"}]
        meta = _save(store, db_agent_context, [(None, None)], columns=cols)
        self._rewrite_rows(
            tmp_path,
            meta,
            ['["hello", ["inner"]]', '["true", ["true"]]'],
            v1=True,
        )
        page = store.read_rows_cells(
            meta.result_ref, session_id=db_agent_context.session_id
        )
        assert page.rows[0][0] == {"kind": "json", "value": '"hello"'}
        assert page.rows[0][1] == {
            "kind": "array",
            "value": [{"kind": "json", "value": '"inner"'}],
        }
        assert page.rows[1][0] == {"kind": "json", "value": '"true"'}
        assert json.loads(page.rows[1][0]["value"]) == "true"  # 字符串，非布尔
        assert json.loads(page.rows[1][1]["value"][0]["value"]) == "true"

    def test_v1_binary_placeholder_column_typed(
        self, store, db_agent_context, tmp_path
    ):
        """v1 bytea 列 $bin 正常解释；占位长度非法时报编码错误而非
        未转换的 ValueError。"""
        cols = [{"name": "b", "pg_type": "bytea"}]
        meta = _save(store, db_agent_context, [(b"x",)], columns=cols)
        self._rewrite_rows(tmp_path, meta, ['[{"$bin": 7}]'], v1=True)
        page = store.read_rows_cells(
            meta.result_ref, session_id=db_agent_context.session_id
        )
        assert page.rows[0][0] == {
            "kind": "binary",
            "value": {"length": 7, "content_saved": False},
        }
        self._rewrite_rows(tmp_path, meta, ['[{"$bin": "hello"}]'], v1=True)
        with pytest.raises(ResultStoreError) as e:
            store.read_rows_cells(
                meta.result_ref, session_id=db_agent_context.session_id
            )
        assert e.value.error_type == "result_encoding_error"

    def test_v1_numeric_array_placeholder_by_element_type(
        self, store, db_agent_context, tmp_path
    ):
        """v1 numeric[] 的 $dec 按元素类型解释；JSON 数组列（_jsonb）的
        元素保持业务 JSON。"""
        cols = [
            {"name": "a", "pg_type": "_numeric"},
            {"name": "ja", "pg_type": "_jsonb"},
        ]
        meta = _save(store, db_agent_context, [([Decimal("1")], [{}])], columns=cols)
        self._rewrite_rows(
            tmp_path,
            meta,
            ['[[{"$dec": "2.50"}], [{"k": 1}]]'],
            v1=True,
        )
        page = store.read_rows_cells(
            meta.result_ref, session_id=db_agent_context.session_id
        )
        assert page.rows[0][0] == {
            "kind": "array",
            "value": [{"kind": "decimal", "value": "2.50"}],
        }
        assert page.rows[0][1] == {
            "kind": "array",
            "value": [{"kind": "json", "value": '{"k": 1}'}],
        }

    def test_pagination_fields(self, store, db_agent_context):
        meta = _save(
            store,
            db_agent_context,
            [(i, Decimal("1"), date(2026, 1, 1)) for i in range(5)],
        )
        page = store.read_rows_cells(
            meta.result_ref, session_id=db_agent_context.session_id, offset=1, limit=2
        )
        assert page.offset == 1
        assert page.next_offset == 3
        assert len(page.rows) == 2
        assert page.saved_row_count == 5
