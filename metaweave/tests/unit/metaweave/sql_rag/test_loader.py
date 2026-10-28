"""SQLExampleLoader 受管口径单元测试（doc 25.2 §4/§7）。

契约要点：`input_dir` 目录即输入（顶层非忽略 `*.json` 合并）、
`source_id`（db_src_N 短 ID）/`publish_id`（发布运行 UUID）注入、
`example_id` 前缀为 source_id、
按源整份替换（upsert + 全部成功后 delete_by_expr）、
结果规则四计数互斥（提前失败路径允许和小于 total）。
"""

import json
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
import yaml

from metaweave.core.sql_rag.loader import (
    SQLExampleLoader,
    collect_sql_example_input_files,
)

SOURCE_ID = "db_src_1"
PUBLISH_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeffff"


class FakeEmbeddingService:
    """可配置失败的假 Embedding：always_raise / missing Questions 不进结果。"""

    def __init__(self, cfg: Dict[str, Any]):
        self.cfg = cfg
        self.always_raise = False
        self.fail_first_n = 0
        self.calls: List[List[str]] = []
        self._attempt = 0
        self.missing: set[str] = set()

    def get_embeddings(self, texts: List[str]) -> Dict[str, List[float]]:
        self.calls.append(list(texts))
        self._attempt += 1
        if self.always_raise or (
            self.fail_first_n and self._attempt <= self.fail_first_n
        ):
            raise RuntimeError("embedding 503")
        return {t: [0.1, 0.2, 0.3, 0.4] for t in texts if t not in self.missing}


class FakeMilvusClient:
    def __init__(self, cfg: Dict[str, Any]):
        self.cfg = cfg
        self.upsert_calls: List[List[Dict]] = []
        self.delete_calls: List[str] = []
        self.ensure_calls: List[Dict] = []
        self.closed = 0
        self.upsert_return_shortfall = 0  # 每批确认数 = len - shortfall
        self.upsert_raise = False
        self.delete_raise = False
        self.ensure_raise = False
        self.ensure_exc: BaseException | None = None  # 非结构类故障注入（如超时）

    def connect(self) -> None:
        pass

    def close(self) -> None:
        self.closed += 1

    def ensure_collection(
        self, collection_name, schema, index_params, clean=False, **kwargs
    ):
        if self.ensure_exc is not None:
            raise self.ensure_exc
        if self.ensure_raise:
            raise ValueError("集合结构不符（旧版本）")
        self.ensure_calls.append(
            {"name": collection_name, "clean": clean, "kwargs": kwargs}
        )

    def upsert_batch(self, collection_name: str, data: List[Dict[str, Any]]) -> int:
        if self.upsert_raise:
            raise RuntimeError("milvus write timeout")
        self.upsert_calls.append(data)
        return max(len(data) - self.upsert_return_shortfall, 0)

    def delete_by_expr(self, collection_name: str, expr: str) -> int:
        if self.delete_raise:
            raise RuntimeError("delete rpc failed")
        self.delete_calls.append(expr)
        return 0


def _write_metadata_config(tmp_path: Path, with_database: bool = True) -> Path:
    cfg = {
        "embedding": {
            "active": "qwen",
            "providers": {"qwen": {"dimensions": 4}},
        },
        "vector_database": {
            "active": "milvus",
            "providers": {
                "milvus": {"host": "localhost", "port": 19530, "database": "nl2sql"}
            },
        },
    }
    if with_database:
        cfg["database"] = {"database": "testdb"}
    meta_path = tmp_path / "metadata_config.yaml"
    meta_path.write_text(yaml.dump(cfg), encoding="utf-8")
    return meta_path


def _write_pairs(path: Path, pairs) -> Path:
    path.write_text(json.dumps(pairs, ensure_ascii=False), encoding="utf-8")
    return path


DEFAULT_PAIRS = [
    {
        "question": "总订单数？",
        "sql": "SELECT COUNT(*) FROM orders;",
        "domain": "订单分析",
    },
    {"question": "客户列表？", "sql": "SELECT * FROM customers;", "domain": "客户管理"},
]


