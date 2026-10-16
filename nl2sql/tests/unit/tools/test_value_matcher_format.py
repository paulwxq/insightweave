"""format_dim_value_matches_for_prompt 提示词边界截断测试（§4.2 出口 5）。

dim_table 为三段表键（Milvus dim_value_embeddings.table_name 实测三段），
两个展示分支（带主键 / 降级）进提示词前均须截断为两段式显示名。
当前 Milvus 链路不进入带主键分支；该分支的固定输入断言仅作兼容回归保护。
"""

from src.tools.schema_retrieval.value_matcher import (
    format_dim_value_matches_for_prompt,
)


class TestDimValuePromptTruncation:
    """两个展示分支的 dim_table 均截断。"""

    def test_key_col_branch_truncated(self):
        """带 key_col/key_value 分支：dim_table 截为两段"""
        matches = [
            {
                "query_value": "京东便利店",
                "dim_table": "orders.public.dim_store",
                "dim_col": "store_name",
                "key_col": "store_id",
                "key_value": "S001",
                "matched_text": "京东便利店(旗舰店)",
                "score": 0.92,
            }
        ]
        text = format_dim_value_matches_for_prompt(matches)
        assert "public.dim_store.store_id='S001'" in text
        assert "orders.public." not in text

    def test_degraded_branch_truncated(self):
        """降级分支（无主键，Milvus 形态）：dim_table 截为两段"""
        matches = [
            {
                "query_value": "北京",
                "dim_table": "orders.public.dim_city",
                "dim_col": "city_name",
                "matched_text": "北京市",
                "score": 0.85,
            }
        ]
        text = format_dim_value_matches_for_prompt(matches)
        assert "public.dim_city.city_name" in text
        assert "orders.public." not in text

    def test_two_segment_idempotent(self):
        """两段 dim_table 原样返回（幂等）"""
        matches = [
            {
                "query_value": "北京",
                "dim_table": "sales.dim_city",
                "dim_col": "city_name",
                "matched_text": "北京市",
                "score": 0.85,
            }
        ]
        text = format_dim_value_matches_for_prompt(matches)
        assert "sales.dim_city.city_name" in text

    def test_no_matches_placeholder(self):
        """空匹配占位文案不受影响"""
        assert format_dim_value_matches_for_prompt([]) == "（无）"

    def test_below_old_display_threshold_still_shown(self):
        """方案 4：低于旧固定 0.5 显示阈值的达标命中仍展示（无二次分数过滤）"""
        matches = [
            {
                "query_value": "某门店",
                "dim_table": "orders.public.dim_store",
                "dim_col": "store_name",
                "matched_text": "京东便利店",
                "score": 0.45,
            }
        ]
        text = format_dim_value_matches_for_prompt(matches)
        assert "京东便利店" in text
        assert "0.45" in text
