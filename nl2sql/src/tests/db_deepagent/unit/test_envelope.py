"""统一返回包与 Command 构造测试（docs/v4/5 §7.1；doc 9 §4 步骤 1/2）"""

from __future__ import annotations

import copy
import json

import pytest
from langchain_core.messages import ToolMessage
from pydantic import BaseModel, ConfigDict, ValidationError

from src.modules.db_deepagent.tools._envelope import (
    error_envelope,
    format_tool_validation_error,
    ok_envelope,
    serialize_tool_output,
    tool_command,
)

pytestmark = pytest.mark.unit


def _reject_constant(value: str):
    """parse_constant：遇到 NaN/Infinity/-Infinity 直接拒绝（doc 9 §1）。

    默认 json.loads 接受这些非标准常量，会产生假阳性；该严格解析仅用于
    测试验收，不改变运行时编码策略。
    """
    raise ValueError(f"非标准 JSON 常量: {value}")


def _strict_loads(text: str):
    return json.loads(text, parse_constant=_reject_constant)


class _ArgsModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str


class TestFormatValidationError:
    def test_only_paths_and_types_no_values(self):
        try:
            _ArgsModel(query=None)  # type: ignore[arg-type]
            raise AssertionError("应抛 ValidationError")
        except ValidationError as exc:
            text = format_tool_validation_error(exc)
        assert text.startswith("invalid_argument")
        assert "query" in text
        assert "None" not in text  # 不回显输入值


class TestEnvelopes:
    def test_ok_envelope_shape(self):
        payload = _strict_loads(ok_envelope({"a": 1}, ["w"]))
        assert payload == {"status": "ok", "data": {"a": 1}, "warnings": ["w"]}

    def test_ok_envelope_empty_flag(self):
        assert _strict_loads(ok_envelope(empty=True))["status"] == "empty"

    def test_error_envelope_hint_optional(self):
        with_hint = _strict_loads(error_envelope("t", "m", True, hint="h"))
        without_hint = _strict_loads(error_envelope("t", "m", False))
        assert with_hint["error"]["hint"] == "h"
        assert "hint" not in without_hint["error"]

    def test_envelopes_use_indented_layout(self):
        """ok/error 信封统一 indent=2 多行布局（doc 9 §2：单一序列化入口）。"""
        assert "\n  " in ok_envelope({"a": 1})
        assert "\n  " in error_envelope("t", "m", True)


class TestSerializeToolOutput:
    def test_roundtrip_standard_payload(self):
        """中文/引号/换行等标准 JSON 载荷：严格解析后与原 payload 相等。"""
        payload = {
            "status": "ok",
            "data": {
                "text": '包含 "引号"、\n换行\t与制表',
                "sql": "SELECT '中文' FROM 公共.订单",
                "nested": {"list": [1, 2.5, True, None, ["内层"]]},
            },
            "warnings": ["警告一"],
        }
        text = serialize_tool_output(payload)
        assert _strict_loads(text) == payload

    def test_indented_multiline_layout(self):
        """indent=2 多行布局：大输出转存后可按行回读（doc 9 §2 回读边界）。"""
        text = serialize_tool_output({"status": "ok", "data": {"x": 1}, "warnings": []})
        lines = text.splitlines()
        assert lines[0] == "{"
        assert lines[1].startswith('  "status"')
        assert lines[-1] == "}"

    def test_input_payload_not_mutated(self):
        payload = {
            "status": "ok",
            "data": {"sql": "S" * 900, "tables": [{"i": i} for i in range(9)]},
            "warnings": [],
        }
        snapshot = copy.deepcopy(payload)
        serialize_tool_output(payload)
        assert payload == snapshot

    @pytest.mark.parametrize("budget", [11999, 12000, 12001])
    def test_no_threshold_trimming(self, budget):
        """旧 12000 字符预算边界：不按阈值删字段/删尾，解码后等于原 payload。"""
        payload = {
            "status": "ok",
            "data": {
                "blob": "B" * (budget - 120),
                "tables": [{"i": i, "pad": "x" * 30} for i in range(9)],
            },
            "warnings": ["w"],
        }
        text = serialize_tool_output(payload)
        assert len(text) > budget  # 实测长度确已越过旧预算
        assert _strict_loads(text) == payload

    def test_multi_warnings_order_preserved(self):
        """多条 warnings 保持原顺序，不做全局 set 去重或排序（doc 9 §5）。"""
        payload = {"status": "ok", "data": {}, "warnings": ["w-2", "w-1", "w-3", "w-2"]}
        assert _strict_loads(serialize_tool_output(payload))["warnings"] == [
            "w-2",
            "w-1",
            "w-3",
            "w-2",
        ]

    def test_large_error_envelope_fields_complete(self):
        """超长错误包：message 不截断，error 全字段与 error_codes 保留。"""
        message = "E" * 30000
        text = error_envelope(
            "validation_failed",
            message,
            retryable=True,
            data={"error_codes": ["c1", "c2"]},
        )
        payload = _strict_loads(text)
        assert payload["error"]["message"] == message
        assert payload["error"]["retryable"] is True
        assert payload["data"]["error_codes"] == ["c1", "c2"]


class TestToolCommand:
    def test_command_updates_and_message(self):
        cmd = tool_command("内容", "call-1", updates={"db_dim_evidence": [1, 2]})
        assert set(cmd.update) == {"db_dim_evidence", "messages"}
        msg = cmd.update["messages"][0]
        assert isinstance(msg, ToolMessage)
        assert msg.content == "内容"
        assert msg.tool_call_id == "call-1"

    def test_command_without_updates(self):
        cmd = tool_command("内容", "call-2")
        assert set(cmd.update) == {"messages"}
