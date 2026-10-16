"""Milvus 适配器单元测试

重点测试：
1. COSINE 相似度的"先raw过滤、再clamp"原则
2. 字段映射正确性（object_desc → text_raw, time_col_hint, grain_hint=None）
3. 配置验证（清晰失败原则）
4. JSON 序列化的精确查询
5. 三段表键契约（§4.1）：表侧 object_id 键、列侧 object_id+parent_id 两键、
   无 db_name 过滤、dim 表向量字段 col_value_embedding
"""

import json
import logging
from unittest.mock import Mock, patch

import pytest

from src.services.vector_adapter.milvus_adapter import MilvusSearchAdapter


@pytest.fixture
def valid_config():
    """有效的 Milvus 配置"""
    return {
        "providers": {
            "milvus": {
                "host": "localhost",
                "port": 19530,
                "database": "nl2sql",
                "user": "root",
                "password": "Milvus",
            }
        }
    }


@pytest.fixture
def search_params():
    """Milvus 搜索参数"""
    return {"metric_type": "COSINE", "params": {"ef": 100}}


@pytest.fixture
def mock_milvus_client():
    """Mock MilvusClient"""
    with patch("src.services.vector_adapter.milvus_adapter.MilvusClient") as mock:
        client_instance = Mock()
        client_instance.alias = "default"
        client_instance.connect = Mock()
        mock.return_value = client_instance
        yield client_instance


@pytest.fixture
def mock_collection():
    """Mock Milvus Collection"""
    collection = Mock()
    return collection


@pytest.fixture
def mock_embedding_client():
    """Mock Embedding Client"""
    with patch(
        "src.services.vector_adapter.milvus_adapter.get_embedding_client"
    ) as mock:
        client = Mock()
        client.embed_query.return_value = [0.1] * 768
        mock.return_value = client
        yield client


@pytest.fixture
def mock_lazy_import():
    """Mock _lazy_import_milvus"""
    with patch(
        "src.services.vector_adapter.milvus_adapter._lazy_import_milvus"
    ) as mock:
        Collection = Mock()
        mock.return_value = (None, None, None, None, Collection, None, None)
        yield Collection


class TestMilvusAdapterInitialization:
    """Milvus 适配器初始化测试"""

    def test_valid_config_initialization(
        self, valid_config, search_params, mock_milvus_client, mock_lazy_import
    ):
        """测试有效配置初始化"""
        adapter = MilvusSearchAdapter(config=valid_config, search_params=search_params)

        assert adapter.config == valid_config
        assert adapter.search_params == search_params
        mock_milvus_client.connect.assert_called_once()

    def test_missing_milvus_config(self):
        """测试缺少 Milvus 配置时抛出异常"""
        config = {"providers": {}}  # 缺少 milvus 配置

        with pytest.raises(ValueError, match="Milvus 配置缺失"):
            MilvusSearchAdapter(config=config)

    def test_missing_required_fields(self):
        """测试缺少必需字段时抛出异常"""
        config = {
            "providers": {
                "milvus": {
                    "host": "localhost",
                    # 缺少 database 字段
                }
            }
        }

        with pytest.raises(ValueError, match="Milvus 配置不完整.*database"):
            MilvusSearchAdapter(config=config)

    def test_default_search_params(
        self, valid_config, mock_milvus_client, mock_lazy_import
    ):
        """测试默认搜索参数"""
        adapter = MilvusSearchAdapter(config=valid_config)

        assert adapter.search_params["metric_type"] == "COSINE"
        assert adapter.search_params["params"]["ef"] == 100