def _make_loader(
    tmp_path: Path,
    pairs=None,
    files: Dict[str, Any] | None = None,
    source_id: Any = SOURCE_ID,
    publish_id: Any = PUBLISH_ID,
    batch_size: Any = 10,
    with_database: bool = True,
    input_dir: Path | None = None,
    collection_name: str = "test_coll",
):
    meta_path = _write_metadata_config(tmp_path, with_database=with_database)
    if input_dir is None:
        input_dir = tmp_path / "sql-rag"
        input_dir.mkdir(exist_ok=True)
        if files:
            for name, content in files.items():
                if content is None:
                    (input_dir / name).mkdir()
                else:
                    _write_pairs(input_dir / name, content)
        else:
            _write_pairs(input_dir / "qs_src_pair.json", pairs or DEFAULT_PAIRS)
    config = {
        "metadata_config_file": str(meta_path),
        "sql_loader": {
            "input_dir": str(input_dir),
            "collection_name": collection_name,
            "options": {"batch_size": batch_size},
        },
    }
    loader = SQLExampleLoader(
        config,
        milvus_client_cls=FakeMilvusClient,
        embedding_service_cls=FakeEmbeddingService,
        source_id=source_id,
        publish_id=publish_id,
    )
    return loader


# ---------------------------------------------------------------------------
# A01：集合 schema 与 ensure_collection 传参
# ---------------------------------------------------------------------------


class TestSchema:
    def _mock_milvus_types(self):
        mock_field = MagicMock()
        mock_schema = MagicMock()
        mock_dtype = MagicMock()
        mock_dtype.VARCHAR = "VARCHAR"
        mock_dtype.FLOAT_VECTOR = "FLOAT_VECTOR"
        mock_dtype.INT64 = "INT64"
        return mock_field, mock_schema, mock_dtype

    def test_schema_seven_fields(self, tmp_path):
        loader = _make_loader(tmp_path)
        assert loader.validate()
        field, schema, dtype = self._mock_milvus_types()
        with patch(
            "metaweave.core.sql_rag.loader._lazy_import_milvus",
            return_value=(field, schema, dtype),
        ):
            loader._build_schema()
        names = [c.kwargs["name"] for c in field.call_args_list]
        assert names == [
            "example_id",
            "source_id",
            "publish_id",
            "question_sql",
            "embedding",
            "domain",
            "updated_at",
        ]
        by_name = {c.kwargs["name"]: c.kwargs for c in field.call_args_list}
        assert by_name["example_id"]["is_primary"] is True
        assert by_name["example_id"]["auto_id"] is False
        assert by_name["source_id"]["max_length"] == 36
        assert by_name["publish_id"]["max_length"] == 36
        assert by_name["question_sql"]["max_length"] == 16384
        assert by_name["domain"]["max_length"] == 256

    def test_ensure_collection_flags_and_index(self, tmp_path):
        loader = _make_loader(tmp_path)
        assert loader.validate()
        loader.load()
        milvus = loader._milvus_client
        assert len(milvus.ensure_calls) == 1
        call = milvus.ensure_calls[0]
        assert call["name"] == "test_coll"
        assert call["clean"] is False
        assert call["kwargs"].get("validate_schema") is True
        assert "index_field_name" not in call["kwargs"]  # 缺省 embedding，零改动


# ---------------------------------------------------------------------------
# example_id：source_id 前缀 + UUID 规范化（A01/A09 loader 侧）
# ---------------------------------------------------------------------------


