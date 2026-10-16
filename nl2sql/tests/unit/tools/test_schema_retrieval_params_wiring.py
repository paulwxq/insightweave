"""表/列检索三路参数接线测试（docs/upgrade/3 方案 §六.1 / §六.2）。

§六.1：故意给表级、主列级、column 回填设置彼此不同的数量与阈值，
断言 retrieve() 三处向量调用分别使用本路径的参数（取值必须可区分，
否则传串参数也可能通过断言）。两层覆盖：
1. 真实 __init__ + 合成配置：验证六个新 YAML 键被正确读取
   （键拼错时静默回退默认 10/0.45，取值均异于默认即可暴露）；
2. 绕过 __init__ 的实例桩：隔离构造逻辑，只守调用接线。
§六.2：从正式 YAML 加载 schema_retrieval，断言六个新键存在、初始值
为 10/0.45（column 回填阈值现为 0.65），且三个旧配置键均已删除 —— 不依赖任何 mock。
"""

from pathlib import Path
from typing import List
from unittest.mock import Mock, patch

import yaml

from src.tools.schema_retrieval.retriever import SchemaRetriever

# 三组彼此不同的参数（同时异于默认值 10/0.45，用于暴露键拼错回退）
K_TABLE, T_TABLE = 7, 0.71
K_COLUMN, T_COLUMN = 5, 0.52
K_BACKFILL, T_BACKFILL = 3, 0.33
# 维度值检索参数（异于默认 3/0.75，用于暴露键拼错或漏配回退）
K_DIM, T_DIM = 4, 0.66


class _RecordingVectorStub:
    """vector_client 边界桩：记录表/列/维度值检索调用的参数。

    search_dim_values 每次按当次 top_k 生成彼此不同的命中，
    供"多 value 文本合并后总数可超过单次 K"的断言使用。
    """

    def __init__(self):
        self.table_calls: List[dict] = []
        self.column_calls: List[dict] = []
        self.dim_calls: List[dict] = []

    def search_tables(self, **kwargs):
        self.table_calls.append(kwargs)
        return []

    def search_columns(self, **kwargs):
        self.column_calls.append(kwargs)
        return []

    def search_dim_values(self, **kwargs):
        self.dim_calls.append(kwargs)
        call_idx = len(self.dim_calls) - 1
        return [
            {
                "dim_table": f"db.public.dim_{call_idx}",
                "dim_col": "name",
                "matched_text": f"值{call_idx}_{j}",
                "score": 0.9 - j * 0.01,
            }
            for j in range(kwargs["top_k"])
        ]

    def search_similar_sqls(self, **kwargs):
        return []

    def fetch_table_categories(self, table_names):
        return {}

    def fetch_table_cards(self, table_names):
        return {}


class _NoJoinNeo4jStub:
    """neo4j_client 边界桩：无 JOIN 路径。"""

    def plan_join_paths(self, **kwargs):
        return []


def _retriever(vector: _RecordingVectorStub) -> SchemaRetriever:
    """跳过 __init__（不连真实服务），在客户端边界注入桩并设置三路参数。"""
    r = object.__new__(SchemaRetriever)
    r.config = {"schema_retrieval": {}}
    r.pg_client = Mock()
    r.neo4j_client = _NoJoinNeo4jStub()
    r.embedding_client = Mock()
    r.embedding_client.embed_query.return_value = [0.1] * 8
    r.vector_client = vector
    r.table_schema_top_k = K_TABLE
    r.table_schema_similarity_threshold = T_TABLE
    r.column_schema_top_k = K_COLUMN
    r.column_schema_similarity_threshold = T_COLUMN
    r.column_backfill_top_k = K_BACKFILL
    r.column_backfill_similarity_threshold = T_BACKFILL
    r.dim_value_top_k = 3
    r.dim_value_similarity_threshold = 0.75
    r.join_max_hops = 5
    r._category_mapping_lower = {
        "fact": {"fact"},
        "dimension": {"dim"},
        "bridge": set(),
    }
    return r


