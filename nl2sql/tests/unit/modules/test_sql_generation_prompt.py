"""SQL 生成提示词边界截断测试（§4.2 出口 1-3 + 规则 2 澄清 + 组合断言）。

表卡片 key、表分类 key、时间列行首表名均为三段表键，
进提示词前必须经 to_display_name 截断为两段式显示名；
完整 _build_prompt 输出不得含三段表键泄漏（§5.3 组合断言）。
"""

import re
from typing import Any, Dict

import pytest

from src.modules.sql_generation.subgraph.nodes.sql_generation import SQLGenerationAgent


def _self_stub() -> SQLGenerationAgent:
    """跳过 LLM 初始化的真实实例（__init__ 会连 LLM，与 formatter 无关）。"""
    stub = object.__new__(SQLGenerationAgent)
    stub.dimension_filter_min_score = 0.5
    return stub


class TestTableCardsTruncation:
    """出口 1：_format_table_cards 卡片 key 截断。"""

    def test_card_key_truncated(self):
        cards = {
            "orders.public.categories": {
                "text_raw": "# public.categories [table]\ncategory_id, category_name",
                "time_col_hint": None,
            }
        }
        text = SQLGenerationAgent._format_table_cards(_self_stub(), cards)
        assert "- **public.categories**" in text
        assert "orders.public." not in text

    def test_card_key_two_segment_idempotent(self):
        cards = {"sales.orders": {"text_raw": "订单表", "time_col_hint": None}}
        text = SQLGenerationAgent._format_table_cards(_self_stub(), cards)
        assert "- **sales.orders**" in text

    def test_card_key_non_public_schema_preserved(self):
        cards = {"orders.sales.orders": {"text_raw": "销售订单", "time_col_hint": None}}
        text = SQLGenerationAgent._format_table_cards(_self_stub(), cards)
        assert "- **sales.orders**" in text
        assert "orders.sales." not in text

    def test_more_than_ten_cards_all_shown(self):
        """方案 4：formatter 展示上游全部卡片，无 10 张或其他数量截断"""
        cards = {
            f"orders.public.t{i:02d}": {"text_raw": f"表{i}", "time_col_hint": None}
            for i in range(12)
        }

        text = SQLGenerationAgent._format_table_cards(_self_stub(), cards)

        assert text.count("- **") == 12
        assert all(f"public.t{i:02d}" in text for i in range(12))


class TestTableCategoriesTruncation:
    """出口 2：_format_table_categories 分类列表中的表名截断。"""

    def test_category_tables_truncated(self):
        schema_context: Dict[str, Any] = {
            "table_categories": {
                "orders.public.orders": "交易表",
                "orders.public.categories": "维度表",
            }
        }
        text = SQLGenerationAgent._format_table_categories(_self_stub(), schema_context)
        assert "public.orders" in text
        assert "public.categories" in text
        assert "orders.public." not in text

    def test_category_empty(self):
        text = SQLGenerationAgent._format_table_categories(_self_stub(), {})
        assert text == "（无分类信息）"


class TestTimeColumnsTruncation:
    """出口 3：_format_time_columns 行首表名截断。"""

    def test_time_column_table_truncated(self):
        cards = {
            "orders.public.orders": {"text_raw": "x", "time_col_hint": "created_at"},
            "orders.public.categories": {"text_raw": "y", "time_col_hint": None},
        }
        text = SQLGenerationAgent._format_time_columns(_self_stub(), cards)
        assert "- public.orders.created_at" in text
        assert "orders.public." not in text

    def test_time_column_none_when_no_hint(self):
        cards = {"orders.public.categories": {"text_raw": "y", "time_col_hint": None}}
        text = SQLGenerationAgent._format_time_columns(_self_stub(), cards)
        assert text == "（无明确时间列）"


