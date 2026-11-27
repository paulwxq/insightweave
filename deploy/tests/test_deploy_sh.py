"""deploy.sh 编排测试（doc 13 §7/§8.1/§9.2；任务 4）。

只替换 Docker/curl/python 文件边界（stub 注入），断言真实编排脚本的
退出码、状态文件内容与关键命令序列。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "deploy" / "scripts" / "deploy.sh"
PREPARE_TOOL = REPO_ROOT / "deploy" / "scripts" / "prepare_app_env.py"
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
  # Doc 16：记录每次 Compose 调用实际使用的两个后端 env 文件（候选或正式），单独落盘不混进调用日志；
  # exec 的调用带多行脚本参数，压成一行才能按调用逐条解析
  printf 'ENVFILES %s :: METAWEAVE_ENV_FILE=%s NL2SQL_ENV_FILE=%s\n' "${ALL//$'\n'/ }" "${METAWEAVE_ENV_FILE--}" "${NL2SQL_ENV_FILE--}" >> "$DEPLOY_ROOT/envfiles.log"
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
  *) if [ "$DEPLOY_TEST_INTERRUPT" = register-fail ] && [ "${7-}" = "set:R1" ]; then exit 1; fi
     exec /usr/bin/python3 "$@" ;;
esac
"""

# 只在注入中断时介入：替换第二份正式 env（nl2sql.env）时打断 deploy.sh；其余情况执行真实 mv
#   fail        只让第一次失败：正式文件替换失败，但随后的备份恢复照常成功（可处理的错误）
#   fail-always 一直失败：连备份恢复也做不了（自动恢复失败）
#   KILL        在第一份已替换、第二份未替换之际杀掉 deploy.sh（进程被强制终止）
MV_STUB = r"""#!/usr/bin/env bash
if [ -n "$DEPLOY_TEST_INTERRUPT" ] && [ -n "$DEPLOY_ROOT" ] && [ "${@: -1}" = "$DEPLOY_ROOT/config/nl2sql.env" ]; then
  case "$DEPLOY_TEST_INTERRUPT" in
    fail) [ -f "$DEPLOY_ROOT/.mv_fail_hit" ] || { : > "$DEPLOY_ROOT/.mv_fail_hit"; exit 1; } ;;
    fail-always) exit 1 ;;
    KILL) kill -KILL "$PPID"; exit 0 ;;
  esac
fi
exec /bin/mv "$@"
"""

# 只在注入中断时介入：写登记的备份（<候选目录>/previous/）时失败；其余情况执行真实 install
INSTALL_STUB = r"""#!/usr/bin/env bash
if [ "$DEPLOY_TEST_INTERRUPT" = backup-fail ] && [[ "${@: -1}" = */config/.pending/*/previous/* ]]; then exit 1; fi
exec /usr/bin/install "$@"
"""