class TestCOSINEDistanceConversion:
    """COSINE 相似度处理测试（核心测试）"""

    @pytest.mark.parametrize(
        "similarity,expected_raw,expected_clamped,should_pass_threshold",
        [
            (0.0, 0.0, 0.0, True),  # 零相似度（边界）
            (0.2, 0.2, 0.2, True),  # 低相似度
            (0.5, 0.5, 0.5, True),  # 阈值边界
            (1.0, 1.0, 1.0, True),  # 完全相同
            (-0.2, -0.2, 0.0, False),  # 负相似度（应被过滤）
            (-1.0, -1.0, 0.0, False),  # 极端负相似度
        ],
    )
    def test_cosine_conversion_logic(
        self,
        similarity,
        expected_raw,
        expected_clamped,
        should_pass_threshold,
    ):
        """测试 COSINE 相似度处理逻辑（先raw过滤、再clamp）"""
        raw_similarity = similarity
        assert abs(raw_similarity - expected_raw) < 1e-10  # 允许浮点误差

        # 验证阈值过滤（使用 raw_similarity）
        threshold = 0.0
        passes_threshold = raw_similarity >= threshold
        assert passes_threshold == should_pass_threshold

        # 验证 clamp（用于返回值）
        similarity = max(0.0, min(1.0, raw_similarity))
        assert abs(similarity - expected_clamped) < 1e-10  # 允许浮点误差

    def test_negative_similarity_filtered_before_clamp(
        self, valid_config, mock_milvus_client, mock_lazy_import
    ):
        """测试负相似度在 clamp 之前被过滤（关键测试）"""
        adapter = MilvusSearchAdapter(config=valid_config)

        # Mock Collection.search() 返回负相似度结果
        mock_collection = Mock()
        mock_hit = Mock()
        mock_hit.distance = -0.2
        mock_hit.entity = {
            "object_id": "orders.public.table1",
            "object_type": "table",
            "table_category": "fact",
            "time_col_hint": "created_at",
        }

        mock_collection.search.return_value = [[mock_hit]]
        adapter._collection_table_schema = mock_collection

        # 调用 search_tables，阈值为 0
        results = adapter.search_tables(
            embedding=[0.1] * 768, top_k=10, similarity_threshold=0.0
        )

        # 验证：负相似度应被过滤，结果为空
        assert len(results) == 0


