"""rel 依赖校验先于 clean 的顺序测试（用户审核第 1 条）。

锁定：
- domains 文件缺失 / 为空 / YAML 损坏时，rel 任务失败且**旧产物未被删除**；
- effective_domain 生效时要求 get_all_domains() 非空（DomainResolver 对
  空文件与损坏 YAML 会静默退化为空配置，必须显式拦截）；
- 合法 domains 时 clean 正常先清后载。
"""

from unittest import mock

import pytest
import yaml

from metaweave.core.metadata.execution import (
    MetadataExecutionRequest,
    execute_rel,
)


def _cfg(rel_dir="out/rel", domains_dir="out/domains"):
    return {
        "database": {"database": "demo"},
        "output": {"rel_directory": rel_dir, "domains_directory": domains_dir},
        "relationships": {"domain": "all"},  # 使 effective_domain 生效
    }


@pytest.fixture
def env(tmp_path, monkeypatch):
    """项目根指向 tmp；预置 rel 目录旧产物。"""
    monkeypatch.setattr("metaweave.utils.file_utils.get_project_root", lambda: tmp_path)
    rel_dir = tmp_path / "out" / "rel"
    rel_dir.mkdir(parents=True)
    old = rel_dir / "relationships_global.json"
    old.write_text('{"old": true}', encoding="utf-8")
    (tmp_path / "cfg.yaml").write_text(
        "database:\n  database: demo\n", encoding="utf-8"
    )
    return tmp_path, rel_dir, old


def _run(tmp_path, cfg):
    with mock.patch(
        "metaweave.core.metadata.execution.load_main_config_snapshot",
        return_value=cfg,
    ), mock.patch("click.echo"):
        return execute_rel(
            MetadataExecutionRequest(
                config_path=tmp_path / "cfg.yaml", operation="rel", clean=True
            )
        )


class TestDependencyBeforeClean:
    def test_missing_domains_keeps_old_artifacts(self, env):
        tmp_path, rel_dir, old = env
        result = _run(tmp_path, _cfg())  # domains 文件不存在

        assert result["success"] is False
        assert "不存在" in result["message"]
        assert old.exists(), "domains 缺失时不得删除旧 rel 产物"

    def test_empty_domains_keeps_old_artifacts(self, env):
        tmp_path, rel_dir, old = env
        domains = tmp_path / "out" / "domains"
        domains.mkdir(parents=True)
        (domains / "db_domains.yaml").write_text("", encoding="utf-8")

        result = _run(tmp_path, _cfg())

        assert result["success"] is False
        assert "为空" in result["message"] or "无法解析" in result["message"]
        assert old.exists(), "空 domains 时不得删除旧 rel 产物"

    def test_broken_yaml_keeps_old_artifacts(self, env):
        tmp_path, rel_dir, old = env
        domains = tmp_path / "out" / "domains"
        domains.mkdir(parents=True)
        (domains / "db_domains.yaml").write_text(
            "domains: [unclosed\n", encoding="utf-8"
        )

        result = _run(tmp_path, _cfg())

        assert result["success"] is False
        # DomainResolver 静默退化 → 由 get_all_domains() 空校验拦截
        assert "为空" in result["message"] or "无法解析" in result["message"]
        assert old.exists(), "损坏 YAML 时不得删除旧 rel 产物"

    def test_domains_without_entries_rejected(self, env):
        """domains 段为空列表（无业务域）同样拒绝。"""
        tmp_path, rel_dir, old = env
        domains = tmp_path / "out" / "domains"
        domains.mkdir(parents=True)
        (domains / "db_domains.yaml").write_text(
            yaml.safe_dump({"database": {"name": "demo"}, "domains": []}),
            encoding="utf-8",
        )

        result = _run(tmp_path, _cfg())

        assert result["success"] is False
        assert old.exists()


class TestValidDomainsCleansAndRuns:
    def test_valid_domains_clean_then_discover(self, env):
        """合法 domains：clean 先清后载（旧产物被删，pipeline 被构造）。"""
        tmp_path, rel_dir, old = env
        domains = tmp_path / "out" / "domains"
        domains.mkdir(parents=True)
        (domains / "db_domains.yaml").write_text(
            yaml.safe_dump(
                {
                    "database": {"name": "demo"},
                    "domains": [{"name": "订单", "description": "订单域"}],
                }
            ),
            encoding="utf-8",
        )

        calls = []

        class _DummyPipeline:
            def __init__(
                self,
                config_path,
                domain_filter=None,
                cross_domain=False,
                domain_resolver=None,
                config=None,
            ):
                calls.append("init")
                assert domain_resolver is not None  # 非空校验通过后才构造

            def discover(self):
                from metaweave.core.relationships.models import (
                    RelationshipDiscoveryResult,
                )

                return RelationshipDiscoveryResult(success=True, total_relations=1)

        with mock.patch(
            "metaweave.core.relationships.pipeline.RelationshipDiscoveryPipeline",
            _DummyPipeline,
        ):
            result = _run(tmp_path, _cfg())

        assert result["success"] is True
        assert calls == ["init"]
        assert not old.exists(), "合法路径下 clean 应删除旧产物"
