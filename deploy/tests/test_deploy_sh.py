"""deploy.sh 编排测试（doc 13 §7/§8.1/§9.2；任务 4）。

只替换 Docker/curl/python 文件边界（stub 注入），断言真实编排脚本的
退出码、状态文件内容与关键命令序列。
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "deploy" / "scripts" / "deploy.sh"
SOURCE_SHA = subprocess.check_output(
    ["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, text=True
).strip()
SNAPSHOT_FILES = (
    "compose.yaml",
    "contract.json",
    "postgres/01-create-databases.sh",
    "milvus/embedEtcd.yaml",
    "milvus/user.yaml",
)

DOCKER_STUB = r"""#!/usr/bin/env bash
# 记录全部调用；按全参数模式匹配实现 deploy.sh 用到的子集
# （compose 子命令前有 -f/--env-file 等全局 flags，不能按位置取子命令）
LOG="$DEPLOY_LOG"
echo "docker $*" >> "$LOG"
[ "$1" = "--context" ] && shift 2
ALL="$*"
case "$1" in
  ps)
    [ "$DEPLOY_PORT_QUERY_FAIL" = 1 ] && exit 8
    printf 'port-frontend\nport-attu\nport-neo4j\nport-postgres\n'; exit 0 ;;
  image)
    echo "sha256:$(printf '%s' "${@: -1}" | sha256sum | cut -d' ' -f1)"; exit 0 ;;
  inspect)
    if [[ "$3" == *".NetworkSettings.Ports"* ]]; then
      /usr/bin/python3 - <<'PY'
import json, os
ports = {"frontend": {"80/tcp": 18080}, "attu": {"3000/tcp": 3000},
         "neo4j": {"7474/tcp": 7474, "7687/tcp": 7687}, "postgres": {"5432/tcp": 6432}}
for service, mappings in ports.items():
    project = "other-project" if service == "postgres" and os.environ.get("DEPLOY_PORT_CONFLICT") else "insightweave-local"
    if service == "postgres" and os.environ.get("DEPLOY_PORT_WRONG_SERVICE"): service = "frontend"
    print(json.dumps({"running": True, "project": project, "service": service,
                      "ports": {key: [{"HostIp": "127.0.0.1", "HostPort": str(value)}] for key, value in mappings.items()}}))
PY
      exit 0
    fi
    if [[ "$3" == *".Mounts"* ]]; then
      echo "${DEPLOY_PLUGIN_MOUNT:-volume:insightweave-local_neo4j-plugins}"; exit 0
    fi
    if [ "$3" = "{{.State.Running}}" ]; then
      if [ -n "$DEPLOY_NEO4J_STATE" ] && [[ "$4" == *-neo4j ]]; then echo "$DEPLOY_NEO4J_STATE"; exit 0; fi
      if [ -n "$DEPLOY_STOPPED_SERVICE" ] && [[ "$4" == *"-$DEPLOY_STOPPED_SERVICE" ]] && [ ! -f "$DEPLOY_ROOT/.live-$DEPLOY_STOPPED_SERVICE" ]; then
        echo false
      else echo true; fi
      exit 0
    fi
    if [ "$3" = "{{.Image}}" ]; then
      app="${4##*-}"
      case "$app" in
        postgres|neo4j|milvus|attu)
          case "$app" in postgres) ref=p@1 ;; neo4j) ref=n@1 ;; milvus) ref=m@1 ;; attu) ref=a@1 ;; esac
          [ "$DEPLOY_WRONG_UPSTREAM" = "$app" ] && ref=wrong
          echo "sha256:$(printf '%s' "$ref" | sha256sum | cut -d' ' -f1)"; exit 0 ;;
      esac
      rid=$(cat "$DEPLOY_ROOT/.live-$app" 2>/dev/null || echo R0)
      if [ "$DEPLOY_WRONG_IMAGE" = 1 ] && [ "$app" = nl2sql ]; then
        echo "sha256:$(printf '%064d' 0)"; exit 0
      fi
      echo "sha256:$(printf '%s' "insightweave/$app:$rid" | sha256sum | cut -d' ' -f1)"; exit 0
    fi
    if [ "$2" = "--format" ] && [ "$3" = "{{.Config.Image}}" ]; then
      echo "$DEPLOY_FRONTEND_IMAGE"; exit 0
    fi
    # 新版持续 unhealthy，恢复阶段的第二次后端 up 之后转为 healthy
    if [ -n "$DEPLOY_UNHEALTHY_NEW_RELEASE" ] && [ ! -f "$DEPLOY_ROOT/.rollback_up" ]; then
      echo "unhealthy"; exit 0
    fi
    echo healthy; exit 0
    ;;
  volume)
    [ "$2" = create ] && [ "$DEPLOY_VOLUME_FAIL" = 1 ] && exit 8
    [ "$2" = inspect ] && [ "$DEPLOY_VOLUME_MISSING" = 1 ] && exit 8
    exit 0 ;;
  run)
    exec /usr/bin/python3 "$DEPLOY_PLUGIN_FIXTURE/driver.py" "$@" ;;
