"""DbMilvusRetrieval 单元测试（docs/v4/5 §6.4）

monkeypatch 模块级 pymilvus 符号（connections / milvus_db / utility / Collection），
不触碰真实 Milvus。覆盖：COSINE 分数即相似度并截断到 [0,1]、阈值过滤、字段映射、
dim 检索附加 query_value、SQL 案例 JSON 解析与回退、向量字段维度读取、
连接失败包装为 RetrievalBackendError。
"""

from __future__ import annotations

import pytest

import src.modules.db_deepagent.services.milvus_retrieval as mr
from src.modules.db_deepagent.config import DbAgentConfig
from src.modules.db_deepagent.errors import RetrievalBackendError
from src.modules.db_deepagent.services.milvus_retrieval import DbMilvusRetrieval

pytestmark = pytest.mark.unit

# doc 5.1 §1 短 ID 夹具（doc 12.2 M-1：合法来源为 db_src_N；UUID 输入非法）
SOURCE_A = "db_src_1"
SOURCE_B = "db_src_2"
UUID_SOURCE = "11111111-1111-4111-8111-111111111111"


class _FakeHit:
    def __init__(self, distance: float, entity: dict):
        self.distance = distance
        self.entity = entity


class _FakeSearchResult:
    """collection.search 返回值：可下标取 per-query 命中列表"""

    def __init__(self, hits: list[_FakeHit]):
        self._hits = hits

    def __getitem__(self, i: int) -> list[_FakeHit]:
        assert i == 0
        return self._hits


class _FakeField:
    def __init__(self, name: str, params: dict | None = None):
        self.name = name
        self.params = params


class _FakeSchema:
    def __init__(self, fields: list[_FakeField]):
        self.fields = fields


class _FakeCollection:
    name: str = "fake"
    schema = _FakeSchema([_FakeField("embedding", {"dim": 1024})])
    search_hits: list[_FakeHit] = []
    query_rows: list[dict] = []
    last_search_kwargs: dict = {}
    last_query_kwargs: dict = {}

    def search(self, **kwargs):
        type(self).last_search_kwargs = kwargs
        return _FakeSearchResult(list(self.search_hits))

    def query(self, **kwargs):
        type(self).last_query_kwargs = kwargs
        return list(self.query_rows)


class _FakeConnections:
    def connect(self, **kwargs):
        return None

    def disconnect(self, alias):
        pass


class _FakeMilvusDb:
    databases: list[str] = ["nl2sql"]

    def list_database(self, using):
        return self.databases

    def using_database(self, name, using):
        pass


class _FakeUtility:
    def __init__(self, collections: set[str]):
        self.collections = collections

    def has_collection(self, name, using):
        return name in self.collections


def _config_collections(cfg: DbAgentConfig) -> set[str]:
    return {
        cfg.milvus.collections.table_schema,
        cfg.milvus.collections.dim_value,
        cfg.milvus.collections.sql_example,
    }


@pytest.fixture()
def retrieval(monkeypatch, db_agent_config: DbAgentConfig) -> DbMilvusRetrieval:
    fake = _FakeCollection()
    monkeypatch.setattr(mr, "connections", _FakeConnections())
    monkeypatch.setattr(mr, "milvus_db", _FakeMilvusDb())
    monkeypatch.setattr(
        mr, "utility", _FakeUtility(_config_collections(db_agent_config))
    )
    monkeypatch.setattr(mr, "Collection", lambda name, using: fake)
    return DbMilvusRetrieval(db_agent_config, embedding=None, source_id=SOURCE_A)


def _table_hit(distance, entity=None):
    return _FakeHit(
        distance,
        entity
        or {
            "object_id": "orders.public.orders",
            "table_category": "订单事实表",
            "time_col_hint": "",
        },
    )


class TestConnect:
    def test_missing_database_rejected(self, monkeypatch, db_agent_config):
        db = _FakeMilvusDb()
        db.databases = ["other"]
        monkeypatch.setattr(mr, "connections", _FakeConnections())
        monkeypatch.setattr(mr, "milvus_db", db)
        monkeypatch.setattr(
            mr, "utility", _FakeUtility(_config_collections(db_agent_config))
        )
        monkeypatch.setattr(mr, "Collection", lambda name, using: _FakeCollection())
        with pytest.raises(RetrievalBackendError, match="不存在"):
            DbMilvusRetrieval(db_agent_config, embedding=None, source_id=SOURCE_A)

    def test_missing_collection_rejected(self, monkeypatch, db_agent_config):
        utility = _FakeUtility(set())
        monkeypatch.setattr(mr, "connections", _FakeConnections())
        monkeypatch.setattr(mr, "milvus_db", _FakeMilvusDb())
        monkeypatch.setattr(mr, "utility", utility)
        monkeypatch.setattr(mr, "Collection", lambda name, using: _FakeCollection())
        with pytest.raises(RetrievalBackendError, match="集合不存在"):
            DbMilvusRetrieval(db_agent_config, embedding=None, source_id=SOURCE_A)