class TestComputeExampleId:
    def test_deterministic_and_prefix(self):
        id1 = SQLExampleLoader.compute_example_id(SOURCE_ID, "Q", "S")
        id2 = SQLExampleLoader.compute_example_id(SOURCE_ID, "Q", "S")
        assert id1 == id2
        assert id1.startswith(f"{SOURCE_ID}:")
        assert len(id1.split(":")[1]) == 8

    def test_different_content_different_id(self):
        id1 = SQLExampleLoader.compute_example_id(SOURCE_ID, "Q1", "S1")
        id2 = SQLExampleLoader.compute_example_id(SOURCE_ID, "Q2", "S2")
        assert id1 != id2

    def test_different_source_different_id(self):
        id1 = SQLExampleLoader.compute_example_id(SOURCE_ID, "Q", "S")
        id2 = SQLExampleLoader.compute_example_id("db_src_2", "Q", "S")
        assert id1 != id2

    def test_uuid_form_source_id_rejected(self):
        """断代后 source_id 为短 ID：旧 UUID 形态不再是合法身份。"""
        with pytest.raises(ValueError):
            SQLExampleLoader.compute_example_id(
                "11111111-2222-3333-4444-555555555555", "Q", "S"
            )


class TestIdNormalization:
    def test_publish_uuid_object_accepted_and_stringified(self, tmp_path):
        pid = uuid4()
        loader = _make_loader(tmp_path, source_id=SOURCE_ID, publish_id=pid)
        assert loader.validate()
        result = loader.load()
        assert result["success"]
        rows = loader._milvus_client.upsert_calls[0]
        assert all(r["source_id"] == SOURCE_ID for r in rows)
        assert all(r["publish_id"] == str(pid) for r in rows)
        assert all(r["example_id"].startswith(f"{SOURCE_ID}:") for r in rows)

    def test_invalid_id_string_rejected(self, tmp_path):
        loader = _make_loader(tmp_path, source_id="not-a-short-id")
        assert not loader.validate()

    def test_uuid_form_source_id_rejected_in_constructor(self, tmp_path):
        """旧 UUID 形态的 source_id 不再合法（doc 6 §1 短 ID 断代）。"""
        loader = _make_loader(
            tmp_path, source_id="11111111-2222-3333-4444-555555555555"
        )
        assert not loader.validate()

    def test_missing_ids_rejected(self, tmp_path):
        loader = _make_loader(tmp_path, source_id=None, publish_id=PUBLISH_ID)
        assert not loader.validate()


# ---------------------------------------------------------------------------
# A04：database.database 不再读取
# ---------------------------------------------------------------------------


class TestNoDatabaseRead:
    def test_loads_without_database_node(self, tmp_path):
        loader = _make_loader(tmp_path, with_database=False)
        assert loader.validate()
        result = loader.load()
        assert result["success"]
        rows = loader._milvus_client.upsert_calls[0]
        assert rows[0]["example_id"].startswith(f"{SOURCE_ID}:")


# ---------------------------------------------------------------------------
# A05：输入目录口径（共享枚举 helper）
# ---------------------------------------------------------------------------


class TestCollectInputFiles:
    def test_only_regular_json_counted(self, tmp_path):
        d = tmp_path / "sql-rag"
        d.mkdir()
        _write_pairs(d / "b.json", [])
        _write_pairs(d / "a.json", [])
        (d / "qs.log").write_text("log", encoding="utf-8")
        (d / "qs.json.backup").write_text("{}", encoding="utf-8")
        (d / ".tmp_crash.json").write_text("{}", encoding="utf-8")
        (d / "sub").mkdir()
        (d / "link.json").symlink_to(d / "a.json")
        files = collect_sql_example_input_files(d)
        assert [f.name for f in files] == ["a.json", "b.json"]

    def test_missing_dir_empty(self, tmp_path):
        assert collect_sql_example_input_files(tmp_path / "nope") == []