class TestFieldMapping:
    """字段映射测试"""

    def test_fetch_table_cards_field_mapping(
        self, valid_config, mock_milvus_client, mock_lazy_import
    ):
        """测试 fetch_table_cards() 的字段映射（object_desc → text_raw）"""
        adapter = MilvusSearchAdapter(config=valid_config)

        # Mock Collection.query() 返回
        mock_collection = Mock()
        mock_collection.query.return_value = [
            {
                "object_id": "orders.public.table1",  # 三段表键
                "object_desc": "表1的描述信息",  # Milvus 字段
                "time_col_hint": "created_at",
                "table_category": "fact",
            }
        ]
        adapter._collection_table_schema = mock_collection

        # 调用 fetch_table_cards
        result = adapter.fetch_table_cards(table_names=["orders.public.table1"])

        # 验证字段映射（结果以三段表键为键）
        assert result["orders.public.table1"]["text_raw"] == "表1的描述信息"
        assert result["orders.public.table1"]["object_id"] == "orders.public.table1"
        assert result["orders.public.table1"]["grain_hint"] is None  # Milvus 无此字段
        assert result["orders.public.table1"]["time_col_hint"] == "created_at"
        assert result["orders.public.table1"]["table_category"] == "fact"

    def test_search_tables_returns_time_col_hint(
        self, valid_config, mock_milvus_client, mock_lazy_import
    ):
        """测试 search_tables() 返回 time_col_hint 字段"""
        adapter = MilvusSearchAdapter(config=valid_config)

        # Mock Collection.search()
        mock_collection = Mock()
        mock_hit = Mock()
        mock_hit.distance = 0.8
        mock_hit.entity = {
            "object_id": "orders.public.fact_sales",  # 三段表键（表记录唯一键）
            "object_type": "table",
            "table_category": "fact",
            "time_col_hint": "sale_date",
        }
        mock_collection.search.return_value = [[mock_hit]]
        adapter._collection_table_schema = mock_collection

        # 调用 search_tables
        results = adapter.search_tables(
            embedding=[0.1] * 768, top_k=10, similarity_threshold=0.5
        )

        # 验证 time_col_hint 被正确返回，且表侧键 = object_id（不再输出 table_name）
        assert len(results) == 1
        assert results[0]["object_id"] == "orders.public.fact_sales"
        assert "table_name" not in results[0]  # §4.1-1
        assert results[0]["time_col_hint"] == "sale_date"
        assert results[0]["grain_hint"] is None  # Milvus 无此字段
        # 验证 output_fields 契约（§4.1-1：不再取 table_name 字段）
        assert mock_collection.search.call_args.kwargs["output_fields"] == [
            "object_id",
            "object_type",
            "table_category",
            "time_col_hint",
        ]

    def test_search_columns_grain_hint_none(
        self, valid_config, mock_milvus_client, mock_lazy_import
    ):
        """测试 search_columns() 返回 grain_hint=None"""
        adapter = MilvusSearchAdapter(config=valid_config)

        # Mock Collection.search()
        mock_collection = Mock()
        mock_hit = Mock()
        mock_hit.distance = 0.7
        mock_hit.entity = {
            "object_id": "orders.public.table1.col1",  # 四段列全名
            "parent_id": "orders.public.table1",  # 所属表的三段表键
            "object_type": "column",
        }
        mock_collection.search.return_value = [[mock_hit]]
        adapter._collection_table_schema = mock_collection

        # 调用 search_columns
        results = adapter.search_columns(
            embedding=[0.1] * 768, top_k=10, similarity_threshold=0.5
        )

        # 验证：object_id + parent_id 两键，无 table_name/table_category
        assert len(results) == 1
        assert results[0]["object_id"] == "orders.public.table1.col1"
        assert results[0]["parent_id"] == "orders.public.table1"
        assert "table_name" not in results[0]  # §4.1-2：列侧归属改用 parent_id
        assert "table_category" not in results[0]
        assert results[0]["grain_hint"] is None
        # 验证 output_fields 契约（§4.1-2）
        assert mock_collection.search.call_args.kwargs["output_fields"] == [
            "object_id",
            "parent_id",
            "object_type",
        ]

    def test_search_dim_values_missing_key_fields(
        self, valid_config, mock_milvus_client, mock_lazy_import, mock_embedding_client
    ):
        """测试 search_dim_values() 不返回 key_col/key_value（Milvus 无此字段）"""
        adapter = MilvusSearchAdapter(config=valid_config)

        # Mock Collection.search()
        mock_collection = Mock()
        mock_hit = Mock()
        mock_hit.distance = 0.85
        mock_hit.entity = {
            "table_name": "orders.public.dim_city",  # 三段表键
            "col_name": "city_name",
            "col_value": "北京市",
        }
        mock_collection.search.return_value = [[mock_hit]]
        adapter._collection_dim_value = mock_collection

        # 调用 search_dim_values
        results = adapter.search_dim_values(query_value="北京", top_k=5)

        # 验证：不返回 key_col/key_value
        assert len(results) == 1
        assert "key_col" not in results[0]
        assert "key_value" not in results[0]
        assert results[0]["dim_table"] == "orders.public.dim_city"
        assert results[0]["dim_col"] == "city_name"
        assert results[0]["matched_text"] == "北京市"
        # §4.1-3：db_name 过滤已删（expr=None），向量字段更名为 col_value_embedding
        search_kwargs = mock_collection.search.call_args.kwargs
        assert search_kwargs["expr"] is None
        assert search_kwargs["anns_field"] == "col_value_embedding"