class TestSearchTables:
    def test_cosine_distance_is_similarity_and_clamped(self, retrieval):
        # 阈值 -1 保留全部命中，验证 distance→similarity 的 [0,1] 截断
        _FakeCollection.search_hits = [
            _table_hit(1.2),
            _table_hit(-0.1),
            _table_hit(0.7),
        ]
        hits = retrieval.search_tables([0.1], top_k=3, similarity_threshold=-1.0)
        scores = [h["similarity"] for h in hits]
        assert scores[0] == 1.0  # 1.2 截断
        assert scores[1] == 0.0  # -0.1 截断
        assert scores[2] == pytest.approx(0.7)

    def test_negative_distance_filtered_by_zero_threshold(self, retrieval):
        """正常阈值 ≥ 0：负相似度（截断前）直接被阈值过滤。"""
        _FakeCollection.search_hits = [_table_hit(-0.1), _table_hit(0.6)]
        hits = retrieval.search_tables([0.1], top_k=2, similarity_threshold=0.0)
        assert len(hits) == 1
        assert hits[0]["similarity"] == pytest.approx(0.6)

    def test_below_threshold_filtered(self, retrieval):
        _FakeCollection.search_hits = [_table_hit(0.9), _table_hit(0.4)]
        hits = retrieval.search_tables([0.1], top_k=2, similarity_threshold=0.5)
        assert len(hits) == 1
        assert hits[0]["similarity"] == pytest.approx(0.9)

    def test_field_mapping(self, retrieval):
        _FakeCollection.search_hits = [_table_hit(0.8)]
        [hit] = retrieval.search_tables([0.1], top_k=1, similarity_threshold=0.0)
        assert hit["object_id"] == "orders.public.orders"
        assert hit["table_category"] == "订单事实表"
        assert hit["grain_hint"] is None  # 不伪造


class TestSearchDimValues:
    def test_query_value_attached_and_fields_mapped(self, retrieval):
        _FakeCollection.search_hits = [
            _FakeHit(
                0.95,
                {
                    "table_name": "orders.public.products",
                    "col_name": "product_name",
                    "col_value": "商品07",
                },
            )
        ]

        class _Emb:
            def embed_query(self, text):
                return [0.0]

        retrieval._embedding = _Emb()
        [hit] = retrieval.search_dim_values("商品7", top_k=1, min_score=0.5)
        assert hit["query_value"] == "商品7"  # 原文附加，便于追溯
        assert hit["dim_table"] == "orders.public.products"
        assert hit["dim_col"] == "product_name"
        assert hit["matched_text"] == "商品07"
        assert hit["score"] == pytest.approx(0.95)


class TestSearchSimilarSqls:
    def test_question_sql_json_parsed(self, retrieval):
        import json as _json

        _FakeCollection.search_hits = [
            _FakeHit(
                0.8,
                {
                    "example_id": "e1",
                    "domain": "sales",
                    "question_sql": _json.dumps(
                        {"question": "问", "sql": "SELECT 1"}, ensure_ascii=False
                    ),
                },
            )
        ]
        [hit] = retrieval.search_similar_sqls([0.1], top_k=1, similarity_threshold=0.0)
        assert hit["question"] == "问"
        assert hit["sql"] == "SELECT 1"

    def test_invalid_json_falls_back_to_raw(self, retrieval):
        _FakeCollection.search_hits = [
            _FakeHit(
                0.8, {"example_id": "e2", "domain": "", "question_sql": "不是JSON"}
            )
        ]
        [hit] = retrieval.search_similar_sqls([0.1], top_k=1, similarity_threshold=0.0)
        assert hit["sql"] == "不是JSON"
        assert hit["question"] == ""


class TestVectorFieldDim:
    def test_reads_dim_from_schema(self, retrieval):
        assert retrieval.vector_field_dim("t", "embedding") == 1024

    def test_non_vector_field_rejected(self, retrieval, monkeypatch):
        no_vector = _FakeCollection()
        no_vector.schema = _FakeSchema([_FakeField("object_id", None)])
        monkeypatch.setattr(mr, "Collection", lambda name, using: no_vector)
        with pytest.raises(RetrievalBackendError, match="不是向量字段"):
            retrieval.vector_field_dim("t", "object_id")


