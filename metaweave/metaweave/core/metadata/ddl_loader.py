"""DDL Loader

Parse generated SQL files (Step 1 output) back into TableMetadata objects so
subsequent steps can rely on the curated schema definitions instead of reading
directly from the database.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

from metaweave.core.metadata.models import (
    ColumnInfo,
    ForeignKey,
    IndexInfo,
    PrimaryKey,
    TableMetadata,
    UniqueConstraint,
)
from metaweave.core.metadata.comment_utils import normalize_comment

# 样例块识别/解码共用当前格式规则（doc 19.2 §8.2）；旧 SAMPLE_RECORDS
# 标记只检测并拒绝，SAMPLE_BLOCK_PATTERN 的旧格式分支已移除。
from metaweave.core.metadata.sample_block import (
    decode_current_records,
    find_legacy_sample_records_block,
    find_sampled_records_blocks,
    has_sampled_records_marker_hint,
    iter_sampled_records_marker_hints,
)

logger = logging.getLogger("metaweave.ddl_loader")

OBJECT_METADATA_PATTERN = re.compile(
    r"/\*\s*OBJECT_METADATA\s*(?P<body>\{.*?\})\s*\*/",
    re.DOTALL | re.IGNORECASE,
)
COLUMN_COMMENT_PATTERN = re.compile(
    r"COMMENT\s+ON\s+COLUMN\s+"
    r"(?P<schema>[A-Za-z0-9_]+)\.(?P<table>[A-Za-z0-9_]+)\.(?P<column>[A-Za-z0-9_]+)\s+"
    r"IS\s+'(?P<comment>(?:''|[^'])*)';",
    re.IGNORECASE | re.DOTALL,
)
TABLE_COMMENT_PATTERN = re.compile(
    r"COMMENT\s+ON\s+TABLE\s+"
    r"(?P<schema>[A-Za-z0-9_]+)\.(?P<table>[A-Za-z0-9_]+)\s+IS\s+'(?P<comment>(?:''|[^'])*)';",
    re.IGNORECASE | re.DOTALL,
)
CREATE_INDEX_START_PATTERN = re.compile(
    r"CREATE\s+(?P<unique>UNIQUE\s+)?INDEX\s+"
    r"(?:IF\s+NOT\s+EXISTS\s+)?(?P<name>[A-Za-z0-9_]+)\s+ON\s+"
    r"(?:ONLY\s+)?(?P<schema>[A-Za-z0-9_]+)\.(?P<table>[A-Za-z0-9_]+)"
    r"(?:\s+USING\s+(?P<method>[A-Za-z0-9_]+))?\s*(?=\()",
    re.IGNORECASE | re.DOTALL,
)
# 匹配完整关键词；constraint_count、unique_constraints 等都是普通字段名。
TABLE_CONSTRAINT_START_PATTERN = re.compile(
    r"^(?:(?P<constraint>CONSTRAINT)(?=\s|$)|"
    r"(?P<primary_key>PRIMARY\s+KEY)(?=\s|\(|$)|"
    r"(?P<unique>UNIQUE)(?=\s|\(|$)|"
    r"(?P<foreign_key>FOREIGN\s+KEY)(?=\s|\(|$))",
    re.IGNORECASE,
)

CHAR_TYPES = {
    "character varying",
    "varchar",
    "character",
    "char",
    "nvarchar",
}
NUMERIC_TYPES = {
    "numeric",
    "decimal",
    "number",
    "int",
    "integer",
    "bigint",
    "smallint",
    "double precision",
    "real",
    "float",
}


@dataclass
class ParsedDDL:
    metadata: TableMetadata
    sample_records: List[Dict]
    ddl_path: Path


class DDLLoaderError(RuntimeError):
    """Domain-specific error for DDL parsing issues."""


class DDLLoader:
    """Load TableMetadata definitions from generated DDL files."""

    def __init__(self, ddl_dir: str | Path, database_name: str = "postgres"):
        self.ddl_dir = Path(ddl_dir)
        self.database_name = database_name
        if not self.ddl_dir.exists():
            raise DDLLoaderError(f"DDL 目录不存在: {self.ddl_dir}")

    def load_table(self, schema: str, table: str) -> ParsedDDL:
        ddl_path = self.ddl_dir / f"{self.database_name}.{schema}.{table}.sql"
        if not ddl_path.exists():
            raise DDLLoaderError(f"DDL 文件不存在: {ddl_path}")
        content = ddl_path.read_text(encoding="utf-8")
        parsed = self._parse_content(content, ddl_path)
        if (
            parsed.metadata.schema_name.lower() != schema.lower()
            or parsed.metadata.table_name.lower() != table.lower()
        ):
            raise DDLLoaderError(
                f"DDL ({ddl_path}) 中的表 {parsed.metadata.full_name} 与请求的 {schema}.{table} 不一致"
            )
        return parsed

    def load_all(self) -> List[ParsedDDL]:
        parsed_items: List[ParsedDDL] = []
        for ddl_file in sorted(self.ddl_dir.glob("*.sql")):
            content = ddl_file.read_text(encoding="utf-8")
            try:
                parsed_items.append(self.parse_content(content, ddl_file))
            except DDLLoaderError as exc:
                logger.error(f"解析 DDL 失败 ({ddl_file}): {exc}")
                raise
        return parsed_items

    def parse_content(self, content: str, ddl_path: Path) -> ParsedDDL:
        """解析调用方已读取的 DDL 文本，与 `load_all()` 同一解析链。

        供需要"单次读取"的调用方使用：解析身份与保存正文来自同一份
        内容，避免两次读取之间文件被并发替换造成身份与正文错位。
        """
        return self._parse_content(content, ddl_path)

    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #

    def _parse_content(self, content: str, ddl_path: Path) -> ParsedDDL:
        sample_records = self._parse_sample_records(content, ddl_path)
        object_metadata = self._parse_object_metadata(content, ddl_path)
        if object_metadata and object_metadata.get("object_type") in {
            "view",
            "materialized_view",
        }:
            metadata = self._metadata_from_object_block(object_metadata, ddl_path)
            metadata.indexes = self._parse_indexes(
                content,
                metadata.schema_name,
                metadata.table_name,
            )
            metadata.sample_records = sample_records
            self._apply_comments(content, metadata)
            return ParsedDDL(
                metadata=metadata,
                sample_records=sample_records,
                ddl_path=ddl_path,
            )

        create_stmt, body = self._extract_create_table_block(content, ddl_path)
        schema_name, table_name = self._parse_table_name(create_stmt, ddl_path)

        metadata = TableMetadata(
            schema_name=schema_name,
            table_name=table_name,
            object_type="table",
            comment="",
            comment_source="ddl",
            row_count=0,
        )

        column_defs = self._split_definitions(body)
        metadata.columns, column_level_pks = self._parse_columns(
            column_defs, metadata, ddl_path
        )
        self._parse_constraints(column_defs, metadata, ddl_path, column_level_pks)
        self._parse_alter_table_constraints(content, metadata, schema_name, table_name)
        metadata.indexes = self._parse_indexes(content, schema_name, table_name)
        metadata.sample_records = sample_records
        self._apply_comments(content, metadata)

        return ParsedDDL(
            metadata=metadata, sample_records=sample_records, ddl_path=ddl_path
        )

    def _parse_object_metadata(
        self,
        content: str,
        ddl_path: Path,
    ) -> Optional[Dict]:
        match = OBJECT_METADATA_PATTERN.search(content)
        if not match:
            return None
        try:
            value = json.loads(match.group("body"))
        except json.JSONDecodeError as exc:
            raise DDLLoaderError(
                f"OBJECT_METADATA 解析失败 ({ddl_path}): {exc}"
            ) from exc
        if not isinstance(value, dict):
            raise DDLLoaderError(f"OBJECT_METADATA 必须是对象 ({ddl_path})")
        return value

    def _metadata_from_object_block(
        self,
        object_metadata: Dict,
        ddl_path: Path,
    ) -> TableMetadata:
        full_name = str(object_metadata.get("object_name") or "")
        if "." not in full_name:
            raise DDLLoaderError(
                f"OBJECT_METADATA.object_name 缺少 schema ({ddl_path})"
            )
        schema_name, table_name = full_name.split(".", 1)
        object_type = str(object_metadata.get("object_type") or "").lower()
        columns_value = object_metadata.get("columns")
        if not isinstance(columns_value, list):
            raise DDLLoaderError(f"OBJECT_METADATA.columns 必须是数组 ({ddl_path})")

        columns = []
        for ordinal, item in enumerate(columns_value, start=1):
            if not isinstance(item, dict):
                raise DDLLoaderError(
                    f"OBJECT_METADATA.columns[] 必须是对象 ({ddl_path})"
                )
            column_name = str(item.get("column_name") or "").strip()
            type_text = str(item.get("data_type") or "").strip()
            if not column_name or not type_text:
                raise DDLLoaderError(
                    f"OBJECT_METADATA 字段缺少 column_name/data_type ({ddl_path})"
                )
            data_type, char_len, numeric_precision, numeric_scale = (
                self._parse_data_type(type_text)
            )
            columns.append(
                ColumnInfo(
                    column_name=column_name,
                    ordinal_position=ordinal,
                    data_type=data_type,
                    character_maximum_length=char_len,
                    numeric_precision=numeric_precision,
                    numeric_scale=numeric_scale,
                    is_nullable=True,
                    column_default=None,
                    comment=normalize_comment(item.get("column_comment")),
                    comment_source=(
                        "ddl" if normalize_comment(item.get("column_comment")) else ""
                    ),
                )
            )

        return TableMetadata(
            schema_name=schema_name,
            table_name=table_name,
            object_type=object_type,
            comment=normalize_comment(object_metadata.get("object_comment")),
            comment_source=(
                "ddl"
                if normalize_comment(object_metadata.get("object_comment"))
                else ""
            ),
            columns=columns,
        )

    def _parse_sample_records(self, content: str, ddl_path: Path) -> List[Dict]:
        """只认当前 SAMPLED_RECORDS 格式（doc 19.2 §8.2）。

        旧 SAMPLE_RECORDS 标记、标记起始但边界不完整、多个样例块都按
        输入错误拒绝；记录为直接字段对象，``data``/``label`` 按普通字段
        处理，不拆包装、不跳过记录。
        """
        if find_legacy_sample_records_block(content):
            raise DDLLoaderError(f"检测到旧 SAMPLE_RECORDS 样例块: {ddl_path}")
        blocks = find_sampled_records_blocks(content)
        if not blocks:
            if has_sampled_records_marker_hint(content):
                raise DDLLoaderError(f"样例块边界不完整: {ddl_path}")
            logger.warning(f"未在 DDL 中找到样例数据注释块: {ddl_path}")
            return []
        if len(blocks) > 1:
            raise DDLLoaderError(f"存在多个 SAMPLED_RECORDS 样例块: {ddl_path}")
        if len(iter_sampled_records_marker_hints(content)) != len(blocks):
            raise DDLLoaderError(f"样例块边界不完整: {ddl_path}")
        body = blocks[0].group("body").strip()
        try:
            payload = json.loads(body)
            if not isinstance(payload, dict):
                raise ValueError("样例块必须是对象")
            return [dict(record) for record in decode_current_records(payload)]
        except (json.JSONDecodeError, ValueError) as exc:
            raise DDLLoaderError(f"样例数据解析失败 ({ddl_path}): {exc}") from exc

    def _extract_create_table_block(
        self, content: str, ddl_path: Path
    ) -> tuple[str, str]:
        create_pattern = re.compile(
            r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(?P<name>[^\s(]+)\s*\(",
            re.IGNORECASE,
        )
        match = create_pattern.search(content)
        if not match:
            raise DDLLoaderError(f"未找到 CREATE TABLE 语句: {ddl_path}")

        open_idx = match.end() - 1  # position of '('
        body, close_idx = self._extract_parenthesized_block(content, open_idx)
        create_stmt = content[
            match.start() : close_idx + 1
        ]  # include closing parenthesis
        return create_stmt, body

    def _extract_parenthesized_block(self, text: str, open_idx: int) -> tuple[str, int]:
        depth = 0
        start = None
        for idx in range(open_idx, len(text)):
            char = text[idx]
            if char == "(":
                depth += 1
                if depth == 1:
                    start = idx + 1
                    continue
            elif char == ")":
                depth -= 1
                if depth == 0:
                    if start is None:
                        raise DDLLoaderError("括号解析错误：缺少起始位置")
                    return text[start:idx], idx
            # nothing else to do
        raise DDLLoaderError("未能找到匹配的括号")

    def _parse_table_name(self, create_stmt: str, ddl_path: Path) -> tuple[str, str]:
        name_pattern = re.compile(
            r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(?P<name>[^\s(]+)",
            re.IGNORECASE,
        )
        match = name_pattern.search(create_stmt)
        if not match:
            raise DDLLoaderError(f"无法解析表名 ({ddl_path})")
        full_name = match.group("name").strip()
        if "." not in full_name:
            raise DDLLoaderError(f"表名缺少 schema: {full_name}")
        schema, table = full_name.split(".", 1)
        return schema.strip(), table.strip()

    def _split_definitions(self, block: str) -> List[str]:
        items: List[str] = []
        current: List[str] = []
        depth = 0
        for char in block:
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
            if char == "," and depth == 0:
                piece = "".join(current).strip()
                if piece:
                    items.append(piece)
                current = []
            else:
                current.append(char)
        tail = "".join(current).strip()
        if tail:
            items.append(tail)
        return items

    def _parse_columns(
        self,
        definitions: List[str],
        metadata: TableMetadata,
        ddl_path: Path,
    ) -> tuple[List[ColumnInfo], List[str]]:
        """
        解析列定义

        Returns:
            tuple[List[ColumnInfo], List[str]]: (列信息列表, 列级主键列表)
        """
        columns: List[ColumnInfo] = []
        column_level_primary_keys: List[str] = []
        ordinal = 1
        for definition in definitions:
            stripped = definition.strip()
            if TABLE_CONSTRAINT_START_PATTERN.match(stripped):
                continue
            if not stripped:
                continue
            column, is_primary_key = self._parse_column_definition(
                stripped, ordinal, ddl_path
            )
            columns.append(column)
            if is_primary_key:
                column_level_primary_keys.append(column.column_name)
            ordinal += 1
        return columns, column_level_primary_keys

    def _parse_column_definition(
        self,
        definition: str,
        ordinal: int,
        ddl_path: Path,
    ) -> tuple[ColumnInfo, bool]:
        """
        解析列定义，返回 ColumnInfo 和是否为主键的标志

        Returns:
            tuple[ColumnInfo, bool]: (列信息, 是否为主键)
        """
        line = definition.rstrip(",")

        # 检测列级 PRIMARY KEY
        is_primary_key = False
        if re.search(r"\bPRIMARY\s+KEY\b", line, re.IGNORECASE):
            is_primary_key = True
            line = re.sub(r"\bPRIMARY\s+KEY\b", "", line, flags=re.IGNORECASE).strip()

        # Default
        column_default = None
        default_match = re.search(r"\bDEFAULT\b\s+(?P<value>.+)", line, re.IGNORECASE)
        if default_match:
            column_default = default_match.group("value").strip()
            line = line[: default_match.start()].strip()

        # Nullability
        is_nullable = True
        if re.search(r"\bNOT\s+NULL\b", line, re.IGNORECASE):
            is_nullable = False
            line = re.sub(r"\bNOT\s+NULL\b", "", line, flags=re.IGNORECASE).strip()

        parts = line.split(None, 1)
        if len(parts) < 2:
            # 消息不内联定义原文：异常会进平台发布日志，字段定义可能
            # 含 DEFAULT 字面量等敏感内容，定位靠序号与文件路径。
            raise DDLLoaderError(f"无法解析第 {ordinal} 个字段定义 ({ddl_path})")
        column_name = parts[0].strip('"')
        type_str = parts[1].strip()

        (
            data_type,
            char_len,
            num_precision,
            num_scale,
        ) = self._parse_data_type(type_str)

        column_info = ColumnInfo(
            column_name=column_name,
            ordinal_position=ordinal,
            data_type=data_type,
            character_maximum_length=char_len,
            numeric_precision=num_precision,
            numeric_scale=num_scale,
            is_nullable=is_nullable,
            column_default=column_default,
            comment="",
            comment_source="ddl",
            statistics=None,
        )

        return column_info, is_primary_key

    def _parse_data_type(
        self, type_str: str
    ) -> tuple[str, Optional[int], Optional[int], Optional[int]]:
        type_str = type_str.strip()
        params_match = re.search(r"\(([^\)]+)\)", type_str)
        params: List[int] = []
        base_type = type_str
        if params_match:
            base_type = type_str[: params_match.start()].strip()
            raw_params = params_match.group(1)
            for value in raw_params.split(","):
                value = value.strip()
                if value.isdigit():
                    params.append(int(value))
        base_type_lower = base_type.lower()
        char_len = None
        num_precision = None
        num_scale = None
        if base_type_lower in CHAR_TYPES and params:
            char_len = params[0]
        elif base_type_lower in NUMERIC_TYPES and params:
            num_precision = params[0]
            if len(params) > 1:
                num_scale = params[1]
        return base_type_lower, char_len, num_precision, num_scale

    def _parse_constraints(
        self,
        definitions: List[str],
        metadata: TableMetadata,
        ddl_path: Path,
        column_level_pks: List[str],
    ):
        primary_keys: List[PrimaryKey] = []
        unique_constraints: List[UniqueConstraint] = []
        foreign_keys: List[ForeignKey] = []

        # 处理列级主键
        if column_level_pks:
            pk_name = f"{metadata.table_name}_pkey"
            primary_keys.append(
                PrimaryKey(constraint_name=pk_name, columns=column_level_pks)
            )

        # 处理表级约束
        for definition in definitions:
            stripped = definition.strip()
            constraint_match = TABLE_CONSTRAINT_START_PATTERN.match(stripped)
            if constraint_match is None:
                continue

            # 处理 CONSTRAINT ... PRIMARY KEY
            if constraint_match.lastgroup == "constraint":
                tokens = stripped.split(None, 2)
                if len(tokens) < 3:
                    # 不内联约束原文（可能含字面量），与字段定义同样的脱敏规则
                    raise DDLLoaderError(f"约束语句无效 ({ddl_path})")
                constraint_name = tokens[1]
                remainder = tokens[2]
                kind_match = TABLE_CONSTRAINT_START_PATTERN.match(remainder)
                kind = kind_match.lastgroup if kind_match else None

                if kind == "primary_key":
                    columns = self._extract_column_list(remainder, "PRIMARY KEY")
                    primary_keys.append(
                        PrimaryKey(constraint_name=constraint_name, columns=columns)
                    )
                elif kind == "unique":
                    columns = self._extract_column_list(remainder, "UNIQUE")
                    unique_constraints.append(
                        UniqueConstraint(
                            constraint_name=constraint_name,
                            columns=columns,
                            is_partial=False,
                        )
                    )
                elif kind == "foreign_key":
                    fk = self._parse_foreign_key(constraint_name, remainder, ddl_path)
                    foreign_keys.append(fk)

            # 处理无名表级 PRIMARY KEY
            elif constraint_match.lastgroup == "primary_key":
                columns = self._extract_column_list(stripped, "PRIMARY KEY")
                pk_name = f"{metadata.table_name}_pkey"
                primary_keys.append(
                    PrimaryKey(constraint_name=pk_name, columns=columns)
                )

        metadata.primary_keys = primary_keys
        metadata.unique_constraints = unique_constraints
        metadata.foreign_keys = foreign_keys

    def _extract_column_list(self, text: str, keyword: str) -> List[str]:
        keyword_pattern = r"\s+".join(re.escape(part) for part in keyword.split())
        pattern = re.compile(
            keyword_pattern + r"\s*\((?P<cols>[^\)]+)\)", re.IGNORECASE
        )
        match = pattern.search(text)
        if not match:
            return []
        cols = [c.strip().strip('"') for c in match.group("cols").split(",")]
        return [c for c in cols if c]

    def _parse_foreign_key(self, name: str, text: str, ddl_path: Path) -> ForeignKey:
        fk_pattern = re.compile(
            r"FOREIGN\s+KEY\s*\((?P<src>[^\)]+)\)\s+REFERENCES\s+"
            r"(?P<target_schema>[A-Za-z0-9_]+)\.(?P<target_table>[A-Za-z0-9_]+)\s*"
            r"\((?P<target_cols>[^\)]+)\)"
            r"(?P<actions>.*)",
            re.IGNORECASE | re.DOTALL,
        )
        match = fk_pattern.search(text)
        if not match:
            # 不内联外键语句原文（可能含字面量），与字段定义同样的脱敏规则
            raise DDLLoaderError(f"外键语句无法解析 ({ddl_path})")
        source_columns = [c.strip().strip('"') for c in match.group("src").split(",")]
        target_columns = [
            c.strip().strip('"') for c in match.group("target_cols").split(",")
        ]
        actions = match.group("actions") or ""
        on_delete = self._extract_fk_action(actions, "ON DELETE")
        on_update = self._extract_fk_action(actions, "ON UPDATE")
        return ForeignKey(
            constraint_name=name,
            source_columns=source_columns,
            target_schema=match.group("target_schema"),
            target_table=match.group("target_table"),
            target_columns=target_columns,
            on_delete=on_delete,
            on_update=on_update,
        )

    def _extract_fk_action(self, text: str, keyword: str) -> str:
        pattern = re.compile(keyword + r"\s+(?P<action>[A-Z\s]+)", re.IGNORECASE)
        match = pattern.search(text)
        if not match:
            return "NO ACTION"
        return match.group("action").strip().upper()

    def _parse_indexes(self, content: str, schema: str, table: str) -> List[IndexInfo]:
        indexes: List[IndexInfo] = []
        for match in CREATE_INDEX_START_PATTERN.finditer(content):
            if (
                match.group("schema").lower() != schema.lower()
                or match.group("table").lower() != table.lower()
            ):
                continue

            open_idx = match.end()
            key_block, close_idx = self._extract_parenthesized_block(
                content,
                open_idx,
            )
            statement_end = content.find(";", close_idx)
            if statement_end < 0:
                statement_end = len(content)
            suffix = content[close_idx + 1 : statement_end]

            key_expressions = self._split_top_level_expressions(key_block)
            columns = []
            for expression in key_expressions:
                column_name = self._plain_index_column(expression)
                if column_name is not None:
                    columns.append(column_name)

            included_columns: List[str] = []
            include_match = re.search(r"\bINCLUDE\s*(?=\()", suffix, re.IGNORECASE)
            if include_match:
                include_open_idx = close_idx + 1 + include_match.end()
                include_block, _ = self._extract_parenthesized_block(
                    content,
                    include_open_idx,
                )
                included_columns = [
                    self._unquote_identifier(value.strip())
                    for value in self._split_top_level_expressions(include_block)
                    if value.strip()
                ]

            condition = None
            where_match = re.search(
                r"\bWHERE\s+(?P<condition>.+)$", suffix, re.IGNORECASE | re.DOTALL
            )
            if where_match:
                condition = where_match.group("condition").strip() or None

            indexes.append(
                IndexInfo(
                    index_name=match.group("name"),
                    index_type=(match.group("method") or "btree").lower(),
                    columns=columns,
                    is_unique=bool(match.group("unique")),
                    is_primary=False,
                    condition=condition,
                    is_constraint_backed=False,
                    constraint_name=None,
                    definition=content[match.start() : statement_end].strip(),
                    included_columns=included_columns,
                    key_expressions=key_expressions,
                )
            )
        return indexes

    @staticmethod
    def _split_top_level_expressions(value: str) -> List[str]:
        """按顶层逗号拆分 SQL 片段，保留括号和引号内的逗号。"""
        items: List[str] = []
        current: List[str] = []
        depth = 0
        in_single_quote = False
        in_double_quote = False
        index = 0

        while index < len(value):
            char = value[index]
            next_char = value[index + 1] if index + 1 < len(value) else ""

            if char == "'" and not in_double_quote:
                current.append(char)
                if in_single_quote and next_char == "'":
                    current.append(next_char)
                    index += 2
                    continue
                in_single_quote = not in_single_quote
            elif char == '"' and not in_single_quote:
                current.append(char)
                if in_double_quote and next_char == '"':
                    current.append(next_char)
                    index += 2
                    continue
                in_double_quote = not in_double_quote
            elif not in_single_quote and not in_double_quote:
                if char == "(":
                    depth += 1
                    current.append(char)
                elif char == ")":
                    depth -= 1
                    current.append(char)
                elif char == "," and depth == 0:
                    item = "".join(current).strip()
                    if item:
                        items.append(item)
                    current = []
                else:
                    current.append(char)
            else:
                current.append(char)
            index += 1

        tail = "".join(current).strip()
        if tail:
            items.append(tail)
        return items

    @classmethod
    def _plain_index_column(cls, expression: str) -> Optional[str]:
        """普通字段键返回字段名；表达式键返回 None。"""
        stripped = expression.strip()
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", stripped):
            return stripped
        if re.fullmatch(r'"(?:[^"]|"")+"', stripped):
            return cls._unquote_identifier(stripped)
        return None

    @staticmethod
    def _unquote_identifier(value: str) -> str:
        if len(value) >= 2 and value.startswith('"') and value.endswith('"'):
            return value[1:-1].replace('""', '"')
        return value

    def _parse_alter_table_constraints(
        self, content: str, metadata: TableMetadata, schema: str, table: str
    ):
        """
        解析 ALTER TABLE 语句中的约束定义

        支持的格式：
        - ALTER TABLE schema.table ADD CONSTRAINT name PRIMARY KEY (col1, col2);
        - ALTER TABLE table ADD CONSTRAINT name PRIMARY KEY (col1, col2);
        """
        # ALTER TABLE pattern
        alter_pattern = re.compile(
            r"ALTER\s+TABLE\s+(?:(?P<schema>[A-Za-z0-9_]+)\.)?(?P<table>[A-Za-z0-9_]+)\s+"
            r"ADD\s+CONSTRAINT\s+(?P<constraint_name>[A-Za-z0-9_]+)\s+"
            r"(?P<constraint_def>.+?);",
            re.IGNORECASE | re.DOTALL,
        )

        for match in alter_pattern.finditer(content):
            alter_schema = match.group("schema") or schema
            alter_table = match.group("table")

            # 只处理当前表的 ALTER TABLE
            if (
                alter_schema.lower() != schema.lower()
                or alter_table.lower() != table.lower()
            ):
                continue

            constraint_name = match.group("constraint_name")
            constraint_def = match.group("constraint_def").strip()
            constraint_def_upper = constraint_def.upper()

            if "PRIMARY KEY" in constraint_def_upper:
                columns = self._extract_column_list(constraint_def, "PRIMARY KEY")
                pk = PrimaryKey(constraint_name=constraint_name, columns=columns)
                # 避免重复添加
                if not any(
                    pk.constraint_name == existing.constraint_name
                    for existing in metadata.primary_keys
                ):
                    metadata.primary_keys.append(pk)
            elif (
                "UNIQUE" in constraint_def_upper
                and "FOREIGN KEY" not in constraint_def_upper
            ):
                columns = self._extract_column_list(constraint_def, "UNIQUE")
                uc = UniqueConstraint(
                    constraint_name=constraint_name, columns=columns, is_partial=False
                )
                if not any(
                    uc.constraint_name == existing.constraint_name
                    for existing in metadata.unique_constraints
                ):
                    metadata.unique_constraints.append(uc)
            elif "FOREIGN KEY" in constraint_def_upper:
                fk = self._parse_foreign_key(
                    constraint_name, constraint_def, Path("ALTER_TABLE")
                )
                if not any(
                    fk.constraint_name == existing.constraint_name
                    for existing in metadata.foreign_keys
                ):
                    metadata.foreign_keys.append(fk)

    def _apply_comments(self, content: str, metadata: TableMetadata):
        table_comment = self._extract_table_comment(
            content, metadata.schema_name, metadata.table_name
        )
        if table_comment is not None:
            metadata.comment = normalize_comment(table_comment)
            metadata.comment_source = "ddl" if metadata.comment else ""
        else:
            metadata.comment = normalize_comment(metadata.comment)
            metadata.comment_source = "ddl" if metadata.comment else ""

        column_comments = self._extract_column_comments(
            content, metadata.schema_name, metadata.table_name
        )
        for column in metadata.columns:
            comment = column_comments.get(column.column_name)
            if comment is not None:
                column.comment = normalize_comment(comment)
            else:
                column.comment = normalize_comment(column.comment)
            column.comment_source = "ddl" if column.comment else ""

    def _extract_table_comment(
        self, content: str, schema: str, table: str
    ) -> Optional[str]:
        for match in TABLE_COMMENT_PATTERN.finditer(content):
            if (
                match.group("schema").lower() == schema.lower()
                and match.group("table").lower() == table.lower()
            ):
                return match.group("comment").replace("''", "'")
        return None

    def _extract_column_comments(
        self, content: str, schema: str, table: str
    ) -> Dict[str, str]:
        comments: Dict[str, str] = {}
        for match in COLUMN_COMMENT_PATTERN.finditer(content):
            if (
                match.group("schema").lower() == schema.lower()
                and match.group("table").lower() == table.lower()
            ):
                column = match.group("column")
                comments[column] = match.group("comment").replace("''", "'")
        return comments