class TestJSONSerialization:
    """JSON 序列化测试（精确查询安全性）"""

    def test_fetch_table_cards_json_serialization(
        self, valid_config, mock_milvus_client, mock_lazy_import
    ):
        """测试 fetch_table_cards() 使用 JSON 序列化"""
        adapter = MilvusSearchAdapter(config=valid_config)

        # Mock Collection.query()
        mock_collection = Mock()
        mock_collection.query.return_value = []
        adapter._collection_table_schema = mock_collection

        # 调用 fetch_table_cards
        table_names = ["orders.public.table1", "orders.public.table2"]
        adapter.fetch_table_cards(table_names=table_names)

        # 验证 expr 使用 JSON 序列化（双引号，而非单引号）
        call_args = mock_collection.query.call_args
        expr = call_args.kwargs["expr"]

        # 验证使用 JSON 格式（双引号列表），且以 object_id 为过滤键（§4.1-5）
        assert (
            '["orders.public.table1", "orders.public.table2"]' in expr
            or '["orders.public.table2", "orders.public.table1"]' in expr
        )  # 顺序可能不同
        assert "object_id in" in expr

    def test_fetch_table_categories_json_serialization(
        self, valid_config, mock_milvus_client, mock_lazy_import
    ):
        """测试 fetch_table_categories() 使用 JSON 序列化"""
        adapter = MilvusSearchAdapter(config=valid_config)

        # Mock Collection.query()
        mock_collection = Mock()
        mock_collection.query.return_value = []
        adapter._collection_table_schema = mock_collection

        # 调用 fetch_table_categories
        table_names = ["orders.public.dim_store"]
        adapter.fetch_table_categories(table_names=table_names)

        # 验证 expr 使用 JSON 序列化
        call_args = mock_collection.query.call_args
        expr = call_args.kwargs["expr"]

        assert '["orders.public.dim_store"]' in expr
        assert "object_id in" in expr  # §4.1-6：表键过滤字段改为 object_id
        assert 'object_type == "table"' in expr


class TestSearchMethods:
    """搜索方法测试"""

    def test_search_similar_sqls_returns_matches(
        self, valid_config, mock_milvus_client, mock_lazy_import
    ):
        """测试 search_similar_sqls() 返回解析后的历史 SQL"""
        adapter = MilvusSearchAdapter(config=valid_config)
        mock_collection = Mock()

        mock_hit = Mock()
        mock_hit.distance = 0.88
        mock_hit.entity = {
            "example_id": "test_db:abc123",
            "domain": "测试域",
            "question_sql": (
                '{"question": "查询昨天销售额", '
                '"sql": "SELECT sum(amount) FROM public.fact_sales"}'
            ),
        }
        mock_collection.search.return_value = [[mock_hit]]
        adapter._collection_sql_example = mock_collection

        result = adapter.search_similar_sqls(
            embedding=[0.1] * 768, top_k=3, similarity_threshold=0.6
        )

        assert len(result) == 1
        assert result[0]["question"] == "查询昨天销售额"
        assert result[0]["sql"] == "SELECT sum(amount) FROM public.fact_sales"
        assert result[0]["similarity"] == 0.88
        # §4.1-4：db_name 过滤已删（expr=None）
        assert mock_collection.search.call_args.kwargs["expr"] is None

    def test_search_dim_values_with_embedding(
        self, valid_config, mock_milvus_client, mock_lazy_import, mock_embedding_client
    ):
        """测试 search_dim_values() 对 query_value 进行向量化"""
        adapter = MilvusSearchAdapter(config=valid_config)

        # Mock Collection.search()
        mock_collection = Mock()
        mock_collection.search.return_value = [[]]
        adapter._collection_dim_value = mock_collection

        # 调用 search_dim_values
        adapter.search_dim_values(query_value="测试值", top_k=5)

        # 验证 embedding_client.embed_query() 被调用，且无 db_name 过滤（§4.1-3）
        mock_embedding_client.embed_query.assert_called_once_with("测试值")
        assert mock_collection.search.call_args.kwargs["expr"] is None


