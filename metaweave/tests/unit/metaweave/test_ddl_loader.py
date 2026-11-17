import textwrap

import pytest

from metaweave.core.metadata.ddl_loader import DDLLoader, DDLLoaderError


def test_ddl_loader_keeps_columns_with_constraint_keyword_prefixes(tmp_path):
    ddl = """
        CREATE TABLE public.table_profile (
            constraint_count INTEGER,
            constraint_label TEXT NOT NULL,
            unique_constraints JSONB,
            unique_count INTEGER,
            constraint$count INTEGER,
            unique$value TEXT,
            "constraint" INTEGER,
            "unique" TEXT
        );
    """
    metadata = DDLLoader(tmp_path).parse_content(
        ddl, tmp_path / "postgres.public.table_profile.sql"
    ).metadata

    assert [column.column_name for column in metadata.columns] == [
        "constraint_count",
        "constraint_label",
        "unique_constraints",
        "unique_count",
        "constraint$count",
        "unique$value",
        "constraint",
        "unique",
    ]
    assert [column.ordinal_position for column in metadata.columns] == list(range(1, 9))
    assert metadata.columns[1].is_nullable is False
    assert metadata.columns[2].data_type == "jsonb"
    assert metadata.primary_keys == []
    assert metadata.unique_constraints == []
    assert metadata.foreign_keys == []


@pytest.mark.parametrize("separator", [" ", "\t", "\n"])
@pytest.mark.parametrize("named_primary_key", [True, False])
def test_ddl_loader_preserves_real_constraints(tmp_path, separator, named_primary_key):
    primary_key = "PRIMARY KEY(id)"
    if named_primary_key:
        primary_key = f"CONSTRAINT pk_profile {primary_key}"
    ddl = f"""
        CREATE TABLE public.table_profile (
            id INTEGER NOT NULL,
            constraint_count INTEGER,
            unique_constraints JSONB,
            {primary_key},
            CONSTRAINT uq_count UNIQUE(constraint_count),
            CONSTRAINT fk_count FOREIGN KEY(constraint_count)
                REFERENCES public.table_profile(id)
        );
    """.replace(" ", separator)
    metadata = DDLLoader(tmp_path).parse_content(
        ddl, tmp_path / "postgres.public.table_profile.sql"
    ).metadata

    assert [column.column_name for column in metadata.columns] == [
        "id",
        "constraint_count",
        "unique_constraints",
    ]
    assert len(metadata.primary_keys) == 1
    assert metadata.primary_keys[0].columns == ["id"]
    assert len(metadata.unique_constraints) == 1
    assert metadata.unique_constraints[0].constraint_name == "uq_count"
    assert metadata.unique_constraints[0].columns == ["constraint_count"]
    assert len(metadata.foreign_keys) == 1
    assert metadata.foreign_keys[0].source_columns == ["constraint_count"]
    assert metadata.foreign_keys[0].target_table == "table_profile"
    assert metadata.foreign_keys[0].target_columns == ["id"]


def _write_sample_ddl(tmp_path):
    ddl_text = textwrap.dedent(
        """
        CREATE TABLE IF NOT EXISTS public.dim_company (
            company_id INTEGER NOT NULL,
            company_name CHARACTER VARYING(200) NOT NULL,
            region_id INTEGER,
            CONSTRAINT pk_dim_company PRIMARY KEY (company_id),
            CONSTRAINT fk_company_region FOREIGN KEY (region_id) REFERENCES public.dim_region(region_id)
        );

        COMMENT ON COLUMN public.dim_company.company_id IS '公司ID';
        COMMENT ON COLUMN public.dim_company.company_name IS '公司名称';
        COMMENT ON TABLE public.dim_company IS '公司维表';

        CREATE INDEX idx_company_name ON public.dim_company(company_name);

        /* SAMPLED_RECORDS
        {
          "object_type": "table",
          "object_name": "public.dim_company",
          "records": [
            {"company_id": "1", "company_name": "A"},
            {"company_id": "2", "company_name": "B"},
            {"company_id": "3", "company_name": "C"}
          ]
        }
        */
        """
    ).strip()
    ddl_file = tmp_path / "postgres.public.dim_company.sql"
    ddl_file.write_text(ddl_text, encoding="utf-8")
    return ddl_file