class TestBuildPromptBoundary:
    """组合断言（§5.3）：字段齐全的 schema_context 下，完整提示词无三段表键泄漏。"""

    def test_full_prompt_no_three_segment_leak(self):
        schema_context: Dict[str, Any] = {
            "table_cards": {
                "orders.public.orders": {
                    "text_raw": "订单表：order_id, product_id, amount, created_at",
                    "time_col_hint": "created_at",
                },
                "orders.public.categories": {
                    "text_raw": "分类表：category_id, category_name",
                    "time_col_hint": None,
                },
            },
            "table_categories": {
                "orders.public.orders": "交易表",
                "orders.public.categories": "维度表",
            },
            "join_plans": [
                {
                    "base": "orders.public.orders",
                    "edges": [
                        {
                            "src_table": "orders.public.orders",
                            "dst_table": "orders.public.categories",
                            "on": "orders.category_id = categories.category_id",
                            "join_type": "INNER JOIN",
                        }
                    ],
                }
            ],
            "dim_value_hits": [
                {
                    "query_value": "零食",
                    "dim_table": "orders.public.categories",
                    "dim_col": "category_name",
                    "matched_text": "零食类",
                    "score": 0.88,
                }
            ],
        }
        prompt = SQLGenerationAgent._build_prompt(
            _self_stub(),
            query="查询零食类订单总额",
            schema_context=schema_context,
            similar_sqls=[],
            parse_result=None,
            dependencies_results=None,
            validation_errors=None,
            conversation_history=None,
        )
        assert "orders.public." not in prompt
        # 五类出口的两段名均应出现在完整提示词中
        assert "**public.orders**" in prompt  # 表卡片 / JOIN base
        assert "public.categories" in prompt  # 分类 / 维度值 / 边
        assert "- public.orders.created_at" in prompt  # 时间列

    def test_rule2_schema_clarification_present(self):
        """规则 2 澄清：schema 以检索结果为准（§4.2 配套）"""
        prompt = SQLGenerationAgent._build_prompt(
            _self_stub(),
            query="任意问题",
            schema_context={},
            similar_sqls=[],
            parse_result=None,
            dependencies_results=None,
            validation_errors=None,
            conversation_history=None,
        )
        assert "所有表必须包含 schema 前缀" in prompt
        assert "表名的 schema 以检索结果提供的为准" in prompt


class TestHistoricalSQLPrompt:
    """检索返回的合格案例全部展示，零命中隐藏整个章节。"""

    @pytest.mark.parametrize("count", [3, 5])
    def test_all_returned_cases_enter_prompt(self, count: int) -> None:
        """prompt 不再截取数量或追加相似度筛选，保持输入顺序。"""
        cases = [
            {
                "question": f"历史案例问题{idx}",
                "sql": f"SELECT {idx} AS example_id",
                "similarity": score,
            }
            for idx, score in enumerate([0.72, 0.62, 0.61, 0.60, 0.60][:count], 1)
        ]
        prompt = _self_stub()._build_prompt(
            query="当前问题",
            schema_context={},
            similar_sqls=cases,
            parse_result=None,
            dependencies_results=None,
            validation_errors=None,
            conversation_history=None,
        )

        assert "历史成功SQL案例（参考）" in prompt
        assert re.findall(r"^(\d+)\. 相似度：", prompt, re.MULTILINE) == [
            str(idx) for idx in range(1, count + 1)
        ]
        assert "相似度：0.62" in prompt
        positions = []
        for case in cases:
            assert prompt.count(case["question"]) == 1
            assert prompt.count(case["sql"]) == 1
            positions.append(prompt.index(case["question"]))
        assert positions == sorted(positions)

    def test_empty_cases_hide_entire_section(self) -> None:
        """零命中时隐藏标题及 formatter 的空列表占位正文。"""
        prompt = _self_stub()._build_prompt(
            query="当前问题",
            schema_context={},
            similar_sqls=[],
            parse_result=None,
            dependencies_results=None,
            validation_errors=None,
            conversation_history=None,
        )

        assert "历史成功SQL案例（参考）" not in prompt
        assert "（无相似案例）" not in prompt


class TestDimValuePromptExit:
    """方案 4 §五.3：低于旧固定 0.5 显示阈值、但达到检索配置阈值的命中，
    经 _build_prompt 最终出口仍进入"维度值匹配"段（真实 prompt 链路无二次过滤）。
    """

    def test_hit_below_old_display_threshold_reaches_prompt(self) -> None:
        """检索阈值 0.4 < 命中分数 0.45 < 旧显示阈值 0.5：仍出现在该段"""
        schema_context: Dict[str, Any] = {
            "dim_value_hits": [
                {
                    "query_value": "零食",
                    "dim_table": "orders.public.categories",
                    "dim_col": "category_name",
                    "matched_text": "零食类",
                    "score": 0.45,
                }
            ]
        }
        prompt = _self_stub()._build_prompt(
            query="查询零食类订单总额",
            schema_context=schema_context,
            similar_sqls=[],
            parse_result=None,
            dependencies_results=None,
            validation_errors=None,
            conversation_history=None,
        )

        section = prompt[prompt.index("维度值匹配（用于 WHERE 条件；仅在有匹配时使用）：") :]
        assert "零食类" in section
        assert "0.45" in section
