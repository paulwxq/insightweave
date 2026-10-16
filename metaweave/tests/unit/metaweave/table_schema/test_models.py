from metaweave.core.table_schema.models import (
    VARCHAR_LIMITS,
    ObjectType,
    SchemaObject,
    build_entity_id,
)

SOURCE = "b4a87275-7c37-4feb-bf1c-e5a8446c174f"
PUBLISH = "11111111-2222-3333-4444-555555555555"


def _column():
    return SchemaObject(
        object_type=ObjectType.COLUMN,
        object_id="orders.public.orders.order_id",
        parent_id="orders.public.orders",
        object_desc="订单ID",
        updated_at=1700000000,
    )


def test_to_milvus_dict_has_full_record_with_entity_id():
    row = _column().to_milvus_dict(
        embedding=[0.1, 0.2], source_id=SOURCE, publish_id=PUBLISH
    )

    assert row["entity_id"] == f"{SOURCE}:orders.public.orders.order_id"
    assert row["source_id"] == SOURCE
    assert row["publish_id"] == PUBLISH
    assert row["object_id"] == "orders.public.orders.order_id"
    assert set(row) == {
        "entity_id", "source_id", "publish_id", "object_id", "object_type",
        "parent_id", "object_desc", "embedding", "time_col_hint",
        "table_category", "updated_at",
    }
    assert row["time_col_hint"] == "" and row["table_category"] == ""


def test_same_object_id_in_two_sources_gets_different_entity_id():
    other = "99999999-7c37-4feb-bf1c-e5a8446c174f"

    assert build_entity_id(SOURCE, "a.b.c") != build_entity_id(other, "a.b.c")


def test_entity_id_without_source_falls_back_to_object_id():
    row = _column().to_milvus_dict(embedding=[0.1])

    assert row["entity_id"] == row["object_id"]
    assert row["source_id"] == "" and row["publish_id"] == ""


def test_varchar_limits_match_collection_definition():
    assert VARCHAR_LIMITS == {
        "entity_id": 512, "source_id": 36, "publish_id": 36, "object_id": 256,
        "object_type": 64, "parent_id": 256, "object_desc": 8192,
        "time_col_hint": 512, "table_category": 64,
    }