def test_ddl_loader_parses_structure_and_samples(tmp_path):
    _write_sample_ddl(tmp_path)

    loader = DDLLoader(tmp_path)
    parsed = loader.load_table("public", "dim_company")
    metadata = parsed.metadata

    assert metadata.schema_name == "public"
    assert metadata.table_name == "dim_company"
    assert metadata.comment == "公司维表"
    assert len(metadata.columns) == 3
    assert metadata.columns[0].column_name == "company_id"
    assert metadata.columns[0].comment == "公司ID"
    assert metadata.primary_keys[0].columns == ["company_id"]
    assert metadata.foreign_keys[0].target_table == "dim_region"
    assert metadata.indexes[0].index_name == "idx_company_name"
    assert metadata.sample_records == [
        {"company_id": "1", "company_name": "A"},
        {"company_id": "2", "company_name": "B"},
        {"company_id": "3", "company_name": "C"},
    ]


def test_ddl_loader_parses_compact_sampled_records(tmp_path):
    ddl_file = tmp_path / "postgres.public.events.sql"
    ddl_file.write_text(
        textwrap.dedent(
            """
            CREATE TABLE IF NOT EXISTS public.events (
                event_id INTEGER,
                event_name TEXT
            );

            /* SAMPLED_RECORDS
            {
              "object_type": "table",
              "object_name": "public.events",
              "records": [
                {"event_id": "1", "event_name": "created"},
                {"event_id": "2", "event_name": "completed"}
              ]
            }
            */
            """
        ).strip(),
        encoding="utf-8",
    )

    parsed = DDLLoader(tmp_path).load_table("public", "events")

    assert parsed.sample_records == [
        {"event_id": "1", "event_name": "created"},
        {"event_id": "2", "event_name": "completed"},
    ]


def test_ddl_loader_parses_postgres_index_features(tmp_path):
    ddl_file = tmp_path / "postgres.public.accounts.sql"
    ddl_file.write_text(
        textwrap.dedent(
            """
            CREATE TABLE public.accounts (
                account_id INTEGER,
                email TEXT,
                created_at TIMESTAMP,
                deleted_at TIMESTAMP
            );

            CREATE UNIQUE INDEX accounts_email_uidx
            ON public.accounts USING btree (account_id, lower(email))
            INCLUDE (created_at)
            WHERE deleted_at IS NULL;
            """
        ).strip(),
        encoding="utf-8",
    )

    parsed = DDLLoader(tmp_path).load_table("public", "accounts")

    assert len(parsed.metadata.indexes) == 1
    index = parsed.metadata.indexes[0]
    assert index.index_name == "accounts_email_uidx"
    assert index.index_type == "btree"
    assert index.is_unique is True
    assert index.columns == ["account_id"]
    assert index.key_expressions == ["account_id", "lower(email)"]
    assert index.included_columns == ["created_at"]
    assert index.condition == "deleted_at IS NULL"
    assert index.is_constraint_backed is False
    assert index.definition.startswith("CREATE UNIQUE INDEX")


def test_ddl_loader_keeps_commas_inside_index_expressions(tmp_path):
    ddl_file = tmp_path / "postgres.public.events.sql"
    ddl_file.write_text(
        textwrap.dedent(
            """
            CREATE TABLE public.events (
                event_id INTEGER,
                event_code TEXT
            );

            CREATE INDEX events_code_idx ON public.events
            USING hash (concat(event_code, ',', event_id));
            """
        ).strip(),
        encoding="utf-8",
    )

    parsed = DDLLoader(tmp_path).load_table("public", "events")

    index = parsed.metadata.indexes[0]
    assert index.index_type == "hash"
    assert index.columns == []
    assert index.key_expressions == ["concat(event_code, ',', event_id)"]