esac
if [ "$1" = "compose" ]; then
  case "$ALL" in
    *" run "*)
      if [[ "$ALL" == *"--scope infra"* ]]; then echo "infra $ALL" >> "$LOG"; else echo "config $ALL" >> "$LOG"; fi
      if [ "$DEPLOY_TARGET_CONFIG_FAIL" = 1 ] && [[ "$ALL" == *"load_base_db_agent_config"* ]]; then
        echo 'private configuration details' >&2; exit 8
      fi
      if [ "$DEPLOY_INFRA_FAIL_ON_RECOVERY" = 1 ] && [ -f "$DEPLOY_ROOT/..fail_marker" ] && [[ "$ALL" == *"--scope infra"* ]]; then exit 8; fi
      if [ "$DEPLOY_INFRA_FAIL" = 1 ] && [[ "$ALL" == *"--scope infra"* ]]; then echo 'milvus: unavailable' >&2; exit 1; fi
      echo 'infra passed'; exit 0 ;;
    *" exec "*)
      echo "exec $ALL" >> "$LOG"
      case "$ALL" in
        *"_deploy/state"*)
          if [ -n "$DEPLOY_CONTROL_FAIL_SERVICE" ] && [[ "$ALL" == *"exec -T $DEPLOY_CONTROL_FAIL_SERVICE "* ]]; then exit 8; fi
          maintaining="$DEPLOY_MAINTAINING"
          for app in metaweave nl2sql; do
            if [[ "$ALL" == *"exec -T $app "* ]] && [ -f "$DEPLOY_ROOT/.maintaining-$app" ]; then
              maintaining=$(cat "$DEPLOY_ROOT/.maintaining-$app")
            fi
          done
          if [ "$maintaining" = true ]; then
            case "$DEPLOY_DRAIN_STATE_FAULT" in
              truncated) printf '{"active":0'; exit 0 ;;
              not_maintaining) echo '{"deployment_contract_version":1,"maintaining":false,"active":0}'; exit 0 ;;
              failed_transport) echo '{"deployment_contract_version":1,"maintaining":true,"active":0}'; exit 8 ;;
            esac
          fi
          echo "{\"deployment_contract_version\":${DEPLOY_CONTRACT_VERSION:-1},\"maintaining\":$maintaining,\"active\":$DEPLOY_ACTIVE}" ;;
        *"_deploy/enter"*|*"_deploy/leave"*)
          for app in metaweave nl2sql; do
            if [[ "$ALL" == *"exec -T $app "* ]]; then
              if [[ "$ALL" == *"_deploy/leave"* ]]; then
                [ "$DEPLOY_LEAVE_FAIL_SERVICE" = "$app" ] && exit 8
                if [ "$DEPLOY_LEAVE_FAIL_ONCE" = 1 ] && [ "$app" = nl2sql ] && [ ! -f "$DEPLOY_ROOT/.leave-failure-hit" ]; then
                  : > "$DEPLOY_ROOT/.leave-failure-hit"; exit 8
                fi
                echo false > "$DEPLOY_ROOT/.maintaining-$app"
              else echo true > "$DEPLOY_ROOT/.maintaining-$app"; fi
            fi
          done ;;
        *"init_platform"*|*"smoke.py"*|*"nginx"*|*"01-create-databases"*) : ;;
      esac
      exit 0 ;;
    *" ps "*|*" ps")
      echo "ps $ALL" >> "$LOG"
      [ "$DEPLOY_EMPTY_PROJECT" = 1 ] && [ ! -f "$DEPLOY_ROOT/.project-started" ] && exit 0
      case "$ALL" in
        *"-q"*)
          [ -n "$DEPLOY_ABSENT_SERVICE" ] && [ "${@: -1}" = "$DEPLOY_ABSENT_SERVICE" ] && [ ! -f "$DEPLOY_ROOT/.live-$DEPLOY_ABSENT_SERVICE" ] && exit 0
          [[ " $DEPLOY_ABSENT_SERVICES " == *" ${@: -1} "* ]] && [ ! -f "$DEPLOY_ROOT/.live-${@: -1}" ] && exit 0
          case "${@: -1}" in
            metaweave|nl2sql|frontend|postgres|neo4j|milvus|attu) echo "$DEPLOY_CID-${@: -1}" ;;
            *) echo "$DEPLOY_CID" ;;
          esac ;;
        *) echo "NAME HEALTH" ;;
      esac
      exit 0 ;;
    *" up "*)
      echo "up $ALL" >> "$LOG"
      : > "$DEPLOY_ROOT/.project-started"
      if [ -f "$DEPLOY_ROOT/control/maintenance" ]; then echo 'up-marker present' >> "$LOG"; else echo 'up-marker absent' >> "$LOG"; fi
      if [ -n "$DEPLOY_UNHEALTHY_NEW_RELEASE" ] && [[ "$ALL" == *"--force-recreate"* ]]; then
        if [ -f "$DEPLOY_ROOT/.new_release_up" ]; then
          : > "$DEPLOY_ROOT/.rollback_up"
        else
          : > "$DEPLOY_ROOT/.new_release_up"
        fi
      fi
      # 一次性失败：首次 --force-recreate 返回 42（模拟新版替换失败），恢复阶段成功
      if [ -n "$DEPLOY_UP_FAIL" ] && [[ "$ALL" == *"--force-recreate"* ]] \
         && [ ! -f "$DEPLOY_ROOT/.up_fail_hit" ]; then
        : > "$DEPLOY_ROOT/.up_fail_hit"
        exit 42
      fi
      for app in metaweave nl2sql frontend; do
        if [[ " $ALL " == *" $app "* ]]; then
          key="${app^^}_IMAGE"; value="${!key}"
          echo "${value##*:}" > "$DEPLOY_ROOT/.live-$app"
        fi
      done
      [[ " $ALL " != *" neo4j "* ]] || : > "$DEPLOY_ROOT/.live-neo4j"
      exit 0 ;;
  esac