class TestInputDirCaliber:
    def test_multiple_json_merged_sorted(self, tmp_path):
        loader = _make_loader(
            tmp_path,
            files={
                "a.json": [{"question": "QA", "sql": "SA", "domain": ""}],
                "b.json": [{"question": "QB", "sql": "SB", "domain": ""}],
                "x.log": None,
            },
        )
        assert loader.validate()
        result = loader.load()
        assert result["success"] and result["total"] == 2
        rows = loader._milvus_client.upsert_calls[0]
        questions = [json.loads(r["question_sql"])["question"] for r in rows]
        assert questions == ["QA", "QB"]  # 按文件名排序合并

    def test_dir_without_json_not_ready(self, tmp_path):
        d = tmp_path / "sql-rag"
        d.mkdir()
        (d / "summary.log").write_text("x", encoding="utf-8")
        loader = _make_loader(tmp_path, input_dir=d)
        assert not loader.validate()

    def test_missing_input_dir_config(self, tmp_path):
        meta_path = _write_metadata_config(tmp_path)
        loader = SQLExampleLoader(
            {
                "metadata_config_file": str(meta_path),
                "sql_loader": {"collection_name": "c"},
            },
            milvus_client_cls=FakeMilvusClient,
            embedding_service_cls=FakeEmbeddingService,
            source_id=SOURCE_ID,
            publish_id=PUBLISH_ID,
        )
        assert not loader.validate()

    def test_old_input_file_key_ignored(self, tmp_path):
        meta_path = _write_metadata_config(tmp_path)
        d = tmp_path / "sql-rag"
        d.mkdir()
        _write_pairs(d / "qs_pair.json", DEFAULT_PAIRS)
        (tmp_path / "legacy_pair.json").write_text("[]", encoding="utf-8")
        loader = SQLExampleLoader(
            {
                "metadata_config_file": str(meta_path),
                "sql_loader": {
                    "input_file": str(tmp_path / "legacy_pair.json"),
                    "input_dir": str(d),
                    "collection_name": "c",
                },
            },
            milvus_client_cls=FakeMilvusClient,
            embedding_service_cls=FakeEmbeddingService,
            source_id=SOURCE_ID,
            publish_id=PUBLISH_ID,
        )
        assert loader.validate()
        result = loader.load()
        assert result["success"] and result["total"] == 2  # 不读 input_file


# ---------------------------------------------------------------------------
# A02/A03：upsert、按源替换、replacement_started
# ---------------------------------------------------------------------------


class TestReplaceSemantics:
    def test_success_deletes_old_of_source(self, tmp_path):
        loader = _make_loader(tmp_path)
        loader.validate()
        result = loader.load()
        assert result["success"] is True
        assert result["replacement_started"] is True
        milvus = loader._milvus_client
        assert milvus.delete_calls == [
            f'source_id == "{SOURCE_ID}" and publish_id != "{PUBLISH_ID}"'
        ]

    def test_identical_pairs_share_example_id(self, tmp_path):
        loader = _make_loader(
            tmp_path,
            files={
                "a.json": [{"question": "Q", "sql": "S", "domain": ""}],
                "b.json": [{"question": "Q", "sql": "S", "domain": ""}],
            },
        )
        loader.validate()
        loader.load()
        rows = loader._milvus_client.upsert_calls[0]
        assert rows[0]["example_id"] == rows[1]["example_id"]

    def test_replacement_started_true_when_first_upsert_raises(self, tmp_path):
        loader = _make_loader(tmp_path)
        loader.validate()
        loader._milvus_client.upsert_raise = True
        result = loader.load()
        assert result["success"] is False
        assert result["replacement_started"] is True
        assert result["records_unconfirmed"] == 2
        assert loader._milvus_client.delete_calls == []

    def test_first_source_publish_delete_returns_zero_ok(self, tmp_path):
        loader = _make_loader(tmp_path)
        loader.validate()
        assert loader.load()["success"] is True  # delete 返回 0 正常成功


# ---------------------------------------------------------------------------
# A11：结果规则与计数
# ---------------------------------------------------------------------------


