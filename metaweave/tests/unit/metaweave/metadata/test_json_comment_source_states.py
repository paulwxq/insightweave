"""G3（doc 17 §3.3）：DDL 输入 → json 产物注释来源三态固化。

链路为生产代码真实链（除数据库 COUNT/采样与 LLM 调用）：
DDL 文件 → DDLLoader（有注释标 ``comment_source="ddl"``）→ MetadataProfiler
画像 → OutputFormatter.build_json_document（v3 校验）→ JsonLlmEnhancer
（llm_service 为替身）。断言表级与列级三态齐全（ddl / llm / ""）、
overwrite: false 不改写已有注释、无 comment_original / comment_source_original
残留键。
"""

import json
from pathlib import Path
from unittest.mock import MagicMock

from metaweave.core.metadata.ddl_loader import DDLLoader
from metaweave.core.metadata.formatter import OutputFormatter
from metaweave.core.metadata.json_llm_enhancer import JsonLlmEnhancer
from metaweave.core.metadata.profiler import MetadataProfiler

DATABASE = "store_db"

ORDERS_DDL = """CREATE TABLE public.orders (
    order_id INTEGER NOT NULL,
    amount NUMERIC(10,2),
    note TEXT,
    CONSTRAINT orders_pkey PRIMARY KEY (order_id)
);
COMMENT ON COLUMN public.orders.order_id IS '订单ID';
COMMENT ON TABLE public.orders IS '订单表';
/* SAMPLED_RECORDS
{"object_type":"table","object_name":"public.orders","records":[{"order_id":"1","amount":"18.00"}]}
*/
"""

BILLS_DDL = """CREATE TABLE public.bills (
    bill_id INTEGER NOT NULL
);
"""


def _write_ddl_dir(tmp_path: Path) -> Path:
    ddl_dir = tmp_path / "ddl"
    ddl_dir.mkdir()
    (ddl_dir / f"{DATABASE}.public.orders.sql").write_text(ORDERS_DDL, encoding="utf-8")
    (ddl_dir / f"{DATABASE}.public.bills.sql").write_text(BILLS_DDL, encoding="utf-8")
    return ddl_dir


def _document_from_ddl(tmp_path: Path, table: str) -> dict:
    """复现 json 步骤的 file-only 段：DDL 解析 → 画像 → v3 文档。"""
    parsed = DDLLoader(tmp_path / "ddl", database_name=DATABASE).load_table(
        "public", table
    )
    metadata = parsed.metadata
    profiler = MetadataProfiler()
    metadata.column_profiles = profiler._profile_columns(metadata, None)
    metadata.table_profile = profiler._profile_table(metadata, metadata.column_profiles)
    formatter = OutputFormatter(
        {"output_dir": str(tmp_path / "output")}, database_name=DATABASE
    )
    return formatter.build_json_document(metadata, None)


def _enhancer(llm_response: dict) -> JsonLlmEnhancer:
    config = {
        "llm": {
            "active": "qwen",
            "providers": {"qwen": {"model": "qwen-plus", "api_key": "mock-key"}},
        },
        "json_generation": {
            "comments": {"llm_enabled": True, "overwrite": False},
            "table_classification": {"llm_enabled": True},
        },
    }
    enhancer = JsonLlmEnhancer(config)
    service = MagicMock(model="test-model")
    service.call_llm.return_value = json.dumps(llm_response, ensure_ascii=False)
    enhancer.llm_service = service
    return enhancer


def _assert_no_temp_comment_keys(value, path="doc") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            assert key not in {
                "comment_original",
                "comment_source_original",
            }, f"残留临时键: {path}.{key}"
            _assert_no_temp_comment_keys(item, f"{path}.{key}")
    elif isinstance(value, list):
        for idx, item in enumerate(value):
            _assert_no_temp_comment_keys(item, f"{path}[{idx}]")


def test_column_comment_source_three_states(tmp_path):
    """列级三态：order_id 来自 DDL、amount 由 LLM 补齐、note LLM 未给出仍为空。"""
    _write_ddl_dir(tmp_path)
    document = _document_from_ddl(tmp_path, "orders")

    outcome = _enhancer(
        {
            "table_category": "fact",
            "confidence": 0.9,
            "reason": "包含金额指标",
            "column_comments": {"amount": "订单金额", "note": "   "},
        }
    ).enhance_document(document)

    profiles = outcome.document["column_profiles"]
    assert (
        profiles["order_id"]["comment"],
        profiles["order_id"]["comment_source"],
    ) == (
        "订单ID",
        "ddl",
    )
    assert (profiles["amount"]["comment"], profiles["amount"]["comment_source"]) == (
        "订单金额",
        "llm",
    )
    assert (profiles["note"]["comment"], profiles["note"]["comment_source"]) == ("", "")
    _assert_no_temp_comment_keys(outcome.document)


def test_table_comment_source_three_states(tmp_path):
    """表级三态：DDL 原始（orders）→ LLM 补齐（bills）→ LLM 未给出仍为空。"""
    _write_ddl_dir(tmp_path)

    from_ddl = _document_from_ddl(tmp_path, "orders")
    outcome = _enhancer(
        {
            "table_category": "fact",
            "confidence": 0.9,
            "reason": "包含金额指标",
            "table_comment": "LLM 想覆盖的表注释",
        }
    ).enhance_document(from_ddl)
    info = outcome.document["object_info"]
    # overwrite: false：DDL 已有注释保持原值，不被 LLM 改写
    assert (info["comment"], info["comment_source"]) == ("订单表", "ddl")

    filled = _document_from_ddl(tmp_path, "bills")
    outcome = _enhancer(
        {
            "table_category": "dim",
            "confidence": 0.9,
            "reason": "维度命名",
            "table_comment": "账单表",
        }
    ).enhance_document(filled)
    info = outcome.document["object_info"]
    assert (info["comment"], info["comment_source"]) == ("账单表", "llm")

    still_empty = _document_from_ddl(tmp_path, "bills")
    outcome = _enhancer(
        {
            "table_category": "dim",
            "confidence": 0.9,
            "reason": "维度命名",
            "table_comment": "   ",
        }
    ).enhance_document(still_empty)
    info = outcome.document["object_info"]
    assert (info["comment"], info["comment_source"]) == ("", "")
    _assert_no_temp_comment_keys(outcome.document)
