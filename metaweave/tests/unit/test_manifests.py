"""manifest 构造与写入前校验（doc 6.1 §11.5、A19）。"""

import pytest

from backend.jobs.manifests import (
    ManifestValidationError,
    build_input_manifest,
    build_output_manifest,
    build_target_manifest,
)

RUN = "11111111-1111-4111-8111-111111111111"


def test_input_manifest_normalizes_uuid_and_allows_null():
    manifest = build_input_manifest(
        [
            ("domain/db_domains.yaml", RUN.upper()),
            ("cql/import_all.cypher", None),
        ]
    )
    assert manifest == {
        "files": [
            {"path": "domain/db_domains.yaml", "collection_run_id": RUN},
            {"path": "cql/import_all.cypher", "collection_run_id": None},
        ]
    }


@pytest.mark.parametrize(
    "entries",
    [
        [("", RUN)],
        [("/abs/path.yaml", RUN)],
        [("../escape.yaml", RUN)],
        [(".backup/x.yaml", RUN)],
        [("rel/.hidden.json", RUN)],
        [("json/a.json", "not-a-uuid")],
        [("json/a.json", 123)],
    ],
)
def test_input_manifest_rejects_invalid(entries):
    with pytest.raises(ManifestValidationError):
        build_input_manifest(entries)


def test_output_manifest_paths():
    assert build_output_manifest(["db/database.json"]) == {
        "files": ["db/database.json"]
    }
    with pytest.raises(ManifestValidationError):
        build_output_manifest([".coll_run_id_x"])


def test_target_manifest_by_type():
    manifest = build_target_manifest(
        [
            {
                "type": "postgresql",
                "database": "metaweave",
                "schema": "public",
                "table": "db_profile",
            },
            {"type": "milvus", "collection": "sql_example_embeddings"},
            {"type": "neo4j", "database": "neo4j"},
        ]
    )
    assert manifest["targets"][0]["table"] == "db_profile"
    with pytest.raises(ManifestValidationError):
        build_target_manifest([{"type": "redis", "collection": "x"}])
    with pytest.raises(ManifestValidationError):
        build_target_manifest([{"type": "milvus"}])