class TestResultRules:
    def test_all_success_equation(self, tmp_path):
        loader = _make_loader(tmp_path)
        loader.validate()
        r = loader.load()
        assert r["success"] and r["records_loaded"] == 2
        assert r["records_failed"] == 0 and r["records_unconfirmed"] == 0
        assert r["records_skipped"] == 0 and r["total"] == 2

    def test_a_all_embedding_fail_no_write_keep_old(self, tmp_path):
        loader = _make_loader(tmp_path)
        loader.validate()
        loader._embedding_service.always_raise = True
        r = loader.load()
        assert r["success"] is False
        assert r["records_failed"] == 2
        assert r["records_unconfirmed"] == 0
        assert r["replacement_started"] is False
        milvus = loader._milvus_client
        assert milvus.upsert_calls == [] and milvus.delete_calls == []

    def test_a_b_partial_embedding_fail(self, tmp_path):
        loader = _make_loader(
            tmp_path,
            files={
                "a.json": [
                    {"question": f"Q{i}", "sql": "S", "domain": ""} for i in range(2)
                ],
                "b.json": [
                    {"question": f"P{i}", "sql": "S", "domain": ""} for i in range(2)
                ],
            },
            batch_size=2,
        )
        loader.validate()
        # 前两次调用（重试）失败，此后成功 → 第一批 failed，第二批 loaded
        loader._embedding_service.fail_first_n = 3
        r = loader.load()
        assert r["success"] is False
        assert r["records_loaded"] == 2 and r["records_failed"] == 2
        assert r["replacement_started"] is True
        assert loader._milvus_client.delete_calls == []

    def test_missing_question_embedding_counted_failed(self, tmp_path):
        loader = _make_loader(tmp_path)
        loader.validate()
        loader._embedding_service.missing = {"总订单数？"}
        r = loader.load()
        assert r["success"] is False
        assert r["records_failed"] == 1 and r["records_loaded"] == 1
        assert loader._milvus_client.delete_calls == []

    def test_upsert_confirm_shortfall_splits_counts(self, tmp_path):
        loader = _make_loader(tmp_path, batch_size=2)
        loader.validate()
        loader._milvus_client.upsert_return_shortfall = 1
        r = loader.load()
        assert r["success"] is False
        assert r["records_loaded"] == 1 and r["records_unconfirmed"] == 1
        assert r["records_failed"] == 0
        assert r["replacement_started"] is True
        assert loader._milvus_client.delete_calls == []

    def test_rerun_same_data_idempotent_ids(self, tmp_path):
        loader = _make_loader(tmp_path)
        loader.validate()
        loader.load()
        ids1 = [r["example_id"] for r in loader._milvus_client.upsert_calls[0]]
        loader2 = _make_loader(tmp_path)
        loader2.validate()
        loader2.load()
        ids2 = [r["example_id"] for r in loader2._milvus_client.upsert_calls[0]]
        assert ids1 == ids2


# ---------------------------------------------------------------------------
# A12：字节口径的超长跳过（先于向量化）
# ---------------------------------------------------------------------------


class TestByteCaliber:
    def test_oversize_question_sql_skipped_before_embedding(self, tmp_path):
        # 5500 个汉字 ≈ 16500 字节 > 16384；字符数 5500+ < 16000 旧口径放行
        big_q = "问" * 5500
        loader = _make_loader(
            tmp_path,
            files={
                "a.json": [
                    {"question": big_q, "sql": "SELECT 1;", "domain": ""},
                    {"question": "正常", "sql": "SELECT 2;", "domain": ""},
                ]
            },
        )
        loader.validate()
        r = loader.load()
        assert r["success"] is True  # 数据类跳过不影响 success
        assert r["records_skipped"] == 1 and r["records_loaded"] == 1
        assert r["total"] == 2
        embedded_texts = [t for call in loader._embedding_service.calls for t in call]
        assert big_q not in embedded_texts  # 跳过项不进 get_embeddings
        rows = loader._milvus_client.upsert_calls[0]
        assert len(rows) == 1 and rows[0]["domain"] == ""

    def test_oversize_domain_skipped(self, tmp_path):
        loader = _make_loader(
            tmp_path,
            files={"a.json": [{"question": "Q", "sql": "S", "domain": "域" * 100}]},
        )
        loader.validate()
        r = loader.load()
        assert r["records_skipped"] == 1 and r["records_loaded"] == 0
        assert r["success"] is True