class TestHistoricalSQLRetrieval:
    """历史 SQL 的请求上限、相似度边界和 DEBUG 候选日志。"""

    @pytest.fixture
    def adapter(
        self, valid_config, mock_milvus_client, mock_lazy_import, mock_embedding_client
    ) -> MilvusSearchAdapter:
        """隔离客户端，保留真实的适配器检索实现。"""
        adapter = MilvusSearchAdapter(config=valid_config)
        adapter._collection_sql_example = Mock()
        return adapter

    @staticmethod
    def _hit(similarity: float, idx: int) -> Mock:
        """生成包含完整原始载荷的 Milvus 候选。"""
        return Mock(
            distance=similarity,
            entity={
                "example_id": f"case_{idx}",
                "domain": "测试域",
                "question_sql": json.dumps(
                    {"question": f"问题{idx}", "sql": f"SELECT {idx}\nFROM public.test"},
                    ensure_ascii=False,
                ),
            },
        )

    @pytest.mark.parametrize(
        "scores,threshold,expected",
        [
            ([0.90, 0.70, 0.65, 0.64, 0.50], 0.65, [0.90, 0.70, 0.65]),
            ([0.90, 0.80, 0.75, 0.70, 0.65], 0.65, [0.90, 0.80, 0.75, 0.70, 0.65]),
            ([0.90, 0.70], 0.65, [0.90, 0.70]),
            ([0.64, 0.50], 0.65, []),
            ([], 0.65, []),
            ([0.65, 0.649999999], 0.65, [0.65]),
            ([1.02, 1.0], 1.01, [1.0]),
        ],
    )
    def test_limit_and_raw_similarity_filter(
        self, adapter, scores, threshold, expected
    ) -> None:
        """limit 直接取 top_k，按原始分数过滤并保持候选顺序。"""
        adapter._collection_sql_example.search.return_value = [
            [self._hit(score, idx) for idx, score in enumerate(scores, start=1)]
        ]

        result = adapter.search_similar_sqls(
            embedding=[0.1, 0.2], top_k=5, similarity_threshold=threshold
        )

        assert [hit["similarity"] for hit in result] == expected
        assert [hit["example_id"] for hit in result] == [
            f"case_{idx}" for idx in range(1, len(expected) + 1)
        ]
        adapter._collection_sql_example.search.assert_called_once_with(
            data=[[0.1, 0.2]],
            anns_field="embedding",
            param=adapter.search_params,
            limit=5,
            expr=None,
            output_fields=["example_id", "question_sql", "domain"],
        )

    @pytest.mark.parametrize("scores", [[0.90, 0.70, 0.65, 0.64, 0.50], [0.64], []])
    def test_debug_logs_all_candidates_before_filter(
        self, adapter, caplog, scores
    ) -> None:
        """DEBUG 保留全部候选及原始载荷，过滤后仅记录数量。"""
        hits = [self._hit(score, idx) for idx, score in enumerate(scores, start=1)]
        if hits:
            hits[-1].entity["question_sql"] = "异常 JSON 载荷：完整保留"
        adapter._collection_sql_example.search.return_value = [hits]

        with caplog.at_level(logging.DEBUG, logger="nl2sql.milvus_adapter"):
            result = adapter.search_similar_sqls(
                embedding=[0.1], top_k=5, similarity_threshold=0.65
            )

        messages = [
            record.getMessage()
            for record in caplog.records
            if record.name == "nl2sql.milvus_adapter"
        ]
        assert messages[0] == f"历史 SQL 候选: limit=5, 返回={len(hits)}"
        assert len(messages) == len(hits) + 2
        for idx, hit in enumerate(hits, start=1):
            assert messages[idx] == (
                f"历史 SQL 候选 #{idx}: 相似度={hit.distance}, "
                f"example_id={hit.entity['example_id']}, domain=测试域, "
                f"question_sql={hit.entity['question_sql']}"
            )
        assert messages[-1] == f"历史 SQL 相似度过滤: 阈值=0.65, 保留={len(result)}"

    def test_info_level_omits_candidate_details(self, adapter, caplog) -> None:
        """INFO 级别不输出候选明细或 DEBUG 过滤摘要。"""
        adapter._collection_sql_example.search.return_value = [[self._hit(0.70, 1)]]

        with caplog.at_level(logging.INFO, logger="nl2sql.milvus_adapter"):
            result = adapter.search_similar_sqls(
                embedding=[0.1], top_k=3, similarity_threshold=0.60
            )

        assert len(result) == 1
        assert not [
            record for record in caplog.records if record.name == "nl2sql.milvus_adapter"
        ]