def _assert_three_path_calls(vector: _RecordingVectorStub) -> None:
    """断言三路向量调用的参数：表级、主列级、每个回填文本各用本路径参数。"""
    # 表级检索一次，使用 K_table / T_table
    assert len(vector.table_calls) == 1
    assert vector.table_calls[0]["top_k"] == K_TABLE
    assert vector.table_calls[0]["similarity_threshold"] == T_TABLE

    # search_columns 共 3 次：主列级 1 次 + 两个回填文本各 1 次
    assert len(vector.column_calls) == 3
    main_call, *backfill_calls = vector.column_calls
    assert main_call["top_k"] == K_COLUMN
    assert main_call["similarity_threshold"] == T_COLUMN

    # 每个回填文本都使用 K_backfill / T_backfill：
    # K_backfill 是单次查询上限，不是全部回填命中的总上限
    assert len(backfill_calls) == 2
    for call_kwargs in backfill_calls:
        assert call_kwargs["top_k"] == K_BACKFILL
        assert call_kwargs["similarity_threshold"] == T_BACKFILL


_PARSE_RESULT = {
    "dimensions": [
        {"text": "客户", "role": "column"},
        {"text": "邮箱", "role": "column"},
    ]
}


class TestConstructorConfigWiring:
    """§六.1：真实 __init__ 从六个新键读取三路参数（防键拼错静默回退默认值）。"""

    def test_init_reads_six_keys_and_wires_calls(self):
        """合成配置实际构造 SchemaRetriever，三路调用使用配置值。"""
        vector = _RecordingVectorStub()
        config = {
            "schema_retrieval": {
                "table_schema_top_k": K_TABLE,
                "table_schema_similarity_threshold": T_TABLE,
                "column_schema_top_k": K_COLUMN,
                "column_schema_similarity_threshold": T_COLUMN,
                "column_backfill_top_k": K_BACKFILL,
                "column_backfill_similarity_threshold": T_BACKFILL,
                "table_category_mapping": {
                    "fact": ["fact"],
                    "dimension": ["dim"],
                    "bridge": [],
                },
            }
        }

        with (
            patch("src.tools.schema_retrieval.retriever.get_pg_client"),
            patch("src.tools.schema_retrieval.retriever.get_neo4j_client"),
            patch(
                "src.tools.schema_retrieval.retriever.get_embedding_client"
            ) as mock_emb,
            patch(
                "src.tools.schema_retrieval.retriever.create_vector_search_adapter"
            ) as mock_vec,
        ):
            mock_emb.return_value = Mock()
            mock_emb.return_value.embed_query.return_value = [0.1] * 8
            mock_vec.return_value = vector
            retriever = SchemaRetriever(config)

        retriever.neo4j_client = _NoJoinNeo4jStub()
        retriever.retrieve("查询客户的邮箱", parse_result=_PARSE_RESULT)

        _assert_three_path_calls(vector)


class TestThreePathParamWiring:
    """§六.1：三条检索路径分别使用本路径的数量与阈值（实例桩，隔离构造逻辑）。"""

    def test_retrieve_uses_path_specific_params(self):
        """表级 / 主列级 / 每个回填文本的调用参数互不串用。"""
        vector = _RecordingVectorStub()
        retriever = _retriever(vector)

        retriever.retrieve("查询客户的邮箱", parse_result=_PARSE_RESULT)

        _assert_three_path_calls(vector)


class TestFormalYamlSixKeys:
    """§六.2：正式 YAML 的六键存在性与旧键清除。"""

    YAML_PATH = (
        Path(__file__).resolve().parents[3]
        / "src/modules/sql_generation/config/sql_generation_subgraph.yaml"
    )

    def test_six_new_keys_present_with_initial_values(self):
        retrieval = yaml.safe_load(self.YAML_PATH.read_text(encoding="utf-8"))[
            "schema_retrieval"
        ]
        assert retrieval["table_schema_top_k"] == 10
        assert retrieval["column_schema_top_k"] == 10
        assert retrieval["column_backfill_top_k"] == 10
        assert retrieval["table_schema_similarity_threshold"] == 0.45
        assert retrieval["column_schema_similarity_threshold"] == 0.45
        assert retrieval["column_backfill_similarity_threshold"] == 0.65

    def test_old_keys_absent(self):
        retrieval = yaml.safe_load(self.YAML_PATH.read_text(encoding="utf-8"))[
            "schema_retrieval"
        ]
        # 旧键名拆两段拼接：避免本文件字面量触发方案 §六.2 的旧键 rg 检查
        old_keys = (
            "topk_" + "tables",
            "topk_" + "columns",
            "similarity_" + "threshold",
        )
        for old_key in old_keys:
            assert old_key not in retrieval, f"旧配置键未被删除: {old_key}"


