"""TableCatalog：表键规范化、类别归类、表卡片与实时结构合并（docs/v4/5 §6.5）

表分类逻辑复制自 src/tools/schema_retrieval/retriever.py 的
_classify_table_category（映射来自新模块配置 retrieval.table_category_mapping）；
to_display_name 直接复用 src/tools/schema_retrieval/table_names（纯函数）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from src.modules.db_deepagent.config import DbAgentConfig
from src.modules.db_deepagent.errors import InvalidTableIdError
from src.modules.db_deepagent.keyspace import (
    db_prefix_problem,
    plain_identifier_problem,
)
from src.modules.db_deepagent.services.business_db import BusinessDbService
from src.modules.db_deepagent.services.milvus_retrieval import DbMilvusRetrieval
from src.tools.schema_retrieval.table_names import to_display_name
from src.utils.logger import get_module_logger

logger = get_module_logger("nl2sql.db_deepagent.table_catalog")

_CARD_TEXT_MAX_CHARS = 2000
# 卡片列解析：实测卡片为 markdown 列表 "- col (type) - 注释 [示例: ...]"；
# 要求标识符后紧跟类型括号，避免把标题行（# public.orders [table]）误判为列
_CARD_COLUMN_LINE = re.compile(r"^\s*[-*]?\s*([A-Za-z_][A-Za-z0-9_]*)\s*\(")

# 预处理的小写映射表（O(1) 查找，沿用旧实现思路）
_CATEGORY_SETS = ("fact", "bridge")


@dataclass(frozen=True)
class TableKey:
    """规范化表键：key 为三段（与 Neo4j/Milvus 同空间），display 为两段显示名"""

    key: str
    display: str


class TableCatalog:
    """表目录服务：normalize_table_id / classify / describe"""

    def __init__(
        self,
        cfg: DbAgentConfig,
        milvus: DbMilvusRetrieval,
        business_db: BusinessDbService,
    ):
        self._cfg = cfg
        self._milvus = milvus
        self._business_db = business_db
        self._allowed_schemas = (
            tuple(cfg.business_db.allowed_schemas)
            if cfg.business_db.allowed_schemas
            else None
        )
        mapping = cfg.retrieval.table_category_mapping
        self._category_mapping_lower = {
            group: {v.lower() for v in values} for group, values in mapping.items()
        }

    # ---------------------------------------------------------------- 表键

    def normalize_table_id(self, table_id: str) -> TableKey:
        """两段 schema.table 补前缀成三段；三段则首段必须等于 table_key_prefix。

        键空间（doc 4 §4.2）：db 前缀（table_key_prefix）走统一判定式（允许
        内部空格/连字符/数字开头，拒点号与控制字符）；schema/table 保持普通
        标识符正则。display 始终剥掉 db 前缀，知识前缀不进入 SQL 表引用。
        allowed_schemas（登记 selected_schemas，doc 4 §4.7）非空时，schema
        不在清单内即拒绝——SQL 表引用入口（plan_joins/inspect_tables 等）
        均经本方法归一。

        Raises:
            InvalidTableIdError: 段数不是 2/3、首段不符、标识符非法或
                schema 不在登记范围
        """
        prefix = self._cfg.business_db.table_key_prefix
        prefix_problem = db_prefix_problem(prefix)
        if prefix_problem:
            raise InvalidTableIdError(
                f"运行配置 table_key_prefix 非法: {prefix_problem}"
            )
        parts = table_id.strip().split(".")
        if len(parts) == 2:
            schema, table = parts
            key = f"{prefix}.{schema}.{table}"
        elif len(parts) == 3:
            if parts[0] != prefix:
                raise InvalidTableIdError(
                    f"表标识 {table_id!r} 首段应为当前源 db_name {prefix!r}"
                )
            schema, table = parts[1], parts[2]
            key = ".".join(parts)
        else:
            raise InvalidTableIdError(
                f"表标识 {table_id!r} 应为两段 schema.table 或三段 db.schema.table"
            )
        for kind, part in (("schema", schema), ("表", table)):
            problem = plain_identifier_problem(part, kind=kind)
            if problem:
                raise InvalidTableIdError(f"表标识 {table_id!r} 非法: {problem}")
        if self._allowed_schemas is not None:
            # 知识键优先按真实名称**精确匹配**并保留大小写（doc 4 §4.2：
            # 登记名原样；模型常回传检索到的知识键原文，如 Sales）；
            # 未精确命中时才按 PG 未加引号规则折叠小写兜底（兼容
            # PUBLIC.orders 这类输入），命中后以登记名构造键，避免大小写
            # 漂移。两者皆不中才拒绝——同时登记 Sales 与 sales 时各自
            # 归一、互不吞并
            matched = None
            if schema in self._allowed_schemas:
                matched = schema
            else:
                folded = schema.lower()
                matched = next((a for a in self._allowed_schemas if a == folded), None)
            if matched is None:
                raise InvalidTableIdError(
                    f"表标识 {table_id!r} 的 schema 不在本源登记的 selected_schemas"
                    f"（{', '.join(self._allowed_schemas)}）内"
                )
            schema = matched
            key = f"{prefix}.{schema}.{table}"
        return TableKey(key=key, display=to_display_name(key))

    # ---------------------------------------------------------------- 类别

    def classify(self, category: str) -> str:
        """原始 table_category → fact/dimension/bridge；未配置归 dimension 并记日志。"""
        if not category:
            return "dimension"
        category_lower = category.lower()
        for group in _CATEGORY_SETS:
            if category_lower in self._category_mapping_lower.get(group, set()):
                return group
        if category_lower not in self._category_mapping_lower.get("dimension", set()):
            logger.debug("未配置的表类别 %r 归为 dimension", category)
        return "dimension"

    # ---------------------------------------------------------------- 描述

    def describe(
        self, table_keys: list[TableKey], include_live_columns: bool
    ) -> list[dict[str, Any]]:
        """合并 Milvus 表卡片与实时结构（§6.5）；逐表独立，失败不影响其他表。"""
        cards = self._milvus.fetch_table_cards([tk.key for tk in table_keys])
        results: list[dict[str, Any]] = []
        for tk in table_keys:
            entry: dict[str, Any] = {
                "table_id": tk.key,
                "display": tk.display,
            }
            warnings: list[str] = []
            card = cards.get(tk.key)
            if card is None:
                warnings.append("Milvus 无该表卡片，仅返回实时结构")
                entry["table_category"] = ""
                entry["category_group"] = "dimension"
                entry["time_col_hint"] = None
                entry["card_text"] = None
                card_columns: list[str] | None = None
            else:
                category = card.get("table_category", "")
                entry["table_category"] = category
                entry["category_group"] = self.classify(category)
                entry["time_col_hint"] = card.get("time_col_hint")
                text = card.get("text_raw") or ""
                entry["card_truncated"] = len(text) > _CARD_TEXT_MAX_CHARS
                entry["card_text"] = text[:_CARD_TEXT_MAX_CHARS]
                card_columns = self._parse_card_columns(text)

            if include_live_columns:
                _schema, table = tk.display.split(".")
                live = self._business_db.get_columns(_schema, table)
                entry["live_columns"] = [
                    {
                        "name": c.column_name,
                        "pg_type": c.data_type,
                        "nullable": c.is_nullable,
                    }
                    for c in live
                ]
                live_names = {c.column_name for c in live}
                if card_columns is None:
                    warnings.append("卡片未提供结构化列信息，仅列出实时列清单")
                    entry["card_only_columns"] = []
                    entry["live_only_columns"] = []
                else:
                    card_set = set(card_columns)
                    entry["card_only_columns"] = sorted(card_set - live_names)
                    entry["live_only_columns"] = sorted(live_names - card_set)
                    if card_set - live_names:
                        warnings.append(
                            f"卡片列 {sorted(card_set - live_names)} 不在实时结构中，可能已变更"
                        )
            entry["warnings"] = warnings
            results.append(entry)
        return results

    @staticmethod
    def _parse_card_columns(card_text: str) -> list[str] | None:
        """保守解析卡片文本中的列名（行首标识符）；解析不可靠时返回 None。

        判定可靠的标准：解析出至少 1 个列名，且解析出的名字均为合法标识符
        （正则已保证）；解析不出任何行首标识符时视为卡片无结构化列信息。
        """
        names: list[str] = []
        for line in card_text.splitlines():
            m = _CARD_COLUMN_LINE.match(line)
            if m:
                names.append(m.group(1))
        return names or None
