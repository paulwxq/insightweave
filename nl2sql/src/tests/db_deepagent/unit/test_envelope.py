"""统一返回包与 Command 构造测试（docs/v4/5 §7.1）"""

from __future__ import annotations

import json

import pytest
from langchain_core.messages import ToolMessage
from pydantic import BaseModel, ConfigDict, ValidationError

from src.modules.db_deepagent.tools._envelope import (
    error_envelope,
    finalize_output,
    format_tool_validation_error,
    ok_envelope,
    tool_command,
)

pytestmark = pytest.mark.unit


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
        payload = json.loads(ok_envelope({"a": 1}, ["w"]))
        assert payload == {"status": "ok", "data": {"a": 1}, "warnings": ["w"]}

    def test_ok_envelope_empty_flag(self):
        assert json.loads(ok_envelope(empty=True))["status"] == "empty"

    def test_error_envelope_hint_optional(self):
        with_hint = json.loads(error_envelope("t", "m", True, hint="h"))
        without_hint = json.loads(error_envelope("t", "m", False))
        assert with_hint["error"]["hint"] == "h"
        assert "hint" not in without_hint["error"]


class TestFinalizeOutput:
    def test_short_payload_untouched(self):
        text, warnings = finalize_output(
            {"status": "ok", "data": {"x": 1}, "warnings": []}, 200
        )
        assert warnings == []
        assert json.loads(text)["data"] == {"x": 1}

    def test_long_text_fields_trimmed_first(self):
        payload = {
            "status": "ok",
            "data": {"sql": "S" * 900},
            "warnings": [],
        }
        text, warnings = finalize_output(payload, max_chars=500)
        assert len(text) <= 500
        assert "已截短" in "".join(warnings)

    def test_hard_truncate_when_still_over(self):
        payload = {"status": "ok", "data": {"blob": "B" * 5000}, "warnings": []}
        text, warnings = finalize_output(payload, max_chars=300)
        assert len(text) <= 300
        assert any("整体截断" in w for w in warnings)

    def test_list_trimmed_to_five(self):
        # 列表裁剪仅在文本超限时发生；裁到 5 项后需落回限内（保持 JSON 完整）
        payload = {
            "status": "ok",
            "data": {"tables": [{"i": i, "pad": "x" * 30} for i in range(9)]},
            "warnings": [],
        }
        text, warnings = finalize_output(payload, max_chars=400)
        assert len(text) <= 400
        data = json.loads(text)["data"]
        assert len(data["tables"]) == 5
        assert any("结果过多" in w for w in warnings)


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