# ---------------------------------------------------------------------------
# A13：input 失败
# ---------------------------------------------------------------------------


class TestInputFailures:
    def test_bad_json(self, tmp_path):
        d = tmp_path / "sql-rag"
        d.mkdir()
        (d / "a.json").write_text("{not json", encoding="utf-8")
        loader = _make_loader(tmp_path, input_dir=d)
        loader.validate()
        r = loader.load()
        assert r["success"] is False
        assert r["error_kind"] == "input"
        assert r["error_file"] == "a.json"
        assert r["error_summary"]
        assert loader._milvus_client.upsert_calls == []

    def test_non_array_json(self, tmp_path):
        d = tmp_path / "sql-rag"
        d.mkdir()
        (d / "a.json").write_text('{"question": "x"}', encoding="utf-8")
        loader = _make_loader(tmp_path, input_dir=d)
        loader.validate()
        r = loader.load()
        assert r["error_kind"] == "input" and r["success"] is False

    def test_item_missing_question(self, tmp_path):
        d = tmp_path / "sql-rag"
        d.mkdir()
        _write_pairs(d / "a.json", [{"sql": "SELECT 1;", "domain": ""}])
        loader = _make_loader(tmp_path, input_dir=d)
        loader.validate()
        r = loader.load()
        assert r["error_kind"] == "input" and r["error_file"] == "a.json"

    def test_merged_zero_pairs(self, tmp_path):
        d = tmp_path / "sql-rag"
        d.mkdir()
        _write_pairs(d / "a.json", [])
        loader = _make_loader(tmp_path, input_dir=d)
        loader.validate()
        r = loader.load()
        assert r["error_kind"] == "input" and r["success"] is False

    def test_unreadable_dir_is_input_kind(self, tmp_path, monkeypatch):
        """目录枚举 OSError 归 input（表②语义），不落 catch-all 表⑥。"""
        from metaweave.core.sql_rag import loader as loader_mod

        d = tmp_path / "sql-rag"
        d.mkdir()
        _write_pairs(d / "a.json", DEFAULT_PAIRS)

        def boom(_dir):
            raise PermissionError("EACCES")

        loader = _make_loader(tmp_path, input_dir=d)
        loader.validate()
        monkeypatch.setattr(loader_mod, "collect_sql_example_input_files", boom)
        r = loader.load()
        assert r["success"] is False
        assert r["error_kind"] == "input"
        assert "EACCES" in r["error_summary"]
        assert loader._milvus_client.upsert_calls == []

    def test_validate_false_on_unreadable_dir(self, tmp_path, monkeypatch):
        from metaweave.core.sql_rag import loader as loader_mod

        d = tmp_path / "sql-rag"
        d.mkdir()
        _write_pairs(d / "a.json", DEFAULT_PAIRS)

        def boom(_dir):
            raise PermissionError("EACCES")

        monkeypatch.setattr(loader_mod, "collect_sql_example_input_files", boom)
        loader = _make_loader(tmp_path, input_dir=d)
        assert loader.validate() is False

    def test_domain_non_string_defaults_empty(self, tmp_path):
        loader = _make_loader(
            tmp_path,
            files={"a.json": [{"question": "Q", "sql": "S", "domain": 123}]},
        )
        loader.validate()
        r = loader.load()
        assert r["success"] is True
        assert loader._milvus_client.upsert_calls[0][0]["domain"] == ""


# ---------------------------------------------------------------------------
# A14：schema_mismatch
# ---------------------------------------------------------------------------


