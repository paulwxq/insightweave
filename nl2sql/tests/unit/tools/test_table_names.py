"""to_display_name 表名截断单元测试（§4.2 唯一翻译点）。

对应改造方案 §4.2：三段表键进提示词前剥掉第一段 db 名，得到两段式显示名。
"""

from src.tools.schema_retrieval.table_names import to_display_name


class TestToDisplayName:
    """表名截断：段数感知，防误剥。"""

    def test_three_segment_strips_db(self):
        """三段表键剥掉首段 db 名，保留真实 schema"""
        assert to_display_name("orders.public.categories") == "public.categories"

    def test_non_public_schema_preserved(self):
        """非 public schema 的三段键截后保留该 schema，而非丢成裸表名"""
        assert to_display_name("orders.sales.orders") == "sales.orders"

    def test_two_segment_idempotent(self):
        """两段输入原样返回（幂等，不误剥）"""
        assert to_display_name("sales.orders") == "sales.orders"

    def test_single_segment_unchanged(self):
        """单段裸表名原样返回"""
        assert to_display_name("orders") == "orders"

    def test_empty_string_unchanged(self):
        """空串原样返回"""
        assert to_display_name("") == ""

    def test_four_segment_strips_only_db(self):
        """四段（列 id，正常不进提示词）也只剥首段，剩余三段"""
        assert (
            to_display_name("orders.public.categories.category_id")
            == "public.categories.category_id"
        )
