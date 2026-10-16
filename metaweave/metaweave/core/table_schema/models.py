from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Optional, List, Dict, Any


class ObjectType(str, enum.Enum):
    TABLE = "table"
    COLUMN = "column"


class TableCategory(str, enum.Enum):
    DIM = "dim"
    FACT = "fact"
    BRIDGE = "bridge"
    OTHER = "other"


# doc 20.1 §10.2 / §10.6.2：Milvus VARCHAR 字段上限，单位为 UTF-8 字节数。
# 与 TableSchemaLoader._ensure_collection 中的集合定义必须保持一致。
VARCHAR_LIMITS: Dict[str, int] = {
    "entity_id": 512,
    "source_id": 36,
    "publish_id": 36,
    "object_id": 256,
    "object_type": 64,
    "parent_id": 256,
    "object_desc": 8192,
    "time_col_hint": 512,
    "table_category": 64,
}


def build_entity_id(source_id: Optional[str], object_id: str) -> str:
    """共用集合的单字段主键：``source_id + ":" + object_id``。

    未提供 source_id（非受管的内部调试路径）时退化为 object_id 本身。
    """
    return f"{source_id}:{object_id}" if source_id else object_id


def check_record_lengths(record: Dict[str, Any]) -> None:
    """按 UTF-8 字节数核对待写入记录的全部 VARCHAR 字段（doc 20.1 §10.2）。

    超限时报出对象 ID、字段名、实际字节数和上限；不回显字段内容。
    """
    object_id = record.get("object_id", "")
    for field, limit in VARCHAR_LIMITS.items():
        size = len(str(record.get(field) or "").encode("utf-8"))
        if size > limit:
            raise ValueError(
                f"对象 {object_id} 的字段 {field} 超过 Milvus 长度上限: "
                f"实际 {size} 字节，上限 {limit} 字节"
            )


@dataclass
class SchemaObject:
    object_type: ObjectType
    object_id: str
    parent_id: str
    object_desc: str
    time_col_hint: Optional[str] = None
    table_category: Optional[str] = None
    updated_at: int = 0

    def to_milvus_dict(
        self,
        embedding: Optional[List[float]] = None,
        source_id: Optional[str] = None,
        publish_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """转换为 Milvus upsert/insert 所需的完整记录。

        entity_id、source_id、publish_id 由应用显式赋值，schema 不会自动填充。
        """
        return {
            "entity_id": build_entity_id(source_id, self.object_id),
            "source_id": source_id or "",
            "publish_id": publish_id or "",
            "object_id": self.object_id,
            "object_type": self.object_type.value,
            "parent_id": self.parent_id,
            "object_desc": self.object_desc,
            "embedding": embedding,
            "time_col_hint": self.time_col_hint or "",
            "table_category": self.table_category or "",
            "updated_at": self.updated_at,
        }


@dataclass
class LoaderOptions:
    batch_size: int = 50
    max_tables: int = 0  # 0 表示不限制
    include_columns: bool = True
    skip_empty_desc: bool = True

    @classmethod
    def from_dict(cls, options: Dict[str, Any]) -> "LoaderOptions":
        return cls(
            batch_size=options.get("batch_size", 50),
            max_tables=options.get("max_tables", 0),
            include_columns=options.get("include_columns", True),
            skip_empty_desc=options.get("skip_empty_desc", True),
        )


__all__ = [
    "ObjectType",
    "TableCategory",
    "SchemaObject",
    "LoaderOptions",
    "VARCHAR_LIMITS",
    "build_entity_id",
    "check_record_lengths",
]
