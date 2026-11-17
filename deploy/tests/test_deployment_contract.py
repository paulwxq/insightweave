"""部署契约测试（doc 13 §6.1、§8、任务 2A 切片 5）。

PG 建库脚本：自包含（直连 psql）、幂等（存在保留/缺才建）、可执行、
兼容官方入口直接执行与 bootstrap 独立 Bash 重入两种上下文。
psql 用桩替身（PATH 注入）做行为断言，不连真实数据库。
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "deploy" / "postgres" / "01-create-databases.sh"

PSQL_STUB = r"""#!/bin/bash
# 桩 psql：记录调用；SELECT pg_database 按 state 文件应答；CREATE DATABASE 落 state
log="$PSQL_STATE_DIR/psql.log"
state="$PSQL_STATE_DIR/dbs"
cmd=""
user=""; db=""
prev=""
for a in "$@"; do
  case "$prev" in
    --username) user="$a" ;;
    --dbname) db="$a" ;;
  esac
  prev="$a"
  case "$a" in SELECT*|CREATE*) cmd="$a" ;; esac
done
echo "user=$user db=$db cmd=$cmd" >> "$log"
if [[ "$cmd" == SELECT* ]]; then
  name=$(echo "$cmd" | sed -E "s/.*datname = '([a-z0-9_]+)'.*/\1/")
  if grep -qx "$name" "$state" 2>/dev/null; then echo 1; else echo ""; fi
elif [[ "$cmd" == CREATE* ]]; then
  name=$(echo "$cmd" | sed -E 's/CREATE DATABASE "([a-z0-9_]+)".*/\1/')
  echo "$name" >> "$state"
