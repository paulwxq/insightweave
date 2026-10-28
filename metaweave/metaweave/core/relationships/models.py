"""关系发现数据模型

定义表间关系和发现结果的数据结构。
"""

from dataclasses import dataclass, field, asdict
from typing import List, Dict, Optional, Any

# 物理外键直通：约束已存在，置信度固定为 1.0（不经过五维评分）
FOREIGN_KEY_COMPOSITE_SCORE = 1.0


@dataclass
class Relation:
    """表间关系对象（单列或复合列）

    Attributes:
        relationship_id: 确定性ID（格式: rel_ + MD5[:12]）
        source_schema: 源表schema
        source_table: 源表名
        source_columns: 源列名列表（可以是1个或多个）
        target_schema: 目标表schema
        target_table: 目标表名
        target_columns: 目标列名列表
        relationship_type: 关系类型（foreign_key | inferred）
        cardinality: 基数（1:1 | 1:N | N:1 | M:N）
        constraint_name: 外键约束名（仅外键关系有值）
        composite_score: 综合评分（0-1）。推断关系来自五维加权；外键直通固定为 1.0
        score_details: 评分明细（5个维度：inclusion_rate, name_similarity, comment_similarity, type_compatibility, jaccard_index）
        inference_method: 推断方法（v3 新分类体系，见 doc 15 §3.15：
            rule_physical_key / rule_logical_key / llm_inferred；仅推断关系有值）
        candidate_origin: 候选来源（v3 统计口径，见 doc 15 §3.8：rule / llm /
            rule+llm；仅推断关系有值，物理外键直通不设置——FK 关系的来源分档
            直接按 relationship_type == "foreign_key" 判断，不需要此字段）
    """

    relationship_id: str
    source_schema: str
    source_table: str
    source_columns: List[str]
    target_schema: str
    target_table: str
    target_columns: List[str]
    relationship_type: str  # foreign_key | inferred
    cardinality: str = "N:1"  # 默认多对一
    constraint_name: Optional[str] = None  # 外键约束名
    composite_score: Optional[float] = None
    score_details: Optional[Dict[str, float]] = None
    inference_method: Optional[str] = None
    candidate_origin: Optional[str] = None  # rule / llm / rule+llm（仅推断关系）

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典（用于JSON序列化）"""
        data = asdict(self)
        if self.relationship_type == "foreign_key":
            data["composite_score"] = (
                FOREIGN_KEY_COMPOSITE_SCORE
                if self.composite_score is None
                else self.composite_score
            )
            data.pop("score_details", None)
            data.pop("inference_method", None)
            data.pop("candidate_origin", None)
        return data

    @property
    def is_single_column(self) -> bool:
        """是否为单列关系"""
        return len(self.source_columns) == 1

    @property
    def is_composite(self) -> bool:
        """是否为复合列关系"""
        return len(self.source_columns) > 1

    @property
    def source_full_name(self) -> str:
        """源表全名"""
        return f"{self.source_schema}.{self.source_table}"

    @property
    def target_full_name(self) -> str:
        """目标表全名"""
        return f"{self.target_schema}.{self.target_table}"

    @property
    def source_full_name_with_columns(self) -> str:
        """源表全名（包含列名）

        单列关系: public.dim_company.company_id
        复合键关系: public.dim_company.[store_id, date_day]
        """
        if self.is_single_column:
            return f"{self.source_schema}.{self.source_table}.{self.source_columns[0]}"
        else:
            cols = ", ".join(self.source_columns)
            return f"{self.source_schema}.{self.source_table}.[{cols}]"

    @property
    def target_full_name_with_columns(self) -> str:
        """目标表全名（包含列名）

        单列关系: public.dim_store.company_id
        复合键关系: public.dim_store.[store_id, date_day]
        """
        if self.is_single_column:
            return f"{self.target_schema}.{self.target_table}.{self.target_columns[0]}"
        else:
            cols = ", ".join(self.target_columns)
            return f"{self.target_schema}.{self.target_table}.[{cols}]"

    @property
    def table_pair(self) -> str:
        """表对标识（用于抑制规则）"""
        return f"{self.source_full_name}->{self.target_full_name}"


@dataclass
class RelationshipDiscoveryResult:
    """关系发现结果

    Attributes:
        success: 是否成功
        total_relations: 总关系数
        foreign_key_relations: 外键直通关系数
        inferred_relations: 推断关系数
        high_confidence_count: 高置信度关系数（≥0.90）
        medium_confidence_count: 中置信度关系数（0.80-0.90）
        suppressed_count: 被复合键抑制的关系数（不含未达阈值）
        below_threshold_count: 未达 accept_threshold 的候选数
        llm_candidates_enabled: 是否启用了 LLM 候选产出
        llm_total_pairs: LLM 处理的表对数
        llm_success_pairs: LLM 成功的表对数
        llm_failed_pairs: LLM 失败的表对数
        output_files: 输出文件路径列表
        errors: 错误信息列表
    """

    success: bool = True
    total_relations: int = 0
    foreign_key_relations: int = 0
    inferred_relations: int = 0
    high_confidence_count: int = 0
    medium_confidence_count: int = 0
    suppressed_count: int = 0
    below_threshold_count: int = 0
    llm_candidates_enabled: bool = False
    llm_total_pairs: int = 0
    llm_success_pairs: int = 0
    llm_failed_pairs: int = 0
    output_files: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)

    def add_error(self, error: str):
        """添加错误信息"""
        self.errors.append(error)
        self.success = False

    def add_output_file(self, file_path: str):
        """添加输出文件"""
        self.output_files.append(file_path)

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return asdict(self)


# rel json 条目 discovery_method → Relation.inference_method 的反向映射
# （writer.py `_INFERENCE_METHOD_DISCOVERY_MAP` 的双射，doc 23.2 §5.2）；
# FK 与 manual 不在此表——序列化层不为它们设置 inference_method。
_DISCOVERY_METHOD_INFERENCE_MAP = {
    "physical_key_matching": "rule_physical_key",
    "logical_key_matching": "rule_logical_key",
    "llm_inferred": "llm_inferred",
}


def relation_from_json_entry(entry: dict) -> Relation:
    """rel json 条目 → ``Relation`` 反序列化（doc 23.2 §5.2，D4）。

    服务于 md 整份重渲染与编辑链：

    - ``relationship_type`` 按 ``discovery_method`` 归三族：
      ``foreign_key_constraint → foreign_key``、``manual → manual``、
      其余 → ``inferred``（推断族的未知值显式报错，不静默回退）；
    - FK 评分恒取 ``FOREIGN_KEY_COMPOSITE_SCORE``（json 中本为 1.0），
      其余取条目值；``score_details`` 取 ``metrics``（空/缺省为 None）；
    - ``inference_method`` 经反向映射还原，FK / manual 置 None；
    - 其余字段（表/列/类型形态/基数/约束名）直读。
    """
    discovery_method = entry.get("discovery_method")
    if discovery_method == "foreign_key_constraint":
        relationship_type = "foreign_key"
    elif discovery_method == "manual":
        relationship_type = "manual"
    else:
        relationship_type = "inferred"

    from_table = entry.get("from_table") or {}
    to_table = entry.get("to_table") or {}
    if "from_columns" in entry:
        source_columns = list(entry["from_columns"])
        target_columns = list(entry["to_columns"])
    else:
        source_columns = [entry["from_column"]]
        target_columns = [entry["to_column"]]

    if relationship_type == "foreign_key":
        composite_score = FOREIGN_KEY_COMPOSITE_SCORE
    else:
        composite_score = entry.get("composite_score")

    metrics = entry.get("metrics")
    score_details = dict(metrics) if metrics else None

    inference_method = None
    if relationship_type == "inferred":
        inference_method = _DISCOVERY_METHOD_INFERENCE_MAP.get(discovery_method)
        if inference_method is None:
            raise ValueError(
                f"关系 {entry.get('relationship_id')!r} 携带未知 discovery_method: "
                f"{discovery_method!r}，仅支持 "
                f"{sorted(_DISCOVERY_METHOD_INFERENCE_MAP)}（doc 23.2 §5.2）"
            )

    return Relation(
        relationship_id=entry["relationship_id"],
        source_schema=from_table.get("schema", ""),
        source_table=from_table.get("table", ""),
        source_columns=source_columns,
        target_schema=to_table.get("schema", ""),
        target_table=to_table.get("table", ""),
        target_columns=target_columns,
        relationship_type=relationship_type,
        cardinality=entry.get("cardinality", "N:1"),
        constraint_name=entry.get("constraint_name"),
        composite_score=composite_score,
        score_details=score_details,
        inference_method=inference_method,
    )
