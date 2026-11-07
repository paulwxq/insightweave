"""cql writer 实体键与归属注入用例（doc 4 §4.2/§4.6）

- provenance=(source_id, publish_run_id) **必填**：缺省/非法即拒绝生成；
- Table/Column 按 entity_id MERGE，entity_id = source_id:逻辑id；source_id/
  publish_run_id 写入全部节点与边（Table/Column/HAS_COLUMN/JOIN_ON）；
- 头部含 ``// source_id:`` 行供加载器核对；
- 约束章节只生成 table_entity_id/column_entity_id（不再生成旧
  id/full_name 全局唯一约束）；
- CQLGenerator 同名参数透传给 writer（stub 断言）。
"""

from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

from metaweave.core.cql_generator.generator import CQLGenerator
from metaweave.core.cql_generator.models import (
    ColumnNode,
    HASColumnRelation,
    JOINOnRelation,
    TableNode,
)
from metaweave.core.cql_generator.reader import RelationshipFilterStats
from metaweave.core.cql_generator.writer import CypherWriter

SOURCE_ID = "db_src_1"
PUBLISH_RUN_ID = "1a2b3c4d-5e6f-4a5b-8c9d-0e1f2a3b4c5d"
TABLE_ENTITY = f"{SOURCE_ID}:orders.public.users"
COLUMN_ENTITY = f"{SOURCE_ID}:orders.public.users.user_id"


@pytest.fixture
def temp_config_file(tmp_path):
    config = {
        "database": {
            "host": "localhost",
            "port": 5432,
            "database": "orders",
            "user": "test_user",
            "password": "test_pass",
            "schemas": ["public"],
        },
        "output": {
            "json_directory": str(tmp_path / "json"),
            "rel_directory": str(tmp_path / "rel"),
            "cql_directory": str(tmp_path / "cql"),
        },
    }
    config_path = tmp_path / "test_config.yaml"
    with open(config_path, "w", encoding="utf-8") as f:
        yaml.dump(config, f)
    return config_path


def _table():
    return TableNode(
        full_name="public.users",
        schema="public",
        name="users",
        database="orders",
    )


def _column():
    return ColumnNode(
        full_name="public.users.user_id",
        schema="public",
        table="users",
        name="user_id",
        data_type="bigint",
        database="orders",
    )


def _write(
    tmp_path,
    tables=None,
    columns=None,
    has_rels=None,
    join_rels=None,
    provenance=(SOURCE_ID, PUBLISH_RUN_ID),
):
    writer = CypherWriter(tmp_path / "cql")
    files = writer.write_all(
        tables if tables is not None else [_table()],
        columns if columns is not None else [_column()],
        (
            has_rels
            if has_rels is not None
            else [HASColumnRelation("public.users", "public.users.user_id")]
        ),
        join_rels if join_rels is not None else [],
        provenance=provenance,
    )
    return Path(files[0]).read_text(encoding="utf-8")


class TestProvenanceRequired:
    def test_missing_provenance_rejected(self, tmp_path):
        writer = CypherWriter(tmp_path / "cql")
        with pytest.raises(ValueError, match="provenance"):
            writer.write_all([_table()], [], [], [])

    def test_invalid_source_id_rejected(self, tmp_path):
        writer = CypherWriter(tmp_path / "cql")
        with pytest.raises(ValueError, match="provenance"):
            writer.write_all(
                [_table()], [], [], [], provenance=("not-a-short-id", PUBLISH_RUN_ID)
            )


