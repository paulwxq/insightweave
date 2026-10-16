"""SAMPLED_RECORDS / OBJECT_METADATA JSON 注释块的统一编码与解码（doc 19.2 §8.2）。

生成、DDL/JSON 步骤回读与 Web 展示共用当前格式规则；旧 SAMPLE_RECORDS
标记只检测并拒绝，不保留旧格式解码或 {label, data} 包装分支。
"""

import json
import re

SAMPLED_RECORDS_MARKER = "SAMPLED_RECORDS"
LEGACY_SAMPLE_RECORDS_MARKER = "SAMPLE_RECORDS"

_current_block_re = re.compile(
    r"/\*\s*SAMPLED_RECORDS\s*(?P<body>\{.*?\})\s*\*/", re.DOTALL
)
# 旧标记不能被当前标记的前缀误匹配：SAMPLE 之后是 "_" 而非 "D"。
_legacy_block_re = re.compile(
    r"/\*\s*SAMPLE_RECORDS\s*(?P<body>\{.*?\})\s*\*/", re.DOTALL
)
# 负向前瞻排除更长标记名（如 SAMPLED_RECORDS_EXTRA）；
# 只匹配块注释形式的起始文本，行注释 "-- SAMPLE_RECORDS ..." 不会命中。
_marker_hint_re = re.compile(r"/\*\s*SAMPLED_RECORDS(?![A-Z_])")
_legacy_marker_hint_re = re.compile(r"/\*\s*SAMPLE_RECORDS(?![A-Z_])")
# 美元引用字符串的开闭定界符：$$ 或 $tag$（tag 按未加引号标识符规则：
# 字母/下划线开头，后跟字母/数字/下划线）；$1 位置参数不匹配。
_dollar_quote_open_re = re.compile(r"\$(?:[A-Za-z_][A-Za-z0-9_]*)?\$")


def serialize_comment_payload(payload: dict) -> str:
    """json.dumps 后把每个 ``*`` 写成 JSON 等价的 ``\\u002a``。

    JSON 结构字符不含 ``*``，单趟全文替换只作用于字符串值且不会产生
    新 ``*``；序列化正文因此不含裸 ``*``，既不会出现 ``/*`` 也不会出
    现 ``*/``，SQL 注释边界（含 PostgreSQL 嵌套注释）与非贪婪正则都
    不会被内容破坏；json.loads 解码回原值。不得改回按 ``*/``、``/*``
    先后做两趟字符串替换：``*/*``、``100%*/*`` 等相邻序列会被静默改值。
    """
    return json.dumps(payload, ensure_ascii=False, indent=2).replace("*", "\\u002a")


def build_comment_block(marker: str, payload: dict) -> str:
    """把 payload 编码为 ``/* MARKER\\n...\\n*/`` 块注释。"""
    return "\n".join([f"/* {marker}", serialize_comment_payload(payload), "*/"])


def _is_ident_char(ch: str) -> bool:
    """PostgreSQL 标识符延续字符：字母/数字/下划线/$（含非 ASCII）。"""
    return bool(ch) and (ch.isalnum() or ch in "_$")