class TestEdgeCases:
    """边界情况测试"""

    def test_empty_table_names(
        self, valid_config, mock_milvus_client, mock_lazy_import
    ):
        """测试空表名列表"""
        adapter = MilvusSearchAdapter(config=valid_config)

        result = adapter.fetch_table_cards(table_names=[])
        assert result == {}

        result = adapter.fetch_table_categories(table_names=[])
        assert result == {}

    def test_no_search_results(
        self, valid_config, mock_milvus_client, mock_lazy_import
    ):
        """测试无搜索结果"""
        adapter = MilvusSearchAdapter(config=valid_config)

        # Mock Collection.search() 返回空
        mock_collection = Mock()
        mock_collection.search.return_value = [[]]
        adapter._collection_table_schema = mock_collection

        results = adapter.search_tables(
            embedding=[0.1] * 768, top_k=10, similarity_threshold=0.5
        )

        assert results == []

    def test_search_dim_values_with_min_score(
        self, valid_config, mock_milvus_client, mock_lazy_import, mock_embedding_client
    ):
        """测试 search_dim_values() 的 min_score 参数过滤低分结果"""
        adapter = MilvusSearchAdapter(config=valid_config)

        # Mock 搜索结果（包含高分和低分）
        mock_hit_high = Mock()
        mock_hit_high.distance = 0.8
        mock_hit_high.entity = {
            "table_name": "orders.public.dim_store",
            "col_name": "store_name",
            "col_value": "京东便利店",
        }

        mock_hit_low = Mock()
        mock_hit_low.distance = 0.3
        mock_hit_low.entity = {
            "table_name": "orders.public.dim_store",
            "col_name": "store_name",
            "col_value": "京东",
        }

        mock_collection = Mock()
        mock_collection.search.return_value = [[mock_hit_high, mock_hit_low]]
        adapter._collection_dim_value = mock_collection

        # 调用 search_dim_values（min_score=0.5，应该过滤掉低分结果）
        results = adapter.search_dim_values(
            query_value="京东便利店", top_k=5, min_score=0.5
        )

        # 验证只返回高分结果
        assert len(results) == 1
        assert results[0]["matched_text"] == "京东便利店"
        assert results[0]["score"] == 0.8

    def test_search_dim_values_default_min_score(
        self, valid_config, mock_milvus_client, mock_lazy_import, mock_embedding_client
    ):
        """测试 search_dim_values() 默认 min_score=0.0（不过滤）"""
        adapter = MilvusSearchAdapter(config=valid_config)

        # Mock 搜索结果（包含正相似度和负相似度）
        mock_hit_positive = Mock()
        mock_hit_positive.distance = 0.9
        mock_hit_positive.entity = {
            "table_name": "orders.public.dim_store",
            "col_name": "store_name",
            "col_value": "京东",
        }

        mock_hit_negative = Mock()
        mock_hit_negative.distance = -0.2
        mock_hit_negative.entity = {
            "table_name": "orders.public.dim_store",
            "col_name": "store_name",
            "col_value": "无关文本",
        }

        mock_collection = Mock()
        mock_collection.search.return_value = [[mock_hit_positive, mock_hit_negative]]
        adapter._collection_dim_value = mock_collection

        # 调用 search_dim_values（默认 min_score=0.0，但负相似度仍会被过滤）
        results = adapter.search_dim_values(query_value="京东", top_k=5)

        # 验证只返回正相似度结果
        assert len(results) == 1
        assert results[0]["matched_text"] == "京东"
        assert abs(results[0]["score"] - 0.9) < 1e-10

    def test_similarity_threshold_filtering(
        self, valid_config, mock_milvus_client, mock_lazy_import
    ):
        """测试相似度阈值过滤"""
        adapter = MilvusSearchAdapter(config=valid_config)

        # Mock Collection.search() 返回多个结果
        mock_collection = Mock()
        hit1 = Mock()
        hit1.distance = 0.9
        hit1.entity = {
            "object_id": "orders.public.table1",
            "object_type": "table",
            "table_category": "fact",
            "time_col_hint": "created_at",
        }

        hit2 = Mock()
        hit2.distance = 0.4
        hit2.entity = {
            "object_id": "orders.public.table2",
            "object_type": "table",
            "table_category": "dimension",
            "time_col_hint": None,
        }

        mock_collection.search.return_value = [[hit1, hit2]]
        adapter._collection_table_schema = mock_collection

        # 调用 search_tables，阈值为 0.5
        results = adapter.search_tables(
            embedding=[0.1] * 768, top_k=10, similarity_threshold=0.5
        )

        # 验证：只有 hit1 通过阈值
        assert len(results) == 1
        assert results[0]["object_id"] == "orders.public.table1"
        assert results[0]["similarity"] == pytest.approx(0.9, abs=0.01)