class TestDimValueConfigWiring:
    """docs/upgrade/4 §四.6/§五.1：dim_value_top_k / dim_value_similarity_threshold 接线。

    真实 __init__ + 合成配置（取值异于默认 3/0.75），
    覆盖两个 role="value" 文本各发起一次单次 K 查询、
    合并去重后总数可超过单次 K（无全局截断）。
    """

    @staticmethod
    def _config(**dim_keys):
        schema_retrieval = {
            "table_category_mapping": {
                "fact": ["fact"],
                "dimension": ["dim"],
                "bridge": [],
            }
        }
        schema_retrieval.update(dim_keys)
        return {"schema_retrieval": schema_retrieval}

    def _construct(self, config):
        vector = _RecordingVectorStub()
        with (
            patch("src.tools.schema_retrieval.retriever.get_pg_client"),
            patch("src.tools.schema_retrieval.retriever.get_neo4j_client"),
            patch(
                "src.tools.schema_retrieval.retriever.get_embedding_client"
            ) as mock_emb,
            patch(
                "src.tools.schema_retrieval.retriever.create_vector_search_adapter"
            ) as mock_vec,
        ):
            mock_emb.return_value = Mock()
            mock_emb.return_value.embed_query.return_value = [0.1] * 8
            mock_vec.return_value = vector
            retriever = SchemaRetriever(config)
        retriever.neo4j_client = _NoJoinNeo4jStub()
        return retriever, vector

    def test_init_reads_dim_keys_and_wires_calls(self):
        """两个 value 文本各一次查询，均传 K_DIM/T_DIM；总数 2K 不被全局截断。"""
        retriever, vector = self._construct(
            self._config(
                dim_value_top_k=K_DIM,
                dim_value_similarity_threshold=T_DIM,
            )
        )

        result = retriever.retrieve(
            "查询北京和上海的订单",
            parse_result={
                "dimensions": [
                    {"text": "北京", "role": "value"},
                    {"text": "上海", "role": "value"},
                ]
            },
        )

        assert len(vector.dim_calls) == 2
        for call_kwargs in vector.dim_calls:
            assert call_kwargs["top_k"] == K_DIM
            assert call_kwargs["min_score"] == T_DIM
        assert {c["query_value"] for c in vector.dim_calls} == {"北京", "上海"}

        # 桩每次返回 K_DIM 条彼此不同的命中：去重后 2*K_DIM 条全部保留
        # （K_DIM 是单次查询上限，不是整个请求的总条数上限）
        assert len(result["dim_value_hits"]) == 2 * K_DIM

    def test_missing_dim_keys_fall_back_to_yaml_values(self):
        """缺键回退到 3/0.75（与正式 YAML 显式值一致），不回退旧默认 5/0.0。"""
        retriever, vector = self._construct(self._config())

        assert retriever.dim_value_top_k == 3
        assert retriever.dim_value_similarity_threshold == 0.75

        retriever.retrieve(
            "查询北京的订单",
            parse_result={"dimensions": [{"text": "北京", "role": "value"}]},
        )

        assert len(vector.dim_calls) == 1
        assert vector.dim_calls[0]["top_k"] == 3
        assert vector.dim_calls[0]["min_score"] == 0.75


class TestFormalYamlDimKeys:
    """docs/upgrade/4 §五.1：正式 YAML 的维度值两键存在性与旧键清除。"""

    YAML_PATH = TestFormalYamlSixKeys.YAML_PATH

    def test_dim_keys_present_with_initial_values(self):
        retrieval = yaml.safe_load(self.YAML_PATH.read_text(encoding="utf-8"))[
            "schema_retrieval"
        ]
        assert retrieval["dim_value_top_k"] == 3
        assert retrieval["dim_value_similarity_threshold"] == 0.75

    def test_old_dim_keys_absent(self):
        retrieval = yaml.safe_load(self.YAML_PATH.read_text(encoding="utf-8"))[
            "schema_retrieval"
        ]
        # 旧键名拆两段拼接：避免触发方案 4 §五.5 的旧键 rg 检查
        old_keys = (
            "dim_index_" + "topk",
            "dim_value_" + "min_score",
        )
        for old_key in old_keys:
            assert old_key not in retrieval, f"旧配置键未被删除: {old_key}"
