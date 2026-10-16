"""doc 20.1 §10.3：受管加载的调用顺序与失败边界（假 Embedding / Milvus）。"""

import numpy as np
import pytest

from metaweave.core.loaders import table_schema_loader as loader_module
from metaweave.core.loaders.table_schema_loader import TableSchemaLoader
from tests.unit.metaweave.table_schema.test_prepare_managed_input import (  # noqa: F401
    FULL,
    NAME,
    SOURCE_ID,
    dirs,
    write_md,
)
from uuid import uuid4

JOB_ID = uuid4()
DIM = 4
METADATA_CONFIG = {
    "vector_database": {"active": "milvus", "providers": {"milvus": {"host": "fake"}}},
    "embedding": {"active": "x", "providers": {"x": {"dimensions": DIM}}},
}


class Env:
    def __init__(self):
        self.events = []
        self.upserts = []
        self.embed_calls = []
        self.upsert_returns = None  # 为 None 时返回输入条数
        self.embed_error = None
        self.embed_dim = DIM
        self.ensure_error = None
        self.delete_error = None
        self.ensure_kwargs = None


def build(dirs, env, publish_id=None, **options):
    class FakeEmbedding:
        def __init__(self, cfg):
            pass

        def get_embeddings(self, texts):
            env.events.append("embed")
            env.embed_calls.append(list(texts))
            if env.embed_error:
                raise env.embed_error
            return {t: np.full(env.embed_dim, 0.5, dtype=np.float64) for t in texts}

    class FakeMilvus:
        def __init__(self, cfg):
            pass

        def connect(self, create_database=False):
            env.events.append("connect")
            env.connect_create_database = create_database

        def test_connection(self, create_database=False):
            env.test_connection_create_database = create_database
            return True

        def ensure_collection(self, **kwargs):
            env.events.append("ensure")
            env.ensure_kwargs = kwargs
            if env.ensure_error:
                raise env.ensure_error

        def upsert_batch(self, name, data):
            env.events.append("upsert")
            env.upserts.append(list(data))
            if env.upsert_returns is not None:
                return env.upsert_returns.pop(0)
            return len(data)

        def delete_by_expr(self, name, expr):
            env.events.append("delete")
            env.delete_expr = expr
            if env.delete_error:
                raise env.delete_error
            return 3

        def insert_batch(self, name, data):  # pragma: no cover - 受管路径不得调用
            raise AssertionError("managed load must not insert")

        def close(self):
            env.events.append("close")

    cfg = {
        "table_schema_loader": {
            "collection_name": "schemas",
            "md_directory": str(dirs[1]),
            "json_llm_directory": str(dirs[0]),
            "source_id": str(SOURCE_ID),
            "job_id": str(JOB_ID),
            "options": options,
        }
    }
    return TableSchemaLoader(
        cfg,
        milvus_client_cls=FakeMilvus,
        embedding_service_cls=FakeEmbedding,
        metadata_config=METADATA_CONFIG,
        publish_id=publish_id,
    )


def test_validate_uses_create_database_connection_path(dirs):
    write_md(dirs[1], "- order_id (integer) - 订单ID\n- amount (numeric(10,2)) - 金额\n")
    env = Env()
    loader = build(dirs, env, str(uuid4()))
    loader.prepare_input()

    assert loader.validate() is True
    assert env.test_connection_create_database is True


def test_collection_schema_varchar_limits_match_varchar_limits(dirs):
    """集合定义的 VARCHAR 上限与预检用的 VARCHAR_LIMITS 对账，防止只改一处。"""
    from pymilvus import DataType

    from metaweave.core.table_schema import VARCHAR_LIMITS

    write_md(dirs[1], "- order_id (integer) - 订单ID\n- amount (numeric(10,2)) - 金额\n")
    env = Env()
    loader = build(dirs, env, str(uuid4()))
    loader.prepare_input()
    assert loader.load()["success"]

    varchar_fields = {
        f.name: f.params["max_length"]
        for f in env.ensure_kwargs["schema"].fields
        if f.dtype == DataType.VARCHAR
    }
    assert varchar_fields == VARCHAR_LIMITS


def test_order_records_and_cleanup(dirs):
    write_md(dirs[1], "- order_id (integer) - 订单ID\n- amount (numeric(10,2)) - 无注释\n")
    env = Env()
    publish_id = str(uuid4())
    loader = build(dirs, env, publish_id)
    loader.prepare_input()
    result = loader.load()

    assert result["success"], result["message"]
    assert env.events == ["connect", "ensure", "embed", "upsert", "delete"]
    # 首次加载走可创建 database 的连接路径（否则全新 Milvus 无法初始化）
    assert env.connect_create_database is True
    assert env.ensure_kwargs["clean"] is False and env.ensure_kwargs["validate_schema"] is True
    names = [f.name for f in env.ensure_kwargs["schema"].fields]
    assert names[0] == "entity_id" and len(names) == 11 and "job_id" not in names
    records = env.upserts[0]
    assert [r["object_id"] for r in records] == [NAME, f"{NAME}.order_id"]
    for r in records:
        assert r["source_id"] == str(SOURCE_ID) and r["publish_id"] == publish_id
        assert r["entity_id"] == f"{SOURCE_ID}:{r['object_id']}"
        assert isinstance(r["embedding"], list) and len(r["embedding"]) == DIM
    # 空描述字段不进入 Embedding 文本，且只统计在 empty_desc_columns，不计失败跳过
    assert all("无注释" != t for call in env.embed_calls for t in call)
    assert result["empty_desc_columns"] == [f"{NAME}.amount"]
    assert result["objects_skipped"] == 0 and result["objects_loaded"] == 2
    assert result["replacement_started"] is True
    assert env.delete_expr == (
        f'source_id == "{SOURCE_ID}" and publish_id != "{publish_id}"'
    )