fi
"""


def setup_stub(tmp_path: Path) -> tuple[Path, Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    psql = bin_dir / "psql"
    psql.write_text(PSQL_STUB, encoding="utf-8")
    psql.chmod(0o755)
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "dbs").write_text("", encoding="utf-8")
    return bin_dir, state_dir


def _run(
    bin_dir: Path, state_dir: Path, *, via_bash: bool, env_extra: dict | None = None
) -> subprocess.CompletedProcess:
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "PSQL_STATE_DIR": str(state_dir),
        "POSTGRES_USER": "deploy_test",
        "POSTGRES_PASSWORD": "unused-by-stub",
    }
    env.update(env_extra or {})
    cmd = ["bash", str(SCRIPT)] if via_bash else [str(SCRIPT)]
    return subprocess.run(
        cmd, env=env, capture_output=True, text=True, timeout=30, check=False
    )


def run_script(
    tmp_path: Path, *, via_bash: bool, env_extra: dict | None = None
) -> subprocess.CompletedProcess:
    bin_dir, state_dir = setup_stub(tmp_path)
    return _run(bin_dir, state_dir, via_bash=via_bash, env_extra=env_extra)


class TestPgCreateDatabases:
    def test_direct_execution_creates_both_when_absent(self, tmp_path):
        result = run_script(tmp_path, via_bash=False)
        assert result.returncode == 0, result.stdout + result.stderr
        dbs = (tmp_path / "state" / "dbs").read_text().split()
        assert dbs == ["metaweave", "lg_persist"]

    def test_bash_reentry_creates_both_when_absent(self, tmp_path):
        result = run_script(tmp_path, via_bash=True)
        assert result.returncode == 0, result.stdout + result.stderr
        dbs = (tmp_path / "state" / "dbs").read_text().split()
        assert dbs == ["metaweave", "lg_persist"]

    def test_rerun_keeps_existing_and_creates_nothing(self, tmp_path):
        bin_dir, state_dir = setup_stub(tmp_path)
        first = _run(bin_dir, state_dir, via_bash=False)
        assert first.returncode == 0
        (state_dir / "psql.log").write_text("", encoding="utf-8")
        second = _run(bin_dir, state_dir, via_bash=True)
        assert second.returncode == 0
        log = (tmp_path / "state" / "psql.log").read_text()
        assert "CREATE" not in log, "幂等重入不应再次建库"
        assert "已存在" in second.stdout

    def test_partial_state_creates_only_missing(self, tmp_path):
        bin_dir, state_dir = setup_stub(tmp_path)
        (state_dir / "dbs").write_text("metaweave\n", encoding="utf-8")
        result = _run(bin_dir, state_dir, via_bash=False)
        assert result.returncode == 0
        dbs = (state_dir / "dbs").read_text().split()
        assert dbs == ["metaweave", "lg_persist"]
        log = (state_dir / "psql.log").read_text()
        assert log.count("CREATE") == 1
        assert "lg_persist" in log

    def test_psql_invoked_with_admin_db_and_user(self, tmp_path):
        result = run_script(tmp_path, via_bash=False)
        assert result.returncode == 0
        log = (tmp_path / "state" / "psql.log").read_text()
        assert "user=deploy_test" in log
        assert "db=postgres" in log

    def test_missing_postgres_user_fails(self, tmp_path):
        result = run_script(tmp_path, via_bash=True, env_extra={"POSTGRES_USER": ""})
        assert result.returncode != 0
        assert "POSTGRES_USER" in result.stderr

    def test_invalid_db_name_rejected(self, tmp_path):
        result = run_script(
            tmp_path, via_bash=True, env_extra={"PLATFORM_DB_NAME": "Bad;Name"}
        )
        assert result.returncode != 0


class TestPgScriptStaticRules:
    def test_script_exists_and_is_executable(self):
        assert SCRIPT.exists(), SCRIPT
        mode = SCRIPT.stat().st_mode
        assert mode & stat.S_IXUSR, "官方入口对可执行 .sh 直接执行，必须置可执行位"

    def test_self_contained_rules(self):
        text = SCRIPT.read_text(encoding="utf-8")
        code_lines = [
            line
            for line in text.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        code = "\n".join(code_lines)
        assert "docker_process_sql" not in code
        assert "exec psql" not in code
        assert "psql " in code  # 直接调用 psql

    def test_bash_syntax_ok(self):
        result = subprocess.run(
            ["bash", "-n", str(SCRIPT)], capture_output=True, text=True, check=False
        )
        assert result.returncode == 0, result.stderr


class TestContractSpecDrift:
    """deploy/contract.json 与权威来源（静态 DDL / platform_pool 常量）不得漂移。"""

    def test_metaweave_schema_matches_static_ddl(self):
        import json

        from deploy.scripts.init_platform import build_metaweave_contract

        sql = (
            REPO_ROOT / "metaweave" / "db" / "rebuild_platform_schema.sql"
        ).read_text(encoding="utf-8")
        contract = json.loads(
            (REPO_ROOT / "deploy" / "contract.json").read_text(encoding="utf-8")
        )
        assert contract["metaweave_schema"] == build_metaweave_contract(sql)

    def test_nl2sql_tables_match_platform_pool(self):
        import json
        import sys

        sys.path.insert(0, str(REPO_ROOT / "nl2sql"))
        try:
            from src.modules.db_deepagent.persistence.platform_pool import (
                PLATFORM_TABLES,
                SNAPSHOT_TABLE,
            )
        finally:
            sys.path.remove(str(REPO_ROOT / "nl2sql"))
        contract = json.loads(
            (REPO_ROOT / "deploy" / "contract.json").read_text(encoding="utf-8")
        )
        assert contract["nl2sql_tables"] == list(PLATFORM_TABLES) + [SNAPSHOT_TABLE]

    def test_contract_declares_version_1(self):
        import json

        contract = json.loads(
            (REPO_ROOT / "deploy" / "contract.json").read_text(encoding="utf-8")
        )
        assert contract["deployment_contract_version"] == 1


class TestDeployRuntimeFiles:
    def test_required_runtime_files_exist(self):
        base = REPO_ROOT / "deploy"
        for rel in [
            "contract.json",
            "runtime/lifecycle.py",
            "scripts/init_platform.py",
            "scripts/smoke.py",
            "postgres/01-create-databases.sh",
        ]:
            assert (base / rel).exists(), rel

    def test_init_platform_script_entrypoint_direct_run(self):
        """直跑文件必须进入 main（回归：修复轮曾丢失 __main__ 调用块）。"""
        import subprocess
        import sys as _sys

        result = subprocess.run(
            [
                _sys.executable,
                str(REPO_ROOT / "deploy" / "scripts" / "init_platform.py"),
                "--help",
            ],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
            env={**os.environ, "PYTHONPATH": str(REPO_ROOT)},
        )
        assert result.returncode == 0, result.stderr
        assert "usage:" in result.stdout, "argparse 未接管，__main__ 调用块缺失"


class TestCiSh:
    """ci.sh 门禁分支（doc 13 §7.2）；完整构建流水由实机/后续验收。"""

    def test_bash_syntax_and_executable(self):
        import stat as _stat

        sh = REPO_ROOT / "deploy" / "scripts" / "ci.sh"
        r = subprocess.run(
            ["bash", "-n", str(sh)], capture_output=True, text=True, check=False
        )
        assert r.returncode == 0, r.stderr
        assert sh.stat().st_mode & _stat.S_IXUSR

    def test_dirty_tree_refused_without_skip(self, tmp_path):
        """自建脏仓库：与宿主树状态无关（CI 的 checkout 是干净的）。"""
        import os as _os
        import subprocess as _sp

        repo = tmp_path / "dirty-repo"
        (repo / "deploy" / "scripts").mkdir(parents=True)
        _sp.run(["git", "init", "-q", str(repo)], check=True)
        sh = repo / "deploy" / "scripts" / "ci.sh"
        sh.write_bytes(
            _sp.run(
                ["git", "-C", str(REPO_ROOT), "show", "HEAD:deploy/scripts/ci.sh"],
                check=True,
                stdout=_sp.PIPE,
            ).stdout
        )
        (repo / "untracked.txt").write_text("x", encoding="utf-8")
        env = {
            **_os.environ,
            "INSIGHTWEAVE_CI_PYTHON": "/usr/bin/python3",
            "REPO_ROOT": str(repo),
        }
        r = _sp.run(
            ["bash", str(sh), "probe-rid"],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert r.returncode != 0
        assert "干净" in r.stderr or "不干净" in r.stderr

    def test_missing_ci_python_refused(self):
        import os as _os
        import subprocess as _sp

        env = {**_os.environ}
        env.pop("INSIGHTWEAVE_CI_PYTHON", None)
        r = _sp.run(
            ["bash", str(REPO_ROOT / "deploy" / "scripts" / "ci.sh"), "probe-rid"],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
            cwd=str(REPO_ROOT),
        )
        assert r.returncode != 0
        assert "INSIGHTWEAVE_CI_PYTHON" in r.stderr

    def test_deploy_scripts_importable(self):
        import importlib

        for module in [
            "deploy.runtime.lifecycle",
            "deploy.scripts.init_platform",
            "deploy.scripts.smoke",
        ]:
            assert importlib.import_module(module) is not None


# ---------------------------------------------------------------- 任务 2B


def _compose() -> dict:
    return yaml.safe_load(
        (REPO_ROOT / "deploy" / "compose.yaml").read_text(encoding="utf-8")
    )


class TestComposeSevenServices:
    def test_exactly_seven_services(self):
        services = _compose()["services"]
        assert set(services) == {
            "frontend",
            "metaweave",
            "nl2sql",
            "postgres",
            "neo4j",
            "milvus",
            "attu",
        }

    def test_upstream_images_pinned_by_digest(self):
        services = _compose()["services"]
        # compose 引用 deploy.env 变量；digest pin 由 TestEnvTemplates 钉死
        for name, var in (
            ("postgres", "${POSTGRES_IMAGE}"),
            ("neo4j", "${NEO4J_IMAGE}"),
            ("milvus", "${MILVUS_IMAGE}"),
            ("attu", "${ATTU_IMAGE}"),
        ):
            assert services[name]["image"] == var, name
            assert services[name].get("platform") == "linux/amd64", name

    def test_apps_use_local_images_never_pull(self):
        services = _compose()["services"]
        for name in ("metaweave", "nl2sql", "frontend"):
            assert services[name].get("pull_policy") == "never", name
            assert "${" in services[name]["image"], f"{name} 镜像须经 deploy.env 插值"

    def test_exactly_five_loopback_port_mappings(self):
        expected = {
            "frontend": {"127.0.0.1:18080:80"},
            "attu": {"127.0.0.1:3000:3000"},
            "neo4j": {"127.0.0.1:7474:7474", "127.0.0.1:7687:7687"},
            "postgres": {"127.0.0.1:6432:5432"},
        }
        for name, svc in _compose()["services"].items():
            ports = {str(p) for p in svc.get("ports", [])}
            assert ports == expected.get(name, set()), f"{name}: {ports}"

    def test_all_services_clear_proxy_env(self):
        cleared = {
            "HTTP_PROXY": "",
            "HTTPS_PROXY": "",
            "ALL_PROXY": "",
            "http_proxy": "",
            "https_proxy": "",
            "all_proxy": "",
        }
        for name, svc in _compose()["services"].items():
            env = svc.get("environment") or {}
            assert isinstance(env, dict), f"{name} environment 应为映射"
            for key in cleared:
                assert env.get(key) == "", f"{name} 未清空 {key}"

    def test_milvus_embedded_config(self):
        svc = _compose()["services"]["milvus"]
        assert "seccomp:unconfined" in svc.get("security_opt", [])
        env = svc["environment"]
        assert env.get("ETCD_USE_EMBED") == "true"
        assert env.get("COMMON_STORAGETYPE") == "local"
        assert svc["command"] == ["milvus", "run", "standalone"]
        volumes = [str(v) for v in svc.get("volumes", [])]
        assert any(
            v.endswith("embedEtcd.yaml:/milvus/configs/embedEtcd.yaml:ro")
            for v in volumes
        )
        assert any(
            v.endswith("user.yaml:/milvus/configs/user.yaml:ro") for v in volumes
        )
        assert any(v.endswith(":/var/lib/milvus") for v in volumes)

    def test_postgres_init_script_mounted_readonly(self):
        svc = _compose()["services"]["postgres"]
        volumes = [str(v) for v in svc.get("volumes", [])]
        assert any("01-create-databases.sh" in v and v.endswith(":ro") for v in volumes)
        assert any(v.endswith(":/var/lib/postgresql") for v in volumes)
        health = svc["healthcheck"]["test"]
        assert any("pg_isready" in part for part in health)

    def test_backends_graceful_shutdown_and_gate_mount(self):
        services = _compose()["services"]
        for name, health_url in (
            ("metaweave", "http://127.0.0.1:8001/openapi.json"),
            ("nl2sql", "http://127.0.0.1:8008/api/v2/health"),
        ):
            svc = services[name]
            assert svc.get("stop_grace_period") == "150s", name
            cmd = (
                " ".join(svc["command"])
                if isinstance(svc["command"], list)
                else svc["command"]
            )
            assert "timeout-graceful-shutdown" in cmd and "120" in cmd, name
            assert any("1200" not in cmd for _ in [0])
            health = " ".join(svc["healthcheck"]["test"])
            assert health_url in health, f"{name} healthcheck 应探 {health_url}"
            volumes = [str(v) for v in svc.get("volumes", [])]
            assert any(
                v.endswith(":/run/insightweave-deploy:ro") for v in volumes
            ), name
            assert (
                svc.get("depends_on", {}).get("postgres", {}).get("condition")
                == "service_healthy"
            )

    def test_restart_and_log_rotation_everywhere(self):
        for name, svc in _compose()["services"].items():
            assert svc.get("restart") == "unless-stopped", name
            logging = svc.get("logging", {})
            assert logging.get("driver") == "json-file", name
            assert logging.get("options", {}).get("max-size") == "10m"
            assert logging.get("options", {}).get("max-file") == "3"

    def test_stable_named_volumes(self):
        volumes = set(_compose().get("volumes", {}))
        expected = {
            "pg-data",
            "neo4j-data",
            "neo4j-logs",
            "neo4j-plugins",
            "milvus-data",
            "metaweave-output",
            "metaweave-logs",
            "nl2sql-workspace",
            "nl2sql-logs",
        }
        assert expected <= volumes, sorted(expected - volumes)


class TestNginxConf:
    def test_routes_and_guards(self):
        conf = (REPO_ROOT / "deploy" / "nginx" / "default.conf").read_text(
            encoding="utf-8"
        )
        assert "proxy_pass http://nl2sql:8008" in conf
        assert "proxy_pass http://metaweave:8001" in conf
        # /api/v2/_deploy/ 与 /api/_deploy/ 先于通用代理被拒绝
        assert re.search(r"location\s+/api/v2/_deploy/", conf)
        assert re.search(r"location\s+/api/_deploy/", conf)
        assert "403" in conf
        assert "proxy_read_timeout 600s" in conf
        assert "proxy_set_header Host $http_host" in conf
        assert "try_files $uri $uri/ /index.html" in conf


class TestDockerfiles:
    BACKEND_DIGEST = "docker.io/library/python:3.12.16-slim-bookworm@sha256:935c3c5b12357a7e10caa9c1e6ca5692344d97cf229832ef584be2cec9fc03df"
    NODE_DIGEST = "docker.io/library/node:22-bookworm-slim@sha256:e9f565d32859f0cbfb52125eafe8be39390f24dac302dc3cdf566e8ba3ab21ee"
    NGINX_DIGEST = "docker.io/library/nginx:1.30.6-trixie@sha256:42c68eab6f7e22611786a9fe1d1c019e275c50185bd54b8f75dc2b9184322703"

    def test_backend_dockerfiles(self):
        for name in ("metaweave", "nl2sql"):
            text = (REPO_ROOT / name / "Dockerfile").read_text(encoding="utf-8")
            assert f"FROM {self.BACKEND_DIGEST}" in text, name
            for pkg in ("ca-certificates", "curl", "tzdata", "libstdc++6", "libgcc-s1"):
                assert pkg in text, f"{name} 缺系统包 {pkg}"
            assert "uv==0.11.2" in text, name
            for rel in (
                "deploy/runtime/",
                "deploy/contract.json",
                "deploy/scripts/init_platform.py",
                "deploy/scripts/smoke.py",
            ):
                assert f"COPY {rel}" in text or re.search(
                    rf"COPY.*{re.escape(rel)}", text
                ), f"{name} 未复制 {rel}"
            assert "uv sync --frozen --no-dev" in text, name
            assert "PYTHONPATH" in text and "/app" in text, name
            assert '"--timeout-graceful-shutdown"' in text and '"120"' in text, name
            # 不烘焙代理
            assert "http_proxy" not in text.lower()

    def test_metaweave_platform_extra(self):
        text = (REPO_ROOT / "metaweave" / "Dockerfile").read_text(encoding="utf-8")
        assert "--extra platform" in text

    def test_frontend_dockerfile(self):
        text = (REPO_ROOT / "frontend" / "Dockerfile").read_text(encoding="utf-8")
        assert f"FROM {self.NODE_DIGEST}" in text
        assert f"FROM {self.NGINX_DIGEST}" in text
        assert "npm ci --include=dev" in text
        assert "VITE_API_BASE_URL=http://localhost:18080" in text
        assert "VITE_NL2SQL_API_BASE_URL=http://localhost:18080" in text
        assert "deploy/nginx/default.conf" in text
        assert "--from=build" in text


class TestDockerignore:
    def test_excludes_secrets_and_host_artifacts(self):
        text = (REPO_ROOT / ".dockerignore").read_text(encoding="utf-8")
        for pattern in (
            ".git",
            ".env",
            ".venv",
            "node_modules",
            "logs",
            "output",
            "dist",
        ):
            assert pattern in text, f"缺少排除 {pattern}"
        for needed in (
            "deploy",
            "deploy/scripts",
            "metaweave/db",
            "uv.lock",
            "package-lock.json",
            ".env.example",
        ):
            assert not re.search(
                rf"^{re.escape(needed)}/?$", text, re.MULTILINE
            ), f"误排除 {needed} 整目录"


class TestEnvTemplates:
    def test_deploy_env_declares_pins_and_paths(self):
        text = (REPO_ROOT / "deploy" / "env" / "deploy.env.example").read_text(
            encoding="utf-8"
        )
        for key in (
            "POSTGRES_IMAGE",
            "NEO4J_IMAGE",
            "MILVUS_IMAGE",
            "ATTU_IMAGE",
            "METAWEAVE_IMAGE",
            "NL2SQL_IMAGE",
            "FRONTEND_IMAGE",
            "METAWEAVE_ENV_FILE",
            "NL2SQL_ENV_FILE",
            "DEPLOY_CONTROL_DIR",
        ):
            assert key in text, key
        for digest in ("1090bc3a", "a7500373", "7208f04c", "709bb38b"):
            assert digest in text, digest
        # 代理与 CI 解释器只声明变量名，不写机器实值
        for key in (
            "INSIGHTWEAVE_WSL_PROXY_URL",
            "INSIGHTWEAVE_BUILD_PROXY_URL",
            "INSIGHTWEAVE_CI_PYTHON",
        ):
            assert key in text, key

    def test_backend_env_templates(self):
        for name, cors_key in (
            ("metaweave", "PLATFORM_CORS_ORIGINS"),
            ("nl2sql", "NL2SQL_CORS_ORIGINS"),
        ):
            text = (REPO_ROOT / "deploy" / "env" / f"{name}.env.example").read_text(
                encoding="utf-8"
            )
            assert (
                f"{cors_key}=http://localhost:18080,http://127.0.0.1:18080" in text
            ), name
            assert "SOURCE_CREDENTIAL_KEY=" in text, name
            assert "DEPLOY_CONTROL_ENABLED=1" in text, name
            assert "DEPLOY_CONTROL_TOKEN=" in text, name
            assert "DASHSCOPE_API_KEY=" in text, name
            # 不配置运行期代理
            assert not re.search(r"^https?_PROXY=\S+", text, re.MULTILINE), name
        nl = (REPO_ROOT / "deploy" / "env" / "nl2sql.env.example").read_text(
            encoding="utf-8"
        )
        assert "NL2SQL_ALLOWED_HOSTS=localhost,127.0.0.1" in nl
