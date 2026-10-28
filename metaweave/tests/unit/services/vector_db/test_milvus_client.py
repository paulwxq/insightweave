import pytest
from types import SimpleNamespace

import services.vector_db.milvus_client as milvus_module
from services.vector_db.milvus_client import MilvusClient


class _FakeField:
    def __init__(self, name, is_primary=False, auto_id=False):
        self.name = name
        self.is_primary = is_primary
        self.auto_id = auto_id


class _FakeCollection:
    registry = {}

    def __init__(self, name, using=None):
        cfg = self.registry[name]
        self._cfg = cfg
        self.schema = SimpleNamespace(fields=cfg["fields"])
        self.indexes = []

    def insert(self, entities):
        self._cfg["insert_entities"] = entities
        count = len(entities[0]) if entities else 0
        return SimpleNamespace(primary_keys=list(range(count)))

    def flush(self):
        return None


def _patch_lazy_import(monkeypatch):
    monkeypatch.setattr(
        milvus_module,
        "_lazy_import_milvus",
        lambda: (None, None, None, None, _FakeCollection, None, None),
    )


def test_insert_batch_skips_auto_id_primary_field(monkeypatch):
    _patch_lazy_import(monkeypatch)
    _FakeCollection.registry = {
        "dim_value_embeddings": {
            "fields": [
                _FakeField("id", is_primary=True, auto_id=True),
                _FakeField("table_name"),
                _FakeField("col_name"),
                _FakeField("col_value"),
                _FakeField("embedding"),
                _FakeField("update_ts"),
            ]
        }
    }

    client = MilvusClient(config={})
    inserted = client.insert_batch(
        "dim_value_embeddings",
        [
            {
                "id": 123,
                "table_name": "public.actor",
                "col_name": "first_name",
                "col_value": "PENELOPE",
                "embedding": [0.1, 0.2],
                "update_ts": 1700000000,
            }
        ],
    )

    assert inserted == 1
    entities = _FakeCollection.registry["dim_value_embeddings"]["insert_entities"]
    assert len(entities) == 5
    assert entities[0] == ["public.actor"]  # table_name


def test_insert_batch_keeps_non_auto_primary_field(monkeypatch):
    _patch_lazy_import(monkeypatch)
    _FakeCollection.registry = {
        "table_schema_embeddings": {
            "fields": [
                _FakeField("object_id", is_primary=True, auto_id=False),
                _FakeField("object_type"),
            ]
        }
    }

    client = MilvusClient(config={})
    inserted = client.insert_batch(
        "table_schema_embeddings",
        [{"object_id": "public.actor", "object_type": "table"}],
    )

    assert inserted == 1
    entities = _FakeCollection.registry["table_schema_embeddings"]["insert_entities"]
    assert len(entities) == 2
    assert entities[0] == ["public.actor"]  # object_id


# ---- doc 20.1 §10.3 / §10.6.3：结构核验、upsert 成功数、按表达式删除 ----


class _SchemaField:
    def __init__(
        self, name, dtype="VARCHAR", is_primary=False, auto_id=False, **params
    ):
        self.name = name
        self.dtype = dtype
        self.is_primary = is_primary
        self.auto_id = auto_id
        self.params = params


def _expected_fields():
    return [
        _SchemaField("entity_id", is_primary=True, max_length=512),
        _SchemaField("source_id", max_length=36),
        _SchemaField("embedding", dtype="FLOAT_VECTOR", dim=4),
    ]


def test_validate_collection_schema_accepts_same_fields_in_any_order():
    existing = SimpleNamespace(fields=list(reversed(_expected_fields())))
    expected = SimpleNamespace(fields=_expected_fields())

    MilvusClient.validate_collection_schema(existing, expected)


def test_validate_collection_schema_accepts_string_params_from_server():
    existing_fields = [
        _SchemaField("entity_id", is_primary=True, max_length="512"),
        _SchemaField("source_id", max_length="36"),
        _SchemaField("embedding", dtype="FLOAT_VECTOR", dim="4"),
    ]

    MilvusClient.validate_collection_schema(
        SimpleNamespace(fields=existing_fields),
        SimpleNamespace(fields=_expected_fields()),
    )


def test_validate_collection_schema_rejects_old_primary_key_layout():
    old_fields = [
        _SchemaField("object_id", is_primary=True, max_length=256),
        _SchemaField("embedding", dtype="FLOAT_VECTOR", dim=4),
    ]

    with pytest.raises(ValueError) as exc:
        MilvusClient.validate_collection_schema(
            SimpleNamespace(fields=old_fields),
            SimpleNamespace(fields=_expected_fields()),
        )

    message = str(exc.value)
    assert "缺少字段" in message and "entity_id" in message
    assert "多出字段" in message and "object_id" in message


