"""TableCatalog 单元测试（docs/v4/5 §6.5）"""

from __future__ import annotations

import pytest

from src.modules.db_deepagent.config import DbAgentConfig
from src.modules.db_deepagent.errors import InvalidTableIdError
from src.modules.db_deepagent.services.table_catalog import TableCatalog, TableKey
from src.tests.db_deepagent.conftest import FakeBusinessDb, FakeMilvus

pytestmark = pytest.mark.unit


@pytest.fixture()
def catalog(
    db_agent_config: DbAgentConfig,
    fake_milvus: FakeMilvus,
    fake_business_db: FakeBusinessDb,
):
    return TableCatalog(db_agent_config, fake_milvus, fake_business_db)


class TestNormalizeTableId:
    def test_two_segment_gets_prefix(self, catalog, db_agent_config):
        key = catalog.normalize_table_id("public.orders")
        prefix = db_agent_config.business_db.table_key_prefix
        assert key == TableKey(key=f"{prefix}.public.orders", display="public.orders")

    def test_three_segment_valid_prefix(self, catalog, db_agent_config):
        key = catalog.normalize_table_id(
            f"{db_agent_config.business_db.table_key_prefix}.public.orders"
        )
        assert key.key.endswith("public.orders")
        assert key.display == "public.orders"

    def test_three_segment_wrong_prefix_rejected(self, catalog):
        with pytest.raises(InvalidTableIdError, match="首段"):
            catalog.normalize_table_id("other.public.orders")

    @pytest.mark.parametrize(
        "bad", ["one", "a.b.c.d", "bad-table!", 'pub"lic.orders', ""]
    )
    def test_invalid_forms_rejected(self, catalog, bad):
        with pytest.raises(InvalidTableIdError):
            catalog.normalize_table_id(bad)


class TestClassify:
    def test_fact_and_bridge_from_mapping(self, catalog, db_agent_config):
        mapping = db_agent_config.retrieval.table_category_mapping
        fact_value = mapping["fact"][0]
        bridge_value = mapping["bridge"][0]
        assert catalog.classify(fact_value) == "fact"
        assert catalog.classify(fact_value.upper()) == "fact"  # 大小写不敏感
        assert catalog.classify(bridge_value) == "bridge"

    def test_unmapped_and_empty_default_dimension(self, catalog):
        assert catalog.classify("完全未配置类别") == "dimension"
        assert catalog.classify("") == "dimension"


class TestDescribe:
    def test_card_columns_parsed_and_diffed(
        self, catalog, fake_milvus, fake_business_db
    ):
        """卡片列与实时列求差；markdown 列表格式可解析。"""
        fake_milvus.cards["orders.public.orders"] = {
            "object_id": "orders.public.orders",
            "text_raw": (
                "# public.orders [table]\n\n"
                "- order_id (integer) - 主键 [示例: 1]\n"
                "- amount (numeric) - 金额\n"
                "- legacy_col (text) - 已删除列\n"
            ),
            "grain_hint": None,
            "time_col_hint": None,
            "table_category": "",
        }
        [entry] = catalog.describe(
            [catalog.normalize_table_id("public.orders")], include_live_columns=True
        )
        # fake_business_db 默认列：order_id / customer_id / amount
        assert entry["card_only_columns"] == ["legacy_col"]
        assert entry["live_only_columns"] == ["customer_id"]
        assert any("不在实时结构中" in w for w in entry["warnings"])

    def test_card_truncation_flag(self, catalog, fake_milvus):
        fake_milvus.cards["orders.public.orders"] = {
            "object_id": "orders.public.orders",
            "text_raw": "x" * 3000,
            "grain_hint": None,
            "time_col_hint": None,
            "table_category": "",
        }
        [entry] = catalog.describe(
            [catalog.normalize_table_id("public.orders")], include_live_columns=False
        )
        assert entry["card_truncated"] is True
        assert len(entry["card_text"]) == 2000

    def test_missing_card_live_only(self, catalog, fake_milvus):
        [entry] = catalog.describe(
            [catalog.normalize_table_id("public.orders")], include_live_columns=True
        )
        assert entry["card_text"] is None
        assert entry["category_group"] == "dimension"
        assert [c["name"] for c in entry["live_columns"]] == [
            "order_id",
            "customer_id",
            "amount",
        ]

    def test_live_columns_disabled(self, catalog, fake_milvus, fake_business_db):
        fake_milvus.cards["orders.public.orders"] = {
            "object_id": "orders.public.orders",
            "text_raw": "- order_id (integer) - 主键",
            "grain_hint": None,
            "time_col_hint": None,
            "table_category": "",
        }
        [entry] = catalog.describe(
            [catalog.normalize_table_id("public.orders")], include_live_columns=False
        )
        assert "live_columns" not in entry
        assert fake_business_db.queries == []

    def test_unparseable_card_reports_no_structured_columns(self, catalog, fake_milvus):
        """卡片无『标识符(类型)』行 → 视为无结构化列信息，仅列实时清单。"""
        fake_milvus.cards["orders.public.orders"] = {
            "object_id": "orders.public.orders",
            "text_raw": "这张表存储订单信息，包含多个字段。",
            "grain_hint": None,
            "time_col_hint": None,
            "table_category": "",
        }
        [entry] = catalog.describe(
            [catalog.normalize_table_id("public.orders")], include_live_columns=True
        )
        assert any("仅列出实时列清单" in w for w in entry["warnings"])


class TestParseCardColumns:
    def test_markdown_list_and_plain_lines(self):
        text = (
            "- col_a (int) - 注释\ncol_b (text) - 注释\n* col_c (date)\n标题行不带括号"
        )
        assert TableCatalog._parse_card_columns(text) == ["col_a", "col_b", "col_c"]

    def test_no_columns_returns_none(self):
        assert TableCatalog._parse_card_columns("无列信息") is None