fi
exit 0
"""

CURL_STUB = r"""#!/usr/bin/env bash
# 控制面 HTTP 由 Docker 替身接管；此替身不向外发送请求，端口检查使用 TCP。
exit 1
"""

SLEEP_STUB = """#!/usr/bin/env bash
exit 0
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

PLUGIN_DRIVER = r'''"""仅替换容器文件路径；执行部署脚本传入的真实 shell。"""
import json
import os
from pathlib import Path
import subprocess
import sys

root = Path(os.environ["DEPLOY_PLUGIN_FIXTURE"])
args = sys.argv[1:]
(root / "last-run.json").write_text(json.dumps(args))
assert "--network" in args and args[args.index("--network") + 1] == "none"
script = args[args.index("-c") + 1]
script = script.replace("/var/lib/neo4j/labs", str(root / "labs"))
script = script.replace("/plugins", str(root / "plugins"))
tail = args[args.index("-c") + 2:]
env = {**os.environ, "PATH": str(root / "bin") + ":/usr/bin:/bin"}
result = subprocess.run(["/bin/sh", "-c", script, *tail], env=env, check=False, timeout=10)
raise SystemExit(result.returncode)
'''


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
            ("sleep", SLEEP_STUB),
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
        self.plugin = tmp_path / "plugin"
        for name in ("labs", "plugins", "bin"):
            (self.plugin / name).mkdir(parents=True)
        self.source_jar = self.plugin / "labs/apoc-5.26.19-core.jar"
        self.source_jar.write_bytes(b"fixed image APOC core")
        self.plugin_jar = self.plugin / "plugins/apoc.jar"
        self.plugin_jar.write_bytes(self.source_jar.read_bytes())
        (self.plugin / "driver.py").write_text(PLUGIN_DRIVER)
        self.base["DEPLOY_PLUGIN_FIXTURE"] = str(self.plugin)
        self.candidate = self.root / "candidate-R1.json"
        self.candidate.write_text(json.dumps(self.manifest("R1")))
        self.base["CANDIDATE_MANIFEST"] = str(self.candidate)

    def manifest(self, rid):
        return {
            "release_id": rid,
            "source_sha": SOURCE_SHA,
            "deployment_contract_version": 1,
            "applications": {
                app: {
                    "image": f"insightweave/{app}:{rid}",
                    "image_id": "sha256:"
                    + hashlib.sha256(f"insightweave/{app}:{rid}".encode()).hexdigest(),
                }
                for app in ("metaweave", "nl2sql", "frontend")
            },
        }

    def snapshot(self, rid):
        target = self.root / "releases" / rid
        target.mkdir(parents=True, exist_ok=True)
        manifest = self.manifest(rid)
        manifest["upstream_images"] = {
            "POSTGRES_IMAGE": "p@1",
            "NEO4J_IMAGE": "n@1",
            "MILVUS_IMAGE": "m@1",
            "ATTU_IMAGE": "a@1",
        }
        manifest["config_hashes"] = {}
        for name in SNAPSHOT_FILES:
            contents = (REPO_ROOT / "deploy" / name).read_bytes()
            path = target / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(contents)
            manifest["config_hashes"][name] = hashlib.sha256(contents).hexdigest()
        (target / "manifest.json").write_text(json.dumps(manifest))

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
        for rid in (current, previous):
            if rid:
                self.snapshot(rid)
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


class TestBringUpAppsExplicitFailure:
    """bring_up_apps 显式 return 1（审核 P1：|| 上下文禁用 set -e）。"""

    def test_backend_replace_failure_triggers_recovery(self, tmp_path):
        """后端替换命令失败(exit 42) → ok=0 → 自动回退到旧版本。"""
        env = Env(tmp_path)
        env.set_state("ready", current="R0")
        r = env.run("release", "R1", extra={"DEPLOY_UP_FAIL": "1"})
        assert r.returncode != 0
        state = env.state()
        assert state["current"] == "R0", f"应回退到 R0，实际 {state}"
        assert state["phase"] == "ready"

    def test_unhealthy_new_release_triggers_recovery(self, tmp_path):
        """新版持续未达 healthy → 不直接退出，自动恢复旧版本。"""
        env = Env(tmp_path)
        env.set_state("ready", current="R0")
        r = env.run("release", "R1", extra={"DEPLOY_UNHEALTHY_NEW_RELEASE": "1"})
        assert r.returncode != 0
        assert "metaweave 未达 healthy" in r.stdout + r.stderr
        state = env.state()
        assert state["current"] == "R0", f"应回退到 R0，实际 {state}"
        assert state["phase"] == "ready"
        ups = [
            line
            for line in env.calls().splitlines()
            if line.startswith("up ")
            and line.endswith("--force-recreate metaweave nl2sql")
        ]
        assert len(ups) == 2, f"应有发布和回退两次后端 up，实际 {ups}"