class TestSampleRecordsCurrentFormat:
    """doc 19.2 §8.2：只认当前格式；旧标记/多块/边界不完整拒绝。"""

    def _ddl(self, records_json: str) -> str:
        return (
            "CREATE TABLE public.orders (order_id integer);\n"
            f"/* SAMPLED_RECORDS\n{records_json}\n*/"
        )

    def test_current_records_direct_and_data_is_plain_field(self, tmp_path):
        path = tmp_path / "postgres.public.orders.sql"
        path.write_text(
            self._ddl(
                '{"object_type":"table","object_name":"public.orders",'
                '"records":[{"data":"1","label":"x"},{"data":null}]}'
            ),
            encoding="utf-8",
        )
        records = (
            DDLLoader(tmp_path)
            .parse_content(path.read_text(encoding="utf-8"), path)
            .sample_records
        )
        assert records == [{"data": "1", "label": "x"}, {"data": None}]

    def test_legacy_marker_rejected(self, tmp_path):
        path = tmp_path / "postgres.public.orders.sql"
        path.write_text(
            "CREATE TABLE public.orders (order_id integer);\n"
            '/* SAMPLE_RECORDS\n{"table":"public.orders","records":[{"label":"a","data":{"id":1}}]}\n*/',
            encoding="utf-8",
        )
        with pytest.raises(DDLLoaderError):
            DDLLoader(tmp_path).parse_content(path.read_text(encoding="utf-8"), path)

    def test_line_comment_with_marker_word_not_rejected(self, tmp_path):
        path = tmp_path / "postgres.public.orders.sql"
        content = (
            "-- 历史说明：SAMPLE_RECORDS 已废弃\n"
            "CREATE TABLE public.orders (order_id integer);\n"
        )
        records = DDLLoader(tmp_path).parse_content(content, path).sample_records
        assert records == []

    def test_broken_boundary_rejected(self, tmp_path):
        path = tmp_path / "postgres.public.orders.sql"
        path.write_text(
            "CREATE TABLE public.orders (order_id integer);\n"
            '/* SAMPLED_RECORDS\n{"records": [{"a": 1}',
            encoding="utf-8",
        )
        with pytest.raises(DDLLoaderError):
            DDLLoader(tmp_path).parse_content(path.read_text(encoding="utf-8"), path)

    def test_multiple_blocks_rejected(self, tmp_path):
        block = '/* SAMPLED_RECORDS\n{"records": []}\n*/'
        path = tmp_path / "postgres.public.orders.sql"
        path.write_text(
            "CREATE TABLE public.orders (order_id integer);\n" + block + "\n" + block,
            encoding="utf-8",
        )
        with pytest.raises(DDLLoaderError):
            DDLLoader(tmp_path).parse_content(path.read_text(encoding="utf-8"), path)

    def test_generated_file_roundtrip_with_tricky_values(self, tmp_path):
        import pandas as pd

        from metaweave.core.metadata.formatter import OutputFormatter
        from metaweave.core.metadata.models import ColumnInfo, TableMetadata
        from metaweave.core.metadata.sample_block import find_sampled_records_blocks
        from tests.unit.metaweave.metadata.test_sample_block import TRICKY_VALUES

        formatter = OutputFormatter(
            {
                "output_dir": tmp_path,
                "ddl_options": {"sample_records": {"count": len(TRICKY_VALUES)}},
            },
            database_name="postgres",
        )
        metadata = TableMetadata(
            schema_name="public",
            table_name="orders",
            columns=[ColumnInfo("val", 1, "text")],
        )
        df = pd.DataFrame([{"val": v} for v in TRICKY_VALUES])
        path = tmp_path / "postgres.public.orders.sql"
        ddl = formatter.generate_ddl(metadata, sample_data=df)
        path.write_text(ddl, encoding="utf-8")

        parsed = DDLLoader(tmp_path).parse_content(ddl, path)
        expected = [{"val": None if v is None else str(v)} for v in TRICKY_VALUES]
        assert parsed.sample_records == expected
        assert find_sampled_records_blocks(ddl)

    def test_orphan_marker_fragment_rejected(self, tmp_path):
        """标记起始多于完整块：按输入错误拒绝（doc 19.2 §8.2）。"""
        block = '/* SAMPLED_RECORDS\n{"records": []}\n*/'
        path = tmp_path / "postgres.public.orders.sql"
        path.write_text(
            "CREATE TABLE public.orders (order_id integer);\n"
            "/* SAMPLED_RECORDS hidden=secret */\n" + block,
            encoding="utf-8",
        )
        with pytest.raises(DDLLoaderError):
            DDLLoader(tmp_path).parse_content(path.read_text(encoding="utf-8"), path)

    def test_comment_string_with_marker_text_not_rejected(self, tmp_path):
        """对象注释含 /* SAMPLE_RECORDS demo */ 字样时仍正常解析（doc 19.2 §8.2）。"""
        path = tmp_path / "postgres.public.users.sql"
        path.write_text(
            "CREATE TABLE public.users (user_id integer);\n"
            "COMMENT ON TABLE public.users IS '/* SAMPLE_RECORDS demo */';\n"
            '/* SAMPLED_RECORDS\n{"records": [{"user_id": "1"}]}\n*/',
            encoding="utf-8",
        )
        parsed = DDLLoader(tmp_path).parse_content(
            path.read_text(encoding="utf-8"), path
        )
        assert parsed.sample_records == [{"user_id": "1"}]

    def test_comment_string_with_current_marker_text_not_rejected(self, tmp_path):
        path = tmp_path / "postgres.public.orders.sql"
        path.write_text(
            "CREATE TABLE public.orders (order_id integer);\n"
            "COMMENT ON TABLE public.orders IS '/* SAMPLED_RECORDS demo */';\n"
            '/* SAMPLED_RECORDS\n{"records": [{"order_id": "1"}]}\n*/',
            encoding="utf-8",
        )
        parsed = DDLLoader(tmp_path).parse_content(
            path.read_text(encoding="utf-8"), path
        )
        assert parsed.sample_records == [{"order_id": "1"}]

    def test_view_definition_with_dollar_quoted_marker_text(self, tmp_path):
        """视图定义表达式含 $$/* SAMPLE_RECORDS demo */$$ 时正常解析（doc 19.2 §8.2）。"""
        path = tmp_path / "postgres.public.v_orders.sql"
        path.write_text(
            "CREATE MATERIALIZED VIEW IF NOT EXISTS public.v_orders AS\n"
            "SELECT $$/* SAMPLE_RECORDS demo */$$ AS note;\n"
            "/* OBJECT_METADATA\n"
            "{\n"
            '  "object_type": "materialized_view",\n'
            '  "object_name": "public.v_orders",\n'
            '  "object_comment": "",\n'
            '  "columns": [\n'
            '    {"column_name": "note", "data_type": "text", "column_comment": ""}\n'
            "  ]\n"
            "}\n"
            "*/\n"
            "/* SAMPLED_RECORDS\n"
            '{"object_type": "materialized_view", "object_name": "public.v_orders", '
            '"records": [{"note": "x"}]}\n'
            "*/",
            encoding="utf-8",
        )
        parsed = DDLLoader(tmp_path).parse_content(
            path.read_text(encoding="utf-8"), path
        )
        assert parsed.metadata.object_type == "materialized_view"
        assert parsed.sample_records == [{"note": "x"}]

    def test_index_name_with_dollar_tag_keeps_sample_block(self, tmp_path):
        """合法索引名含 $tag$ 时样例块仍被识别（doc 19.2 §8.2）。"""
        path = tmp_path / "postgres.public.orders.sql"
        path.write_text(
            "CREATE TABLE public.orders (order_id integer);\n"
            'CREATE INDEX "idx$tag$" ON public.orders(order_id);\n'
            '/* SAMPLED_RECORDS\n{"records": [{"order_id": "1"}]}\n*/',
            encoding="utf-8",
        )
        parsed = DDLLoader(tmp_path).parse_content(
            path.read_text(encoding="utf-8"), path
        )
        assert parsed.sample_records == [{"order_id": "1"}]

    def test_view_definition_with_escape_string_marker_text(self, tmp_path):
        """视图定义含 E'...\\'...' 转义串时正常解析（doc 19.2 §8.2）。"""
        path = tmp_path / "postgres.public.v_esc.sql"
        path.write_text(
            "CREATE MATERIALIZED VIEW IF NOT EXISTS public.v_esc AS\n"
            "SELECT E'abc\\'/* SAMPLE_RECORDS demo */' AS note;\n"
            "/* OBJECT_METADATA\n"
            "{\n"
            '  "object_type": "materialized_view",\n'
            '  "object_name": "public.v_esc",\n'
            '  "object_comment": "",\n'
            '  "columns": [\n'
            '    {"column_name": "note", "data_type": "text", "column_comment": ""}\n'
            "  ]\n"
            "}\n"
            "*/\n"
            "/* SAMPLED_RECORDS\n"
            '{"object_type": "materialized_view", "object_name": "public.v_esc", '
            '"records": [{"note": "x"}]}\n'
            "*/",
            encoding="utf-8",
        )
        parsed = DDLLoader(tmp_path).parse_content(
            path.read_text(encoding="utf-8"), path
        )
        assert parsed.sample_records == [{"note": "x"}]
