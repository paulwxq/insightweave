"""doc 23.1 §5.5：管线把 database.database 作为 db_name 传给 DomainResolver。

rel 管线的 available_tables 为两段式 schema.table，db_domains.yaml 表名为
三段式——管线在 `_resolve_table_pairs` 与 LLM 发现器的独立调用场景中均需
把 `database.database` 传入 `resolve_table_pairs(db_name=...)`。
"""

import json

from metaweave.core.relationships.llm_relationship_discovery import (
    LLMRelationshipDiscovery,
)
from metaweave.core.relationships.pipeline import RelationshipDiscoveryPipeline


class _FakeResolver:
    def __init__(self):
        self.captured = {}

    def resolve_table_pairs(
        self, available_tables, domain_filter=None, cross_domain=False, db_name=None
    ):
        self.captured = {
            "available": list(available_tables),
            "domain_filter": domain_filter,
            "cross_domain": cross_domain,
            "db_name": db_name,
        }
        return [("public.a", "public.b")]


class TestPipelineDbNamePassThrough:
    def test_resolve_table_pairs_passes_db_name(self, monkeypatch, tmp_path):
        """管线构造读取 database.database，_resolve_table_pairs 传给 resolver"""
        from metaweave.core.relationships import pipeline as pipeline_mod

        class _FakeConnector:
            def __init__(self, config):
                self.config = config

            def close(self):
                pass

        monkeypatch.setattr(pipeline_mod, "DatabaseConnector", _FakeConnector)

        json_dir = tmp_path / "json"
        json_dir.mkdir()
        (json_dir / "orders.public.a.json").write_text(
            json.dumps({"object_info": {"schema_name": "public", "object_name": "a"}}),
            encoding="utf-8",
        )

        config = {
            "database": {"database": "orders"},
            "output": {
                "json_directory": str(json_dir),
                "rel_directory": str(tmp_path / "rel"),
            },
            "relationships": {"llm_candidates": {"enabled": False}},
        }
        resolver = _FakeResolver()
        pipeline = RelationshipDiscoveryPipeline(
            tmp_path / "config.yaml",
            domain_filter="all",
            cross_domain=False,
            domain_resolver=resolver,
            config=config,
        )
        assert pipeline.db_name == "orders"

        pairs = pipeline._resolve_table_pairs({"public.a": {}, "public.b": {}})
        assert resolver.captured["db_name"] == "orders"
        assert resolver.captured["available"] == ["public.a", "public.b"]
        assert resolver.captured["domain_filter"] == "all"
        assert pairs == [("public.a", "public.b")]


class TestLLMDiscoveryDbNamePassThrough:
    def test_independent_resolve_passes_db_name(self):
        """独立调用场景：LLMRelationshipDiscovery 从 config 取 database.database"""
        discovery = LLMRelationshipDiscovery.__new__(LLMRelationshipDiscovery)
        discovery.config = {"database": {"database": "orders"}}
        discovery.domain_filter = "all"
        discovery.cross_domain = False
        resolver = _FakeResolver()
        discovery.domain_resolver = resolver

        discovery.resolve_table_pairs({"public.a": {}, "public.b": {}})
        assert resolver.captured["db_name"] == "orders"
        assert resolver.captured["available"] == ["public.a", "public.b"]