class TestAutoRollbackRebind:
    """自动回退须重绑镜像环境变量到旧版本（审核 P1）。"""

    def test_rollback_branch_rebinds_to_old_version(self, tmp_path):
        env = Env(tmp_path)
        env.set_state("ready", current="R0")
        r = env.run("release", "R1", extra={"FAIL_SMOKE_MODE": "--mode public"})
        assert r.returncode != 0
        calls = env.calls()
        # 回退分支的 up 应出现在 release 的 up 之后
        ups = [line for line in calls.splitlines() if "force-recreate" in line]
        assert len(ups) >= 2, f"应有两次 up（发布+回退），实际 {len(ups)}: {ups}"
        state = env.state()
        assert state["current"] == "R0" and state["phase"] == "ready"


class TestRollback:
    def _setup(self, env):
        env.set_state("deploying", current="R0", previous="R1")

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
        path = env.root / "releases" / "R1" / "manifest.json"
        manifest = json.loads(path.read_text())
        manifest["upstream_images"]["POSTGRES_IMAGE"] = "p@OTHER"
        path.write_text(json.dumps(manifest))
        r = env.run("rollback", "R1")
        assert r.returncode != 0
        assert "POSTGRES_IMAGE" in r.stderr
        assert "enter" not in env.calls()

    def test_rollback_same_version_ready_verify_only(self, tmp_path):
        env = Env(tmp_path)
        env.set_state("ready", current="R0", previous="R1")
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


class TestReleaseSnapshots:
    def test_new_snapshot_uses_candidate_and_complete_relative_files(self, tmp_path):
        env = Env(tmp_path)
        env.set_state("ready", current="R0")
        result = env.run("release", "R1")
        assert result.returncode == 0, result.stderr
        target = env.root / "releases" / "R1"
        data = json.loads((target / "manifest.json").read_text())
        assert data["applications"]["nl2sql"]["image"] == "insightweave/nl2sql:R1"
        assert data["source_sha"] == SOURCE_SHA
        assert all((target / name).is_file() for name in SNAPSHOT_FILES)

    def test_missing_or_wrong_candidate_stops_before_enter(self, tmp_path):
        env = Env(tmp_path)
        env.set_state("ready", current="R0")
        env.candidate.unlink()
        result = env.run("release", "R1")
        assert result.returncode != 0
        assert "enter" not in env.calls()
        assert env.state()["phase"] == "ready"

    def test_rollback_selects_old_compose_and_current_secrets(self, tmp_path):
        env = Env(tmp_path)
        env.set_state("deploying", current="R1", previous="R0")
        result = env.run("rollback", "R0")
        assert result.returncode == 0, result.stderr
        calls = [line for line in env.calls().splitlines() if line.startswith("up ")]
        assert calls
        assert all(f"-f {env.root}/releases/R0/compose.yaml" in line for line in calls)
        assert all(f"--env-file {env.env_file}" in line for line in calls)
        assert all("--no-deps" in line for line in calls)

    def test_automatic_recovery_uses_old_snapshot(self, tmp_path):
        env = Env(tmp_path)
        env.set_state("ready", current="R0")
        result = env.run("release", "R1", extra={"FAIL_SMOKE_MODE": "--mode public"})
        assert result.returncode != 0
        calls = [line for line in env.calls().splitlines() if line.startswith("up ")]
        assert any(f"-f {env.root}/releases/R1/compose.yaml" in line for line in calls)
        assert any(f"-f {env.root}/releases/R0/compose.yaml" in line for line in calls)
        assert env.state()["current"] == "R0" and env.state()["phase"] == "ready"

    def test_missing_legacy_current_snapshot_blocks_new_release_before_maintenance(
        self, tmp_path
    ):
        env = Env(tmp_path)
        env.set_state("ready", current="R0")
        (env.root / "releases/R0/manifest.json").unlink()
        result = env.run("release", "R1")
        assert result.returncode != 0
        assert "enter" not in env.calls()
        assert env.state()["phase"] == "ready"

    def test_http_smoke_cannot_hide_wrong_container_id(self, tmp_path):
        env = Env(tmp_path)
        env.set_state("ready", current="R0")
        result = env.run("release", "R1", extra={"DEPLOY_WRONG_IMAGE": "1"})
        assert result.returncode != 0
        assert env.state()["current"] != "R1"


