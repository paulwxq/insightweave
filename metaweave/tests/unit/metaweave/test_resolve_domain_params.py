"""_resolve_domain_params 合并逻辑测试

直接导入 metadata_cli 中的真实实现，覆盖 CLI > yaml > 默认值 null 的优先级链。
"""

import pytest

from metaweave.core.metadata.execution import resolve_domain_params


class TestResolveDomainParams:

    def test_both_unset(self):
        assert resolve_domain_params(None, None, {}) == (None, False)

    def test_yaml_domain_all_no_cross(self):
        cfg = {"relationships": {"domain": "all"}}
        assert resolve_domain_params(None, None, cfg) == ("all", False)

    def test_yaml_domain_all_cross_true(self):
        cfg = {"relationships": {"domain": "all", "cross_domain": True}}
        assert resolve_domain_params(None, None, cfg) == ("all", True)

    def test_cli_domain_overrides_yaml(self):
        cfg = {"relationships": {"domain": "all"}}
        assert resolve_domain_params("A,B", None, cfg) == ("A,B", False)

    def test_cli_no_cross_domain_overrides_yaml_true(self):
        cfg = {"relationships": {"domain": "all", "cross_domain": True}}
        assert resolve_domain_params(None, False, cfg) == ("all", False)

    def test_cli_cross_domain_overrides_yaml_false(self):
        cfg = {"relationships": {"domain": "all", "cross_domain": False}}
        assert resolve_domain_params(None, True, cfg) == ("all", True)

    def test_yaml_relationships_null(self):
        cfg = {"relationships": None}
        assert resolve_domain_params(None, None, cfg) == (None, False)

    def test_yaml_relationships_empty_dict(self):
        cfg = {"relationships": {}}
        assert resolve_domain_params(None, None, cfg) == (None, False)

    def test_yaml_no_relationships_key(self):
        cfg = {}
        assert resolve_domain_params(None, None, cfg) == (None, False)

    def test_domain_none_cross_domain_true_ignored(self):
        cfg = {"relationships": {"cross_domain": True}}
        assert resolve_domain_params(None, None, cfg) == (None, False)

    def test_cli_domain_none_cli_cross_true_ignored(self):
        assert resolve_domain_params(None, True, {}) == (None, False)

    def test_yaml_domain_specific(self):
        cfg = {"relationships": {"domain": "订单,库存", "cross_domain": True}}
        assert resolve_domain_params(None, None, cfg) == ("订单,库存", True)

    def test_cli_overrides_both(self):
        cfg = {"relationships": {"domain": "all", "cross_domain": True}}
        assert resolve_domain_params("订单", False, cfg) == ("订单", False)
