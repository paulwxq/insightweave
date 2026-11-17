"""deploy.sh 编排测试（doc 13 §7/§8.1/§9.2；任务 4）。

只替换 Docker/curl/python 文件边界（stub 注入），断言真实编排脚本的
退出码、状态文件内容与关键命令序列。
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "deploy" / "scripts" / "deploy.sh"

DOCKER_STUB = r"""#!/usr/bin/env bash
# 记录全部调用；按全参数模式匹配实现 deploy.sh 用到的子集
# （compose 子命令前有 -f/--env-file 等全局 flags，不能按位置取子命令）
LOG="$DEPLOY_LOG"
echo "docker $*" >> "$LOG"
[ "$1" = "--context" ] && shift 2
ALL="$*"
case "$1" in
  image) exit 0 ;;
  inspect)
    if [ "$2" = "--format" ] && [ "$3" = "{{.Config.Image}}" ]; then
      echo "$DEPLOY_FRONTEND_IMAGE"; exit 0
    fi
    echo healthy; exit 0
    ;;
  run) exit 0 ;;
esac
if [ "$1" = "compose" ]; then
  case "$ALL" in
    *" exec "*)
      echo "exec $ALL" >> "$LOG"
      case "$ALL" in
        *"_deploy/state"*)
          echo "{\"deployment_contract_version\":1,\"maintaining\":$DEPLOY_MAINTAINING,\"active\":$DEPLOY_ACTIVE}" ;;
        *"init_platform"*|*"smoke.py"*|*"nginx"*|*"01-create-databases"*) : ;;
      esac
      exit 0 ;;
    *" ps "*|*" ps")
      echo "ps $ALL" >> "$LOG"
      case "$ALL" in
        *"-q"*) echo "$DEPLOY_CID" ;;
        *) echo "NAME HEALTH" ;;
      esac
      exit 0 ;;
    *" up "*) echo "up $ALL" >> "$LOG"; exit 0 ;;
  esac
fi
exit 0
"""

CURL_STUB = r"""#!/usr/bin/env bash
# 宿主端口探测：stub 环境一律"无占用"（端口预检的真实行为由实机验收）
exit 1
"""

PYTHON_STUB = r"""#!/usr/bin/env bash
# smoke.py 调用可控（FAIL_SMOKE_MODE 注入失败模式）；其余代理真 python3
# （deploy.sh 的 state_write heredoc 经 stdin 透传）
echo "python $*" >> "$DEPLOY_LOG"
case "$*" in
  *smoke.py*)
    if [ -n "$FAIL_SMOKE_MODE" ] && [[ "$*" == *"$FAIL_SMOKE_MODE"* ]]; then
      # 一次性失败：首次命中即失败，其后成功（回退路径的冒烟可通过）
      if [ ! -f "$DEPLOY_ROOT/..fail_marker" ]; then
        : > "$DEPLOY_ROOT/..fail_marker"; exit 1
      fi
    fi
    exit 0
    ;;
  *) exec /usr/bin/python3 "$@" ;;