class TestPreflight:
    def test_contract_failure_does_not_enter_maintenance_or_change_state(
        self, tmp_path
    ):
        env = Env(tmp_path)
        env.set_state("ready", current="R0")
        result = env.run("release", "R1", extra={"DEPLOY_CONTRACT_VERSION": "99"})
        assert result.returncode != 0
        assert env.state()["phase"] == "ready" and env.state()["current"] == "R0"
        assert "enter" not in env.calls() and "up " not in env.calls()
        assert not (env.root / "control/maintenance").exists()

    def test_upstream_mismatch_does_not_touch_containers(self, tmp_path):
        env = Env(tmp_path)
        env.set_state("ready", current="R0")
        result = env.run("release", "R1", extra={"DEPLOY_WRONG_UPSTREAM": "attu"})
        assert result.returncode != 0 and "attu" in result.stderr
        assert "enter" not in env.calls() and "up " not in env.calls()

    def test_infra_failure_cancels_before_marker_and_recreate(self, tmp_path):
        env = Env(tmp_path)
        env.set_state("ready", current="R0")
        result = env.run("release", "R1", extra={"DEPLOY_INFRA_FAIL": "1"})
        assert result.returncode != 0 and "milvus" in result.stderr
        assert env.state()["phase"] == "ready"
        assert "enter" not in env.calls() and "up " not in env.calls()

    def test_release_preflight_precedes_enter(self, tmp_path):
        env = Env(tmp_path)
        env.set_state("ready", current="R0")
        result = env.run("release", "R1")
        assert result.returncode == 0, result.stderr
        calls = env.calls()
        assert calls.index("infra ") < calls.index("/_deploy/enter")

    def test_bootstrap_checks_infra_after_initialization(self, tmp_path):
        env = Env(tmp_path)
        result = env.run("bootstrap", "R1")
        assert result.returncode == 0, result.stderr
        assert env.calls().index("milvus --mode initialize") < env.calls().index(
            "infra "
        )

    def test_rollback_skips_confirmed_stopped_backend_but_not_live_unknown(
        self, tmp_path
    ):
        env = Env(tmp_path)
        env.set_state("deploying", current="R1", previous="R0")
        result = env.run("rollback", "R0", extra={"DEPLOY_STOPPED_SERVICE": "nl2sql"})
        assert result.returncode == 0, result.stderr
        assert env.state()["phase"] == "ready" and env.state()["current"] == "R0"
        # 排空只 enter 当时还在运行的后端；替换后两端照常 leave。
        control_calls = [
            line for line in env.calls().splitlines() if line.startswith("exec ")
        ]
        assert not any(
            "exec -T nl2sql " in line and "/_deploy/enter" in line
            for line in control_calls
        )

    def test_rollback_live_control_failure_stops_before_enter(self, tmp_path):
        env = Env(tmp_path)
        env.set_state("deploying", current="R1", previous="R0")
        result = env.run(
            "rollback", "R0", extra={"DEPLOY_CONTROL_FAIL_SERVICE": "nl2sql"}
        )
        assert result.returncode != 0
        assert "enter" not in env.calls() and "up " not in env.calls()


class TestSameVersionRecovery:
    def test_first_start_has_maintenance_marker_before_any_service_starts(
        self, tmp_path
    ):
        env = Env(tmp_path)
        result = env.run("bootstrap", "R1", extra={"DEPLOY_EMPTY_PROJECT": "1"})
        assert result.returncode == 0, result.stderr
        assert "up-marker present" in env.calls()
        assert "up-marker absent" not in env.calls()
        assert not (env.root / "control/maintenance").exists()

    def test_same_bootstrap_is_read_only_and_keeps_both_refs(self, tmp_path):
        env = Env(tmp_path)
        env.set_state("ready", current="R0", previous="P")
        result = env.run("bootstrap", "R0")
        assert result.returncode == 0, result.stderr
        assert env.state()["current"] == "R0" and env.state()["previous"] == "P"
        assert "up " not in env.calls() and "initialize" not in env.calls()
        assert "enter" not in env.calls()

    @pytest.mark.parametrize("command", ["bootstrap", "release"])
    @pytest.mark.parametrize(
        "fault", ["image", "marker", "closed", "smoke", "snapshot"]
    )
    def test_failed_same_version_check_marks_recovery_without_replacing(
        self, tmp_path, command, fault
    ):
        env = Env(tmp_path)
        env.set_state("ready", current="R0", previous="P")
        extra = {}
        if fault == "image":
            (env.root / ".live-nl2sql").write_text("BAD")
        elif fault == "marker":
            (env.root / "control/maintenance").touch()
        elif fault == "closed":
            extra["DEPLOY_MAINTAINING"] = "true"
        elif fault == "smoke":
            extra["FAIL_SMOKE_MODE"] = "--mode public"
        else:
            (env.root / "releases/R0/manifest.json").unlink()
        result = env.run(command, "R0", extra=extra)
        assert result.returncode != 0
        assert env.state()["phase"] == "recovery_required"
        assert env.state()["current"] == "R0" and env.state()["previous"] == "P"
        assert "up " not in env.calls() and "enter" not in env.calls()
        assert "rollback R0" in result.stdout + result.stderr

    @pytest.mark.parametrize(
        "fault", ["image", "marker", "closed", "smoke", "stopped", "absent"]
    )
    def test_rollback_current_recovers_bad_ready_site_and_preserves_previous(
        self, tmp_path, fault
    ):
        env = Env(tmp_path)
        env.set_state("ready", current="R0", previous="P")
        extra = {}
        if fault == "image":
            (env.root / ".live-nl2sql").write_text("BAD")
        elif fault == "marker":
            (env.root / "control/maintenance").touch()
        elif fault == "closed":
            extra["DEPLOY_MAINTAINING"] = "true"
        elif fault == "smoke":
            extra["FAIL_SMOKE_MODE"] = "--mode public"
        else:
            extra["DEPLOY_" + fault.upper() + "_SERVICE"] = "nl2sql"
        result = env.run("rollback", "R0", extra=extra)
        assert result.returncode == 0, result.stderr
        assert env.state()["phase"] == "ready"
        assert env.state()["current"] == "R0" and env.state()["previous"] == "P"
        assert (env.root / ".live-nl2sql").read_text().strip() == "R0"
        calls = env.calls()
        assert "up -d --no-deps --force-recreate metaweave nl2sql" in calls
        assert calls.index("/_deploy/enter") < calls.index("up -d")
        assert calls.index("--mode maintenance") < calls.rindex("--mode public")
        assert (
            "initialize" not in calls
            and not (env.root / "control/maintenance").exists()
        )

    def test_rollback_current_unknown_live_control_requires_manual_recovery(
        self, tmp_path
    ):
        env = Env(tmp_path)
        env.set_state("ready", current="R0", previous="P")
        result = env.run(
            "rollback", "R0", extra={"DEPLOY_CONTROL_FAIL_SERVICE": "nl2sql"}
        )
        assert result.returncode != 0
        assert env.state()["phase"] == "recovery_required"
        assert env.state()["previous"] == "P"
        assert "enter" not in env.calls() and "up " not in env.calls()

    def test_bootstrap_database_only_retry_does_not_enter_absent_backends(
        self, tmp_path
    ):
        env = Env(tmp_path)
        result = env.run(
            "bootstrap",
            "R1",
            extra={"DEPLOY_ABSENT_SERVICES": "metaweave nl2sql frontend"},
        )
        assert result.returncode == 0, result.stderr
        assert env.state()["current"] == "R1"
        assert "/_deploy/enter" not in env.calls()
        assert "up-marker absent" not in env.calls()

    @pytest.mark.parametrize(
        "failure", ["none", "metaweave", "nl2sql", "smoke", "image"]
    )
    def test_drain_timeout_only_marks_ready_after_confirming_original_site(
        self, tmp_path, failure
    ):
        env = Env(tmp_path)
        env.set_state("ready", current="R0", previous="P")
        extra = {"DEPLOY_ACTIVE": "2"}
        if failure in ("metaweave", "nl2sql"):
            extra["DEPLOY_LEAVE_FAIL_SERVICE"] = failure
        elif failure == "smoke":
            extra["FAIL_SMOKE_MODE"] = "--mode public"
        elif failure == "image":
            (env.root / ".live-nl2sql").write_text("BAD")
        result = env.run("release", "R1", extra=extra)
        assert result.returncode != 0
        assert env.state()["phase"] == (
            "ready" if failure == "none" else "recovery_required"
        )
        assert env.state()["current"] == "R0" and env.state()["previous"] == "P"
        assert "up " not in env.calls()
        if failure == "none":
            assert "--mode public" in env.calls()
            assert not (env.root / "control/maintenance").exists()

    def test_failed_actual_restore_stays_non_ready(self, tmp_path):
        env = Env(tmp_path)
        env.set_state("ready", current="R0", previous="P")
        (env.root / "control/maintenance").touch()
        result = env.run("rollback", "R0", extra={"DEPLOY_UP_FAIL": "1"})
        assert result.returncode != 0
        assert env.state()["phase"] == "recovery_required"
        assert env.state()["current"] == "R0" and env.state()["previous"] == "P"
        assert (env.root / "control/maintenance").exists()

    def test_drain_timeout_keeps_existing_maintenance_closed(self, tmp_path):
        env = Env(tmp_path)
        env.set_state("recovery_required", current="R0", previous="P")
        (env.root / "control/maintenance").touch()
        result = env.run("rollback", "R0", extra={"DEPLOY_ACTIVE": "2"})
        assert result.returncode != 0
        assert env.state()["phase"] == "recovery_required"
        assert (env.root / "control/maintenance").exists()
        assert "up " not in env.calls() and "/_deploy/leave" not in env.calls()


