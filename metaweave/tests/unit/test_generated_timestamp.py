import re

from metaweave.core.metadata.models import ColumnInfo, TableMetadata


def test_table_metadata_to_dict_uses_generated_timestamp_utc():
    """doc 19.3 §2.2.4：generated_timestamp 为 UTC 六位微秒、Z 结尾。"""
    data = TableMetadata(
        schema_name="public",
        table_name="fact_store_sales_month",
        database="store_db",
        columns=[
            ColumnInfo(
                column_name="id",
                ordinal_position=1,
                data_type="integer",
                is_nullable=False,
            )
        ],
    ).to_dict()

    assert "generated_timestamp" in data
    assert "generated_at" not in data
    assert isinstance(data["generated_timestamp"], str)
    assert re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z",
        data["generated_timestamp"],
    )
