"""dim_tables.yaml 配置文件生成器。"""

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from metaweave.core.dim_value.models import DimTablesConfig
from metaweave.core.dim_value.recommendation import (
    validate_dimension_value_recommendation,
)
from metaweave.utils.file_utils import ensure_dir, load_json

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _DimTableEntry:
    """单张维表的生成结果：embedding_col 与生成时推荐注释行。"""

    schema_name: str
    table_name: str
    embedding_col: Optional[str]
    comment: str


class DimTableConfigGenerator:
    """dim_tables.yaml 配置文件生成器。

    自动从 json 目录扫描维表（table_category='dim'），读取画像中的维度值
    字段推荐（doc 7.1）映射到 embedding_col；无推荐或推荐非法时留 null
    待人工填写。

    `json_files`（doc 22.1 §5.3）：Web 受管步骤传入准备链筛出的普通画像
    文件列表，生成时只读该列表、不再 glob 目录；None（CLI 路径）保持
    原有目录扫描行为。
    """

    def __init__(
        self,
        json_dir: Path,
        output_path: Path,
        json_files: Optional[List[Path]] = None,
    ):
        self.json_dir = Path(json_dir)
        self.output_path = Path(output_path)
        self.json_files = (
            [Path(p) for p in json_files] if json_files is not None else None
        )
        # 本轮扫描中成功解析为 JSON 对象的画像文件数（doc 22.1 §5.3，
        # 审核修订 7）：Web 受管步骤据此区分"有画像但无维表"与"无画像可读"
        self.last_readable_count: Optional[int] = None
        # doc 7.1 §6：有推荐 / 无推荐（含非法清空）的维度表数量摘要
        self.last_recommended_count: int = 0
        self.last_unrecommended_count: int = 0

    def generate(self) -> Dict[str, Any]:
        """生成配置文件并写入磁盘。

        Returns:
            生成的配置字典，格式：
            {
                "databases": {
                    "database_name": {
                        "tables": {
                            "schema.table": {"embedding_col": ...}
                        }
                    }
                }
            }
        """
        dim_tables_by_db = self._scan_dim_tables()
        config: Dict[str, Any] = {
            "databases": {
                db_name: {
                    "tables": {
                        f"{entry.schema_name}.{entry.table_name}": {
                            "embedding_col": entry.embedding_col
                        }
                        for entry in sorted(
                            entries,
                            key=lambda e: (e.schema_name, e.table_name),
                        )
                    }
                }
                for db_name, entries in sorted(dim_tables_by_db.items())
            }
        }

        self._write_yaml(dim_tables_by_db)
        total_tables = sum(len(entries) for entries in dim_tables_by_db.values())
        logger.info(
            "✅ 已生成 dim_tables.yaml，识别到 %s 个数据库、%s 个维度表"
            "（%s 个自动推荐、%s 个待人工填写）",
            len(dim_tables_by_db),
            total_tables,
            self.last_recommended_count,
            self.last_unrecommended_count,
        )
        return config

    def _scan_dim_tables(self) -> Dict[str, List[_DimTableEntry]]:
        """扫描 json 目录（或显式文件列表），识别 dim 表并映射推荐。"""

        if self.json_files is not None:
            # doc 22.1 §5.3：Web 受管步骤传入筛好的普通文件列表，不再 glob
            json_files = list(self.json_files)
        else:
            if not self.json_dir.exists():
                logger.warning("json 目录不存在: %s", self.json_dir)
                return {}
            json_files = sorted(self.json_dir.glob("*.json"))

        dim_tables_by_db: Dict[str, List[_DimTableEntry]] = {}

        # 实时日志：开始扫描
        logger.info("📋 发现 %d 个 JSON 文件，开始扫描...", len(json_files))

        readable = 0
        recommended = 0
        unrecommended = 0
        for idx, path in enumerate(json_files, 1):
            # 实时日志：当前处理的文件
            logger.info("  [%d/%d] 检查: %s", idx, len(json_files), path.name)

            data = load_json(path)
            if not data or not isinstance(data, dict):
                continue
            readable += 1

            table_profile = data.get("table_profile") or {}
            if table_profile.get("table_category") != "dim":
                continue

            database, schema, table = self._extract_table_identifiers(data, path)
            if database and schema and table:
                db_name = str(database)
                schema_name = str(schema)
                table_name = str(table)
                embedding_col, comment = self._resolve_recommendation(
                    data, db_name, schema_name, table_name
                )
                dim_tables_by_db.setdefault(db_name, []).append(
                    _DimTableEntry(
                        schema_name=schema_name,
                        table_name=table_name,
                        embedding_col=embedding_col,
                        comment=comment,
                    )
                )
                if embedding_col is None:
                    unrecommended += 1
                else:
                    recommended += 1
                # 实时日志：识别到维度表
                logger.info(
                    "    ✅ 识别为维度表: %s.%s.%s", db_name, schema_name, table_name
                )

        # 实时日志：扫描完成统计
        logger.info("")
        self.last_readable_count = readable
        self.last_recommended_count = recommended
        self.last_unrecommended_count = unrecommended
        total_tables = sum(len(entries) for entries in dim_tables_by_db.values())
        logger.info(
            "📊 扫描完成，共识别到 %d 个数据库、%d 个维度表",
            len(dim_tables_by_db),
            total_tables,
        )
        return dim_tables_by_db

    @staticmethod
    def _resolve_recommendation(
        data: Dict[str, Any], db_name: str, schema_name: str, table_name: str
    ) -> tuple[Optional[str], str]:
        """按 doc 7.1 §5.2 校验画像推荐；非法整组不采用，返回 (embedding_col, 注释)。"""
        table_profile = data.get("table_profile")
        if not isinstance(table_profile, dict):
            table_profile = {}
        column_profiles = data.get("column_profiles")
        # column_profiles 结构异常时按无字段全集处理：任何推荐都无法
        # 核对真实列名，走整组清空，不阻断该表进入清单（§4.4）
        column_names = (
            column_profiles.keys() if isinstance(column_profiles, dict) else ()
        )
        columns, confidence, reason = validate_dimension_value_recommendation(
            table_profile.get("dimension_value_columns"),
            table_profile.get("dimension_value_confidence"),
            column_names,
        )
        display = f"{db_name}.{schema_name}.{table_name}"
        if reason:
            logger.warning("维度值字段推荐整组不采用（%s）: %s", display, reason)
        if columns:
            return columns[0], (
                f"模型原始推荐: [{columns[0]}]；推荐置信度: {confidence:.2f}"
            )
        return None, "模型原始推荐: 无；推荐置信度: 不适用"

    @staticmethod
    def _extract_table_identifiers(
        data: Dict[str, Any], path: Path
    ) -> tuple[Optional[str], Optional[str], Optional[str]]:
        object_info = data.get("object_info") or {}
        table_profile = data.get("table_profile") or {}

        database = (
            object_info.get("database")
            or table_profile.get("database")
            or data.get("database")
            or data.get("db_name")
            or DimTableConfigGenerator._infer_database_from_filename(path)
        )
        schema = (
            object_info.get("schema_name")
            or table_profile.get("schema_name")
            or data.get("schema_name")
            or data.get("schema")
        )
        table = (
            object_info.get("object_name")
            or table_profile.get("table_name")
            or data.get("table_name")
            or data.get("name")
        )
        return database, schema, table

    @staticmethod
    def _infer_database_from_filename(path: Path) -> Optional[str]:
        parts = path.stem.split(".")
        if len(parts) >= 3 and parts[0]:
            return parts[0]
        return None

    @staticmethod
    def _yaml_key(key: str) -> str:
        """借 safe_dump 的键渲染规则输出带引号安全的 `key:` 行前缀。"""
        rendered = yaml.safe_dump(
            {key: None}, allow_unicode=True, default_flow_style=False
        ).strip()
        return rendered[: -len(": null")]

    def _write_yaml(self, dim_tables_by_db: Dict[str, List[_DimTableEntry]]) -> None:
        """写入 YAML 文件（新文件头 + 逐表推荐注释，doc 7.1 §4.3）。"""

        ensure_dir(self.output_path.parent)
        header = (
            "# 维度表加载配置\n"
            "# 说明：此文件由 metadata --generate-dim-config 按画像推荐自动填入单个维度值字段\n"
            "# （embedding_col）；无法识别推荐时留空（null），可人工检查和修改。Web 配置编辑\n"
            "# 每张表仅支持一个字段。\n"
            "#\n"
            "# embedding_col 支持两种取值：\n"
            "#   1. 单列字符串：embedding_col: column_name\n"
            "#   2. 未指定：embedding_col: null\n"
            "#\n"
            "# 逐表注释描述生成时的模型推荐及置信度，仅作参考：手工修改配置不会自动更新注释，\n"
            "# 注释不参与发布校验。\n"
            "#\n"
            "# 示例：\n"
            "# databases:\n"
            "#   your_database:\n"
            "#     tables:\n"
            "#       public.dim_region:\n"
            "#         embedding_col: region_name                   # 单列\n"
            "#       public.dim_product:\n"
            "#         embedding_col: null                          # 未指定，待人工补齐\n"
            "\n"
        )
        if not dim_tables_by_db:
            body_lines = ["databases: {}"]
        else:
            body_lines = ["databases:"]
            for db_name, entries in sorted(dim_tables_by_db.items()):
                body_lines.append(f"  {self._yaml_key(db_name)}:")
                body_lines.append("    tables:")
                for entry in sorted(
                    entries, key=lambda e: (e.schema_name, e.table_name)
                ):
                    dumped = yaml.safe_dump(
                        {
                            f"{entry.schema_name}.{entry.table_name}": {
                                "embedding_col": entry.embedding_col
                            }
                        },
                        allow_unicode=True,
                        default_flow_style=False,
                        sort_keys=False,
                    ).rstrip("\n")
                    entry_lines = dumped.split("\n")
                    body_lines.append(f"      # {entry.comment}")
                    body_lines.append(f"      {entry_lines[0].strip()}")
                    for extra in entry_lines[1:]:
                        body_lines.append(f"        {extra.strip()}")

        self.output_path.write_text(
            header + "\n".join(body_lines) + "\n", encoding="utf-8"
        )

        logger.info("✅ 写入 dim_tables.yaml: %s", self.output_path)


__all__ = ["DimTableConfigGenerator", "DimTablesConfig"]
