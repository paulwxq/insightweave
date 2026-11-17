"""CLI JSON 输出隔离子进程测试（docs/v4/5 §15.2、doc 6 §3）

断言分组：真启用 --json 的路径 stdout 单 JSON 文档；`--` 后的字面量
--json 不启用 JSON 模式；--help 标准输出与退出码 0。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_CMD = [sys.executable, "-m", "src.modules.db_deepagent.cli"]

# 项目根目录：src/tests/db_deepagent/unit/ 向上 4 级（unit→db_deepagent→tests→src）
_PROJECT_ROOT = Path(__file__).resolve().parents[4]


def _run(argv: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [*_CMD, *argv],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=str(_PROJECT_ROOT),
    )


class TestJsonModeSingleDocument:
    def test_unknown_option_json_single_doc(self):
        proc = _run(["query", "--json", "--unknown-option"])
        assert proc.returncode == 2
        payload = json.loads(proc.stdout)  # 整体可解析且唯一
        assert payload["status"] == "error"
        assert payload["error"]["error_type"] == "invalid_argument"

    def test_missing_positional_json(self):
        proc = _run(["query", "--json"])
        assert proc.returncode == 2
        assert json.loads(proc.stdout)["error"]["error_type"] == "invalid_argument"

    def test_invalid_choice_json(self):
        proc = _run(["doctor", "--stage", "bogus", "--json"])
        assert proc.returncode == 2
        assert json.loads(proc.stdout)["error"]["error_type"] == "invalid_argument"

    def test_json_after_subcommand(self):
        proc = _run(["query", "--unknown", "--json"])
        assert proc.returncode == 2
        json.loads(proc.stdout)  # --json 在子命令后同样进入 JSON 错误出口

    def test_doctor_json_single_doc(self):
        """doctor --json：stdout 单文档（初始化直写被重定向到 stderr）。"""
        proc = _run(["doctor", "--stage", "env", "--json"])
        assert proc.returncode in (0, 1)
        payload = json.loads(proc.stdout)
        assert {"stage", "passed", "total", "items"} <= set(payload)
        # ConfigLoader 的 [OK] 直写不得出现在 stdout
        assert "[OK]" not in proc.stdout

    def test_chat_with_json_rejected(self):
        proc = _run(["chat", "--json"])
        assert proc.returncode == 2
        assert json.loads(proc.stdout)["error"]["error_type"] == "invalid_argument"


class TestSeparatorBoundary:
    def test_literal_json_after_separator_not_json_mode(self):
        """`cli query -- "--json"`：--json 是问题文本，非 JSON 模式。"""
        proc = _run(["query", "--", "--json"])
        # 非空问题进入运行路径（可能因环境失败），但绝不走 JSON 错误出口
        assert proc.returncode != 2 or "invalid_argument" not in proc.stdout
        assert not proc.stdout.strip().startswith("{") or "question" in proc.stdout


class TestHelpBoundary:
    def test_help_standard_output_and_zero_exit(self):
        proc = _run(["--help"])
        assert proc.returncode == 0
        assert "usage" in proc.stdout


class TestRenderFallbackAnswer:
    """doc 6.2 §7.8：CLI 文本与 JSON 的 answer 同源；failed 退出码规则不变。"""

    def _result(self, answer: str):
        from src.modules.db_deepagent.schemas import DbAgentResult, RunStats

        return DbAgentResult(
            session_id="s1",
            thread_id="dbda:s1:1",
            request_id="req-r",
            run_id="run-r",
            status="failed",
            answer=answer,
            findings=[],
            evidence=[],
            metric_definitions=[],
            limitations=[],
            open_questions=[],
            errors=[],
            warnings=["最终说明由服务端兜底生成，非模型最终提交。"],
            archived=True,
            stats=RunStats(
                elapsed_ms=1,
                model_calls=1,
                summary_model_calls=0,
                total_model_calls=1,
                tool_calls=0,
                input_tokens=None,
                output_tokens=None,
                token_usage_complete=True,
            ),
        )

    def test_render_prints_fallback_text(self, capsys):
        from src.modules.db_deepagent.cli import _render_result

        result = self._result("系统兜底说明：本轮未正常完成，未能生成有效的最终答案。")
        _render_result(result)
        out = capsys.readouterr().out
        assert "系统兜底说明" in out
        assert "（无答案）" not in out  # 非空兜底不再显示占位
        assert "状态: failed" in out

    def test_json_and_render_share_answer_field(self, capsys):
        from src.modules.db_deepagent.cli import _render_result

        result = self._result("系统兜底说明：示例")
        _render_result(result)
        out = capsys.readouterr().out
        assert "系统兜底说明：示例" in out
        assert (
            result.model_dump_json().count("系统兜底说明：示例") == 1
        )  # 同一 answer 字段
