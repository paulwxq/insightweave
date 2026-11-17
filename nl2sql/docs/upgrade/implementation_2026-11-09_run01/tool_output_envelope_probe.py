"""复现 doc 6 所述工具返回包硬截断导致的非法 JSON。"""

import json
from typing import Any

from src.modules.db_deepagent.tools._envelope import finalize_output


def run_case(name: str, payload: dict[str, Any], max_chars: int, construction: str) -> None:
    """执行一个截断案例并断言当前缺陷可以复现。"""
    text, warnings = finalize_output(payload, max_chars)
    record: dict[str, Any] = {
        "case": name,
        "construction": construction,
        "max_chars": max_chars,
        "output_length": len(text),
        "warnings": warnings,
    }
    try:
        json.loads(text)
    except json.JSONDecodeError as exc:
        record.update(
            {
                "json_valid": False,
                "error_type": type(exc).__name__,
                "error": str(exc),
            }
        )
    else:
        record["json_valid"] = True
    print(json.dumps(record, ensure_ascii=False))
    assert record["json_valid"] is False, f"{name}: 未复现非法 JSON"


run_case(
    "controlled_low_limit",
    {"status": "ok", "data": {"blob": "B" * 5000}, "warnings": []},
    300,
    "单个 5000 字符 blob 字符串",
)

wide_table_payload = {
    "status": "ok",
    "data": {
        "tables": [
            {
                "table_id": "public.wide_table",
                "columns": [
                    {"name": f"column_{index:03d}", "comment": "C" * 80}
                    for index in range(300)
                ],
            }
        ]
    },
    "warnings": [],
}
run_case(
    "production_limit_nested_columns",
    wide_table_payload,
    12000,
    "单表 300 列，每列含名称和 80 字符注释",
)
