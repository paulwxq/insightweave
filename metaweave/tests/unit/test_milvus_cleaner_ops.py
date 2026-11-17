"""MilvusCleanerOps 生产适配器：按真实七项 ``_lazy_import_milvus`` 结构验证。

真实返回为 ``(connections, db, FieldSchema, CollectionSchema, Collection,
DataType, utility)``。此前按五项拆包导致 ValueError，且 Collection 位置
错误；本文件用哨兵对象锁住各方法取用的位置、连接 alias 与过滤表达式。
不连接真实 Milvus。
"""

from types import SimpleNamespace

from backend.deletion.cleaners import MilvusCleanerOps

SEVEN = [SimpleNamespace(name=i) for i in range(7)]
CONNECTIONS, DB, FIELD_SCHEMA, COLLECTION_SCHEMA, COLLECTION, DATATYPE, UTILITY = SEVEN


class FakeCollectionInstance:
    instances = []

    def __init__(self, name, using):
        self.name = name
        self.using = using
        self.delete_calls = []
        self.query_calls = []
        FakeCollectionInstance.instances.append(self)

    def delete(self, expr):
        self.delete_calls.append(expr)
        return SimpleNamespace(delete_count=7)

    def query(self, *, expr, output_fields):
        self.query_calls.append((expr, output_fields))
        return [{"count(*)": 3}]


class FakeUtility:
    calls = []

    @staticmethod
    def has_collection(name, using):
        FakeUtility.calls.append((name, using))
        return name == "known"


def _ops() -> MilvusCleanerOps:
    ops = MilvusCleanerOps.__new__(MilvusCleanerOps)
    ops._client = SimpleNamespace(alias="alias-1")
    ops._lazy = lambda: (
        CONNECTIONS,
        DB,
        FIELD_SCHEMA,
        COLLECTION_SCHEMA,
        FakeCollectionInstance,
        DATATYPE,
        FakeUtility,
    )
    return ops


def setup_function(_fn):
    FakeCollectionInstance.instances = []
    FakeUtility.calls = []


def test_has_collection_uses_seventh_item_and_alias():
    assert _ops().has_collection("known") is True
    assert _ops().has_collection("unknown") is False
    assert FakeUtility.calls == [("known", "alias-1"), ("unknown", "alias-1")]


def test_delete_by_expr_uses_fifth_item_collection_and_expr():
    deleted = _ops().delete_by_expr("dim_value_embeddings", 'source_id == "db_src_1"')
    assert deleted == 7
    (instance,) = FakeCollectionInstance.instances
    assert instance.name == "dim_value_embeddings"
    assert instance.using == "alias-1"
    assert instance.delete_calls == ['source_id == "db_src_1"']


def test_count_by_expr_counts_remaining_rows():
    remaining = _ops().count_by_expr("dim_value_embeddings", 'source_id == "db_src_1"')
    assert remaining == 3
    (instance,) = FakeCollectionInstance.instances
    assert instance.query_calls == [('source_id == "db_src_1"', ["count(*)"])]


def test_count_by_expr_empty_result_is_zero():
    ops = _ops()

    class EmptyCollection(FakeCollectionInstance):
        def query(self, *, expr, output_fields):
            return []

    ops._lazy = lambda: (
        CONNECTIONS,
        DB,
        FIELD_SCHEMA,
        COLLECTION_SCHEMA,
        EmptyCollection,
        DATATYPE,
        FakeUtility,
    )
    EmptyCollection("dim_value_embeddings", "alias-1")
    assert ops.count_by_expr("dim_value_embeddings", "expr") == 0
