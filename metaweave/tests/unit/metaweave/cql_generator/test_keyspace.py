"""键空间契约测试（doc 4 §4.2）

db 前缀统一判定式与 schema/table/column 正则的边界样例——与 NL2SQL
``src/modules/db_deepagent/unit/test_source_binding.py`` 使用同一组输入
输出样例（两模块各自实现，不建立跨模块依赖）。
"""

import pytest

from metaweave.core.cql_generator.keyspace import (
    build_entity_id,
    db_prefix_problem,
    identifier_problem,
    validate_graph_keyspace,
)


@pytest.mark.parametrize(
    "name", ["my orders", "my-orders", "2026orders", "Orders", "a"]
)
def test_valid_db_prefixes(name):
    assert db_prefix_problem(name) is None


@pytest.mark.parametrize(
    "name",
    ["", " orders", "orders ", "my.orders", "order\t s", "order\ns", 123, None],
)
def test_invalid_db_prefixes(name):
    assert db_prefix_problem(name) is not None


@pytest.mark.parametrize("name", ["public", "orders", "_t", "a1_b2"])
def test_valid_identifiers(name):
    assert identifier_problem(name, kind="schema") is None


@pytest.mark.parametrize("name", ["my-schema", "1abc", "a b", "", "a.b", "列名", None])
def test_invalid_identifiers(name):
    assert identifier_problem(name, kind="表") is not None


def test_build_entity_id():
    assert (
        build_entity_id("S-UUID", "orders.public.orders")
        == "S-UUID:orders.public.orders"
    )


class TestValidateGraphKeyspace:
    def test_valid_entries_pass(self):
        validate_graph_keyspace(
            "my-orders",
            [("表", "public", "orders"), ("列", "public", "order_id")],
        )

    def test_any_invalid_object_rejects_whole_source(self):
        with pytest.raises(ValueError, match="整体拒绝"):
            validate_graph_keyspace(
                "orders",
                [
                    ("表", "public", "orders"),
                    ("表", "public", "my-table"),  # 连字符表名
                    ("列", "public", "1bad"),
                ],
            )

    def test_bad_db_prefix_rejects(self):
        with pytest.raises(ValueError, match="库名前缀"):
            validate_graph_keyspace("my.orders", [("表", "public", "orders")])

    def test_message_names_object_and_rule(self):
        with pytest.raises(ValueError, match="my-table"):
            validate_graph_keyspace("orders", [("表", "public", "my-table")])
