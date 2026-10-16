"""在独立临时集合验证 Milvus 服务端 2.5.2 的整行 upsert 与按批次清理。

运行：.venv/bin/python scripts/probe_milvus_upsert_252.py
读取 configs/metadata_config.yaml / .env 中的 Milvus 连接，绝不修改正式集合。
"""

from __future__ import annotations

from uuid import uuid4

from pymilvus import Collection, CollectionSchema, DataType, FieldSchema, __version__, utility

from services.config_loader import ConfigLoader
from services.vector_db.milvus_client import MilvusClient


def _row(source_id: str, publish_id: str, object_id: str, description: str) -> dict:
    return {
        "entity_id": f"{source_id}:{object_id}",
        "source_id": source_id,
        "publish_id": publish_id,
        "object_id": object_id,
        "object_desc": description,
        "embedding": [1.0, 0.0],
    }


def _read_source(collection: Collection, source_id: str) -> dict[str, dict]:
    rows = collection.query(
        expr=f'source_id == "{source_id}"',
        output_fields=["entity_id", "object_id", "publish_id", "object_desc"],
        consistency_level="Strong",
    )
    result = {row["object_id"]: row for row in rows}
    if len(result) != len(rows):
        raise AssertionError("同一源下出现重复 object_id")
    return result


def _check_rows(rows: dict[str, dict], expected: dict[str, str]) -> None:
    actual = {object_id: row["publish_id"] for object_id, row in rows.items()}
    if actual != expected:
        raise AssertionError(f"批次不匹配：actual={actual} expected={expected}")


def main() -> None:
    config = ConfigLoader("configs/metadata_config.yaml").load()
    milvus_config = dict(config["vector_database"]["providers"]["milvus"])
    milvus_config["alias"] = f"mw_probe_{uuid4().hex}"
    client = MilvusClient(milvus_config)
    collection_name = f"mw_probe_upsert_{uuid4().hex}"
    collection = None

    try:
        client.connect()  # 只连接已存在的数据库，不自动建库。
        server_version = utility.get_server_version(using=client.alias)
        print(f"server={server_version} pymilvus={__version__}")
        if server_version.lstrip("v").split("-")[0] != "2.5.2":
            raise RuntimeError("服务端不是 2.5.2，未创建测试集合")

        schema = CollectionSchema(
            fields=[
                FieldSchema("entity_id", DataType.VARCHAR, is_primary=True, auto_id=False, max_length=512),
                FieldSchema("source_id", DataType.VARCHAR, max_length=36),
                FieldSchema("publish_id", DataType.VARCHAR, max_length=36),
                FieldSchema("object_id", DataType.VARCHAR, max_length=256),
                FieldSchema("object_desc", DataType.VARCHAR, max_length=8192),
                FieldSchema("embedding", DataType.FLOAT_VECTOR, dim=2),
            ],
            description="Temporary MetaWeave upsert probe",
        )
        collection = Collection(collection_name, schema=schema, using=client.alias)
        collection.create_index(
            "embedding",
            {"index_type": "HNSW", "metric_type": "COSINE", "params": {"M": 16, "efConstruction": 200}},
        )
        collection.load()

        source_a, source_b = str(uuid4()), str(uuid4())
        old, interrupted, current = str(uuid4()), str(uuid4()), str(uuid4())
        other = str(uuid4())

        original = [
            _row(source_a, old, "a", "old a"),
            _row(source_a, old, "b", "old b"),
            _row(source_a, old, "c", "old c"),
            _row(source_b, other, "a", "other source a"),
        ]
        assert client.insert_batch(collection_name, original) == len(original)
        _check_rows(_read_source(collection, source_a), {"a": old, "b": old, "c": old})
        _check_rows(_read_source(collection, source_b), {"a": other})
        print("PASS: 初始两源记录")

        # 第一批已成功，第二批制造维度错误：观察失败后旧记录是否仍完整。
        assert client.upsert_batch(collection_name, [_row(source_a, interrupted, "a", "new a")]) == 1
        invalid = _row(source_a, interrupted, "b", "bad dimension")
        invalid["embedding"] = [1.0, 0.0, 0.0]
        try:
            client.upsert_batch(collection_name, [invalid])
        except Exception as exc:  # noqa: BLE001 - 预期由客户端或服务端拒绝
            print(f"PASS: 第二批维度错误被拒绝 ({type(exc).__name__})")
        else:
            raise AssertionError("维度错误未被拒绝")
        _check_rows(_read_source(collection, source_a), {"a": interrupted, "b": old, "c": old})
        print("PASS: 写入失败后是新旧混合，旧批次并非完整保留")

        replacement = [
            _row(source_a, current, "a", "final a"),
            _row(source_a, current, "b", "final b"),
            _row(source_a, current, "d", "final d"),
        ]
        assert client.upsert_batch(collection_name, replacement) == len(replacement)
        _check_rows(
            _read_source(collection, source_a),
            {"a": current, "b": current, "c": old, "d": current},
        )
        print("PASS: 全部 upsert 后，已移除对象仍属于旧批次")

        collection.delete(
            expr=f'source_id == "{source_a}" and publish_id != "{current}"'
        )
        collection.flush()
        _check_rows(
            _read_source(collection, source_a),
            {"a": current, "b": current, "d": current},
        )
        _check_rows(_read_source(collection, source_b), {"a": other})
        print("PASS: 清理旧批次后仅保留本源新批次，另一源不变")
    finally:
        try:
            if client.connected and utility.has_collection(collection_name, using=client.alias):
                Collection(collection_name, using=client.alias).drop()
                if utility.has_collection(collection_name, using=client.alias):
                    raise AssertionError("临时测试集合清理失败")
                print("测试集合已清理")
        finally:
            client.close()


if __name__ == "__main__":
    main()
