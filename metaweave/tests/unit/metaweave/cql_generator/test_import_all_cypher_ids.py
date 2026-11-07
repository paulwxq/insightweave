"""import_all 脚本内容结构测试（doc 4 §4.2/§4.6 改造后口径）

- 保留业务 id（db.schema.table 三段）与 database 属性；
- MERGE 身份为 entity_id（source_id:id），端点定位全部按实体键；
- 头部含 source_id 行与统计头。
"""

import re

from metaweave.core.cql_generator.writer import CypherWriter
from metaweave.core.cql_generator.models import (
    TableNode,
    ColumnNode,
    HASColumnRelation,
    JOINOnRelation,
)

SOURCE_ID = "db_src_1"
PUBLISH_RUN_ID = "1a2b3c4d-5e6f-4a5b-8c9d-0e1f2a3b4c5d"


def test_import_all_includes_db_prefixed_ids_and_database_property(tmp_path):
    writer = CypherWriter(tmp_path)
    tables = [
        TableNode(
            full_name="public.dim_region",
            schema="public",
            name="dim_region",
            database="store_db",
        ),
    ]
    columns = [
        ColumnNode(
            full_name="public.dim_region.city_name",
            schema="public",
            table="dim_region",
            name="city_name",
            data_type="varchar",
            database="store_db",
        )
    ]
    has_column_rels = [
        HASColumnRelation("public.dim_region", "public.dim_region.city_name"),
    ]
    join_on_rels = [
        JOINOnRelation(
            relationship_id="rel_test001",
            src_full_name="public.dim_region",
            dst_full_name="public.dim_region",
            cardinality="1:1",
        ),
    ]

    output_files = writer.write_all(
        tables,
        columns,
        has_column_rels,
        join_on_rels,
        provenance=(SOURCE_ID, PUBLISH_RUN_ID),
    )
    assert str(tmp_path / "import_all.store_db.cypher") in output_files

    content = (tmp_path / "import_all.store_db.cypher").read_text(encoding="utf-8")

    assert re.search(
        r"// 生成时间: \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z", content
    )

    # 实体键唯一约束（旧 id/full_name 约束不再生成）
    assert "CONSTRAINT table_entity_id IF NOT EXISTS" in content
    assert "CONSTRAINT column_entity_id IF NOT EXISTS" in content
    assert "REQUIRE t.entity_id IS UNIQUE" in content
    assert "REQUIRE c.entity_id IS UNIQUE" in content

    # 业务属性保留（full_name 显式 SET：MERGE 身份不再自动写入该属性）
    assert "n.id        = t.id" in content
    assert "n.full_name = t.full_name" in content
    assert "n.database  = t.database" in content
    assert "n.id           = c.id" in content
    assert "n.full_name    = c.full_name" in content
    assert "n.database     = c.database" in content

    # 实体键与归属注入
    assert f'"entity_id": "{SOURCE_ID}:store_db.public.dim_region"' in content
    assert f'"entity_id": "{SOURCE_ID}:store_db.public.dim_region.city_name"' in content
    assert f'"source_id": "{SOURCE_ID}"' in content
    assert f'"publish_run_id": "{PUBLISH_RUN_ID}"' in content
    assert f"// source_id: {SOURCE_ID}" in content
    assert "// 统计: 1 张表, 1 个列, 1 个关系" in content

    # Verify embedded JSON contains expected values
    assert '"id": "store_db.public.dim_region"' in content
    assert '"database": "store_db"' in content