def mask_sql_strings_and_line_comments(content: str) -> str:
    """把 SQL 字符串与 ``--`` 行注释替换为等长空白，其余原文保留。

    标记识别只在块注释区域有效（doc 19.2 §8.2）：``COMMENT ON ... IS
    '/* SAMPLE_RECORDS demo */'``、美元引用 ``$$/* SAMPLE_RECORDS
    demo */$$``/``$tag$...$tag$``（源库注释原文、视图定义表达式）与双引号
    标识符 ``"idx$tag$"`` 里的字样都不构成标记，既不能被计为起点，也不能
    被当作完整块。美元引用定界符还须处于合法词法位置：前一字符是标识符
    延续字符（PG 标识符允许含 ``$``）时不作引用起点，避免吞掉后续样例块。
    等长替换保证匹配位置仍可直接用于原文切片；块注释（含嵌套）整体跳过，
    样例块所在区域保持逐字节原样。
    """
    chars = list(content)
    i = 0
    length = len(content)
    while i < length:
        if content[i] == '"':
            # 双引号标识符："" 为转义的双写引号；其中的 /*、$tag$ 等均为
            # 标识符内容，不是注释或美元引用定界符
            j = i + 1
            while j < length:
                if content[j] == '"':
                    if j + 1 < length and content[j + 1] == '"':
                        j += 2
                        continue
                    j += 1
                    break
                j += 1
            for k in range(i, min(j, length)):
                chars[k] = " "
            i = j
        elif content[i] == "$":
            # 美元引用定界符只出现在合法词法位置：前一字符若为标识符延续
            # 字符（PG 标识符允许含 $），该 $ 属于标识符而非引用起点
            prev = content[i - 1] if i > 0 else ""
            if prev and _is_ident_char(prev):
                i += 1
                continue
            match = _dollar_quote_open_re.match(content, i)
            if match is None:
                i += 1
                continue
            delimiter = match.group(0)
            close = content.find(delimiter, match.end())
            # 美元引用无嵌套：内容止于下一个同名定界符；未闭合时整段到结尾
            end = length if close == -1 else close + len(delimiter)
            for k in range(i, end):
                chars[k] = " "
            i = end
        elif content[i] == "'":
            # E'...'（或 e'...'）按 PG 规则启用反斜杠转义（\' \\ 等）；
            # 前缀 E 必须是独立词（前面不是标识符延续字符），否则按普通
            # 字符串（standard_conforming_strings 下反斜杠是普通字符）
            backslash_escapes = (
                i > 0
                and content[i - 1] in "Ee"
                and (i < 2 or not _is_ident_char(content[i - 2]))
            )
            j = i + 1
            while j < length:
                if backslash_escapes and content[j] == "\\":
                    j += 2
                    continue
                if content[j] == "'":
                    if j + 1 < length and content[j + 1] == "'":
                        j += 2
                        continue
                    j += 1
                    break
                j += 1
            for k in range(i, min(j, length)):
                chars[k] = " "
            i = j
        elif content.startswith("--", i):
            j = content.find("\n", i)
            if j == -1:
                j = length
            for k in range(i, j):
                chars[k] = " "
            i = j
        elif content.startswith("/*", i):
            # 块注释（含嵌套）整体跳过，不改动内容
            depth = 1
            j = i + 2
            while j < length and depth > 0:
                if content.startswith("/*", j):
                    depth += 1
                    j += 2
                elif content.startswith("*/", j):
                    depth -= 1
                    j += 2
                else:
                    j += 1
            i = j
        else:
            i += 1
    return "".join(chars)


def find_sampled_records_blocks(content: str) -> list:
    """当前标记的完整块（可能多个，供展示层做唯一性校验）。

    只在块注释区域匹配：SQL 字符串与行注释中的字样不算（doc 19.2 §8.2）。
    """
    return list(_current_block_re.finditer(mask_sql_strings_and_line_comments(content)))


def find_legacy_sample_records_block(content: str):
    """旧 SAMPLE_RECORDS 完整块；仅用于检测并拒绝，不做解码。"""
    return _legacy_block_re.search(mask_sql_strings_and_line_comments(content))


def has_sampled_records_marker_hint(content: str) -> bool:
    """块注释形式的当前/旧标记起始文本（字符串与行注释中的字样不算）。"""
    masked = mask_sql_strings_and_line_comments(content)
    return bool(_marker_hint_re.search(masked) or _legacy_marker_hint_re.search(masked))


def iter_sampled_records_marker_hints(content: str) -> list:
    """全部标记起点（当前 + 旧，块注释形式；字符串/行注释中的字样不算）。

    与 find_sampled_records_blocks 的完整块对照：起点数多于完整块数
    即存在“有标记起始但不成块”的畸形片段，按边界不完整拒绝
    （doc 19.2 §8.2）。转义后的生成器产物值内不含裸 ``*``，正常文件
    起点数与完整块数恰好相等。
    """
    masked = mask_sql_strings_and_line_comments(content)
    return list(_marker_hint_re.finditer(masked)) + list(
        _legacy_marker_hint_re.finditer(masked)
    )


def decode_current_records(payload: dict) -> list:
    """按当前格式校验并返回 ``records``；``data``/``label`` 按普通字段处理。"""
    records = payload.get("records")
    if not isinstance(records, list):
        raise ValueError("records 必须是数组")
    for record in records:
        if not isinstance(record, dict):
            raise ValueError("records 中的记录必须是对象")
    return records