@pytest.mark.parametrize(
    "field_name, changes",
    [
        ("source_id", {"max_length": 64}),
        ("embedding", {"dim": 8}),
        ("entity_id", {"is_primary": False}),
    ],
)
def test_validate_collection_schema_rejects_changed_definition(field_name, changes):
    fields = _expected_fields()
    for field in fields:
        if field.name == field_name:
            for key, value in changes.items():
                if key in ("max_length", "dim"):
                    field.params[key] = value
                else:
                    setattr(field, key, value)

    with pytest.raises(ValueError):
        MilvusClient.validate_collection_schema(
            SimpleNamespace(fields=fields), SimpleNamespace(fields=_expected_fields())
        )


class _UpsertCollection:
    calls = []
    result = None

    def __init__(self, name, using=None):
        self.schema = SimpleNamespace(
            fields=[
                _SchemaField("entity_id", is_primary=True),
                _SchemaField("source_id"),
            ]
        )

    def upsert(self, entities):
        _UpsertCollection.calls.append(("upsert", entities))
        return _UpsertCollection.result

    def delete(self, expr):
        _UpsertCollection.calls.append(("delete", expr))
        return _UpsertCollection.result

    def flush(self):
        _UpsertCollection.calls.append(("flush", None))


def _patch_upsert_collection(monkeypatch, result):
    _UpsertCollection.calls = []
    _UpsertCollection.result = result
    monkeypatch.setattr(
        milvus_module,
        "_lazy_import_milvus",
        lambda: (None, None, None, None, _UpsertCollection, None, None),
    )


def test_upsert_batch_returns_server_confirmed_count(monkeypatch):
    _patch_upsert_collection(monkeypatch, SimpleNamespace(primary_keys=["a", "b"]))

    written = MilvusClient(config={}).upsert_batch(
        "c",
        [{"entity_id": "a", "source_id": "s"}, {"entity_id": "b", "source_id": "s"}],
    )

    assert written == 2
    assert _UpsertCollection.calls[0] == ("upsert", [["a", "b"], ["s", "s"]])


@pytest.mark.parametrize("response", [None, SimpleNamespace(primary_keys=[])])
def test_upsert_batch_does_not_report_input_count_when_response_is_missing(
    monkeypatch, response
):
    _patch_upsert_collection(monkeypatch, response)

    written = MilvusClient(config={}).upsert_batch(
        "c", [{"entity_id": "a", "source_id": "s"}]
    )

    assert written == 0


def test_delete_by_expr_passes_filter_and_does_not_flush(monkeypatch):
    _patch_upsert_collection(monkeypatch, SimpleNamespace(delete_count=3))

    deleted = MilvusClient(config={}).delete_by_expr(
        "c", 'source_id == "s" and publish_id != "p"'
    )

    assert deleted == 3
    assert _UpsertCollection.calls == [
        ("delete", 'source_id == "s" and publish_id != "p"')
    ]


def test_delete_by_expr_zero_matches_is_success(monkeypatch):
    _patch_upsert_collection(monkeypatch, SimpleNamespace(delete_count=0))

    assert MilvusClient(config={}).delete_by_expr("c", 'source_id == "s"') == 0


@pytest.mark.parametrize("expr", ["", "   ", None])
def test_delete_by_expr_rejects_unfiltered_delete(monkeypatch, expr):
    _patch_upsert_collection(monkeypatch, SimpleNamespace(delete_count=0))

    with pytest.raises(ValueError):
        MilvusClient(config={}).delete_by_expr("c", expr)

    assert _UpsertCollection.calls == []


class _FakeConnections:
    def connect(self, **kwargs):
        return None


class _FakeDb:
    def __init__(self, existing):
        self.databases = list(existing)
        self.created = []
        self.used = []

    def list_database(self, using=None):
        return list(self.databases)

    def create_database(self, name, using=None):
        self.created.append(name)
        self.databases.append(name)

    def using_database(self, name, using=None):
        self.used.append(name)


def _patch_connect(monkeypatch, db):
    monkeypatch.setattr(
        milvus_module,
        "_lazy_import_milvus",
        lambda: (
            _FakeConnections(),
            db,
            None,
            None,
            None,
            None,
            SimpleNamespace(list_collections=lambda using=None: []),
        ),
    )


def test_connect_fails_clearly_when_database_missing_by_default(monkeypatch):
    db = _FakeDb(["default"])
    _patch_connect(monkeypatch, db)
    client = MilvusClient(config={"database": "nl2sql"})

    with pytest.raises(ValueError, match="不存在"):
        client.connect()

    assert db.created == [] and client.connected is False
    assert client.test_connection() is False


def test_connect_creates_missing_database_when_requested(monkeypatch):
    db = _FakeDb(["default"])
    _patch_connect(monkeypatch, db)
    client = MilvusClient(config={"database": "nl2sql"})

    assert client.test_connection(create_database=True) is True

    assert (
        db.created == ["nl2sql"] and db.used == ["nl2sql"] and client.connected is True
    )
