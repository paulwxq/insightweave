"""JSON 产物加载器（doc 17 §3.4/§3.5）。

把 ``output/json/*.json`` 还原为 ``TableMetadata``，作为 ``--step md`` 的
唯一输入源。结构校验统一复用 ``MetadataDocument.from_dict()``，不另写
第二套 schema 校验。

失败语义（与 DDL 侧枚举同口径）：
- 枚举阶段（``infer_schemas`` / ``list_objects``）：文件名三段式不符、
  v3 校验失败、文件名与对象身份不一致 → 直接抛 ``JsonLoaderError``，
  使整个 md 步骤失败并报告文件名；
- 映射阶段（``load_object``）：结构字段缺失或类型不符 → 抛
  ``JsonLoaderError``，由 generator 按单对象失败统计，不中断其余对象。
  枚举通过的文档保留为内存快照，加载复用快照、不重读磁盘（doc 17 §3.4）。
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from metaweave.core.metadata.metadata_document import (
    MetadataDocument,
    MetadataDocumentError,
)
from metaweave.core.metadata.models import (
    ColumnInfo,
    DatabaseObjectRef,
    ForeignKey,
    IndexInfo,
    PrimaryKey,
    TableMetadata,
    UniqueConstraint,
)

logger = logging.getLogger("metaweave.json_loader")

SUPPORTED_MD_OBJECT_TYPES = frozenset({"table", "view", "materialized_view"})

# 与 format_data_type 的修饰符规则保持一致（data_utils.py）
_CHARACTER_TYPES = frozenset(
    {
        "character varying",
        "varchar",
        "character",
        "char",
        "bit varying",
        "varbit",
        "bit",
    }
)
_NUMERIC_TYPES = frozenset({"numeric", "decimal"})

_TYPE_MODIFIER_PATTERN = re.compile(
    r"^(?P<base>.+?)\s*\(\s*(?P<first>\d+)\s*(?:,\s*(?P<second>\d+)\s*)?\)$"
)


class JsonLoaderError(RuntimeError):
    """JSON 产物文件名、结构或身份校验失败。"""


def parse_strict_stem(stem: str) -> Optional[Tuple[str, str, str]]:
    """严格三段式 stem 解析（doc 19.1 §5：JSON 枚举与 MD 核验共用）。

    恰好三段且每段非空时返回 ``(database, schema, object)``，否则返回
    ``None``。库名前缀筛选与扩展名检查由各调用方完成。
    """
    parts = stem.split(".")
    if len(parts) != 3 or not all(parts):
        return None
    return (parts[0], parts[1], parts[2])


def _split_type_modifier(
    data_type: str,
) -> Tuple[str, Optional[int], Optional[int], Optional[int]]:
    """拆出 JSON 类型串中的修饰符（doc 17 §3.5 / §8 D2）。

    JSON v3 的 column_profiles 没有独立的 precision/scale 字段，类型串已含
    修饰符（如 ``numeric(10,2)``、``character varying(100)``）。这里还原为
    基础类型与独立修饰符，交给 format_data_type() 重新渲染；其他带括号
    类型保持原样，避免二次追加修饰符。

    Returns:
        (基础类型, character_maximum_length, numeric_precision, numeric_scale)
    """
    match = _TYPE_MODIFIER_PATTERN.match(data_type.strip())
    if not match:
        return data_type.strip(), None, None, None
    base = match.group("base").strip()
    first = int(match.group("first"))
    second = match.group("second")
    lowered = base.lower()
    if lowered in _NUMERIC_TYPES:
        return base, None, first, (int(second) if second is not None else None)
    if lowered in _CHARACTER_TYPES:
        return base, first, None, None
    return data_type.strip(), None, None, None


class JsonLoader:
    """读取 JSON 产物目录：枚举对象并还原 TableMetadata。"""

    def __init__(self, json_dir: Path | str, database_name: str = "postgres"):
        self.json_dir = Path(json_dir)
        self.database_name = str(database_name)
        # 枚举阶段已通过 v3 校验的文档缓存：list_objects 校验一次，
        # load_object 直接复用，避免同一次 md 运行重复读盘与校验。
        self._documents: Dict[Tuple[str, str], Dict[str, Any]] = {}

    @staticmethod
    def _require_object_type(object_info: Dict[str, Any], context: str) -> str:
        """对象类型决定 MD 枚举与标题，缺失时不能默认为普通表。"""
        object_type = object_info.get("object_type")
        if not isinstance(object_type, str) or not object_type:
            raise JsonLoaderError(
                f"JSON 对象类型缺失或无效 ({context}): object_info.object_type"
            )
        if object_type not in SUPPORTED_MD_OBJECT_TYPES:
            raise JsonLoaderError(f"JSON 对象类型不受支持 ({context}): {object_type!r}")
        return object_type

    def _database_files(self) -> List[Tuple[Path, List[str]]]:
        """枚举当前库的 JSON 文件，并拒绝不完整的三段式文件名。"""
        files = []
        for json_file in sorted(self.json_dir.glob("*.json")):
            if not json_file.name.startswith(f"{self.database_name}."):
                continue
            parsed = parse_strict_stem(json_file.stem)
            if parsed is None:
                raise JsonLoaderError(
                    f"JSON 文件 stem 格式异常: {json_file.name}"
                    f"（期望 {self.database_name}.<schema>.<object>.json）"
                )
            files.append((json_file, list(parsed)))
        return files

    def infer_schemas(self) -> List[str]:
        """从 JSON 目录推断 schema 列表（database.schemas 为空时的 md 步骤）。

        文件名 stem 非严格三段式时与 list_objects 同口径：抛错使整个
        md 步骤失败并报告文件名，不静默跳过（doc 17 §3.4）。
        """
        if not self.json_dir.exists():
            logger.warning(f"JSON 目录不存在: {self.json_dir}")
            return []

        schemas: set[str] = set()
        for _, parts in self._database_files():
            schemas.add(parts[1])

        schema_list = sorted(schemas)
        logger.info(f"从 JSON 目录推断出 {len(schema_list)} 个 schema: {schema_list}")
        return schema_list

    def list_objects(self, schema: str) -> List[DatabaseObjectRef]:
        """枚举某 schema 下的数据库对象；逐文件做 v3 校验与身份核对。

        校验失败（旧版本、缺字段、根节点非对象）与身份不一致（库/schema/
        对象名和文件名不符）都抛错，使整个 md 步骤失败并报告文件名
        （doc 17 §3.4）。
        """
        if not self.json_dir.exists():
            logger.warning(f"JSON 目录不存在: {self.json_dir}")
            return []

        objects: List[DatabaseObjectRef] = []
        for json_file, parts in self._database_files():
            if parts[1] != schema:
                continue
            document = self._load_document(json_file)
            object_info = document["object_info"]
            object_name = parts[2]
            # 身份三方核对：文件名前缀已由 glob 保证，这里核对
            # object_info.database 与配置库、object_info 身份与文件名。
            if str(object_info.get("database") or "") != self.database_name:
                raise JsonLoaderError(
                    f"JSON 对象身份不一致 ({json_file.name}): "
                    f"object_info.database={object_info.get('database')!r}"
                )
            if (
                str(object_info.get("schema_name") or "") != schema
                or str(object_info.get("object_name") or "") != object_name
            ):
                raise JsonLoaderError(
                    f"JSON 对象身份不一致 ({json_file.name}): object_info 记录为 "
                    f"{object_info.get('schema_name')}.{object_info.get('object_name')}"
                )
            object_type = self._require_object_type(object_info, json_file.name)

            self._documents[(schema, object_name)] = document
            objects.append(DatabaseObjectRef(schema, object_name, object_type))
            logger.debug(
                "从 JSON 文件发现数据库对象: %s.%s (%s)",
                schema,
                object_name,
                object_type,
            )

        logger.info("从 JSON 目录扫描到 %d 个对象: %s.*", len(objects), schema)
        return objects

    def load_object(self, schema: str, name: str) -> TableMetadata:
        """把已通过枚举校验的 JSON 还原为 TableMetadata（doc 17 §3.5）。

        映射错误抛 ``JsonLoaderError``，由调用方按单对象失败统计。
        枚举通过的文档保留为内存快照，这里直接复用、不重读磁盘
        （快照语义：枚举后文件被删除或外部修改不影响本次运行）；
        仅快照未命中且文件缺失时在此暴露为同类失败。
        """
        document = self._documents.get((schema, name))
        if document is None:
            json_file = self.json_dir / f"{self.database_name}.{schema}.{name}.json"
            if not json_file.exists():
                raise JsonLoaderError(f"JSON 文件不存在: {json_file.name}")
            document = self._load_document(json_file)
        return self._to_metadata(document)

    def load_cached_object(self, schema: str, name: str) -> TableMetadata:
        """仅从已枚举的内存快照还原对象（doc 19.1 §5：缓存限定只读接口）。

        缓存未命中即抛 ``JsonLoaderError``，**不回退读盘**——受管 MD 的
        重渲染核验用它保证读到的就是本轮生成器枚举时的同一文档版本。
        元数据映射复用现有 ``_to_metadata()``。
        """
        document = self._documents.get((schema, name))
        if document is None:
            raise JsonLoaderError(
                f"JSON 文档缓存未命中（未在本轮枚举中见到）: {schema}.{name}"
            )
        return self._to_metadata(document)

    def _load_document(self, json_file: Path) -> Dict[str, Any]:
        """读取单个 JSON 文件并做 v3 结构校验（复用 MetadataDocument）。"""
        try:
            data = json.loads(json_file.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            # 异常原文可能带出文件内容；只记文件名与错误类别。
            raise JsonLoaderError(
                f"JSON 文件读取失败 ({json_file.name}): {type(exc).__name__}"
            ) from None
        try:
            MetadataDocument.from_dict(data)
        except MetadataDocumentError as exc:
            # MetadataDocumentError 文案只含字段名与版本号，无数据值。
            raise JsonLoaderError(f"JSON 校验失败 ({json_file.name}): {exc}") from None
        return data

    def _to_metadata(self, document: Dict[str, Any]) -> TableMetadata:
        object_info = document["object_info"]
        table_profile = document.get("table_profile") or {}
        constraints = table_profile.get("physical_constraints") or {}
        schema_name = str(object_info.get("schema_name") or "")
        object_name = str(object_info.get("object_name") or "")
        try:
            object_type = self._require_object_type(
                object_info, f"{schema_name}.{object_name}"
            )
            return TableMetadata(
                schema_name=schema_name,
                table_name=object_name,
                database=object_info.get("database"),
                object_type=object_type,
                comment=str(object_info.get("comment") or ""),
                comment_source=str(object_info.get("comment_source") or ""),
                row_count=int(object_info.get("total_rows") or 0),
                columns=self._columns_from_profiles(document["column_profiles"]),
                primary_keys=self._primary_keys(constraints),
                foreign_keys=self._foreign_keys(constraints),
                unique_constraints=self._unique_constraints(constraints),
                indexes=self._indexes(table_profile),
                sample_records=self._sample_records(document),
            )
        except JsonLoaderError:
            raise
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            raise JsonLoaderError(
                f"JSON 映射失败 ({schema_name}.{object_name}): {type(exc).__name__}"
            ) from None

    @staticmethod
    def _sample_records(document: Dict[str, Any]) -> List[Dict[str, Any]]:
        """顶层样例记录还原（md 渲染示例值的唯一来源，doc 17 §3.5）。

        字段存在但类型不符按映射错误抛 ``JsonLoaderError``（doc 17 §3.4
        逐表失败），不静默当作"没有样例"；只有节点缺失、``records``
        缺失/为 null 或空列表才是合法的"无样例"。
        """
        sample_records = document.get("sample_records")
        if sample_records is None:
            return []
        if not isinstance(sample_records, dict):
            raise JsonLoaderError("sample_records 必须是对象")
        records = sample_records.get("records")
        if records is None:
            return []
        if not isinstance(records, list):
            raise JsonLoaderError("sample_records.records 必须是对象数组")
        for record in records:
            if not isinstance(record, dict):
                raise JsonLoaderError("sample_records.records 的每一项必须是对象")
        return [dict(record) for record in records]

    def _columns_from_profiles(
        self, column_profiles: Dict[str, Any]
    ) -> List[ColumnInfo]:
        """按 ordinal_position 还原列顺序；类型串拆出独立修饰符（D2）。"""
        columns: List[ColumnInfo] = []
        for column_name, profile in column_profiles.items():
            try:
                data_type, char_length, precision, scale = _split_type_modifier(
                    str(profile["data_type"])
                )
                columns.append(
                    ColumnInfo(
                        column_name=str(column_name),
                        ordinal_position=int(profile["ordinal_position"]),
                        data_type=data_type,
                        character_maximum_length=char_length,
                        numeric_precision=precision,
                        numeric_scale=scale,
                        is_nullable=bool(profile.get("is_nullable", True)),
                        column_default=profile.get("column_default"),
                        comment=str(profile.get("comment") or ""),
                        comment_source=str(profile.get("comment_source") or ""),
                    )
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise JsonLoaderError(
                    f"JSON 字段映射失败 ({column_name}): {type(exc).__name__}"
                ) from None
        columns.sort(key=lambda column: column.ordinal_position)
        return columns

    @staticmethod
    def _primary_keys(constraints: Dict[str, Any]) -> List[PrimaryKey]:
        """v3 只保存第一个物理主键，还原为单元素列表（或空）。"""
        primary_key = constraints.get("primary_key")
        if not primary_key:
            return []
        return [
            PrimaryKey(
                constraint_name=str(primary_key.get("constraint_name") or ""),
                columns=[str(c) for c in primary_key.get("columns") or []],
            )
        ]

    @staticmethod
    def _foreign_keys(constraints: Dict[str, Any]) -> List[ForeignKey]:
        return [
            ForeignKey(
                constraint_name=str(fk.get("constraint_name") or ""),
                source_columns=[str(c) for c in fk.get("source_columns") or []],
                target_schema=str(fk.get("target_schema") or ""),
                target_table=str(fk.get("target_table") or ""),
                target_columns=[str(c) for c in fk.get("target_columns") or []],
                on_delete=str(fk.get("on_delete") or "NO ACTION"),
                on_update=str(fk.get("on_update") or "NO ACTION"),
            )
            for fk in constraints.get("foreign_keys") or []
        ]

    @staticmethod
    def _unique_constraints(constraints: Dict[str, Any]) -> List[UniqueConstraint]:
        # v3 的唯一约束只保留 constraint_name 与 columns，is_partial 不落盘。
        return [
            UniqueConstraint(
                constraint_name=str(uc.get("constraint_name") or ""),
                columns=[str(c) for c in uc.get("columns") or []],
            )
            for uc in constraints.get("unique_constraints") or []
        ]

    @staticmethod
    def _indexes(table_profile: Dict[str, Any]) -> List[IndexInfo]:
        return [
            IndexInfo(
                index_name=str(raw.get("index_name") or ""),
                index_type=str(raw.get("index_type") or "btree"),
                columns=[str(c) for c in raw.get("columns") or []],
                is_unique=bool(raw.get("is_unique", False)),
                is_primary=bool(raw.get("is_primary", False)),
                condition=raw.get("condition"),
                is_constraint_backed=bool(raw.get("is_constraint_backed", False)),
                constraint_name=raw.get("constraint_name"),
                definition=raw.get("definition"),
                included_columns=[str(c) for c in raw.get("included_columns") or []],
                key_expressions=[str(c) for c in raw.get("key_expressions") or []],
            )
            for raw in table_profile.get("indexes") or []
        ]