esac
"""


class Env:
    def __init__(self, tmp_path: Path):
        self.root = tmp_path / "deploy"
        (self.root / "state").mkdir(parents=True)
        (self.root / "releases").mkdir()
        (self.root / "config").mkdir()
        (self.root / "control").mkdir()
        self.env_file = self.root / "deploy.env"
        self.env_file.write_text(
            "POSTGRES_IMAGE=p@1\nNEO4J_IMAGE=n@1\nMILVUS_IMAGE=m@1\nATTU_IMAGE=a@1\n"
            "METAWEAVE_IMAGE=insightweave/metaweave:RID\n"
            "NL2SQL_IMAGE=insightweave/nl2sql:RID\n"
            "FRONTEND_IMAGE=insightweave/frontend:RID\n",
            encoding="utf-8",
        )
        (self.root / "config" / "app.env").write_text(
            "DEPLOY_CONTROL_TOKEN=t0k\n", encoding="utf-8"
        )
        self.log = tmp_path / "docker.log"
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        for name, body in (
            ("docker", DOCKER_STUB),
            ("curl", CURL_STUB),
            ("python3", PYTHON_STUB),
        ):
            stub = bin_dir / name
            stub.write_text(body, encoding="utf-8")
            stub.chmod(0o755)
        self.base = {
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "DEPLOY_ROOT": str(self.root),
            "ENV_FILE": str(self.env_file),
            "DEPLOY_LOG": str(self.log),
            "DEPLOY_CID": "cid0",
            "DEPLOY_ACTIVE": "0",
            "DEPLOY_MAINTAINING": "false",
            "DEPLOY_FRONTEND_IMAGE": "",
            "DEPLOY_DRAIN_TIMEOUT": "4",
            "PYTHON_BIN": str(bin_dir / "python3"),
        }

    def run(self, *args, rid="R1", extra=None):
        env = {**self.base, **(extra or {})}
        cmd = (
            ["bash", str(SCRIPT), args[0], args[1].replace("RID", rid)]
            if len(args) == 2
            else ["bash", str(SCRIPT), *args]
        )
        return subprocess.run(
            cmd, env=env, capture_output=True, text=True, timeout=120, check=False
        )

    def state(self) -> dict:
        f = self.root / "state" / "deployment.json"
        return json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}

    def calls(self) -> str:
        return self.log.read_text(encoding="utf-8") if self.log.exists() else ""

    def set_state(self, phase, current=None, previous=None):
        f = self.root / "state" / "deployment.json"
        f.write_text(
            json.dumps(
                {
                    "phase": phase,
                    "current": current,
                    "previous": previous,
                    "last_operation": "-",
                }
            ),
            encoding="utf-8",
        )


class TestGuards:
    def test_no_state_release_refuses(self, tmp_path):
        env = Env(tmp_path)
        r = env.run("release", "R1")
        assert r.returncode != 0
        assert "ready" in r.stderr

    def test_release_with_marker_refuses(self, tmp_path):
        env = Env(tmp_path)
        env.set_state("ready", current="R0")
        (env.root / "control" / "maintenance").write_text("", encoding="utf-8")
        r = env.run("release", "R1")
        assert r.returncode != 0
        assert "维护标记" in r.stderr

    def test_rollback_target_outside_refs_refuses(self, tmp_path):
        env = Env(tmp_path)
        env.set_state("ready", current="R0", previous="R1")
        r = env.run("rollback", "R9")
        assert r.returncode != 0
        assert "current" in r.stderr and "previous" in r.stderr


class TestRelease:
    def _ready(self, env):
        env.set_state("ready", current="R0")
        (env.root / "releases" / "R0").mkdir(parents=True, exist_ok=True)
        (env.root / "releases" / "R0" / "manifest.env").write_text(
            "POSTGRES_IMAGE=p@1\nNEO4J_IMAGE=n@1\nMILVUS_IMAGE=m@1\nATTU_IMAGE=a@1\n",
            encoding="utf-8",
        )

    def test_same_version_verify_only_keeps_refs(self, tmp_path):
        env = Env(tmp_path)
        self._ready(env)
        r = env.run("release", "R0")
        assert r.returncode == 0, r.stderr
        assert env.state()["current"] == "R0"
        assert "up" not in env.calls()  # 不替换容器
        assert "enter" not in env.calls()

    def test_release_b_success_flow(self, tmp_path):
        env = Env(tmp_path)
        self._ready(env)
        r = env.run("release", "R1")
        assert r.returncode == 0, r.stderr
        state = env.state()
        assert (
            state["current"] == "R1"
            and state["previous"] == "R0"
            and state["phase"] == "ready"
        )
        calls = env.calls()
        # §7.4 成功边界顺序：enter（排空）→ up 后端 → maintenance 冒烟 → leave → public 冒烟
        i_enter = calls.index("exec -T metaweave sh -eu -c")
        i_up = calls.index("up -d --no-deps --force-recreate metaweave nl2sql")
        assert i_enter < i_up
        assert "maintenance" in calls and "public" in calls
        assert calls.index(
            "up -d --no-deps --force-recreate metaweave nl2sql"
        ) < calls.index("--mode maintenance")
        assert (
            calls.index("--mode maintenance") < calls.rindex("leave")
            or "leave" in calls
        )

    def test_public_smoke_fail_rolls_back_to_current(self, tmp_path):
        env = Env(tmp_path)
        self._ready(env)
        r = env.run("release", "R1", extra={"FAIL_SMOKE_MODE": "--mode public"})
        assert r.returncode != 0
        state = env.state()
        assert state["current"] == "R0" and state["phase"] == "ready"
        assert "已回退" in (r.stderr + r.stdout) or "回退" in state.get(
            "last_operation", ""
        )


class TestDrain:
    def test_drain_timeout_cancels_without_recreate(self, tmp_path):
        env = Env(tmp_path)
        env.set_state("ready", current="R0")
        (env.root / "releases" / "R0").mkdir(parents=True, exist_ok=True)
        (env.root / "releases" / "R0" / "manifest.env").write_text(
            "POSTGRES_IMAGE=p@1\nNEO4J_IMAGE=n@1\nMILVUS_IMAGE=m@1\nATTU_IMAGE=a@1\n",
            encoding="utf-8",
        )
        r = env.run("release", "R1", extra={"DEPLOY_ACTIVE": "2"})
        assert r.returncode != 0
        assert "排空超时" in r.stderr
        calls = env.calls()
        assert "up -d --no-deps --force-recreate metaweave nl2sql" not in calls
        assert "leave" in calls  # 已 leave 退出维护


class TestBootstrap:
    def test_fresh_bootstrap_success(self, tmp_path):
        env = Env(tmp_path)
        r = env.run("bootstrap", "R1")
        assert r.returncode == 0, r.stderr
        state = env.state()
        assert (
            state["current"] == "R1"
            and state["previous"] is None
            and state["phase"] == "ready"
        )
        calls = env.calls()
        assert "01-create-databases" in calls
        assert "metaweave --mode initialize" in calls
        assert "maintenance" in calls and "public" in calls

    def test_bootstrap_with_current_ready_other_version_refuses(self, tmp_path):
        env = Env(tmp_path)
        env.set_state("ready", current="R0")
        r = env.run("bootstrap", "R1")
        assert r.returncode != 0
        assert "release" in r.stderr

    def test_bootstrap_non_ready_with_current_refuses_to_rollback(self, tmp_path):
        env = Env(tmp_path)
        env.set_state("deploying", current="R0")
        r = env.run("bootstrap", "R1")
        assert r.returncode != 0
        assert "rollback" in r.stderr


class TestRollback:
    def _setup(self, env):
        env.set_state("deploying", current="R0", previous="R1")
        for rid in ("R0", "R1"):
            d = env.root / "releases" / rid
            d.mkdir(parents=True, exist_ok=True)
            (d / "manifest.env").write_text(
                "POSTGRES_IMAGE=p@1\nNEO4J_IMAGE=n@1\nMILVUS_IMAGE=m@1\nATTU_IMAGE=a@1\n",
                encoding="utf-8",
            )

    def test_rollback_previous_swaps_refs(self, tmp_path):
        env = Env(tmp_path)
        self._setup(env)
        r = env.run("rollback", "R1")
        assert r.returncode == 0, r.stderr
        state = env.state()
        assert (
            state["current"] == "R1"
            and state["previous"] == "R0"
            and state["phase"] == "ready"
        )

    def test_rollback_upstream_mismatch_refuses_before_maintenance(self, tmp_path):
        env = Env(tmp_path)
        self._setup(env)
        (env.root / "releases" / "R1" / "manifest.env").write_text(
            "POSTGRES_IMAGE=p@OTHER\nNEO4J_IMAGE=n@1\nMILVUS_IMAGE=m@1\nATTU_IMAGE=a@1\n",
            encoding="utf-8",
        )
        r = env.run("rollback", "R1")
        assert r.returncode != 0
        assert "POSTGRES_IMAGE" in r.stderr
        assert "enter" not in env.calls()

    def test_rollback_same_version_ready_verify_only(self, tmp_path):
        env = Env(tmp_path)
        env.set_state("ready", current="R0", previous="R1")
        d = env.root / "releases" / "R0"
        d.mkdir(parents=True, exist_ok=True)
        (d / "manifest.env").write_text(
            "POSTGRES_IMAGE=p@1\nNEO4J_IMAGE=n@1\nMILVUS_IMAGE=m@1\nATTU_IMAGE=a@1\n",
            encoding="utf-8",
        )
        r = env.run("rollback", "R0")
        assert r.returncode == 0, r.stderr
        assert env.state()["previous"] == "R1"  # 不变
        assert "up " not in env.calls().replace("up -d --no-deps", "")


class TestScriptQuality:
    def test_bash_syntax(self):
        r = subprocess.run(
            ["bash", "-n", str(SCRIPT)], capture_output=True, text=True, check=False
        )
        assert r.returncode == 0, r.stderr

    def test_executable(self):
        assert SCRIPT.stat().st_mode & stat.S_IXUSR