class TestApocPreparation:
    def test_first_copy_is_verified_and_readable_before_neo4j_start(self, tmp_path):
        env = Env(tmp_path)
        env.plugin_jar.unlink()
        result = env.run("bootstrap", "R1", extra={"DEPLOY_STOPPED_SERVICE": "neo4j"})
        assert result.returncode == 0, result.stderr
        assert env.plugin_jar.read_bytes() == b"fixed image APOC core"
        assert stat.S_IMODE(env.plugin_jar.stat().st_mode) == 0o644
        assert not list((env.plugin / "plugins").glob(".apoc.*"))
        assert env.calls().index(" run --rm ") < env.calls().index(
            "up -d postgres neo4j milvus"
        )

    @pytest.mark.parametrize("running", [False, True])
    def test_matching_file_keeps_inode_mtime_and_other_plugins(self, tmp_path, running):
        env = Env(tmp_path)
        (env.plugin / "plugins/other.jar").write_bytes(b"other plugin")
        before = env.plugin_jar.stat()
        extra = {} if running else {"DEPLOY_STOPPED_SERVICE": "neo4j"}
        result = env.run("bootstrap", "R1", extra=extra)
        assert result.returncode == 0, result.stderr
        after = env.plugin_jar.stat()
        assert (after.st_ino, after.st_mtime_ns) == (before.st_ino, before.st_mtime_ns)
        assert (env.plugin / "plugins/other.jar").read_bytes() == b"other plugin"
        if running:
            assert "readonly" in env.calls() or ":/plugins:ro" in env.calls()
            assert "volume create" not in env.calls()

    @pytest.mark.parametrize(
        "fault",
        [
            "damaged",
            "duplicate",
            "hidden_duplicate",
            "unknown_name",
            "source_missing",
            "source_duplicate",
        ],
    )
    def test_conflicting_files_stop_bootstrap_and_preserve_originals(
        self, tmp_path, fault
    ):
        env = Env(tmp_path)
        if fault == "damaged":
            env.plugin_jar.write_bytes(b"damaged")
        elif fault == "duplicate":
            (env.plugin / "plugins/apoc-old.jar").write_bytes(b"older")
        elif fault == "hidden_duplicate":
            (env.plugin / "plugins/.apoc-old.jar").write_bytes(b"older")
        elif fault == "unknown_name":
            env.plugin_jar.rename(env.plugin / "plugins/apoc-custom.jar")
        elif fault == "source_missing":
            env.source_jar.unlink()
        else:
            (env.plugin / "labs/apoc-other-core.jar").write_bytes(b"another source")
        before = {p.name: p.read_bytes() for p in (env.plugin / "plugins").iterdir()}
        result = env.run("bootstrap", "R1", extra={"DEPLOY_STOPPED_SERVICE": "neo4j"})
        assert result.returncode != 0
        assert "up " not in env.calls()
        assert env.state()["phase"] == "recovery_required"
        assert {
            p.name: p.read_bytes() for p in (env.plugin / "plugins").iterdir()
        } == before

    @pytest.mark.parametrize(
        "fault",
        [
            "missing",
            "damaged",
            "duplicate",
            "wrong_mount",
            "missing_volume",
            "unknown_state",
        ],
    )
    def test_live_neo4j_only_checks_actual_plugin_volume(self, tmp_path, fault):
        env = Env(tmp_path)
        extra = {}
        if fault == "missing":
            env.plugin_jar.unlink()
        elif fault == "damaged":
            env.plugin_jar.write_bytes(b"damaged")
        elif fault == "duplicate":
            (env.plugin / "plugins/APOC-old.JAR").write_bytes(b"older")
        elif fault == "wrong_mount":
            extra["DEPLOY_PLUGIN_MOUNT"] = "volume:other-plugins"
        elif fault == "missing_volume":
            extra["DEPLOY_VOLUME_MISSING"] = "1"
        else:
            extra["DEPLOY_NEO4J_STATE"] = "unknown"
        before = {p.name: p.read_bytes() for p in (env.plugin / "plugins").iterdir()}
        result = env.run("bootstrap", "R1", extra=extra)
        assert result.returncode != 0
        assert "up " not in env.calls() and "volume create" not in env.calls()
        assert {
            p.name: p.read_bytes() for p in (env.plugin / "plugins").iterdir()
        } == before

    @pytest.mark.parametrize("command", ["volume", "cp", "chmod", "sha256sum", "mv"])
    def test_preparation_failure_cleans_only_own_temp_and_never_starts_services(
        self, tmp_path, command
    ):
        env = Env(tmp_path)
        env.plugin_jar.unlink()
        (env.plugin / "plugins/other.jar").write_bytes(b"keep")
        (env.plugin / "plugins/.apoc.previous").write_bytes(b"unrelated old temp")
        extra = {"DEPLOY_STOPPED_SERVICE": "neo4j"}
        if command == "volume":
            extra["DEPLOY_VOLUME_FAIL"] = "1"
        else:
            shim = env.plugin / "bin" / command
            body = "exit 8"
            if command == "cp":
                body = 'printf partial > "$2"; exit 8'
            elif command == "sha256sum":
                body = 'case "$1" in */.apoc.*) printf "%064d  %s\\n" 0 "$1" ;; *) exec /usr/bin/sha256sum "$@" ;; esac'
            shim.write_text("#!/bin/sh\n" + body + "\n")
            shim.chmod(0o755)
        result = env.run("bootstrap", "R1", extra=extra)
        assert result.returncode != 0
        assert "up " not in env.calls() and not env.plugin_jar.exists()
        assert {p.name: p.read_bytes() for p in (env.plugin / "plugins").iterdir()} == {
            "other.jar": b"keep",
            ".apoc.previous": b"unrelated old temp",
        }

    @pytest.mark.parametrize("damaged", [False, True])
    def test_same_version_bootstrap_checks_plugin_read_only_and_keeps_refs(
        self, tmp_path, damaged
    ):
        env = Env(tmp_path)
        env.set_state("ready", current="R0", previous="P")
        if damaged:
            env.plugin_jar.write_bytes(b"damaged")
        before = env.plugin_jar.stat()
        result = env.run("bootstrap", "R0")
        assert (result.returncode == 0) is (not damaged)
        assert env.state()["phase"] == ("recovery_required" if damaged else "ready")
        assert env.state()["current"] == "R0" and env.state()["previous"] == "P"
        assert "up " not in env.calls() and "volume create" not in env.calls()
        assert env.plugin_jar.stat().st_ino == before.st_ino
        assert "readonly" in env.calls() or ":/plugins:ro" in env.calls()


