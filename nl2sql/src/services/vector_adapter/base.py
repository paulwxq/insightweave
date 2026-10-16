"""向量检索适配器基类。

定义统一的向量检索接口，供 NL2SQL 模块使用。
"""

from abc import ABC, abstractmethod
from typing import Any, Dict, List


class BaseVectorSearchAdapter(ABC):
    """向量检索适配器基类。

    定义 NL2SQL 模块所需的 6 个核心检索方法，由 PgVector 和 Milvus 适配器实现。
    """

    def __init__(self, config: Dict[str, Any]):
        """初始化适配器。

        Args:
            config: 向量数据库配置（来自 config.yaml 的 vector_database 段）
        """
        self.config = config

    @abstractmethod
    def search_tables(
        self,
        embedding: List[float],
        top_k: int,
        similarity_threshold: float,
    ) -> List[Dict[str, Any]]:
        """检索语义相关的表。

        Args:
            embedding: 查询向量
            top_k: 返回最相似的 top_k 条结果
            similarity_threshold: 相似度阈值（0.0 - 1.0）

        Returns:
            匹配结果列表，每条记录包含：
            - object_id: 表标识（三段表键 db.schema.table，表记录唯一键）
            - similarity: 相似度分数（0.0 - 1.0）
            - object_type: 固定值 "table"
            - grain_hint: 表的粒度提示（可选，Milvus 返回 None）
            - time_col_hint: 时间列提示（可选，仅 table 类型可能有值）
            - table_category: 表的分类（实体表/维度表/桥接表等，可选）
        """

    @abstractmethod
    def search_columns(
        self,
        embedding: List[float],
        top_k: int,
        similarity_threshold: float,
    ) -> List[Dict[str, Any]]:
        """检索语义相关的列。

        Args:
            embedding: 查询向量
            top_k: 返回最相似的 top_k 条结果
            similarity_threshold: 相似度阈值（0.0 - 1.0）

        Returns:
            匹配结果列表，每条记录包含：
            - object_id: 列全名（四段 db.schema.table.column）
            - parent_id: 所属表的三段表键（db.schema.table）
            - similarity: 相似度分数（0.0 - 1.0）
            - object_type: 固定值 "column"
            - grain_hint: 列的粒度提示（可选，Milvus 返回 None）
        """

    @abstractmethod
    def search_dim_values(
        self,
        query_value: str,
        top_k: int,
        min_score: float = 0.0,
    ) -> List[Dict[str, Any]]:
        """检索维度值匹配。

        Args:
            query_value: 用户查询中提取的维度值（如 "张三"、"北京"）
            top_k: 返回最相似的 top_k 条结果
            min_score: 最小分数阈值（0.0 - 1.0），低于此阈值的结果将被过滤

        Returns:
            匹配结果列表，每条记录包含：
            - dim_table: 维度表名（三段表键 db.schema.table）
            - dim_col: 维度列名
            - matched_text: 匹配到的维度值文本
            - score: 匹配分数（0.0 - 1.0）
            - key_col: 主键列名（可选，Milvus 不返回）
            - key_value: 主键值（可选，Milvus 不返回）
        """

    @abstractmethod
    def search_similar_sqls(
        self,
        embedding: List[float],
        top_k: int,
        similarity_threshold: float,
    ) -> List[Dict[str, Any]]:
        """检索历史相似 SQL。

        Args:
            embedding: 查询向量
            top_k: 返回最相似的 top_k 条结果
            similarity_threshold: 相似度阈值（0.0 - 1.0）

        Returns:
            匹配结果列表，每条记录包含：
            - question: 历史问题
            - sql: 历史 SQL
            - similarity: 相似度分数（0.0 - 1.0）

        """

    @abstractmethod
    def fetch_table_cards(
        self,
        table_names: List[str],
    ) -> Dict[str, Dict[str, Any]]:
        """获取表卡片（批量精确查询）。

        Args:
            table_names: 表名列表（三段表键 db.schema.table 格式）

        Returns:
            字典，key 为三段表键，value 为表卡片，包含：
            - object_id: 三段表键
            - text_raw: 表描述文本（对应 Milvus 字段 object_desc）
            - grain_hint: 粒度提示（Milvus 无此字段，返回 None）
            - time_col_hint: 时间列提示（可选，仅 table 类型可能有值）
            - table_category: 表分类（可选）
        """

    @abstractmethod
    def fetch_table_categories(
        self,
        table_names: List[str],
    ) -> Dict[str, str]:
        """批量查询表的 table_category 字段。

        Args:
            table_names: 表名列表（三段表键 db.schema.table 格式）

        Returns:
            字典，key 为三段表键，value 为 table_category
        """