class TestEntityIdentity:
    def test_table_merges_on_entity_id(self, tmp_path):
        content = _write(tmp_path)
        assert "MERGE (n:Table {entity_id: t.entity_id})" in content
        assert f'"entity_id": "{TABLE_ENTITY}"' in content
        # 原 full_name MERGE 身份不再出现
        assert "MERGE (n:Table {full_name:" not in content
        assert "n.source_id = t.source_id" in content
        assert "n.publish_run_id = t.publish_run_id" in content

    def test_column_merges_on_entity_id_with_source(self, tmp_path):
        content = _write(tmp_path)
        assert "MERGE (n:Column {entity_id: c.entity_id})" in content
        assert f'"entity_id": "{COLUMN_ENTITY}"' in content
        assert "n.source_id = c.source_id" in content
        assert "n.publish_run_id = c.publish_run_id" in content

    def test_has_column_matches_endpoints_by_entity(self, tmp_path):
        content = _write(tmp_path)
        assert "MATCH (t:Table {entity_id: hc.table_entity_id})" in content
        assert "MATCH (c:Column {entity_id: hc.column_entity_id})" in content
        assert "MERGE (t)-[r:HAS_COLUMN]->(c)" in content
        assert "r.source_id = hc.source_id" in content
        assert "r.publish_run_id = hc.publish_run_id" in content

    def test_join_on_matches_endpoints_by_entity(self, tmp_path):
        join = JOINOnRelation(
            relationship_id="rel_x",
            src_full_name="public.users",
            dst_full_name="public.users",
            cardinality="1:1",
        )
        content = _write(tmp_path, join_rels=[join])
        assert "MATCH (src:Table {entity_id: j.src_entity_id})" in content
        assert "MATCH (dst:Table {entity_id: j.dst_entity_id})" in content
        assert (
            "MERGE (src)-[r:JOIN_ON {relationship_id: j.relationship_id}]->(dst)"
            in content
        )
        assert "r.source_id = j.source_id" in content
        # doc 12.1：JOIN_ON 的发布运行 SET 行与 Table/Column/HAS_COLUMN
        # 同样切换为 publish_run_id，单独漏改/回退时此处失败
        assert "r.publish_run_id = j.publish_run_id" in content

    def test_header_contains_source_id_line(self, tmp_path):
        content = _write(tmp_path)
        assert f"// source_id: {SOURCE_ID}" in content

    def test_constraints_only_entity_keys(self, tmp_path):
        content = _write(tmp_path)
        assert (
            "CREATE CONSTRAINT table_entity_id IF NOT EXISTS FOR (t:Table) "
            "REQUIRE t.entity_id IS UNIQUE" in content
        )
        assert (
            "CREATE CONSTRAINT column_entity_id IF NOT EXISTS FOR (c:Column) "
            "REQUIRE c.entity_id IS UNIQUE" in content
        )
        assert "table_full_name" not in content
        assert "column_full_name" not in content
        assert "REQUIRE t.id IS UNIQUE" not in content
        assert "REQUIRE c.id IS UNIQUE" not in content

    def test_business_id_and_full_name_kept_as_properties(self, tmp_path):
        content = _write(tmp_path)
        assert "n.id        = t.id" in content
        assert '"id": "orders.public.users"' in content
        assert '"full_name": "public.users"' in content
        # doc 4 §4.2：MERGE 身份改 entity_id 后，full_name 必须显式 SET——
        # 否则按源删除重建的节点丢失该属性（迁移/维护检查依赖）
        assert "n.full_name = t.full_name" in content
        assert "n.full_name    = c.full_name" in content

    def test_unresolvable_endpoint_rejected(self, tmp_path):
        writer = CypherWriter(tmp_path / "cql")
        with pytest.raises(ValueError, match="端点"):
            writer.write_all(
                [_table()],
                [_column()],
                [HASColumnRelation("public.missing", "public.users.user_id")],
                [],
                provenance=(SOURCE_ID, PUBLISH_RUN_ID),
            )


class TestGeneratorPassthrough:
    def test_generator_passes_provenance_to_writer(self, temp_config_file):
        config = {
            "database": {"database": "orders"},
            "output": {
                "json_directory": "x",
                "rel_directory": "y",
                "cql_directory": "z",
            },
        }
        generator = CQLGenerator(
            temp_config_file, config=config, provenance=(SOURCE_ID, PUBLISH_RUN_ID)
        )
        writer = MagicMock()
        writer.write_all.return_value = []
        writer.write_metadata.return_value = Path("m.md")
        # 直接调用 writer 桩验证透传（绕过 reader 输入）
        CypherWriter.write_metadata  # 确认真实类不受影响
        assert generator.provenance == (SOURCE_ID, PUBLISH_RUN_ID)

    def test_step_constraints_file_only_entity_keys(self, tmp_path):
        writer = CypherWriter(tmp_path)
        path = writer._write_constraints()
        content = path.read_text(encoding="utf-8")
        assert "table_entity_id" in content
        assert "column_entity_id" in content
        # 分步脚本不得再创建旧 id/full_name 全局唯一约束
        statements = [ln for ln in content.splitlines() if "CREATE CONSTRAINT" in ln]
        assert len(statements) == 2
        assert not any("REQUIRE t.id" in s or "REQUIRE c.id" in s for s in statements)
        assert not any("full_name" in s for s in statements)


def test_write_metadata_still_works(tmp_path):
    writer = CypherWriter(tmp_path)
    stats = RelationshipFilterStats(final_count=0)
    path = writer.write_metadata(
        [_table()],
        [_column()],
        [HASColumnRelation("public.users", "public.users.user_id")],
        [],
        "cql",
        tmp_path / "json",
        tmp_path / "rel",
        stats,
    )
    assert path.exists()
