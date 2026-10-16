"""doc 22.2 任务 1：dim_value loader 集合 schema 与加载行为改造的单元测试。

覆盖 §11 分配到 loader 层的验收：A01（schema/index_field_name）、A02（col_id/upsert）、
A03（按源替换与 replacement_started 前置）、A15（注入内容防御性校验）、A18（结果规则
与计数拆分）、A19（UTF-8 字节硬边界）、A24（schema_mismatch）、A26（零值成功替换）、
A27（源库查询失败策略）。
"""

import inspect
import logging
from pathlib import Path

import pytest
import yaml

from metaweave.core.dim_value.models import DimTableConfig, DimTablesConfig
from metaweave.core.loaders.dim_value_loader import ConfigurationError, DimValueLoader

SOURCE_ID = "11111111-1111-1111-1111-111111111111"
PUBLISH_ID = "22222222-2222-2222-2222-222222222222"
COLLECTION = "dim_value_embeddings"


class FakeMilvusClient:
    """记录调用协议；可注入 ensure 异常、upsert 异常、确认数不足。"""

    ensure_error: BaseException | None = None
    upsert_error: BaseException | None = None
    upsert_confirm: int | None = None  # None = 全部确认；int = 指定确认数

    def __init__(self, config):
        self.config = config
        self.ensure_calls = []
        self.upserts = []
        self.deletes = []
        self.closed = 0

    def test_connection(self) -> bool:
        return True

    def connect(self):
        return None

    def ensure_collection(
        self,
        collection_name,
        schema,
        index_params,
        clean=False,
        validate_schema=False,
        index_field_name="embedding",
    ):
        if self.ensure_error is not None:
            raise self.ensure_error
        self.ensure_calls.append(
            {
                "collection": collection_name,
                "schema": schema,
                "index_params": index_params,
                "clean": clean,
                "validate_schema": validate_schema,
                "index_field_name": index_field_name,
            }
        )

    def upsert_batch(self, collection_name, data):
        if self.upsert_error is not None:
            raise self.upsert_error
        self.upserts.append(list(data))
        if self.upsert_confirm is None:
            return len(data)
        return self.upsert_confirm

    def delete_by_expr(self, collection_name, expr):
        self.deletes.append(expr)
        return 0

    def close(self):
        self.closed += 1


class FakeEmbeddingService:
    def __init__(self, cfg):
        self.cfg = cfg
        self.fail_batches = 0  # 前 N 次调用抛异常
        self.missing = set()  # 这些值不返回向量

    def get_embeddings(self, texts):
        if self.fail_batches > 0:
            self.fail_batches -= 1
            raise RuntimeError("embedding api 限流")
        return {t: [0.1, 0.2] for t in texts if t not in self.missing}


class FakePGManager:
    def __init__(self, cfg=None):
        self.cfg = cfg

    def test_connection(self) -> bool:
        return True

    def get_connection(self):  # pragma: no cover - 测试中不会调用
        raise RuntimeError("should be patched")