class TestTableColumnLimitContract:
    """表/列检索的 limit=K 契约（docs/upgrade/3 方案 §六.3）。

    语义与历史 SQL 检索一致：Milvus limit 直接取 top_k 先限候选数，
    返回后按原始分数过滤；不请求 2×K，也不依赖过滤后补足 K 条。
    """

    @pytest.fixture
    def adapter(self, valid_config, mock_milvus_client, mock_lazy_import):
        """真实适配器 + mock Collection，隔离外部服务。"""
        adapter = MilvusSearchAdapter(config=valid_config)
        adapter._collection_table_schema = Mock()
        return adapter

    @staticmethod
    def _table_hit(name: str, similarity: float) -> Mock:
        hit = Mock()
        hit.distance = similarity
        hit.entity = {
            "object_id": name,
            "object_type": "table",
            "table_category": "fact",
            "time_col_hint": None,
        }
        return hit

    @staticmethod
    def _column_hit(name: str, parent: str, similarity: float) -> Mock:
        hit = Mock()
        hit.distance = similarity
        hit.entity = {
            "object_id": name,
            "parent_id": parent,
            "object_type": "column",
        }
        return hit

    def test_search_tables_requests_limit_equal_top_k(self, adapter):
        """表检索请求 limit 恰为 top_k（非 2×K），expr 限定 object_type='table'"""
        adapter._collection_table_schema.search.return_value = [[]]

        adapter.search_tables(embedding=[0.1], top_k=7, similarity_threshold=0.45)

        kwargs = adapter._collection_table_schema.search.call_args.kwargs
        assert kwargs["limit"] == 7
        assert kwargs["expr"] == 'object_type == "table"'
        assert kwargs["anns_field"] == "embedding"

    def test_search_columns_requests_limit_equal_top_k(self, adapter):
        """列检索请求 limit 恰为 top_k（非 2×K），expr 限定 object_type='column'"""
        adapter._collection_table_schema.search.return_value = [[]]

        adapter.search_columns(embedding=[0.1], top_k=6, similarity_threshold=0.45)

        kwargs = adapter._collection_table_schema.search.call_args.kwargs
        assert kwargs["limit"] == 6
        assert kwargs["expr"] == 'object_type == "column"'
        assert kwargs["anns_field"] == "embedding"

    def test_search_tables_partial_below_threshold(self, adapter):
        """候选 [0.9, 0.5, 0.4]、阈值 0.5：保留前两条，筛掉的不补位"""
        adapter._collection_table_schema.search.return_value = [
            [
                self._table_hit("db.public.t1", 0.9),
                self._table_hit("db.public.t2", 0.5),
                self._table_hit("db.public.t3", 0.4),
            ]
        ]

        results = adapter.search_tables(
            embedding=[0.1], top_k=3, similarity_threshold=0.5
        )

        assert [r["object_id"] for r in results] == ["db.public.t1", "db.public.t2"]

    def test_search_tables_boundary_score_equal_threshold(self, adapter):
        """边界：分数等于阈值（>= 判断）应保留"""
        adapter._collection_table_schema.search.return_value = [
            [self._table_hit("db.public.t2", 0.5)]
        ]

        results = adapter.search_tables(
            embedding=[0.1], top_k=3, similarity_threshold=0.5
        )

        assert len(results) == 1
        assert results[0]["similarity"] == pytest.approx(0.5)

    def test_search_tables_all_below_threshold_returns_empty(self, adapter):
        """阈值高于全部分数：返回空（允许零命中）"""
        adapter._collection_table_schema.search.return_value = [
            [self._table_hit("db.public.t1", 0.49)]
        ]

        results = adapter.search_tables(
            embedding=[0.1], top_k=3, similarity_threshold=0.5
        )

        assert results == []

    def test_search_columns_partial_below_threshold(self, adapter):
        """列检索同口径：按原始分数过滤，合格命中可能少于 K"""
        adapter._collection_table_schema.search.return_value = [
            [
                self._column_hit("db.public.t1.c1", "db.public.t1", 0.9),
                self._column_hit("db.public.t1.c2", "db.public.t1", 0.5),
                self._column_hit("db.public.t2.c1", "db.public.t2", 0.4),
            ]
        ]

        results = adapter.search_columns(
            embedding=[0.1], top_k=3, similarity_threshold=0.5
        )

        assert [r["object_id"] for r in results] == [
            "db.public.t1.c1",
            "db.public.t1.c2",
        ]
        assert results[0]["parent_id"] == "db.public.t1"

    def test_search_columns_boundary_score_equal_threshold(self, adapter):
        """列路径边界：分数等于阈值（>= 判断）应保留"""
        adapter._collection_table_schema.search.return_value = [
            [self._column_hit("db.public.t1.c2", "db.public.t1", 0.5)]
        ]

        results = adapter.search_columns(
            embedding=[0.1], top_k=3, similarity_threshold=0.5
        )

        assert len(results) == 1
        assert results[0]["similarity"] == pytest.approx(0.5)

    def test_search_columns_all_below_threshold_returns_empty(self, adapter):
        """列路径阈值高于全部分数：返回空（允许零命中）"""
        adapter._collection_table_schema.search.return_value = [
            [self._column_hit("db.public.t1.c1", "db.public.t1", 0.49)]
        ]

        results = adapter.search_columns(
            embedding=[0.1], top_k=3, similarity_threshold=0.5
        )

        assert results == []