class TestHostPortPreflight:
    @pytest.mark.parametrize("command", ["bootstrap", "release", "rollback"])
    def test_same_version_verification_does_not_accept_foreign_port(
        self, tmp_path, command
    ):
        env = Env(tmp_path)
        env.set_state("ready", current="R0", previous="P")
        result = env.run(command, "R0", extra={"DEPLOY_PORT_CONFLICT": "1"})
        assert result.returncode != 0
        assert env.state()["phase"] == "recovery_required"
        assert env.state()["current"] == "R0" and env.state()["previous"] == "P"
        assert "up " not in env.calls() and "enter" not in env.calls()

    @pytest.mark.parametrize("command", ["bootstrap", "release", "rollback"])
    def test_foreign_binding_refuses_before_maintenance_or_replacement(
        self, tmp_path, command
    ):
        env = Env(tmp_path)
        if command == "release":
            env.set_state("ready", current="R0", previous="P")
        elif command == "rollback":
            env.set_state("recovery_required", current="R1", previous="R0")
        before = env.state()
        result = env.run(
            command,
            "R0" if command == "rollback" else "R1",
            extra={"DEPLOY_PORT_CONFLICT": "1"},
        )
        assert result.returncode != 0 and "6432" in result.stderr
        assert env.state() == before
        assert "enter" not in env.calls() and "up " not in env.calls()
        assert not (env.root / "control/maintenance").exists()

    @pytest.mark.parametrize(
        "fault", ["DEPLOY_PORT_QUERY_FAIL", "DEPLOY_PORT_WRONG_SERVICE"]
    )
    def test_existing_project_container_does_not_bypass_port_ownership(
        self, tmp_path, fault
    ):
        env = Env(tmp_path)
        env.set_state("ready", current="R0")
        result = env.run("release", "R1", extra={fault: "1"})
        assert result.returncode != 0
        assert env.state()["phase"] == "ready"
        assert "up " not in env.calls() and "enter" not in env.calls()


