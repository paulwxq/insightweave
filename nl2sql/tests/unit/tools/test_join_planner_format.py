"""format_join_plan_for_prompt 提示词边界截断测试（§4.2 出口 4）。

JOIN 计划的 base 与边的 src/dst 表名来自 Neo4j 节点 id（三段表键），
进提示词前必须经 to_display_name 截断为两段式显示名。
"""

from src.tools.schema_retrieval.join_planner import format_join_plan_for_prompt


def _make_plan(base: str, src: str, dst: str) -> dict:
    return {
        "base": base,
        "edges": [
            {
                "src_table": src,
                "dst_table": dst,
                "on": "src.fk = dst.pk",
                "join_type": "INNER JOIN",
            }
        ],
    }


class TestJoinPlanPromptTruncation:
    """三段表键截断：base 与边两侧均剥掉首段 db 名。"""

    def test_base_and_edges_truncated(self):
        """三段 base 与边 src/dst 均截为两段，输出无三段泄漏"""
        text = format_join_plan_for_prompt(
            [
                _make_plan(
                    "orders.public.orders",
                    "orders.public.orders",
                    "orders.public.products",
                )
            ]
        )
        assert "**public.orders**" in text
        assert "- public.orders --INNER JOIN--> public.products" in text
        assert "orders.public." not in text

    def test_two_segment_idempotent(self):
        """两段输入原样返回（幂等，不误剥）"""
        text = format_join_plan_for_prompt(
            [_make_plan("sales.orders", "sales.orders", "sales.customers")]
        )
        assert "**sales.orders**" in text
        assert "sales.orders --INNER JOIN--> sales.customers" in text

    def test_non_public_schema_not_over_stripped(self):
        """非 public schema 三段键只剥 db 段，保留真实 schema"""
        text = format_join_plan_for_prompt(
            [
                _make_plan(
                    "orders.sales.orders",
                    "orders.sales.orders",
                    "orders.public.customers",
                )
            ]
        )
        assert "**sales.orders**" in text
        assert "sales.orders --INNER JOIN--> public.customers" in text
        assert "orders.sales." not in text

    def test_empty_plans_placeholder(self):
        """空计划占位文案不受影响"""
        assert format_join_plan_for_prompt([]) == "（无JOIN，单表查询）"


class TestPlanListNotTruncated:
    """方案 4：formatter 不再截取前三个计划，边数不受展示层限制。"""

    def test_four_plans_all_shown(self):
        """四个计划全部展示（不再保留写死的 [:3]）"""
        plans = [
            _make_plan(
                f"orders.public.base{i}",
                f"orders.public.base{i}",
                "orders.public.products",
            )
            for i in range(4)
        ]

        text = format_join_plan_for_prompt(plans)

        for idx in range(1, 5):
            assert f"Base #{idx}：" in text
        assert "Base #5" not in text

    def test_six_edges_all_kept(self):
        """一个计划含六条 edges：六条 JOIN 关系全部保留"""
        plan = {
            "base": "orders.public.orders",
            "edges": [
                {
                    "src_table": "orders.public.orders",
                    "dst_table": f"orders.public.dim_{i}",
                    "on": f"orders.d{i} = dim_{i}.id",
                    "join_type": "INNER JOIN",
                }
                for i in range(6)
            ],
        }

        text = format_join_plan_for_prompt([plan])

        assert text.count("--INNER JOIN-->") == 6
        for i in range(6):
            assert f"public.dim_{i}" in text
