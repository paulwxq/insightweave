from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple


# 保留以兼容旧导入；字段行解析已改为 §5.1 的结构化规则，不再使用该正则。
COLUMN_PATTERN = re.compile(r"^-\s+([\w.-]+)\s+\([^)]+\)\s+-\s+(.+?)(?:\s+\[示例:.*)?$")
TABLE_NAME_PATTERN = re.compile(r"^#\s+([\w.]+)")

# 字段列表章节标题：接受生成器现用的“## 字段列表：”，也接受“## 字段列表”。
FIELD_SECTION_PREFIX = "## 字段列表"
# 描述分隔符：至少一个空白 + "-" + 空白或行尾（doc 20.1 §5.1）。
_SEPARATOR = re.compile(r"^\s+-(?P<after>(?:\s.*)?)$")
_EXAMPLE_MARKER = "[示例: "
# 生成器对缺少字段注释写入的字面文本；与空字符串、纯空白同样视为没有有效描述。
NO_COMMENT_TEXT = "无注释"


def is_empty_description(description: Optional[str]) -> bool:
    """没有有效字段描述：去除首尾空白后为空，或恰好等于“无注释”。

    只判断完整相等，描述中仅包含“无注释”三个字但另有说明的文本不算空。
    """
    text = (description or "").strip()
    return not text or text == NO_COMMENT_TEXT


class MDParser:
    """Markdown 文件解析器，用于提取表描述与字段注释。"""

    def __init__(self, md_file: Path):
        self.md_file = md_file
        self._lines: List[str] = self.md_file.read_text(encoding="utf-8").splitlines()

    @classmethod
    def from_string(cls, content: str) -> "MDParser":
        parser = cls.__new__(cls)
        parser.md_file = Path("<string>")
        parser._lines = content.splitlines()
        return parser

    def get_table_description(self) -> str:
        """返回整份 Markdown 内容（保留原格式）。"""
        return "\n".join(self._lines)

    def get_column_descriptions(self) -> Dict[str, str]:
        """解析“## 字段列表”章节，返回 {字段名: 原始描述}（保持出现顺序）。

        - 先识别字段，再由调用方决定是否跳过：没有有效描述的字段（空字符串、
          纯空白、“无注释”）同样保留在返回值中，不在解析阶段删除；
        - 以 ``-`` 开头的列表行一律按 ``- 字段名 (类型) - 描述 [示例: ...]`` 解析，
          畸形行、重复字段名抛 ValueError，不静默忽略；
        - 空行及不以 ``-`` 开头的说明文字忽略；遇到下一条 ``##`` 标题结束。
        """
        columns: Dict[str, str] = {}
        in_field_section = False

        for line_no, line in enumerate(self._lines, start=1):
            stripped = line.strip()

            if stripped.startswith(FIELD_SECTION_PREFIX):
                in_field_section = True
                continue

            if in_field_section and stripped.startswith("##"):
                break

            if not in_field_section or not stripped.startswith("-"):
                continue

            name, description = self._parse_field_line(stripped, line_no)
            if name in columns:
                raise ValueError(f"MD 第 {line_no} 行字段名重复: {name}")
            columns[name] = description

        return columns

    def _parse_field_line(self, line: str, line_no: int) -> Tuple[str, str]:
        """解析单条已 strip 的字段行；异常信息不回显示例值。"""

        def fail(reason: str) -> ValueError:
            return ValueError(f"MD 第 {line_no} 行字段格式错误: {reason}")

        if len(line) < 2 or not line[1].isspace():
            raise fail("“-”后缺少空格")
        rest = line[1:].strip()
        if not rest:
            raise fail("缺少字段名")

        # 类型左括号：行首或前面是空白的第一个“(”；字段名可含空格、$ 等字符。
        type_start = -1
        for index, char in enumerate(rest):
            if char == "(" and (index == 0 or rest[index - 1].isspace()):
                type_start = index
                break
        if type_start < 0:
            raise fail("缺少类型")
        name = rest[:type_start].strip()
        if not name:
            raise fail("缺少字段名")

        # 从类型左括号起匹配成对的右括号，支持 numeric(10,2) 等嵌套括号。
        depth = 0
        type_end = -1
        for index in range(type_start, len(rest)):
            if rest[index] == "(":
                depth += 1
            elif rest[index] == ")":
                depth -= 1
                if depth == 0:
                    type_end = index
                    break
        if type_end < 0:
            raise fail(f"字段 {name} 的类型括号未闭合")
        if not rest[type_start + 1 : type_end].strip():
            raise fail(f"字段 {name} 的类型为空")

        separator = _SEPARATOR.match(rest[type_end + 1 :])
        if separator is None:
            raise fail(f"字段 {name} 缺少描述分隔符")
        after = separator.group("after") or ""

        # 行末完整示例标记：取最右侧的“[示例: ”，要求整行以“]”闭合。
        marker_at = after.rfind(_EXAMPLE_MARKER)
        while marker_at > 0 and not after[marker_at - 1].isspace():
            marker_at = after.rfind(_EXAMPLE_MARKER, 0, marker_at)
        if marker_at >= 0 and (marker_at == 0 or after[marker_at - 1].isspace()):
            if not after.endswith("]"):
                raise fail(f"字段 {name} 的示例标记缺少行末“]”")
            after = after[:marker_at]

        return name, after.strip()

    def extract_table_name(self) -> str:
        """从第一行标题提取 schema.table 名。"""
        if not self._lines:
            raise ValueError(f"文件为空，无法提取表名: {self.md_file}")

        first_line = self._lines[0].strip()
        match = TABLE_NAME_PATTERN.match(first_line)
        if not match:
            raise ValueError(f"无法从 MD 第一行提取表名: {first_line}")
        return match.group(1)


__all__ = [
    "MDParser",
    "COLUMN_PATTERN",
    "TABLE_NAME_PATTERN",
    "NO_COMMENT_TEXT",
    "is_empty_description",
]