class TestSchemaMismatch:
    def test_ensure_collection_mismatch(self, tmp_path):
        loader = _make_loader(tmp_path)
        loader.validate()
        loader._milvus_client.ensure_raise = True
        r = loader.load()
        assert r["success"] is False
        assert r["error_kind"] == "schema_mismatch"
        assert r["replacement_started"] is False
        milvus = loader._milvus_client
        assert milvus.upsert_calls == [] and milvus.delete_calls == []
        assert r["total"] == 2 and r["records_loaded"] == 0  # 等式不适用该路径

    def test_oversize_before_mismatch_counts_skipped(self, tmp_path):
        """长度预检先于集合核验：超长样例已计入 skipped（审核修订 27 口径）。"""
        big_q = "问" * 5500
        loader = _make_loader(
            tmp_path,
            files={
                "a.json": [
                    {"question": big_q, "sql": "SELECT 1;", "domain": ""},
                    {"question": "正常", "sql": "SELECT 2;", "domain": ""},
                ]
            },
        )
        loader.validate()
        loader._milvus_client.ensure_raise = True
        r = loader.load()
        assert r["error_kind"] == "schema_mismatch"
        assert r["total"] == 2
        assert r["records_skipped"] == 1
        assert (
            r["records_loaded"] == 0
            and r["records_failed"] == 0
            and r["records_unconfirmed"] == 0
        )


# ---------------------------------------------------------------------------
# A19：cleanup 与未分类兜底（loader 侧）
# ---------------------------------------------------------------------------


class TestCleanupAndCatchAll:
    def test_delete_failure_classified_cleanup(self, tmp_path):
        loader = _make_loader(tmp_path)
        loader.validate()
        loader._milvus_client.delete_raise = True
        r = loader.load()
        assert r["success"] is False
        assert r["error_kind"] == "cleanup"
        assert r["replacement_started"] is True
        assert r["records_loaded"] == 2  # 写入全部成功
        milvus = loader._milvus_client
        assert milvus.delete_calls == []  # 抛错未完成
        assert len(milvus.upsert_calls) == 1  # 已写入保留、不重试删除

    def test_all_skipped_delete_timeout_revokes_current(self, tmp_path):
        """全跳过零 upsert + 删旧超时：删旧前置位（审核修订 29），不误保留旧 current。"""

        loader = _make_loader(
            tmp_path,
            files={
                "a.json": [{"question": "问" * 5500, "sql": "SELECT 1;", "domain": ""}]
            },
        )
        loader.validate()
        loader._milvus_client.delete_raise = True
        r = loader.load()
        assert r["success"] is False
        assert r["error_kind"] == "cleanup"
        assert r["replacement_started"] is True  # 删旧调用前已置位
        assert r["records_loaded"] == 0 and r["records_skipped"] == 1
        milvus = loader._milvus_client
        assert milvus.upsert_calls == [] and milvus.delete_calls == []

    def test_ensure_timeout_is_not_schema_mismatch(self, tmp_path):
        """ensure_collection 的普通故障（如加载超时）不归 schema_mismatch（审核修订 29）。"""

        loader = _make_loader(tmp_path)
        loader.validate()
        loader._milvus_client.ensure_exc = TimeoutError("collection load timeout")
        r = loader.load()
        assert r["success"] is False
        assert r["error_kind"] is None  # 落 catch-all，不误导删集合
        assert "collection load timeout" in r["error_summary"]
        milvus = loader._milvus_client
        assert milvus.upsert_calls == [] and milvus.delete_calls == []

    def test_unclassified_exception_keeps_none(self, tmp_path):
        loader = _make_loader(tmp_path)
        loader.validate()
        with patch.object(
            SQLExampleLoader, "_build_schema", side_effect=RuntimeError("boom")
        ):
            r = loader.load()
        assert r["success"] is False
        assert r["error_kind"] is None
        assert "boom" in r["error_summary"]
        assert r["replacement_started"] is False

    def test_mixed_failed_then_unclassified(self, tmp_path):
        loader = _make_loader(
            tmp_path,
            files={
                "a.json": [
                    {"question": f"Q{i}", "sql": "S", "domain": ""} for i in range(2)
                ],
                "b.json": [
                    {"question": f"P{i}", "sql": "S", "domain": ""} for i in range(2)
                ],
            },
            batch_size=2,
        )
        loader.validate()
        loader._embedding_service.fail_first_n = 3  # 第一批向量化失败 → records_failed
        original = SQLExampleLoader.compute_example_id

        def _cid(sid, question, sql):
            if question == "P0":
                raise RuntimeError("unexpected mid-flight")
            return original(sid, question, sql)

        with patch.object(SQLExampleLoader, "compute_example_id", staticmethod(_cid)):
            r = loader.load()
        assert r["success"] is False
        assert r["records_failed"] == 2
        assert r["error_kind"] is None and "unexpected mid-flight" in r["error_summary"]