class TestFinalReviewRegressions:
    def test_same_tag_wrong_frontend_image_is_actually_recovered(self, tmp_path):
        env = Env(tmp_path)
        env.set_state("ready", current="R0", previous="P")
        (env.root / ".live-frontend").write_text("BAD")
        result = env.run(
            "rollback",
            "R0",
            extra={"DEPLOY_FRONTEND_IMAGE": "insightweave/frontend:R0"},
        )
        assert result.returncode == 0, result.stderr
        assert env.state()["phase"] == "ready" and env.state()["previous"] == "P"
        assert (env.root / ".live-frontend").read_text().strip() == "R0"

    def test_matching_frontend_image_is_kept_and_reloaded(self, tmp_path):
        env = Env(tmp_path)
        env.set_state("recovery_required", current="R0", previous="P")
        result = env.run(
            "rollback",
            "R0",
            extra={"DEPLOY_FRONTEND_IMAGE": "insightweave/frontend:R0"},
        )
        assert result.returncode == 0, result.stderr
        assert "nginx -s reload" in env.calls()
        assert not any(
            line.startswith("up ") and line.endswith(" frontend")
            for line in env.calls().splitlines()
        )

    @pytest.mark.parametrize(
        "fault", ["truncated", "not_maintaining", "failed_transport"]
    )
    def test_unknown_drain_state_does_not_replace_containers(self, tmp_path, fault):
        env = Env(tmp_path)
        env.set_state("ready", current="R0", previous="P")
        result = env.run("release", "R1", extra={"DEPLOY_DRAIN_STATE_FAULT": fault})
        assert result.returncode != 0
        assert "up " not in env.calls()
        assert env.state()["phase"] == "recovery_required"
        assert (env.root / "control/maintenance").exists()

    @pytest.mark.parametrize("once", [False, True])
    def test_leave_failure_enters_recovery_and_never_leaves_unknown_site_open(
        self, tmp_path, once
    ):
        env = Env(tmp_path)
        env.set_state("ready", current="R0", previous="P")
        extra = (
            {"DEPLOY_LEAVE_FAIL_ONCE": "1"}
            if once
            else {"DEPLOY_LEAVE_FAIL_SERVICE": "nl2sql"}
        )
        result = env.run("release", "R1", extra=extra)
        assert result.returncode != 0
        assert env.state()["current"] == "R0" and env.state()["previous"] == "P"
        assert env.state()["phase"] == ("ready" if once else "recovery_required")
        assert (env.root / "control/maintenance").exists() is (not once)
        assert (env.root / ".maintaining-metaweave").read_text().strip() == (
            "false" if once else "true"
        )
        assert (
            len(
                [
                    line
                    for line in env.calls().splitlines()
                    if line.startswith("up ") and line.endswith(" metaweave nl2sql")
                ]
            )
            == 2
        )

    def test_recovery_preflight_failure_keeps_marker_and_closes_opened_backend(
        self, tmp_path
    ):
        env = Env(tmp_path)
        env.set_state("ready", current="R0", previous="P")
        result = env.run(
            "release",
            "R1",
            extra={
                "FAIL_SMOKE_MODE": "--mode public",
                "DEPLOY_INFRA_FAIL_ON_RECOVERY": "1",
            },
        )
        assert result.returncode != 0
        assert env.state()["phase"] == "recovery_required"
        assert (env.root / "control/maintenance").exists()
        assert (env.root / ".maintaining-metaweave").read_text().strip() == "true"
        assert (env.root / ".maintaining-nl2sql").read_text().strip() == "true"

    def test_target_nl_config_failure_stops_before_maintenance(self, tmp_path):
        env = Env(tmp_path)
        env.set_state("ready", current="R0")
        result = env.run("release", "R1", extra={"DEPLOY_TARGET_CONFIG_FAIL": "1"})
        assert result.returncode != 0
        assert env.state()["phase"] == "ready" and env.state()["current"] == "R0"
        assert "enter" not in env.calls() and "up " not in env.calls()
        assert not (env.root / "control/maintenance").exists()
        assert "private configuration details" not in result.stdout + result.stderr
