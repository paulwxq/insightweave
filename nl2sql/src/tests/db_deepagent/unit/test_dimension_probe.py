"""DimensionProbeBuilder 单元测试（docs/v4/5 §7.6、§18.2 R06）"""

from __future__ import annotations

import pytest

from src.modules.db_deepagent.services.dimension_probe import (
    DimensionProbeBuilder,
    ProbeSpec,
    is_probeable_type,
)

validate = DimensionProbeBuilder.validate

pytestmark = pytest.mark.unit


@pytest.fixture()
def builder() -> DimensionProbeBuilder:
    return DimensionProbeBuilder()


def sql_of(builder, **kwargs) -> str:
    return builder.sql_string(ProbeSpec(**kwargs))


class TestShapes:
    def test_exact_shape(self, builder):
        """exact：DISTINCT + 单等值 + ORDER BY 1 + LIMIT n+1。"""
        s = sql_of(
            builder,
            mode="exact",
            schema_name="sales",
            table="orders",
            column="status",
            value="已支付",
            limit=20,
        )
        assert (
            'SELECT DISTINCT "status" AS value FROM "sales"."orders" '
            "WHERE \"status\" = '已支付' ORDER BY 1 LIMIT 21" == s
        )

    def test_contains_escapes_and_ilike(self, builder):
        """contains：ILIKE + ESCAPE；% _ \\ 转义（psycopg 以 E'' 渲染）。"""
        s = sql_of(
            builder,
            mode="contains",
            schema_name="sales",
            table="orders",
            column="name",
            value="50%_\\折",
            limit=5,
        )
        assert "ILIKE" in s and "ESCAPE" in s
        assert s.endswith("LIMIT 6")
        # 模式构造：\% \_ \\ 逐项转义后包 %...%
        assert builder.contains_pattern("50%_\\折") == "%50\\%\\_\\\\折%"

    def test_distinct_shape(self, builder):
        """distinct：仅 IS NOT NULL，零取值谓词。"""
        s = sql_of(
            builder,
            mode="distinct",
            schema_name="sales",
            table="orders",
            column="channel",
            limit=10,
        )
        assert (
            'SELECT DISTINCT "channel" AS value FROM "sales"."orders" '
            'WHERE "channel" IS NOT NULL ORDER BY 1 LIMIT 11' == s
        )

    def test_build_is_pure(self, builder):
        """同一 spec 恒产生同一 SQL（守卫按重建核对形态）。"""
        spec = ProbeSpec(
            mode="exact", schema_name="s", table="t", column="c", value="v"
        )
        assert builder.sql_string(spec) == builder.sql_string(spec)


class TestValidate:
    def test_unknown_mode(self):
        with pytest.raises(ValueError, match="未知探针模式"):
            validate(
                ProbeSpec(mode="weird", schema_name="s", table="t", column="c")  # type: ignore[arg-type]
            )

    def test_exact_requires_value(self):
        with pytest.raises(ValueError, match="必须提供非空 value"):
            validate(ProbeSpec(mode="exact", schema_name="s", table="t", column="c"))

    def test_distinct_rejects_value(self):
        with pytest.raises(ValueError, match="不接受 value"):
            validate(
                ProbeSpec(
                    mode="distinct", schema_name="s", table="t", column="c", value="v"
                )
            )

    def test_limit_bounds(self):
        with pytest.raises(ValueError, match="limit"):
            validate(
                ProbeSpec(
                    mode="distinct", schema_name="s", table="t", column="c", limit=0
                )
            )

    def test_identifier_rejected(self):
        """标识符注入（含引号／分号）被拒。"""
        with pytest.raises(ValueError, match="不合法"):
            validate(
                ProbeSpec(
                    mode="exact",
                    schema_name="s",
                    table='t"; DROP',
                    column="c",
                    value="v",
                )
            )


class TestProbeableTypes:
    @pytest.mark.parametrize(
        "t", ["text", "varchar", "character varying", "char", "citext", "USER-DEFINED"]
    )
    def test_text_family_and_enum_allowed(self, t):
        assert is_probeable_type(t)

    @pytest.mark.parametrize(
        "t", ["integer", "numeric", "timestamp with time zone", "boolean"]
    )
    def test_non_text_rejected(self, t):
        assert not is_probeable_type(t)
