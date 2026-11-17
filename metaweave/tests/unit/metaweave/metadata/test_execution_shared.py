"""共享执行层地基测试（doc 26 §6.2）：normalize 函数与请求/映射契约。"""

import pytest

from metaweave.core.metadata.execution import (
    METADATA_OPERATIONS,
    OPERATION_STEP_MAP,
    MetadataExecutionRequest,
    normalize_generate_domains_options,
)


class TestNormalizeGenerateDomainsOptions:
    @pytest.mark.parametrize("desc", [None, "", "   ", "\t\n "])
    def test_description_blank_to_none(self, desc):
        assert normalize_generate_domains_options(desc, None, None).description is None

    @pytest.mark.parametrize("desc", ["  订单业务  ", "dvd 租赁"])
    def test_description_stripped(self, desc):
        assert (
            normalize_generate_domains_options(desc, None, None).description
            == desc.strip()
        )

    @pytest.mark.parametrize("bad", [123, 1.5, True, ["a"], {"k": 1}])
    def test_description_non_string_rejected(self, bad):
        """非字符串 description 拒绝（不做 str() 强转；用户审核第 7 条）。"""
        with pytest.raises(ValueError) as ei:
            normalize_generate_domains_options(bad, None, None)
        assert "description 必须是字符串或 None" in str(ei.value)

    def test_mode_default_and_valid(self):
        assert (
            normalize_generate_domains_options(None, None, None).md_context_mode
            == "name_comment"
        )
        for m in ("name", "name_comment", "full"):
            assert (
                normalize_generate_domains_options(None, m, None).md_context_mode == m
            )

    @pytest.mark.parametrize("mode", ["FULL", "Name_Comment", "namecomment", 1, True])
    def test_mode_rejects_non_canonical(self, mode):
        with pytest.raises(ValueError):
            normalize_generate_domains_options(None, mode, None)

    def test_limit_none_means_uncovered(self):
        assert normalize_generate_domains_options(None, None, None).md_limit is None

    @pytest.mark.parametrize("limit", [1, 5, 100])
    def test_limit_positive_int_ok(self, limit):
        assert normalize_generate_domains_options(None, None, limit).md_limit == limit

    @pytest.mark.parametrize("limit", [0, -1, "5", 1.5, True, False])
    def test_limit_rejects_non_positive_or_non_int(self, limit):
        with pytest.raises(ValueError):
            normalize_generate_domains_options(None, None, limit)


class TestRequestAndMap:
    def test_request_rejects_unknown_operation(self, tmp_path):
        with pytest.raises(ValueError):
            MetadataExecutionRequest(tmp_path / "c.yaml", "standard")

    def test_request_accepts_all_eight(self, tmp_path):
        for op in METADATA_OPERATIONS:
            MetadataExecutionRequest(tmp_path / "c.yaml", op)

    def test_step_map_covers_all_operations(self):
        # doc 21.1 §5.1：方向为 ops ⊆ map——每个 operation 必须有映射，
        # map 允许额外包含 Web 步骤（domain/dim）。
        assert set(METADATA_OPERATIONS) <= set(OPERATION_STEP_MAP)
        # doc 6 §2：generate-dim-config 复用更名后的 dim 日志段
        assert OPERATION_STEP_MAP["generate-dim-config"] == "dim"
        assert OPERATION_STEP_MAP["generate-domains"] == "generate-domains"

    def test_step_map_includes_web_domain_step(self):
        """Web 步骤 domain 只进映射、不进 METADATA_OPERATIONS（doc 21.1 §5.1）。"""
        assert OPERATION_STEP_MAP["domain"] == "domain"
        assert "domain" not in METADATA_OPERATIONS

    def test_step_map_includes_web_dim_step(self):
        """Web 步骤 dim（doc 6 §2 由 dim_config 更名）只进映射、不进 METADATA_OPERATIONS。"""
        assert OPERATION_STEP_MAP["dim"] == "dim"
        assert "dim" not in METADATA_OPERATIONS

    def test_step_map_includes_web_sql_step(self):
        """Web sql 复用 SQL 生成日志段，CLI 仍使用 generate-sql-rag。"""
        assert OPERATION_STEP_MAP["sql"] == "sql-rag-generate"
        assert "sql" not in METADATA_OPERATIONS

    def test_set_operation_step_dim_no_keyerror(self):
        """doc 22.1 A11a：set_operation_step("dim") 不抛 KeyError。"""
        from metaweave.core.metadata.execution import set_operation_step

        set_operation_step("dim")

    def test_step_map_no_empty_values(self):
        assert all(v for v in OPERATION_STEP_MAP.values())