class TestDimValueLimitContract:
    """维度值检索的 limit=K 契约（docs/upgrade/4 方案 §五.2）。

    limit 直接取 top_k，与 min_score 是否大于零无关；
    先限候选数、再按原始分数过滤，合格命中可能少于 K。
    """

    @pytest.fixture
    def adapter(
        self, valid_config, mock_milvus_client, mock_lazy_import, mock_embedding_client
    ):
        """真实适配器 + mock dim collection，隔离外部服务。"""
        adapter = MilvusSearchAdapter(config=valid_config)
        adapter._collection_dim_value = Mock()
        return adapter

    @staticmethod
    def _dim_hit(table: str, col: str, value: str, score: float) -> Mock:
        hit = Mock()
        hit.distance = score
        hit.entity = {
            "table_name": table,
            "col_name": col,
            "col_value": value,
        }
        return hit

    @pytest.mark.parametrize("min_score", [0.75, 0.0])
    def test_limit_equals_top_k_regardless_of_min_score(self, adapter, min_score):
        """min_score>0 与 =0 两种情况下 limit 都恰为 top_k（不再条件性 2×K）"""
        adapter._collection_dim_value.search.return_value = [[]]

        adapter.search_dim_values(query_value="北京", top_k=3, min_score=min_score)

        kwargs = adapter._collection_dim_value.search.call_args.kwargs
        assert kwargs["limit"] == 3
        assert kwargs["anns_field"] == "col_value_embedding"
        assert kwargs["expr"] is None
        assert kwargs["output_fields"] == ["table_name", "col_name", "col_value"]

    def test_threshold_filter_keeps_prefix_and_boundary(self, adapter):
        """候选 [0.9, 0.75, 0.74]、阈值 0.75：保留前两条（等于阈值保留）"""
        adapter._collection_dim_value.search.return_value = [
            [
                self._dim_hit("db.public.dim_city", "city_name", "北京市", 0.9),
                self._dim_hit("db.public.dim_city", "city_name", "北京", 0.75),
                self._dim_hit("db.public.dim_city", "city_name", "津京", 0.74),
            ]
        ]

        results = adapter.search_dim_values(
            query_value="北京", top_k=3, min_score=0.75
        )

        assert [r["matched_text"] for r in results] == ["北京市", "北京"]
        assert results[0]["dim_table"] == "db.public.dim_city"
        assert results[0]["dim_col"] == "city_name"

    def test_all_below_threshold_returns_empty(self, adapter):
        """阈值高于全部分数：返回空（允许零命中）"""
        adapter._collection_dim_value.search.return_value = [
            [self._dim_hit("db.public.dim_city", "city_name", "天津", 0.7)]
        ]

        results = adapter.search_dim_values(
            query_value="北京", top_k=3, min_score=0.75
        )

        assert results == []