BACKENDS = ("metaweave", "nl2sql")
# Doc 16：正式 env 是已生成的 raw 文件；受保护变量（控制令牌、数据源加密密钥）默认与候选一致
FORMAL_ENV = "DEPLOY_CONTROL_TOKEN=t0k\nSOURCE_CREDENTIAL_KEY=cr3d\nAPP_SETTING=old-value\n"
CANDIDATE_ENV = (
    "DEPLOY_CONTROL_TOKEN=t0k\nSOURCE_CREDENTIAL_KEY=cr3d\nAPP_SETTING=candidate-s3cret-value\n"
)

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
        self.log = tmp_path / "docker.log"
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        # Doc 16：正式 env（已生成的 raw 文件）与默认候选配置（属于 RID 这次发布）
        self.formal = {svc: self.root / "config" / f"{svc}.env" for svc in BACKENDS}
        for path in self.formal.values():
            self._private_env(path, FORMAL_ENV)
        self.pending = self.root / "config" / ".pending"
        for name, body in (
            ("docker", DOCKER_STUB),
            ("curl", CURL_STUB),
            ("sleep", SLEEP_STUB),
            ("python3", PYTHON_STUB),
            ("mv", MV_STUB),
            ("install", INSTALL_STUB),
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
        self.base["CANDIDATE_APP_CONFIG_DIR"] = str(self.app_config("R1"))
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

    def app_config(self, rid, text=CANDIDATE_ENV):
        """为一次发布准备候选配置目录（doc 16 §3.2）；已存在的文件不改写。"""
        target = self.pending / rid
        for svc in BACKENDS:
            path = target / f"{svc}.env"
            if not path.exists():
                self._private_env(path, text)
        return target

    @staticmethod
    def _private_env(path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        path.chmod(0o600)

    def envfiles(self) -> list[str]:
        """每次 Compose 调用及其使用的两个后端 env 文件；脚本未覆盖时记为 -。"""
        log = self.root / "envfiles.log"
        return log.read_text().splitlines() if log.exists() else []

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


# ---------------------------------------------------------------- doc 16：后端 env 随发布同步


def envfiles_of(line: str) -> tuple[str, str]:
    """ENVFILES 记录行 → (METAWEAVE_ENV_FILE, NL2SQL_ENV_FILE)；脚本未覆盖时为 -。"""
    mw, nl = line.split(" :: ", 1)[1].split(" ")
    return mw.partition("=")[2], nl.partition("=")[2]


def formal_binding(env) -> tuple[str, str]:
    """正式 env 的路径：部署根目录 config/ 里的两份文件（doc 16 §1）。doc 16 起部署脚本的
    Compose 调用、受保护变量比较、正式文件的更新与恢复共用这一组路径，不取调用方或 deploy.env 的取值。"""
    return str(env.formal["metaweave"]), str(env.formal["nl2sql"])


def compose_file_of(line: str) -> str:
    """ENVFILES 记录行里这次 Compose 调用使用的 Compose 文件（-f 的值）。"""
    return line.split(" :: ", 1)[0].split(" -f ", 1)[1].split(" ", 1)[0]


def candidate_binding(env, rid="R1") -> tuple[str, str]:
    return str(env.pending / rid / "metaweave.env"), str(env.pending / rid / "nl2sql.env")


def backend_ups(env) -> list[tuple[str, str]]:
    """按发生顺序列出每次后端 up 使用的 env 文件。"""
    return [
        envfiles_of(line)
        for line in env.envfiles()
        if "up -d --no-deps --force-recreate metaweave nl2sql" in line
    ]


def config_checks(env) -> list[tuple[str, str]]:
    """按发生顺序列出每次目标配置预检（容器内加载应用配置）使用的 env 文件。"""
    return [
        envfiles_of(line) for line in env.envfiles() if "load_base_db_agent_config" in line
    ]


def without_raw_env(text: str) -> str:
    old = (
        "      - path: ${NL2SQL_ENV_FILE:?deploy.env 需提供 NL2SQL_ENV_FILE}\n"
        "        format: raw\n"
    )
    assert old in text
    return text.replace(
        old, "      - ${NL2SQL_ENV_FILE:?deploy.env 需提供 NL2SQL_ENV_FILE}\n"
    )


def break_raw_env_format(env, rid) -> None:
    """把快照里的 env_file 改回普通写法，并同步清单哈希：快照本身仍然自洽。"""
    target = env.root / "releases" / rid
    compose = target / "compose.yaml"
    compose.write_text(
        without_raw_env(compose.read_text(encoding="utf-8")), encoding="utf-8"
    )
    manifest_path = target / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["config_hashes"]["compose.yaml"] = hashlib.sha256(
        compose.read_bytes()
    ).hexdigest()
    manifest_path.write_text(json.dumps(manifest))


def deploy_copy_without_raw(tmp_path) -> Path:
    """复制一套部署配置并把 nl2sql 的 env_file 改回普通写法，用作本次发布的候选 Compose。"""
    target = tmp_path / "deploy"
    for name in SNAPSHOT_FILES:
        (target / name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO_ROOT / "deploy" / name, target / name)
    compose = target / "compose.yaml"
    compose.write_text(
        without_raw_env(compose.read_text(encoding="utf-8")), encoding="utf-8"
    )
    return compose


def config_dir_names(env) -> list[str]:
    return sorted(path.name for path in (env.root / "config").iterdir())


def change_log(result) -> list[str]:
    """发布日志里的配置变更行（去掉 deploy.sh 的日志前缀），按输出顺序。"""
    return [
        match.group(1)
        for match in (
            re.search(r"^\[deploy\] \d\d:\d\d:\d\d 配置变更 (.+)$", line)
            for line in result.stdout.splitlines()
        )
        if match
    ]


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

    def test_release_reusing_the_current_release_id_is_a_config_error(self, tmp_path):
        """doc 16 §3.1：应用新配置必须用新的 release_id；同版本只读核验走 bootstrap/rollback。"""
        env = Env(tmp_path)
        self._ready(env)
        env.app_config("R0")
        # 候选配置和候选清单都为 R0 备齐：唯一拦住这次发布的只能是"同版本"这条规则
        manifest = tmp_path / "candidate-R0.json"
        manifest.write_text(json.dumps(env.manifest("R0")), encoding="utf-8")
        before = env.state()
        result = env.run(
            "release",
            "R0",
            extra={
                "CANDIDATE_MANIFEST": str(manifest),
                "CANDIDATE_APP_CONFIG_DIR": str(env.pending / "R0"),
            },
        )
        assert result.returncode != 0 and "R0" in result.stderr
        assert env.state() == before
        assert "up " not in env.calls() and "enter" not in env.calls()

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

    # release <current> 自 doc 16 起是配置错误（见 TestRelease），同版本核验只剩 bootstrap
    @pytest.mark.parametrize("command", ["bootstrap"])
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
    # release <current> 自 doc 16 起是配置错误，轮不到端口核验
    @pytest.mark.parametrize("command", ["bootstrap", "rollback"])
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


# ---------------------------------------------------------------- doc 16：候选配置的准入


class TestAppConfigCandidate:
    def _ready(self, env):
        env.set_state("ready", current="R0")

    def test_release_without_candidate_config_is_refused_before_any_change(
        self, tmp_path
    ):
        env = Env(tmp_path)
        self._ready(env)
        result = env.run("release", "R1", extra={"CANDIDATE_APP_CONFIG_DIR": ""})
        assert result.returncode != 0
        assert "CANDIDATE_APP_CONFIG_DIR" in result.stderr
        assert env.state()["phase"] == "ready" and env.state()["current"] == "R0"
        assert "enter" not in env.calls() and "up " not in env.calls()

    @pytest.mark.parametrize(
        "fault",
        ["other_release", "missing_dir", "missing_metaweave", "world_readable"],
    )
    def test_candidate_config_must_belong_to_this_release_and_stay_private(
        self, tmp_path, fault
    ):
        env = Env(tmp_path)
        self._ready(env)
        candidate = env.app_config("R1")
        extra = {}
        if fault == "other_release":
            extra["CANDIDATE_APP_CONFIG_DIR"] = str(env.app_config("R9"))
        elif fault == "missing_dir":
            shutil.rmtree(candidate)
        elif fault == "missing_metaweave":
            (candidate / "metaweave.env").unlink()
        else:
            (candidate / "nl2sql.env").chmod(0o644)
        before = env.state()
        result = env.run("release", "R1", extra=extra)
        assert result.returncode != 0 and "候选配置" in result.stderr
        assert env.state() == before
        assert "enter" not in env.calls() and "up " not in env.calls()
        assert all(
            path.read_text(encoding="utf-8") == FORMAL_ENV
            for path in env.formal.values()
        )

    @pytest.mark.parametrize(
        ("service", "key", "replacement"),
        [
            ("metaweave", "DEPLOY_CONTROL_TOKEN", "DEPLOY_CONTROL_TOKEN=other-t0k"),
            ("nl2sql", "DEPLOY_CONTROL_TOKEN", "DEPLOY_CONTROL_TOKEN=other-t0k"),
            ("metaweave", "SOURCE_CREDENTIAL_KEY", "SOURCE_CREDENTIAL_KEY=other-cr3d"),
            ("nl2sql", "SOURCE_CREDENTIAL_KEY", "SOURCE_CREDENTIAL_KEY=other-cr3d"),
            ("nl2sql", "SOURCE_CREDENTIAL_KEY", ""),  # 候选里没有这一项：无法确认
        ],
    )
    def test_control_token_and_credential_key_must_match_the_formal_config(
        self, tmp_path, service, key, replacement
    ):
        """排空旧容器和冒烟都靠正式令牌；密钥变了就解不开平台库里已登记的数据源凭据。"""
        env = Env(tmp_path)
        self._ready(env)
        line = f"{key}=" + ("t0k" if key == "DEPLOY_CONTROL_TOKEN" else "cr3d")
        (env.app_config("R1") / f"{service}.env").write_text(
            CANDIDATE_ENV.replace(line + "\n", replacement + "\n"), encoding="utf-8"
        )
        before = env.state()
        result = env.run("release", "R1")
        assert result.returncode != 0
        output = result.stdout + result.stderr
        assert f"{service} 的 {key}" in output
        for value in ("t0k", "cr3d", "candidate-s3cret-value"):
            assert value not in output
        assert env.state() == before
        assert "enter" not in env.calls() and "up " not in env.calls()

    @pytest.mark.parametrize(
        ("service", "key", "formal_value", "candidate_value", "accepted"),
        [
            ("metaweave", "DEPLOY_CONTROL_TOKEN", ' t0k # a=b "q" ', ' t0k # a=b "q" ', True),
            # 只差尾部空格：容器拿到的值不同，不能当作一致
            ("metaweave", "DEPLOY_CONTROL_TOKEN", ' t0k # a=b "q" ', ' t0k # a=b "q"', False),
            ("nl2sql", "SOURCE_CREDENTIAL_KEY", "cr3d # part=1", "cr3d # part=1", True),
            # 只差 # 之后的内容：按 dotenv 语义会被当成注释，raw 规则下是值的一部分
            ("nl2sql", "SOURCE_CREDENTIAL_KEY", "cr3d # part=1", "cr3d # other=1", False),
        ],
    )
    def test_protected_values_are_compared_with_the_raw_rule(
        self, tmp_path, service, key, formal_value, candidate_value, accepted
    ):
        env = Env(tmp_path)
        self._ready(env)
        old = f"{key}=" + ("t0k" if key == "DEPLOY_CONTROL_TOKEN" else "cr3d") + "\n"
        # 放行的情形要让整组配置自洽：两个服务用同一把数据源加密密钥，所以两边一起换
        targets = BACKENDS if accepted else (service,)
        for target in targets:
            env.formal[target].write_text(
                FORMAL_ENV.replace(old, f"{key}={formal_value}\n"), encoding="utf-8"
            )
            (env.app_config("R1") / f"{target}.env").write_text(
                CANDIDATE_ENV.replace(old, f"{key}={candidate_value}\n"), encoding="utf-8"
            )
        before = env.state()
        result = env.run("release", "R1")
        output = result.stdout + result.stderr
        for value in ("t0k", "cr3d"):
            assert value not in output
        if accepted:
            assert result.returncode == 0, result.stderr
            assert env.state()["current"] == "R1"
        else:
            assert result.returncode != 0
            assert f"{service} 的 {key}" in output
            assert env.state() == before
            assert "enter" not in env.calls() and "up " not in env.calls()

    def test_formal_value_that_cannot_be_confirmed_blocks_the_release(self, tmp_path):
        env = Env(tmp_path)
        self._ready(env)
        env.formal["nl2sql"].write_text(
            FORMAL_ENV.replace("SOURCE_CREDENTIAL_KEY=cr3d\n", ""), encoding="utf-8"
        )
        before = env.state()
        result = env.run("release", "R1")
        assert result.returncode != 0
        assert "nl2sql 的 SOURCE_CREDENTIAL_KEY" in result.stdout + result.stderr
        assert env.state() == before
        assert "enter" not in env.calls() and "up " not in env.calls()

    @pytest.mark.parametrize(
        ("metaweave_key", "nl2sql_key"),
        [
            ("k3y-metaweave", "k3y-other"),  # 两把都合法，但不是同一把
            ("k3y-metaweave", ""),  # NL2SQL 那份缺失
            ("", ""),  # 两份都缺失（覆盖模板里这一行留空就是这种情形）
        ],
    )
    def test_release_is_refused_unless_both_services_share_the_credential_key(
        self, tmp_path, metaweave_key, nl2sql_key
    ):
        """doc 16 §2、§4：两个服务用同一把数据源加密密钥，否则 NL2SQL 解不开 MetaWeave 登记的凭据。
        这里候选与各自的正式值都相同，逐服务比较发现不了，必须再比较两个服务之间是否一致。"""
        env = Env(tmp_path)
        self._ready(env)
        for service, key in (
            ("metaweave", metaweave_key),
            ("nl2sql", nl2sql_key),
        ):
            line = f"SOURCE_CREDENTIAL_KEY={key}\n"
            env.formal[service].write_text(
                FORMAL_ENV.replace("SOURCE_CREDENTIAL_KEY=cr3d\n", line), encoding="utf-8"
            )
            (env.app_config("R1") / f"{service}.env").write_text(
                CANDIDATE_ENV.replace("SOURCE_CREDENTIAL_KEY=cr3d\n", line),
                encoding="utf-8",
            )
        before = env.state()
        result = env.run("release", "R1")
        output = result.stdout + result.stderr
        assert result.returncode != 0
        assert "SOURCE_CREDENTIAL_KEY" in output and "k3y" not in output
        assert env.state() == before
        assert "enter" not in env.calls() and "up " not in env.calls()

    @pytest.mark.parametrize(
        ("metaweave_key", "nl2sql_key"),
        [("k3y-metaweave", "k3y-other"), ("k3y-metaweave", ""), ("", "")],
    )
    def test_bootstrap_is_refused_unless_both_formal_files_share_the_credential_key(
        self, tmp_path, metaweave_key, nl2sql_key
    ):
        """全新开发栈按 doc 16 §4.1 把候选文件装到正式路径后执行 bootstrap：
        两份正式 env 的密钥必须非空且一致，否则两个服务各自都能启动，之后却解不开对方登记的凭据。"""
        env = Env(tmp_path)
        for service, key in (
            ("metaweave", metaweave_key),
            ("nl2sql", nl2sql_key),
        ):
            env.formal[service].write_text(
                FORMAL_ENV.replace(
                    "SOURCE_CREDENTIAL_KEY=cr3d\n", f"SOURCE_CREDENTIAL_KEY={key}\n"
                ),
                encoding="utf-8",
            )
        result = env.run("bootstrap", "R1")
        output = result.stdout + result.stderr
        assert result.returncode != 0
        assert "SOURCE_CREDENTIAL_KEY" in output and "k3y" not in output
        assert env.state() == {}  # 写入 deploying 之前就停止
        assert "up " not in env.calls() and "init_platform" not in env.calls()

    def test_bootstrap_refuses_a_snapshot_that_reads_env_the_old_way(self, tmp_path):
        """bootstrap 登记的快照就是此后每次 release 的 current 基线（doc 16 §3.2）：
        它必须按 format: raw 读取后端 env，否则基线一旦登记，之后的 release 全部被拒绝。"""
        env = Env(tmp_path)
        compose = deploy_copy_without_raw(tmp_path)
        result = env.run("bootstrap", "R1", extra={"COMPOSE_FILE": str(compose)})
        output = result.stdout + result.stderr
        assert result.returncode != 0
        assert "format: raw" in output
        assert env.state() == {}  # 不登记基线
        assert "up " not in env.calls() and "init_platform" not in env.calls()

    def test_release_is_refused_while_the_running_snapshot_reads_env_the_old_way(
        self, tmp_path
    ):
        env = Env(tmp_path)
        self._ready(env)
        break_raw_env_format(env, "R0")
        before = env.state()
        result = env.run("release", "R1")
        assert result.returncode != 0
        assert "format: raw" in result.stdout + result.stderr
        assert env.state() == before
        assert "enter" not in env.calls() and "up " not in env.calls()

    def test_release_is_refused_when_the_candidate_compose_reads_env_the_old_way(
        self, tmp_path
    ):
        env = Env(tmp_path)
        self._ready(env)
        compose = deploy_copy_without_raw(tmp_path)
        before = env.state()
        result = env.run("release", "R1", extra={"COMPOSE_FILE": str(compose)})
        assert result.returncode != 0
        assert "format: raw" in result.stdout + result.stderr
        assert env.state() == before
        assert "enter" not in env.calls() and "up " not in env.calls()


# ---------------------------------------------------------------- doc 16：候选与正式 env 的切换


class TestAppConfigRelease:
    def test_candidate_env_is_used_for_preflight_and_recreation(self, tmp_path):
        env = Env(tmp_path)
        env.set_state("ready", current="R0")
        result = env.run("release", "R1")
        assert result.returncode == 0, result.stderr
        assert config_checks(env) == [candidate_binding(env)]
        assert backend_ups(env) == [candidate_binding(env)]

    def test_recovery_switches_back_to_the_formal_env(self, tmp_path):
        """验收失败：正式文件不能被改动，恢复原版本时也不再使用候选配置。"""
        env = Env(tmp_path)
        env.set_state("ready", current="R0")
        result = env.run("release", "R1", extra={"FAIL_SMOKE_MODE": "--mode public"})
        assert result.returncode != 0
        assert config_checks(env) == [candidate_binding(env), formal_binding(env)]
        assert backend_ups(env) == [candidate_binding(env), formal_binding(env)]
        state = env.state()
        assert state["current"] == "R0" and state["phase"] == "ready"
        assert "pending_config_release_id" not in state
        assert all(
            path.read_text(encoding="utf-8") == FORMAL_ENV
            for path in env.formal.values()
        )
        assert not (env.pending / "R1").exists()

    def test_drain_timeout_verifies_the_original_site_with_the_formal_env(
        self, tmp_path
    ):
        env = Env(tmp_path)
        env.set_state("ready", current="R0")
        result = env.run("release", "R1", extra={"DEPLOY_ACTIVE": "2"})
        assert result.returncode != 0 and env.state()["phase"] == "ready"
        assert config_checks(env) == [candidate_binding(env), formal_binding(env)]
        assert backend_ups(env) == []

    def test_old_containers_are_drained_with_their_own_snapshot_and_the_formal_env(
        self, tmp_path
    ):
        """doc 16 §3.2 第 3 步：排空旧容器使用正式配置。旧容器是用当前快照和正式 env 创建的；
        候选配置只用于目标配置预检、创建新容器和验收。"""
        env = Env(tmp_path)
        env.set_state("ready", current="R0")
        result = env.run("release", "R1")
        assert result.returncode == 0, result.stderr
        lines = env.envfiles()
        recreate = next(
            i
            for i, line in enumerate(lines)
            if "up -d --no-deps --force-recreate metaweave nl2sql" in line
        )
        enters = [(i, line) for i, line in enumerate(lines) if "_deploy/enter" in line]
        assert [line.split("exec -T ", 1)[1].split(" ", 1)[0] for _, line in enters] == list(
            BACKENDS
        )
        for index, line in enters:
            assert index < recreate  # 两次 enter 排空的是旧容器
            assert envfiles_of(line) == formal_binding(env)
            assert compose_file_of(line) == str(env.root / "releases" / "R0" / "compose.yaml")
        # 排空之后才切到候选快照和候选配置，重建容器
        assert compose_file_of(lines[recreate]) == str(
            env.root / "releases" / "R1" / "compose.yaml"
        )
        assert envfiles_of(lines[recreate]) == candidate_binding(env)


# ---------------------------------------------------------------- doc 16：正式配置的路径


class TestAppConfigPaths:
    """正式配置的读取、更新和恢复共用部署根目录 config/ 里的一组路径（doc 16 §1）。"""

    def _elsewhere(self, tmp_path):
        """调用方把两个 *_ENV_FILE 指到的另一组文件。"""
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        for service in BACKENDS:
            (elsewhere / f"{service}.env").write_text(
                "DEPLOY_CONTROL_TOKEN=elsewhere\n", encoding="utf-8"
            )
        return {
            "METAWEAVE_ENV_FILE": str(elsewhere / "metaweave.env"),
            "NL2SQL_ENV_FILE": str(elsewhere / "nl2sql.env"),
        }

    def test_release_updates_the_files_that_compose_reads(self, tmp_path):
        """调用方（或 deploy.env）把两个 *_ENV_FILE 指到别处时，不能出现
        「Compose 读一处、脚本更新另一处」：比对、更新、恢复和 Compose 都用部署根目录里的两份文件。"""
        env = Env(tmp_path)
        env.set_state("ready", current="R0")
        extra = self._elsewhere(tmp_path)
        result = env.run("release", "R1", extra=extra)
        assert result.returncode == 0, result.stderr
        assert {envfiles_of(line) for line in env.envfiles()} == {
            candidate_binding(env),
            formal_binding(env),
        }
        for service in BACKENDS:
            assert env.formal[service].read_text(encoding="utf-8") == CANDIDATE_ENV
            assert Path(extra[f"{service.upper()}_ENV_FILE"]).read_text(
                encoding="utf-8"
            ) == "DEPLOY_CONTROL_TOKEN=elsewhere\n"

    @pytest.mark.parametrize(("command", "rid"), [("bootstrap", "R1"), ("rollback", "R0")])
    def test_bootstrap_and_rollback_read_the_formal_files_from_the_deploy_root(
        self, tmp_path, command, rid
    ):
        env = Env(tmp_path)
        if command == "rollback":
            env.set_state("ready", current="R0")
        result = env.run(command, rid, extra=self._elsewhere(tmp_path))
        assert result.returncode == 0, result.stderr
        assert {envfiles_of(line) for line in env.envfiles()} == {formal_binding(env)}


# ---------------------------------------------------------------- doc 16：正式配置的更新与中断


class TestAppConfigPromotion:
    def _ready(self, env):
        env.set_state("ready", current="R0")

    def test_accepted_candidate_becomes_the_formal_config(self, tmp_path):
        env = Env(tmp_path)
        self._ready(env)
        result = env.run("release", "R1")
        assert result.returncode == 0, result.stderr
        for service in BACKENDS:
            formal = env.formal[service]
            assert formal.read_text(encoding="utf-8") == CANDIDATE_ENV
            assert stat.S_IMODE(formal.stat().st_mode) == 0o600
        assert not (env.pending / "R1").exists()
        state = env.state()
        assert (
            state["current"] == "R1"
            and state["previous"] == "R0"
            and state["phase"] == "ready"
        )
        assert "pending_config_release_id" not in state
        assert config_dir_names(env) == [".pending", "metaweave.env", "nl2sql.env"]

    def test_change_log_names_variables_but_never_values(self, tmp_path):
        env = Env(tmp_path)
        self._ready(env)
        formal = (
            "DEPLOY_CONTROL_TOKEN=t0k\nSOURCE_CREDENTIAL_KEY=cr3d\n"
            "APP_SETTING=old-s3cret-value\nREMOVED_VAR=removed-s3cret-value\n"
        )
        candidate = (
            "DEPLOY_CONTROL_TOKEN=t0k\nSOURCE_CREDENTIAL_KEY=cr3d\n"
            "APP_SETTING=new-s3cret-value\nADDED_VAR=added-s3cret-value\n"
        )
        for service in BACKENDS:
            env.formal[service].write_text(formal, encoding="utf-8")
            (env.app_config("R1") / f"{service}.env").write_text(
                candidate, encoding="utf-8"
            )
        result = env.run("release", "R1")
        assert result.returncode == 0, result.stderr
        assert change_log(result) == [
            f"{service}：新增 ADDED_VAR；修改 APP_SETTING；删除 REMOVED_VAR"
            for service in BACKENDS
        ]
        output = result.stdout + result.stderr
        for value in ("s3cret", "t0k", "cr3d"):
            assert value not in output

    def test_unchanged_config_is_reported_as_no_change(self, tmp_path):
        env = Env(tmp_path)
        self._ready(env)
        for service in BACKENDS:
            (env.app_config("R1") / f"{service}.env").write_text(
                FORMAL_ENV, encoding="utf-8"
            )
        result = env.run("release", "R1")
        assert result.returncode == 0, result.stderr
        assert change_log(result) == [f"{service}：无变化" for service in BACKENDS]

    def test_change_log_compares_raw_values(self, tmp_path):
        """raw 规则：第一个 = 之后直到行末都是值。只差尾部空格或 # 之后的内容也算修改；
        逐字相同的不报；正式文件里的注释行不是被删除的变量。"""
        env = Env(tmp_path)
        self._ready(env)
        formal = (
            'DEPLOY_CONTROL_TOKEN= t0k # a=b "q" \n'
            "SOURCE_CREDENTIAL_KEY= cr3d \n"
            'RAW_SAME=  same-s3cret # "q" a=b  \n'
            "TRAILING=  trail-s3cret  \n"
            "AFTER_HASH=x # first-s3cret\n"
            "#COMMENTED_OUT=comment-s3cret\n"
        )
        candidate = (
            'DEPLOY_CONTROL_TOKEN= t0k # a=b "q" \n'
            "SOURCE_CREDENTIAL_KEY= cr3d \n"
            'RAW_SAME=  same-s3cret # "q" a=b  \n'
            "TRAILING=  trail-s3cret \n"
            "AFTER_HASH=x # second-s3cret\n"
            'RAW_ADDED= "added-s3cret" # z=w \n'
        )
        for service in BACKENDS:
            env.formal[service].write_text(formal, encoding="utf-8")
            (env.app_config("R1") / f"{service}.env").write_text(candidate, encoding="utf-8")
        result = env.run("release", "R1")
        assert result.returncode == 0, result.stderr
        assert change_log(result) == [
            f"{service}：新增 RAW_ADDED；修改 AFTER_HASH、TRAILING" for service in BACKENDS
        ]
        assert "s3cret" not in result.stdout + result.stderr
        for service in BACKENDS:  # 正式文件与候选逐字相同：首尾空格、# 和引号都保留
            assert env.formal[service].read_text(encoding="utf-8") == candidate

    def test_dev_change_hidden_by_the_docker_override_is_reported_as_no_change(
        self, tmp_path
    ):
        """开发文件改了被 Docker 覆盖的变量：合并后的候选与正式配置相同，按无变化处理。
        本次候选和上一份正式配置都由真实的 prepare_app_env.py 生成。"""
        env = Env(tmp_path)
        self._ready(env)
        dev = tmp_path / "dev"

        def put(path, lines):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        def generate(release_id):
            subprocess.run(
                [
                    sys.executable, str(PREPARE_TOOL),
                    "--dev-root", str(dev), "--deploy-root", str(env.root),
                    "--release-id", release_id,
                ],
                check=True, capture_output=True, text=True, cwd="/",
            )

        shutil.rmtree(env.pending / "R1")  # 本次发布的候选配置改由同步程序生成
        for service in BACKENDS:
            put(dev / service / ".env", ["APP_SETTING=first", "DEV_ONLY=1"])
            put(
                env.root / "config" / "overrides" / f"{service}.env",
                ["APP_SETTING=pinned-by-override", "DEPLOY_CONTROL_TOKEN=t0k",
                 "SOURCE_CREDENTIAL_KEY=cr3d"],
            )
        generate("previous")
        for service in BACKENDS:  # 上一次发布生成的配置就是现在的正式文件
            shutil.copyfile(env.pending / "previous" / f"{service}.env", env.formal[service])
        shutil.rmtree(env.pending / "previous")
        for service in BACKENDS:  # 开发文件：只改了被 Docker 覆盖的那个变量
            put(dev / service / ".env", ["APP_SETTING=second", "DEV_ONLY=1"])
        generate("R1")

        result = env.run("release", "R1")
        assert result.returncode == 0, result.stderr
        assert change_log(result) == [f"{service}：无变化" for service in BACKENDS]
        for service in BACKENDS:
            assert env.formal[service].read_text(encoding="utf-8") == (
                "APP_SETTING=pinned-by-override\n"
                "DEPLOY_CONTROL_TOKEN=t0k\n"
                "DEV_ONLY=1\n"
                "SOURCE_CREDENTIAL_KEY=cr3d\n"
            )
            assert stat.S_IMODE(env.formal[service].stat().st_mode) == 0o600

    def test_failed_replacement_restores_the_formal_config_and_recovers_the_old_version(
        self, tmp_path
    ):
        """第二份正式文件替换失败属于可处理的错误：从登记的备份恢复两份正式文件，
        再走原版本自动恢复；验收通过后清除待恢复 ID 并清理备份。"""
        env = Env(tmp_path)
        self._ready(env)
        result = env.run("release", "R1", extra={"DEPLOY_TEST_INTERRUPT": "fail"})
        assert result.returncode != 0
        state = env.state()
        assert state["phase"] == "ready" and state["current"] == "R0"
        assert "pending_config_release_id" not in state
        assert all(
            path.read_text(encoding="utf-8") == FORMAL_ENV
            for path in env.formal.values()
        )
        assert backend_ups(env) == [candidate_binding(env), formal_binding(env)]
        assert config_dir_names(env) == [".pending", "metaweave.env", "nl2sql.env"]
        assert list((env.root / "config" / ".pending").iterdir()) == []

    @pytest.mark.parametrize("interrupt", ["backup-fail", "register-fail"])
    def test_failure_before_registration_replaces_nothing_and_recovers_the_old_version(
        self, tmp_path, interrupt
    ):
        """写登记的备份或登记待恢复 ID 时失败：两份正式文件还没被改动，不需要从备份恢复，
        直接走原版本自动恢复；写回 ready 并清理候选目录。"""
        env = Env(tmp_path)
        self._ready(env)
        result = env.run("release", "R1", extra={"DEPLOY_TEST_INTERRUPT": interrupt})
        assert result.returncode != 0
        state = env.state()
        assert state["phase"] == "ready" and state["current"] == "R0"
        assert "pending_config_release_id" not in state
        assert all(
            path.read_text(encoding="utf-8") == FORMAL_ENV for path in env.formal.values()
        )
        assert backend_ups(env) == [candidate_binding(env), formal_binding(env)]
        assert config_dir_names(env) == [".pending", "metaweave.env", "nl2sql.env"]
        assert list((env.root / "config" / ".pending").iterdir()) == []

    def test_failed_replacement_and_failed_restore_keep_the_backup_and_pending_id(
        self, tmp_path
    ):
        """自动恢复失败：收尾函数把 deploying 转为 recovery_required，
        并保留待恢复 ID 和登记的备份，等待人工恢复。"""
        env = Env(tmp_path)
        self._ready(env)
        result = env.run("release", "R1", extra={"DEPLOY_TEST_INTERRUPT": "fail-always"})
        assert result.returncode != 0
        state = env.state()
        assert state["phase"] == "recovery_required" and state["current"] == "R0"
        assert state["pending_config_release_id"] == "R1"
        backup = env.pending / "R1" / "previous"
        for service in BACKENDS:
            assert (backup / f"{service}.env").read_text(encoding="utf-8") == FORMAL_ENV
        assert (env.root / "control/maintenance").exists()
        # 自动恢复失败后，收尾函数重新关闭入口时也使用正式 env
        enters = [envfiles_of(line) for line in env.envfiles() if "_deploy/enter" in line]
        assert enters[-1] == formal_binding(env)

    def test_manual_rollback_after_a_failed_restore_recovers_config_and_version(
        self, tmp_path
    ):
        env = Env(tmp_path)
        self._ready(env)
        env.run("release", "R1", extra={"DEPLOY_TEST_INTERRUPT": "fail-always"})
        result = env.run("rollback", "R0")
        assert result.returncode == 0, result.stderr
        state = env.state()
        assert state["phase"] == "ready" and state["current"] == "R0"
        assert "pending_config_release_id" not in state
        assert all(
            path.read_text(encoding="utf-8") == FORMAL_ENV
            for path in env.formal.values()
        )
        assert backend_ups(env)[-1] == formal_binding(env)
        assert config_dir_names(env) == [".pending", "metaweave.env", "nl2sql.env"]
        assert not (env.pending / "R1").exists()  # 成功恢复后才清理备份

    def test_kill_between_replacements_leaves_the_site_for_manual_recovery(
        self, tmp_path
    ):
        """进程被强制终止：状态停在 deploying，待恢复 ID 已登记，两份正式文件一新一旧。"""
        env = Env(tmp_path)
        self._ready(env)
        result = env.run("release", "R1", extra={"DEPLOY_TEST_INTERRUPT": "KILL"})
        assert result.returncode == -9  # SIGKILL：收尾函数来不及运行
        state = env.state()
        assert state["phase"] == "deploying" and state["current"] == "R0"
        assert state["pending_config_release_id"] == "R1"
        assert env.formal["metaweave"].read_text(encoding="utf-8") == CANDIDATE_ENV
        assert env.formal["nl2sql"].read_text(encoding="utf-8") == FORMAL_ENV
        assert (env.pending / "R1" / "previous" / "metaweave.env").read_text(
            encoding="utf-8"
        ) == FORMAL_ENV

        result = env.run("rollback", "R0")
        assert result.returncode == 0, result.stderr
        state = env.state()
        assert state["phase"] == "ready" and state["current"] == "R0"
        assert "pending_config_release_id" not in state
        assert all(
            path.read_text(encoding="utf-8") == FORMAL_ENV
            for path in env.formal.values()
        )
        assert backend_ups(env) == [candidate_binding(env), formal_binding(env)]
        assert config_dir_names(env) == [".pending", "metaweave.env", "nl2sql.env"]
        assert not (env.pending / "R1").exists()


# ---------------------------------------------------------------- doc 16：回退与恢复


class TestAppConfigRollback:
    def _pending(self, env):
        """模拟发布中断后的现场：状态登记了待恢复 ID，备份完整。"""
        env.set_state("deploying", current="R0")
        env.app_config("R1")
        for service in BACKENDS:
            env._private_env(
                env.pending / "R1" / "previous" / f"{service}.env", FORMAL_ENV
            )
        path = env.root / "state" / "deployment.json"
        data = json.loads(path.read_text())
        data["pending_config_release_id"] = "R1"
        path.write_text(json.dumps(data), encoding="utf-8")

    @pytest.mark.parametrize(
        ("fault", "missing"),
        [
            ("missing_dir", None),
            ("missing_metaweave", "metaweave"),
            ("missing_nl2sql", "nl2sql"),  # 第一份已被改写后才发现第二份缺失
            ("unreadable_nl2sql", "nl2sql"),
        ],
    )
    def test_rollback_stops_when_the_registered_backup_is_incomplete(
        self, tmp_path, fault, missing
    ):
        """整组备份核对通过之前不改写任何正式文件：缺任何一份（或读不了）时两份都保持原样。"""
        env = Env(tmp_path)
        self._pending(env)
        backup = env.pending / "R1" / "previous"
        for service in BACKENDS:  # 正式文件与备份的内容不同，才能看出有没有被改写
            env.formal[service].write_text(CANDIDATE_ENV, encoding="utf-8")
        if fault == "missing_dir":
            shutil.rmtree(backup)
        elif fault.startswith("missing_"):
            (backup / f"{missing}.env").unlink()
        else:
            if os.geteuid() == 0:
                pytest.skip("root 无视文件权限")
            (backup / f"{missing}.env").chmod(0)
        before = env.state()
        result = env.run("rollback", "R0")
        assert result.returncode != 0
        assert "备份" in result.stdout + result.stderr
        assert env.state() == before
        assert all(
            path.read_text(encoding="utf-8") == CANDIDATE_ENV for path in env.formal.values()
        )
        assert "enter" not in env.calls() and "up " not in env.calls()

    def test_rollback_refuses_a_snapshot_that_reads_env_the_old_way(self, tmp_path):
        env = Env(tmp_path)
        env.set_state("deploying", current="R1", previous="R0")
        break_raw_env_format(env, "R0")
        before = env.state()
        result = env.run("rollback", "R0")
        assert result.returncode != 0
        assert "format: raw" in result.stdout + result.stderr
        assert env.state() == before
        assert "enter" not in env.calls() and "up " not in env.calls()

    def test_pending_id_and_backup_survive_a_failed_recovery(self, tmp_path):
        env = Env(tmp_path)
        self._pending(env)
        result = env.run("rollback", "R0", extra={"DEPLOY_UP_FAIL": "1"})
        assert result.returncode != 0
        state = env.state()
        assert state["phase"] == "recovery_required"
        assert state["pending_config_release_id"] == "R1"
        assert (env.pending / "R1" / "previous" / "metaweave.env").exists()