# ---------------------------------------------------------------------------
# A20：batch_size 校验
# ---------------------------------------------------------------------------


class TestBatchSizeValidation:
    @pytest.mark.parametrize("bad", [-1, 0, True, 50.5, "50"])
    def test_validate_rejects_bad_values(self, tmp_path, bad):
        loader = _make_loader(tmp_path, batch_size=bad)
        assert not loader.validate()

    @pytest.mark.parametrize("good", [10, 50])
    def test_validate_accepts_positive_int(self, tmp_path, good):
        loader = _make_loader(tmp_path, batch_size=good)
        assert loader.validate()

    def test_default_when_absent(self, tmp_path):
        meta_path = _write_metadata_config(tmp_path)
        d = tmp_path / "sql-rag"
        d.mkdir()
        _write_pairs(d / "a.json", DEFAULT_PAIRS)
        loader = SQLExampleLoader(
            {
                "metadata_config_file": str(meta_path),
                "sql_loader": {
                    "input_dir": str(d),
                    "collection_name": "c",
                    "options": {},
                },
            },
            milvus_client_cls=FakeMilvusClient,
            embedding_service_cls=FakeEmbeddingService,
            source_id=SOURCE_ID,
            publish_id=PUBLISH_ID,
        )
        assert loader.validate()

    def test_direct_load_negative_no_write_no_delete(self, tmp_path):
        loader = _make_loader(tmp_path, batch_size=-1)
        # 绕过 validate 直调 load()：防御性复查生效
        loader._embedding_service = FakeEmbeddingService({})
        loader._milvus_client = FakeMilvusClient({})
        loader._metadata_config = {}
        r = loader.load()
        assert r["success"] is False
        assert r["error_kind"] is None
        assert "batch_size" in r["error_summary"]
        assert r["error_file"] is None
        milvus = loader._milvus_client
        assert milvus.upsert_calls == [] and milvus.delete_calls == []

    def test_direct_load_empty_input_dir_controlled_failure(self, tmp_path):
        """空 input_dir 直调 load()：受控失败，不去枚举项目根（行为随环境漂移）。"""
        meta_path = _write_metadata_config(tmp_path)
        loader = SQLExampleLoader(
            {
                "metadata_config_file": str(meta_path),
                "sql_loader": {"collection_name": "c"},
            },
            milvus_client_cls=FakeMilvusClient,
            embedding_service_cls=FakeEmbeddingService,
            source_id=SOURCE_ID,
            publish_id=PUBLISH_ID,
        )
        assert loader.input_dir == ""
        loader._embedding_service = FakeEmbeddingService({})
        loader._milvus_client = FakeMilvusClient({})
        loader._metadata_config = {}
        r = loader.load()
        assert r["success"] is False
        assert r["error_kind"] is None
        assert "input_dir" in r["error_summary"]
        milvus = loader._milvus_client
        assert milvus.upsert_calls == [] and milvus.delete_calls == []


# ---------------------------------------------------------------------------
# close()（§7.4）
# ---------------------------------------------------------------------------


class TestClose:
    def test_close_without_client_ok_and_repeatable(self, tmp_path):
        loader = _make_loader(tmp_path)
        loader.close()
        loader.close()

    def test_close_disconnects_and_swallows_errors(self, tmp_path):
        loader = _make_loader(tmp_path)
        loader.validate()
        loader._milvus_client.close = MagicMock(side_effect=RuntimeError("x"))
        loader.close()  # 仅告警不抛
        loader._milvus_client.close.assert_called_once()
