"""公开通知工作流 notify-deploy.yml 的测试（doc 15 §5.3）。

行为测试取出工作流里通知步骤的内嵌脚本，在 gh 替身上执行：检查发出的请求、回执校验
和失败分支。替身只记录调用并按预设应答，不访问网络。结构测试固定触发器、环境、权限和
来源参数的绑定。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "notify-deploy.yml"
DEPLOY_REPOSITORY = "example-owner/example-deploy"
TOKEN = "stub-trigger-token-0123456789"
SOURCE_SHA = "a" * 40
SOURCE_RUN_ID = "111111"
SOURCE_RUN_ATTEMPT = "2"
SOURCE_INPUTS = {"source_sha": SOURCE_SHA, "source_run_id": SOURCE_RUN_ID, "source_run_attempt": SOURCE_RUN_ATTEMPT}
RUN_ID = 123456789
GOOD_RECEIPT = {
    "workflow_run_id": RUN_ID,
    "run_url": f"https://api.github.com/repos/{DEPLOY_REPOSITORY}/actions/runs/{RUN_ID}",
    "html_url": f"https://github.com/{DEPLOY_REPOSITORY}/actions/runs/{RUN_ID}",
}

# 桩 gh：记录参数、收到的令牌和 --input 文件；先输出 FAKE_GH_RECEIPT_FILE 的内容（出错时也输出，
# 用来确认失败的命令留下的内容不会被当成回执），再按 FAKE_GH_EXIT 退出。
GH_STUB = """#!/usr/bin/env bash
state="$FAKE_GH_STATE_DIR"
printf '%s\\n' "$@" > "$state/args"
printf '%s' "$GH_TOKEN" > "$state/token"
echo call >> "$state/calls"
while [ $# -gt 0 ]; do
  [ "$1" = "--input" ] && cp "$2" "$state/request.json"
  shift
done
[ -n "$FAKE_GH_RECEIPT_FILE" ] && cat "$FAKE_GH_RECEIPT_FILE"
if [ "$FAKE_GH_EXIT" != 0 ]; then
  echo "HTTP 403: Resource not accessible (stub)" >&2
  exit "$FAKE_GH_EXIT"
fi
"""


def load_workflow() -> dict:
    document = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    return document | {"on": document.get("on", document.get(True))}


def notify_script() -> str:
    return load_workflow()["jobs"]["notify"]["steps"][0]["run"]


def run_notify(
    tmp_path: Path, *, exit_code: int = 0, receipt: str | None = None
) -> tuple[subprocess.CompletedProcess[str], Path]:
    """在 gh 替身上执行通知脚本；返回进程结果和替身的记录目录。"""
    jq = shutil.which("jq")
    if jq is None:
        pytest.skip("需要 jq 才能执行通知脚本")
    bindir = tmp_path / "bin"
    state = tmp_path / "state"
    runner_temp = tmp_path / "runner-temp"
    for directory in (bindir, state, runner_temp):
        directory.mkdir()
    stub = bindir / "gh"
    stub.write_text(GH_STUB, encoding="utf-8")
    stub.chmod(0o755)
    script = tmp_path / "notify.sh"
    script.write_text(notify_script(), encoding="utf-8")
    receipt_file = tmp_path / "receipt.json"
    receipt_file.write_text(receipt or "", encoding="utf-8")
    result = subprocess.run(
        ["bash", str(script)],
        env={
            "PATH": f"{bindir}:{Path(jq).parent}:/usr/bin:/bin",
            "HOME": str(tmp_path),
            "LANG": "C.UTF-8",
            "GH_TOKEN": TOKEN,
            "DEPLOY_REPOSITORY": DEPLOY_REPOSITORY,
            "SOURCE_SHA": SOURCE_SHA,
            "SOURCE_RUN_ID": SOURCE_RUN_ID,
            "SOURCE_RUN_ATTEMPT": SOURCE_RUN_ATTEMPT,
            "GITHUB_RUN_ID": "222222",
            "GITHUB_RUN_ATTEMPT": "3",
            "RUNNER_TEMP": str(runner_temp),
            "GITHUB_STEP_SUMMARY": str(tmp_path / "summary.md"),
            "FAKE_GH_STATE_DIR": str(state),
            "FAKE_GH_EXIT": str(exit_code),
            "FAKE_GH_RECEIPT_FILE": str(receipt_file) if receipt is not None else "",
        },
        capture_output=True,
        text=True,
        check=False,
    )
    return result, state


def recorded(state: Path, name: str) -> str:
    return (state / name).read_text(encoding="utf-8")


def summary(tmp_path: Path) -> str:
    path = tmp_path / "summary.md"
    return path.read_text(encoding="utf-8") if path.exists() else ""


class TestNotifyRequest:
    def test_sends_the_three_source_inputs_for_main_and_records_the_private_run(self, tmp_path):
        result, state = run_notify(tmp_path, receipt=json.dumps(GOOD_RECEIPT))

        assert result.returncode == 0, result.stderr
        args = recorded(state, "args").splitlines()
        assert args[:4] == ["api", "--method", "POST", "-H"]
        assert "Accept: application/vnd.github+json" in args
        assert "X-GitHub-Api-Version: 2026-03-10" in args
        endpoint = f"repos/{DEPLOY_REPOSITORY}/actions/workflows/deploy.yml/dispatches"
        assert endpoint in args
        request_file = args[args.index("--input") + 1]
        assert request_file == str(tmp_path / "runner-temp" / "deploy-request-n222222-a3.json")
        assert json.loads(recorded(state, "request.json")) == {"ref": "main", "inputs": SOURCE_INPUTS}
        assert recorded(state, "token") == TOKEN

        lines = summary(tmp_path).splitlines()
        intent, accepted = (json.loads(line) for line in lines)
        assert intent == {"ref": "main", "inputs": SOURCE_INPUTS, "dispatch_phase": "intent"}
        assert accepted == GOOD_RECEIPT | {"notification_id": "n222222-a3", "dispatch_phase": "accepted"}
        assert TOKEN not in summary(tmp_path)
        assert TOKEN not in recorded(state, "args")
        assert recorded(state, "calls").splitlines() == ["call"]


class TestNotifyFailures:
    def test_send_error_fails_without_resending_even_if_its_output_looks_like_a_receipt(self, tmp_path):
        result, state = run_notify(tmp_path, exit_code=1, receipt=json.dumps(GOOD_RECEIPT))

        assert result.returncode == 1
        assert "不自动重发" in result.stderr
        assert recorded(state, "calls").splitlines() == ["call"]
        assert "intent" in summary(tmp_path)
        assert "accepted" not in summary(tmp_path)

    @pytest.mark.parametrize(
        "receipt",
        [
            pytest.param("", id="空响应"),
            pytest.param(json.dumps({"run_url": GOOD_RECEIPT["run_url"], "html_url": GOOD_RECEIPT["html_url"]}), id="缺编号"),
            pytest.param(json.dumps(GOOD_RECEIPT | {"workflow_run_id": 0}), id="编号为 0"),
            pytest.param(json.dumps(GOOD_RECEIPT | {"workflow_run_id": "123456789"}), id="编号是字符串"),
            pytest.param(
                json.dumps(GOOD_RECEIPT | {"run_url": GOOD_RECEIPT["run_url"].replace(DEPLOY_REPOSITORY, "other/repo")}),
                id="接口地址指向别的仓库",
            ),
            pytest.param(
                json.dumps(GOOD_RECEIPT | {"html_url": GOOD_RECEIPT["html_url"].replace(DEPLOY_REPOSITORY, "other/repo")}),
                id="网页地址指向别的仓库",
            ),
            pytest.param(json.dumps(GOOD_RECEIPT) + "\n" + json.dumps(GOOD_RECEIPT), id="两份回执"),
        ],
    )
    def test_incomplete_or_mismatched_receipt_fails_after_the_post(self, tmp_path, receipt):
        result, state = run_notify(tmp_path, receipt=receipt)

        assert result.returncode != 0
        assert recorded(state, "calls").splitlines() == ["call"]
        assert "accepted" not in summary(tmp_path)


class TestNotifyWorkflowShape:
    def test_triggers_only_after_insightweave_ci_completes_on_main(self):
        assert load_workflow()["on"] == {
            "workflow_run": {"workflows": ["InsightWeave CI"], "types": ["completed"], "branches": ["main"]}
        }

    def test_job_accepts_only_a_successful_main_push_of_this_repository(self):
        condition = load_workflow()["jobs"]["notify"]["if"]
        for fragment in (
            "github.event.workflow_run.conclusion == 'success'",
            "github.event.workflow_run.event == 'push'",
            "github.event.workflow_run.head_branch == 'main'",
            "github.event.workflow_run.head_repository.full_name == github.repository",
        ):
            assert fragment in condition

    def test_dispatch_runs_in_the_restricted_environment_without_checking_out_source(self):
        workflow = load_workflow()
        job = workflow["jobs"]["notify"]

        assert workflow["permissions"] == {"contents": "read"}
        assert job["environment"] == "deployment-dispatch"
        assert job["runs-on"] == "ubuntu-latest"
        assert [step for step in job["steps"] if "uses" in step] == []

    def test_source_inputs_are_bound_to_the_completed_run_and_the_owner_placeholder_is_replaced(self):
        text = WORKFLOW.read_text(encoding="utf-8")
        step_env = load_workflow()["jobs"]["notify"]["steps"][0]["env"]

        assert step_env["SOURCE_SHA"] == "${{ github.event.workflow_run.head_sha }}"
        assert step_env["SOURCE_RUN_ID"] == "${{ github.event.workflow_run.id }}"
        assert step_env["SOURCE_RUN_ATTEMPT"] == "${{ github.event.workflow_run.run_attempt }}"
        assert re.fullmatch(r"[A-Za-z0-9-]+/[A-Za-z0-9._-]+", step_env["DEPLOY_REPOSITORY"])
        assert "OWNER" not in text
        assert set(re.findall(r"secrets\.[A-Z_]+", text)) == {"secrets.DEPLOY_TRIGGER_TOKEN"}