class TestSourceFilter:
    """doc 4 §3.1：六个入口统一按绑定 source_id 过滤（精确回查同样过滤）。"""

    def test_search_tables_expr_includes_source(self, retrieval):
        retrieval.search_tables([0.1], top_k=1, similarity_threshold=0.0)
        expr = _FakeCollection.last_search_kwargs["expr"]
        assert f'source_id == "{SOURCE_A}"' in expr
        assert 'object_type == "table"' in expr

    def test_search_columns_expr_includes_source(self, retrieval):
        retrieval.search_columns([0.1], top_k=1, similarity_threshold=0.0)
        expr = _FakeCollection.last_search_kwargs["expr"]
        assert f'source_id == "{SOURCE_A}"' in expr
        assert 'object_type == "column"' in expr

    def test_search_dim_values_expr_includes_source(self, retrieval):
        class _Emb:
            def embed_query(self, text):
                return [0.0]

        retrieval._embedding = _Emb()
        retrieval.search_dim_values("x", top_k=1, min_score=0.0)
        assert (
            _FakeCollection.last_search_kwargs["expr"] == f'source_id == "{SOURCE_A}"'
        )

    def test_search_similar_sqls_expr_includes_source(self, retrieval):
        retrieval.search_similar_sqls([0.1], top_k=1, similarity_threshold=0.0)
        assert (
            _FakeCollection.last_search_kwargs["expr"] == f'source_id == "{SOURCE_A}"'
        )

    def test_fetch_table_cards_expr_includes_source(self, retrieval):
        retrieval.fetch_table_cards(["orders.public.orders"])
        expr = _FakeCollection.last_query_kwargs["expr"]
        assert f'source_id == "{SOURCE_A}"' in expr
        assert 'object_id in ["orders.public.orders"]' in expr
        assert 'object_type == "table"' in expr

    def test_fetch_table_categories_expr_includes_source(self, retrieval):
        retrieval.fetch_table_categories(["orders.public.orders"])
        expr = _FakeCollection.last_query_kwargs["expr"]
        assert f'source_id == "{SOURCE_A}"' in expr

    def test_other_source_never_matches_bound_filter(self, retrieval):
        """表达式按绑定源构造：B 源前缀不出现在任何入口的表达式中。"""
        retrieval.search_tables([0.1], top_k=1, similarity_threshold=0.0)
        assert SOURCE_B not in _FakeCollection.last_search_kwargs["expr"]

    def test_missing_source_id_rejected(self, monkeypatch, db_agent_config):
        monkeypatch.setattr(mr, "connections", _FakeConnections())
        monkeypatch.setattr(mr, "milvus_db", _FakeMilvusDb())
        monkeypatch.setattr(
            mr, "utility", _FakeUtility(_config_collections(db_agent_config))
        )
        monkeypatch.setattr(mr, "Collection", lambda name, using: _FakeCollection())
        with pytest.raises(ValueError, match="source_id"):
            DbMilvusRetrieval(db_agent_config, embedding=None)

    def test_invalid_source_id_rejected(self, monkeypatch, db_agent_config):
        monkeypatch.setattr(mr, "connections", _FakeConnections())
        monkeypatch.setattr(mr, "milvus_db", _FakeMilvusDb())
        monkeypatch.setattr(
            mr, "utility", _FakeUtility(_config_collections(db_agent_config))
        )
        monkeypatch.setattr(mr, "Collection", lambda name, using: _FakeCollection())
        with pytest.raises(ValueError, match="短 ID"):
            DbMilvusRetrieval(db_agent_config, embedding=None, source_id="abc")

    def test_uuid_source_id_rejected(self, monkeypatch, db_agent_config):
        """doc 12.2 M-1：UUID 属非法来源（合法身份为 db_src_N 短 ID）。"""
        monkeypatch.setattr(mr, "connections", _FakeConnections())
        monkeypatch.setattr(mr, "milvus_db", _FakeMilvusDb())
        monkeypatch.setattr(
            mr, "utility", _FakeUtility(_config_collections(db_agent_config))
        )
        monkeypatch.setattr(mr, "Collection", lambda name, using: _FakeCollection())
        with pytest.raises(ValueError, match="短 ID"):
            DbMilvusRetrieval(db_agent_config, embedding=None, source_id=UUID_SOURCE)

    def test_constructor_failure_disconnects_own_alias(
        self, monkeypatch, db_agent_config
    ):
        """连接成功但集合缺失（构造抛错、对象未返回）：自行断开本实例
        alias，不残留连接（doc 4 §4.7 构造失败资源回收）。"""
        monkeypatch.setattr(mr, "connections", _FakeConnections())
        monkeypatch.setattr(mr, "milvus_db", _FakeMilvusDb())
        # 集合全部缺失 → _connect 在连接建立后抛 RetrievalBackendError
        monkeypatch.setattr(mr, "utility", _FakeUtility(set()))

        disconnected = []
        import unittest.mock as _mock

        with _mock.patch.object(
            mr.connections, "disconnect", side_effect=lambda a: disconnected.append(a)
        ):
            with pytest.raises(RetrievalBackendError, match="集合不存在"):
                DbMilvusRetrieval(db_agent_config, embedding=None, source_id=SOURCE_A)

        assert len(disconnected) == 1  # 本实例 alias 已断开，无残留
        assert disconnected[0].startswith(db_agent_config.milvus.alias + "_")

    def test_alias_unique_per_instance(self, retrieval, monkeypatch, db_agent_config):
        """doc 4 §3.3：每实例独立 alias（含配置前缀 + 唯一后缀），close 只断开自身。"""
        assert retrieval._alias.startswith(db_agent_config.milvus.alias + "_")
        disconnected = []
        monkeypatch.setattr(
            mr.connections,
            "disconnect",
            lambda alias: disconnected.append(alias),
        )
        second = DbMilvusRetrieval(db_agent_config, embedding=None, source_id=SOURCE_A)
        assert second._alias != retrieval._alias
        retrieval.close()
        assert disconnected == [retrieval._alias]
        second.close()