def test_embedding_input_is_description_only(dirs):
    write_md(dirs[1], FULL)
    env = Env()
    loader = build(dirs, env)
    assert loader.load()["success"]
    texts = [t for call in env.embed_calls for t in call]
    assert "订单ID" in texts and "金额" in texts
    assert not any(str(SOURCE_ID) in t for t in texts)


def test_batches_and_numpy_vectors_stay_arrays_until_write(dirs):
    write_md(dirs[1], FULL)
    env = Env()
    loader = build(dirs, env, batch_size=1)
    loader.prepare_input()
    loader._embedding_service = loader.embedding_service_cls({})
    vectors, skipped = loader._embed_all([o for g in loader._prepared_objects for o in g])
    assert skipped == 0
    assert all(isinstance(v, np.ndarray) for v in vectors.values())
    env.events.clear()
    env.embed_calls.clear()
    result = loader.load()
    assert result["success"]
    # 三个对象按 batch_size=1 分三批 upsert；全部 embed 在首个 upsert 之前
    assert env.events.count("upsert") == 3
    assert env.events.index("upsert") > max(
        i for i, e in enumerate(env.events) if e == "embed"
    )


def test_skip_disabled_with_empty_desc_touches_no_service(dirs):
    write_md(dirs[1], "- order_id (integer) - 无注释\n- amount (numeric(10,2)) - 金额\n")
    env = Env()
    loader = build(dirs, env, skip_empty_desc=False)
    result = loader.load()
    assert not result["success"] and "order_id" in result["message"]
    assert env.events == [] and result["replacement_started"] is False


def test_all_columns_empty_loads_table_only(dirs):
    write_md(dirs[1], "- order_id (integer) -\n- amount (numeric(10,2)) - 无注释\n")
    env = Env()
    result = build(dirs, env).load()
    assert result["success"] and result["objects_loaded"] == 1
    assert len(result["empty_desc_columns"]) == 2


def test_include_columns_false_writes_table_only(dirs):
    write_md(dirs[1], FULL)
    env = Env()
    result = build(dirs, env, include_columns=False).load()
    assert result["success"] and [r["object_id"] for r in env.upserts[0]] == [NAME]


def test_schema_mismatch_fails_before_embedding(dirs):
    write_md(dirs[1], FULL)
    env = Env()
    env.ensure_error = ValueError("集合结构不符")
    result = build(dirs, env).load()
    assert not result["success"] and result["replacement_started"] is False
    assert env.events == ["connect", "ensure"]


def test_embedding_failure_before_any_upsert(dirs):
    write_md(dirs[1], FULL)
    env = Env()
    env.embed_error = RuntimeError("boom")
    result = build(dirs, env).load()
    assert not result["success"] and result["replacement_started"] is False
    assert "upsert" not in env.events and "delete" not in env.events


def test_dimension_mismatch_fails_before_upsert(dirs):
    write_md(dirs[1], FULL)
    env = Env()
    env.embed_dim = DIM + 1
    result = build(dirs, env).load()
    assert not result["success"] and "维度" in result["message"]
    assert result["replacement_started"] is False
    assert "upsert" not in env.events


def test_short_upsert_count_fails_without_cleanup(dirs):
    write_md(dirs[1], FULL)
    env = Env()
    env.upsert_returns = [1]  # 一批 3 条，仅返回 1
    result = build(dirs, env).load()
    assert not result["success"] and result["replacement_started"] is True
    assert "delete" not in env.events


def test_second_batch_failure_stops_and_skips_cleanup(dirs):
    write_md(dirs[1], FULL)
    env = Env()
    env.upsert_returns = [1, 0]
    result = build(dirs, env, batch_size=1).load()
    assert not result["success"] and result["replacement_started"] is True
    assert env.events.count("upsert") == 2 and "delete" not in env.events


def test_cleanup_failure_is_reported_as_failure(dirs):
    write_md(dirs[1], FULL)
    env = Env()
    env.delete_error = RuntimeError("delete failed")
    result = build(dirs, env).load()
    assert not result["success"] and result["replacement_started"] is True


def test_length_recheck_happens_before_embedding(dirs, monkeypatch):
    write_md(dirs[1], FULL)
    env = Env()

    def boom(record):
        raise ValueError("超限")

    monkeypatch.setattr(loader_module, "check_record_lengths", boom)
    result = build(dirs, env).load()
    assert not result["success"] and env.events == []


def test_clean_flag_is_ignored_for_managed_load(dirs):
    write_md(dirs[1], FULL)
    env = Env()
    assert build(dirs, env).load(clean=True)["success"]
    assert env.ensure_kwargs["clean"] is False and env.events[-1] == "delete"


def test_close_is_idempotent_and_swallows_errors(dirs):
    write_md(dirs[1], FULL)
    env = Env()
    loader = build(dirs, env)
    loader.close()  # 客户端未创建
    assert env.events == []
    loader.load()
    loader.close()
    loader.close()
    assert env.events.count("close") == 1

    env2 = Env()
    loader2 = build(dirs, env2)
    loader2.load()
    loader2._milvus_client.close = lambda: (_ for _ in ()).throw(RuntimeError("x"))
    loader2.close()  # 不抛出
    assert loader2._milvus_client is None