def _metadata_config(tmp_path: Path) -> Path:
    path = tmp_path / "metadata_config.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "database": {
                    "host": "localhost",
                    "port": 5432,
                    "database": "db",
                    "user": "u",
                    "password": "p",
                },
                "embedding": {
                    "active": "qwen",
                    "providers": {
                        "qwen": {
                            "model": "text-embedding-v3",
                            "api_key": "k",
                            "dimensions": 1024,
                        }
                    },
                },
                "vector_database": {
                    "active": "milvus",
                    "providers": {
                        "milvus": {
                            "host": "localhost",
                            "port": 19530,
                            "database": "nl2sql",
                        }
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    return path


def _loader_config(tmp_path: Path, options=None) -> dict:
    metadata_path = _metadata_config(tmp_path)
    # config_file 指向不存在的文件：注入路径不得访问它（A20 loader 侧）
    missing_yaml = tmp_path / "dim_tables.yaml"
    return {
        "dim_loader": {
            "config_file": str(missing_yaml),
            "collection_name": COLLECTION,
            "options": options or {},
        },
        "metadata_config_file": str(metadata_path),
    }


def _injected(tables: dict) -> DimTablesConfig:
    cfg_tables = {}
    for full_name, col in tables.items():
        schema, table = full_name.split(".", 1)
        cfg_tables[full_name] = DimTableConfig(
            schema=schema, table=table, embedding_col=col
        )
    return DimTablesConfig(database="db", tables=cfg_tables)


def _make_loader(tmp_path, tables: dict, *, options=None, embedding=None) -> tuple:
    config = _loader_config(tmp_path, options)
    embedding = embedding if embedding is not None else FakeEmbeddingService({})
    loader = DimValueLoader(
        config,
        milvus_client_cls=FakeMilvusClient,
        pg_manager_cls=FakePGManager,
        embedding_service_cls=lambda cfg: embedding,
        source_id=SOURCE_ID,
        publish_id=PUBLISH_ID,
        dim_tables=_injected(tables),
    )
    return loader, embedding


def _patch_fetch(loader, rows_by_table: dict, calls: list | None = None):
    def fake_fetch(schema, table, col):
        if calls is not None:
            calls.append(f"{schema}.{table}.{col}")
        return rows_by_table.get(f"{schema}.{table}", [])

    loader._fetch_table_data = fake_fetch


# ---- A01 集合 schema 与 index_field_name ----


def test_a01_schema_fields_and_index(tmp_path):
    loader, _ = _make_loader(tmp_path, {"public.t1": "c1"})
    _patch_fetch(loader, {"public.t1": [{"col_value": "v1"}]})
    result = loader.load(clean=False)

    assert result["success"] is True
    client = loader._milvus_client
    assert len(client.ensure_calls) == 1
    call = client.ensure_calls[0]
    assert call["collection"] == COLLECTION
    assert call["clean"] is False
    assert call["validate_schema"] is True
    assert call["index_field_name"] == "col_value_embedding"
    assert call["index_params"]["index_type"] == "HNSW"
    assert call["index_params"]["metric_type"] == "COSINE"

    fields = {f.name: f for f in call["schema"].fields}
    assert set(fields) == {
        "col_id",
        "source_id",
        "publish_id",
        "table_name",
        "col_name",
        "col_value",
        "col_value_embedding",
        "updated_at",
    }
    assert fields["col_id"].is_primary is True
    assert fields["col_id"].max_length == 2048
    assert fields["source_id"].max_length == 36
    assert fields["publish_id"].max_length == 36
    assert fields["table_name"].max_length == 256
    assert fields["col_name"].max_length == 128
    assert fields["col_value"].max_length == 1024


def test_a01_milvus_client_index_field_default():
    from services.vector_db import milvus_client as mc

    sig = inspect.signature(mc.MilvusClient.ensure_collection)
    assert sig.parameters["index_field_name"].default == "embedding"


# ---- A02/A03 col_id、幂等与按源替换 ----


def test_a02_col_id_and_upsert_fields(tmp_path):
    loader, _ = _make_loader(tmp_path, {"public.t1": "c1"})
    _patch_fetch(
        loader,
        {"public.t1": [{"col_value": "v1"}, {"col_value": "v1"}, {"col_value": "v2"}]},
    )
    result = loader.load(clean=False)

    assert result["success"] is True
    assert result["records_loaded"] == 2
    assert result["records_skipped"] == 1  # 列内重复

    client = loader._milvus_client
    rows = [r for batch in client.upserts for r in batch]
    by_value = {r["col_value"]: r for r in rows}
    assert set(by_value) == {"v1", "v2"}
    row = by_value["v1"]
    assert row["source_id"] == SOURCE_ID
    assert row["publish_id"] == PUBLISH_ID
    assert row["table_name"] == "db.public.t1"
    assert row["col_name"] == "c1"
    assert row["col_id"] == f"{SOURCE_ID}:db.public.t1:c1:v1"
    assert row["col_value_embedding"] == [0.1, 0.2]
    assert "updated_at" in row


def test_a02_upsert_idempotent_on_reload(tmp_path):
    rows = [{"col_value": "v1"}]
    loader1, _ = _make_loader(tmp_path, {"public.t1": "c1"})
    _patch_fetch(loader1, {"public.t1": rows})
    assert loader1.load(clean=False)["success"] is True
    first = loader1._milvus_client.upserts[0][0]["col_id"]

    loader2, _ = _make_loader(tmp_path, {"public.t1": "c1"})
    _patch_fetch(loader2, {"public.t1": rows})
    loader2.load(clean=False)
    second = loader2._milvus_client.upserts[0][0]["col_id"]
    # 两次发布同一数据 → 相同主键，Milvus upsert 幂等（记录不翻倍的前提）
    assert first == second


def test_a03_full_success_deletes_old(tmp_path):
    loader, _ = _make_loader(tmp_path, {"public.t1": "c1"})
    _patch_fetch(loader, {"public.t1": [{"col_value": "v1"}]})
    result = loader.load(clean=False)

    assert result["success"] is True
    assert result["replacement_started"] is True
    client = loader._milvus_client
    assert client.deletes == [
        f'source_id == "{SOURCE_ID}" and publish_id != "{PUBLISH_ID}"'
    ]


def test_a03_replacement_started_set_before_upsert_call(tmp_path):
    loader, _ = _make_loader(tmp_path, {"public.t1": "c1"})
    _patch_fetch(loader, {"public.t1": [{"col_value": "v1"}]})
    loader._milvus_client  # 尚未构造——通过配置注入 upsert 异常
    # 在 load 构造 client 后注入异常：用类属性预置
    FakeMilvusClient.upsert_error = RuntimeError("milvus write timeout")
    try:
        result = loader.load(clean=False)
    finally:
        FakeMilvusClient.upsert_error = None

    assert result["success"] is False
    assert result["replacement_started"] is True  # 调用前置位
    assert result["records_unconfirmed"] == 1
    assert result["records_loaded"] == 0
    assert loader._milvus_client.deletes == []  # 不删旧
    assert result["error_kind"] == "embedding"  # 未确认场景由 publish 层表③处理


# ---- A18 结果规则 ----


def test_a18a_all_embedding_batches_fail(tmp_path):
    embedding = FakeEmbeddingService({})
    embedding.fail_batches = 99
    loader, _ = _make_loader(
        tmp_path, {"public.t1": "c1"}, options={"batch_size": 1}, embedding=embedding
    )
    _patch_fetch(loader, {"public.t1": [{"col_value": "v1"}, {"col_value": "v2"}]})
    result = loader.load(clean=False)

    assert result["success"] is False
    assert result["replacement_started"] is False  # 无任何 upsert 调用
    assert result["records_failed"] == 2
    assert result["records_loaded"] == 0
    assert loader._milvus_client.upserts == []
    assert loader._milvus_client.deletes == []


def test_a18b_partial_batches_success(tmp_path):
    embedding = FakeEmbeddingService({})
    embedding.fail_batches = 3  # 第一批 3 次尝试全部耗尽（最终失败），第二批成功
    loader, _ = _make_loader(
        tmp_path, {"public.t1": "c1"}, options={"batch_size": 1}, embedding=embedding
    )
    _patch_fetch(loader, {"public.t1": [{"col_value": "v1"}, {"col_value": "v2"}]})
    result = loader.load(clean=False)

    assert result["success"] is False
    assert result["replacement_started"] is True  # 第二批已 upsert
    assert result["records_loaded"] == 1
    assert result["records_failed"] == 1
    assert loader._milvus_client.deletes == []


def test_a18_write_underconfirm_split_counts(tmp_path):
    loader, _ = _make_loader(tmp_path, {"public.t1": "c1"})
    _patch_fetch(loader, {"public.t1": [{"col_value": f"v{i}"} for i in range(100)]})
    loader_cls = loader.milvus_client_cls
    original_init = loader_cls.__init__

    def init_with_confirm(self, config):
        original_init(self, config)
        self.upsert_confirm = 97

    loader.milvus_client_cls = type(
        "ConfirmClient", (FakeMilvusClient,), {"__init__": init_with_confirm}
    )
    try:
        result = loader.load(clean=False)
    finally:
        loader.milvus_client_cls = loader_cls

    assert result["success"] is False
    assert result["records_loaded"] == 97
    assert result["records_unconfirmed"] == 3
    assert result["records_failed"] == 0
    assert result["replacement_started"] is True
    assert loader._milvus_client.deletes == []


def test_a18_single_missing_vector(tmp_path):
    embedding = FakeEmbeddingService({})
    embedding.missing = {"v2"}
    loader, _ = _make_loader(tmp_path, {"public.t1": "c1"}, embedding=embedding)
    _patch_fetch(loader, {"public.t1": [{"col_value": "v1"}, {"col_value": "v2"}]})
    result = loader.load(clean=False)

    assert result["success"] is False
    assert result["error_kind"] == "embedding"
    assert result["records_loaded"] == 1
    assert result["records_failed"] == 1
    assert loader._milvus_client.deletes == []


def test_a18_counts_partition(tmp_path):
    loader, _ = _make_loader(tmp_path, {"public.t1": "c1"})
    _patch_fetch(
        loader,
        {
            "public.t1": [
                {"col_value": "v1"},
                {"col_value": "  "},  # 空白 → 数据类跳过
                {"col_value": "v1"},  # 重复 → 数据类跳过
                {"col_value": "v2"},
            ]
        },
    )
    result = loader.load(clean=False)

    assert result["success"] is True
    assert result["records_loaded"] == 2
    assert result["records_skipped"] == 2
    assert result["records_failed"] == 0
    assert result["records_unconfirmed"] == 0
    total = (
        result["records_loaded"]
        + result["records_failed"]
        + result["records_unconfirmed"]
        + result["records_skipped"]
    )
    assert total == 4  # = 实际读取记录数


# ---- A19 字节口径 ----


def test_a19_chinese_value_truncated_by_bytes(tmp_path, caplog):
    loader, _ = _make_loader(tmp_path, {"public.t1": "c1"})
    long_value = "汉" * 600  # 1800 字节
    _patch_fetch(loader, {"public.t1": [{"col_value": long_value}]})
    with caplog.at_level(logging.WARNING, logger="metaweave.dim_value.loader"):
        result = loader.load(clean=False)

    assert result["success"] is True
    rows = [r for b in loader._milvus_client.upserts for r in b]
    stored = rows[0]["col_value"]
    assert len(stored.encode("utf-8")) <= 1024
    stored.encode("utf-8").decode("utf-8")  # 不撕裂多字节字符
    assert rows[0]["col_id"].endswith(stored)  # col_id 尾段与 col_value 一致
    assert len(rows[0]["col_id"].encode("utf-8")) <= 1317
    assert any("截断" in r.message for r in caplog.records)


@pytest.mark.parametrize(
    "options",
    [{"truncate_long_text": False}, {"max_text_length": 2048}],
    ids=["truncate-off", "max-2048"],
)
def test_a19_config_cannot_break_boundary(tmp_path, options):
    loader, _ = _make_loader(tmp_path, {"public.t1": "c1"}, options=options)
    _patch_fetch(loader, {"public.t1": [{"col_value": "汉" * 600}]})
    result = loader.load(clean=False)

    assert result["success"] is True
    rows = [r for b in loader._milvus_client.upserts for r in b]
    assert len(rows[0]["col_value"].encode("utf-8")) <= 1024


def test_a19_truncation_merges_same_prefix(tmp_path):
    loader, _ = _make_loader(tmp_path, {"public.t1": "c1"})
    a = "A" * 1024 + "B"
    b = "A" * 1024 + "C"  # 前 1024 字节相同 → 截断后同文本
    _patch_fetch(loader, {"public.t1": [{"col_value": a}, {"col_value": b}]})
    result = loader.load(clean=False)

    assert result["success"] is True
    rows = [r for batch in loader._milvus_client.upserts for r in batch]
    assert len(rows) == 1  # 合并为一条
    assert result["records_skipped"] == 1


# ---- A24 集合结构不符 ----


def test_a24_schema_mismatch(tmp_path):
    loader, _ = _make_loader(tmp_path, {"public.t1": "c1"})
    _patch_fetch(loader, {"public.t1": [{"col_value": "v1"}]})
    FakeMilvusClient.ensure_error = ValueError("已有集合结构与期望不一致")
    try:
        result = loader.load(clean=False)
    finally:
        FakeMilvusClient.ensure_error = None

    assert result["success"] is False
    assert result["error_kind"] == "schema_mismatch"
    assert result["replacement_started"] is False
    assert loader._milvus_client.upserts == []
    assert loader._milvus_client.deletes == []


# ---- A26 有表零值 ----


def test_a26_zero_values_success_replace(tmp_path):
    loader, _ = _make_loader(tmp_path, {"public.t1": "c1"})
    _patch_fetch(loader, {"public.t1": []})
    result = loader.load(clean=False)

    assert result["success"] is True
    assert result["records_loaded"] == 0
    assert result["replacement_started"] is True  # 删旧前确保置位
    assert loader._milvus_client.deletes == [
        f'source_id == "{SOURCE_ID}" and publish_id != "{PUBLISH_ID}"'
    ]


# ---- A27 源库查询失败 ----


def test_a27_first_table_query_fails(tmp_path):
    calls = []
    loader, _ = _make_loader(tmp_path, {"public.t1": "c1", "public.t2": "c2"})

    def failing_fetch(schema, table, col):
        calls.append(f"{schema}.{table}")
        if table == "t1":
            raise RuntimeError("relation does not exist")
        return [{"col_value": "v"}]

    loader._fetch_table_data = failing_fetch
    result = loader.load(clean=False)

    assert result["success"] is False
    assert result["error_kind"] == "source"
    assert result["failed_table"] == "public.t1"
    assert result["error_summary"]
    assert result["replacement_started"] is False
    assert calls == ["public.t1"]  # 后续表零查询
    assert loader._milvus_client.deletes == []


def test_a27_midway_table_query_fails(tmp_path):
    loader, _ = _make_loader(tmp_path, {"public.t1": "c1", "public.t2": "c2"})

    def fetch(schema, table, col):
        if table == "t1":
            return [{"col_value": "v1"}]
        raise RuntimeError("permission denied")

    loader._fetch_table_data = fetch
    result = loader.load(clean=False)

    assert result["success"] is False
    assert result["error_kind"] == "source"
    assert result["failed_table"] == "public.t2"
    assert result["replacement_started"] is True  # t1 已完整写入
    assert result["records_loaded"] == 1
    assert loader._milvus_client.deletes == []


# ---- A15 注入内容防御性校验 / A20 loader 侧 / close ----


def test_a15_injected_empty_col_fails(tmp_path):
    loader, _ = _make_loader(tmp_path, {"public.t1": None})
    _patch_fetch(loader, {})
    result = loader.load(clean=False)

    assert result["success"] is False
    assert result["error_kind"] == "input"
    assert result["replacement_started"] is False


def test_a20_injected_config_never_reads_file(tmp_path):
    loader, _ = _make_loader(tmp_path, {"public.t1": "c1"})
    _patch_fetch(loader, {"public.t1": [{"col_value": "v1"}]})
    # config_file 指向不存在的文件（_loader_config 已设）——注入路径不得触碰
    assert not (tmp_path / "dim_tables.yaml").exists()
    assert loader.validate() is True  # 注入下 validate 跳过存在性短路
    result = loader.load(clean=False)
    assert result["success"] is True


def test_source_id_publish_id_required(tmp_path):
    config = _loader_config(tmp_path)
    with pytest.raises(ConfigurationError):
        DimValueLoader(config)


def test_close_idempotent_and_disconnects(tmp_path):
    loader, _ = _make_loader(tmp_path, {"public.t1": "c1"})
    _patch_fetch(loader, {"public.t1": [{"col_value": "v1"}]})
    loader.close()  # 未构造 client：直接返回
    loader.load(clean=False)
    client = loader._milvus_client
    loader.close()
    loader.close()  # 可重复调用
    assert client.closed >= 1
