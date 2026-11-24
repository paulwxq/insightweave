"""本机发布清单与快照；仅使用标准库，不读取或保存应用凭据。"""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import platform
import re
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

APPS = ("metaweave", "nl2sql", "frontend")
UPSTREAMS = ("POSTGRES_IMAGE", "NEO4J_IMAGE", "MILVUS_IMAGE", "ATTU_IMAGE")
UPSTREAM_SERVICES = dict(zip(("postgres", "neo4j", "milvus", "attu"), UPSTREAMS))
CONFIG_FILES = (
    "compose.yaml",
    "contract.json",
    "postgres/01-create-databases.sh",
    "milvus/embedEtcd.yaml",
    "milvus/user.yaml",
)
# 首版固定宿主映射；与 compose.yaml 的五项回环映射共同由契约测试约束。
HOST_PORTS = (
    ("frontend", 18080, "80/tcp"),
    ("attu", 3000, "3000/tcp"),
    ("neo4j", 7474, "7474/tcp"),
    ("neo4j", 7687, "7687/tcp"),
    ("postgres", 6432, "5432/tcp"),
)
_CONTROL_CONFIG_CHECK = (
    "import os; from deploy.runtime.lifecycle import load_contract_version; "
    "assert os.environ.get('DEPLOY_CONTROL_ENABLED') == '1'; "
    "assert os.environ.get('DEPLOY_CONTROL_TOKEN'); assert load_contract_version() == 1; "
)
TARGET_CONFIG_CHECKS = {
    "metaweave": _CONTROL_CONFIG_CHECK
    + (
        "from backend.persistence.platform_db import load_platform_settings_from_env; "
        "from backend.api.cors import parse_cors_origins; "
        "load_platform_settings_from_env(); parse_cors_origins(os.getenv('PLATFORM_CORS_ORIGINS'))"
    ),
    "nl2sql": _CONTROL_CONFIG_CHECK
    + (
        "from src.modules.db_deepagent.config import load_base_db_agent_config; "
        "load_base_db_agent_config()"
    ),
}


def docker(*args: str) -> str:
    command = shlex.split(os.environ.get("DOCKER_BIN", "docker --context default"))
    try:
        return subprocess.check_output(
            command + list(args), text=True, stderr=subprocess.PIPE, timeout=30
        ).strip()
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError(f"Docker 身份核验失败（{args[0]}）") from exc


def windows_listeners(ports: list[int]) -> set[int]:
    """WSL 回环不一定能看到 Windows 监听；只读查询宿主 TCP，不查询进程或改配置。"""
    if not ports or "microsoft" not in platform.release().lower():
        return set()
    executable = shutil.which("powershell.exe")
    if not executable:
        fallback = Path("/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe")
        if not fallback.is_file():
            raise ValueError(
                "Windows TCP 监听查询不可用：需要 WSL 的 PowerShell 互操作"
            )
        executable = str(fallback)
    wanted = ",".join(str(port) for port in ports)
    script = (
        '[Console]::OutputEncoding=[Text.UTF8Encoding]::new();$ErrorActionPreference="Stop";'
        f"$ports=@({wanted});"
        "$found=@(Get-NetTCPConnection -State Listen -ErrorAction Stop | "
        'Where-Object {$_.LocalPort -in $ports -and $_.LocalAddress -in @("127.0.0.1","0.0.0.0","::")} | '
        "Select-Object -ExpandProperty LocalPort -Unique);"
        "ConvertTo-Json -InputObject $found -Compress"
    )
    try:
        result = subprocess.run(
            [
                executable,
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                script,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            check=False,
        )
        if result.returncode:
            raise ValueError("Windows TCP 监听查询失败")
        found = json.loads(result.stdout.lstrip("\ufeff").strip())
        if not isinstance(found, list) or any(
            type(p) is not int or p not in ports for p in found
        ):
            raise ValueError("Windows TCP 监听查询响应无效")
        return set(found)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
        raise ValueError("Windows TCP 监听查询失败或超时") from exc


def check_ports(project: str) -> None:
    """只放行本项目正确服务的实际映射；未知监听/读取失败均阻止变更。"""
    ids = docker("ps", "--quiet").split()
    records = []
    if ids:
        fields = (
            '{"running":{{json .State.Running}},'
            '"project":{{json (index .Config.Labels "com.docker.compose.project")}},'
            '"service":{{json (index .Config.Labels "com.docker.compose.service")}},'
            '"ports":{{json .NetworkSettings.Ports}}}'
        )
        lines = docker("inspect", "--format", fields, *ids).splitlines()
        if len(lines) != len(ids):
            raise ValueError("Docker 端口归属读取不完整")
        records = [json.loads(line) for line in lines]
    wanted = {port: (service, internal) for service, port, internal in HOST_PORTS}
    owned = set()
    for record in records:
        if not isinstance(record, dict) or type(record.get("running")) is not bool:
            raise ValueError("Docker 容器运行状态无效")
        if not record["running"]:
            continue
        published = record.get("ports")
        if published is None:
            continue
        if not isinstance(published, dict):
            raise TypeError("Docker 发布端口响应无效")
        for internal, bindings in published.items():
            if not internal.endswith("/tcp") or bindings is None:
                continue
            if not isinstance(bindings, list):
                raise TypeError("Docker 端口绑定响应无效")
            for binding in bindings:
                if not isinstance(binding, dict):
                    raise TypeError("Docker 端口绑定响应无效")
                port = int(binding["HostPort"])
                address = binding["HostIp"]
                if port not in wanted or address not in (
                    "127.0.0.1",
                    "0.0.0.0",
                    "",
                    "::",
                ):
                    continue
                service, expected = wanted[port]
                if (
                    record.get("project") != project
                    or record.get("service") != service
                    or internal != expected
                    or address != "127.0.0.1"
                ):
                    raise ValueError(
                        f"宿主端口 127.0.0.1:{port} 不属于本项目 {service} 的预期映射；请释放端口后重试"
                    )
                owned.add(port)
    unowned = [port for port in wanted if port not in owned]
    occupied_windows = windows_listeners(unowned)
    for port in unowned:
        if port in occupied_windows:
            raise ValueError(f"Windows 宿主端口 127.0.0.1:{port} 已被其他程序占用")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                pass
        except OSError as exc:
            if exc.errno == errno.ECONNREFUSED:
                continue
            raise ValueError(f"无法确认宿主端口 127.0.0.1:{port} 的 TCP 状态") from exc
        raise ValueError(f"宿主端口 127.0.0.1:{port} 已被其他程序占用")


def read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"清单/快照缺失或无效：{path}；旧快照须人工核验补齐") from exc
    if not isinstance(data, dict):
        raise TypeError(f"清单必须是 JSON 对象：{path}")
    return data


def contract_version_supported(version: object) -> bool:
    """契约版本只接受整数 1：bool 是 int 的子类，1.0 与 1 相等，都不能放行。"""
    return type(version) is int and version == 1


def validate_candidate(
    data: dict, release_id: str, source_sha: str | None = None
) -> None:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", release_id):
        raise ValueError("非法 release_id")
    if data.get("release_id") != release_id:
        raise ValueError("候选 release_id 不匹配")
    if not re.fullmatch(r"[0-9a-f]{40}", str(data.get("source_sha", ""))):
        raise ValueError("缺少完整源码 SHA")
    if source_sha is not None and data["source_sha"] != source_sha:
        raise ValueError("候选 source_sha 与本次 checkout 不一致")
    if not contract_version_supported(data.get("deployment_contract_version")):
        raise ValueError("不支持的 deployment_contract_version")
    apps = data.get("applications")
    if not isinstance(apps, dict) or set(apps) != set(APPS):
        raise ValueError("清单须包含三个应用")
    for app in APPS:
        entry = apps[app]
        if (
            not isinstance(entry, dict)
            or entry.get("image") != f"insightweave/{app}:{release_id}"
        ):
            raise ValueError(f"应用镜像引用不匹配：{app}")
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", str(entry.get("image_id", ""))):
            raise ValueError(f"应用缺少实际 image ID：{app}")


def verify_local_images(data: dict) -> None:
    for app, entry in data["applications"].items():
        if (
            docker("image", "inspect", "--format", "{{.Id}}", entry["image"])
            != entry["image_id"]
        ):
            raise ValueError(f"本地镜像 ID 不匹配：{app}")


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".manifest-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def upstream_images(env_file: Path) -> dict[str, str]:
    values = {}
    for line in env_file.read_text(encoding="utf-8").splitlines():
        key, separator, value = line.partition("=")
        if separator and key in UPSTREAMS:
            values[key] = value.strip().strip("\"'")
    for key in UPSTREAMS:
        if key in os.environ:
            values[key] = os.environ[key]
        if not values.get(key):
            raise ValueError(f"缺少上游镜像引用：{key}")
    return values


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_snapshot(path: Path, env_file: Path) -> dict:
    data = read_json(path / "manifest.json")
    validate_candidate(data, path.name)
    hashes = data.get("config_hashes")
    if not isinstance(hashes, dict) or set(hashes) != set(CONFIG_FILES):
        raise ValueError("快照相对配置文件不完整")
    for name, expected in hashes.items():
        if file_hash(path / name) != expected:
            raise ValueError(f"快照配置哈希不匹配：{name}")
    if not contract_version_supported(
        read_json(path / "contract.json").get("deployment_contract_version")
    ):
        raise ValueError("快照契约不支持")
    current = upstream_images(env_file)
    for key in UPSTREAMS:
        if data.get("upstream_images", {}).get(key) != current[key]:
            raise ValueError(f"上游镜像引用不一致：{key}")
    verify_local_images(data)
    return data


def create_snapshot(args: argparse.Namespace) -> None:
    data = read_json(args.candidate)
    validate_candidate(data, args.release_id, args.source_sha)
    verify_local_images(data)
    compose_file = args.compose_file.resolve()
    source = compose_file.parent
    if not contract_version_supported(
        read_json(source / "contract.json").get("deployment_contract_version")
    ):
        raise ValueError("当前配置契约不支持")
    sources = {
        name: compose_file if name == "compose.yaml" else source / name
        for name in CONFIG_FILES
    }
    data["config_hashes"] = {name: file_hash(path) for name, path in sources.items()}
    data["upstream_images"] = upstream_images(args.env_file)
    destination = args.releases_dir / args.release_id
    if destination.exists():
        if load_snapshot(destination, args.env_file) != data:
            raise ValueError("已有快照与本次输入不一致，拒绝覆盖")
        return
    args.releases_dir.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".snapshot-", dir=args.releases_dir))
    try:
        for name, path in sources.items():
            target = staging / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
            if file_hash(target) != data["config_hashes"][name]:
                raise ValueError(f"准备快照时源配置变化：{name}")
        write_json(staging / "manifest.json", data)
        os.rename(staging, destination)
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def running_container(compose: list[str], service: str) -> str:
    """不存在/已退出返回空；Docker 读取失败或状态未知明确报错。"""
    cid = docker(*compose, "ps", "--all", "-q", service)
    if not cid:
        return ""
    running = docker("inspect", "--format", "{{.State.Running}}", cid)
    if running not in ("true", "false"):
        raise ValueError(f"容器运行状态未知：{service}")
    return cid if running == "true" else ""


def run_check_container(
    compose: list[str],
    project: str,
    service: str,
    arguments: list[str],
    *,
    kind: str,
    timeout: float,
) -> subprocess.CompletedProcess:
    """短时纯检查容器共用限时/清理边界，不执行 API 启动或初始化。"""
    probe = f"{project}-{kind}-check-{uuid.uuid4().hex[:12]}"
    command = shlex.split(os.environ.get("DOCKER_BIN", "docker --context default"))
    try:
        result = subprocess.run(
            command
            + compose
            + [
                "run",
                "--rm",
                "--no-deps",
                "--pull",
                "never",
                "-T",
                "--name",
                probe,
                "--entrypoint",
                "python",
                service,
            ]
            + arguments,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return result
    except subprocess.TimeoutExpired as exc:
        raise ValueError(f"部署预检超时（{timeout}s）：{probe}") from exc
    finally:
        # --rm 负责正常退出；超时终止 Docker 客户端后仍须清理已创建的检查容器。
        try:
            subprocess.run(
                command + ["rm", "-f", probe],
                capture_output=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            print(f"检查容器清理受阻，请核对：{probe}", file=sys.stderr)


def run_infra_check(compose: list[str], project: str, *, timeout: float = 60) -> None:
    result = run_check_container(
        compose,
        project,
        "metaweave",
        ["/app/deploy/scripts/smoke.py", "--scope", "infra"],
        kind="infra",
        timeout=timeout,
    )
    if result.returncode:
        # SDK 异常可能含连接信息，只报告失败组件，不回显原始异常/凭据。
        failed = [
            name
            for name in ("postgres", "neo4j", "milvus")
            if any(line.startswith(name + ":") for line in result.stderr.splitlines())
        ]
        raise ValueError("基础设施预检失败：" + (", ".join(failed) or "检查器执行异常"))


def parse_control_state(text: str, *, maintaining: bool | None = None) -> dict:
    try:
        state = json.loads(text)
    except ValueError as exc:
        raise ValueError("控制状态不是合法 JSON") from exc
    if (
        not isinstance(state, dict)
        or type(state.get("deployment_contract_version")) is not int
        or state["deployment_contract_version"] != 1
        or type(state.get("maintaining")) is not bool
        or type(state.get("active")) is not int
        or state["active"] < 0
    ):
        raise ValueError("控制状态/契约不支持")
    if maintaining is not None and state["maintaining"] is not maintaining:
        raise ValueError("后端维护状态与当前阶段不符")
    return state


def preflight(
    data: dict,
    compose: list[str],
    project: str,
    allow_missing: bool,
    require_open: bool = False,
) -> None:
    for service, key in UPSTREAM_SERVICES.items():
        cid = running_container(compose, service)
        if not cid or docker("inspect", "--format", "{{.Image}}", cid) != docker(
            "image", "inspect", "--format", "{{.Id}}", data["upstream_images"][key]
        ):
            raise ValueError(f"上游容器未运行或镜像 ID 不匹配：{service}")
    for service, port, prefix in (
        ("metaweave", 8001, "/api/_deploy"),
        ("nl2sql", 8008, "/api/v2/_deploy"),
    ):
        if not running_container(compose, service):
            if allow_missing:
                continue
            raise ValueError(f"当前后端未运行：{service}；请使用恢复入口")
        text = docker(
            *compose,
            "exec",
            "-T",
            service,
            "sh",
            "-eu",
            "-c",
            ': "${DEPLOY_CONTROL_TOKEN:?缺少控制令牌}"; '
            'printf "X-InsightWeave-Deploy-Token: %s\\n" "$DEPLOY_CONTROL_TOKEN" | '
            'curl --noproxy "*" --fail --silent --show-error --connect-timeout 3 '
            f"--max-time 10 --header @- http://127.0.0.1:{port}{prefix}/state",
        )
        try:
            state = parse_control_state(text)
        except ValueError as exc:
            raise ValueError(f"{service}：{exc}") from exc
        if require_open and state["maintaining"]:
            raise ValueError(f"后端仍处于维护：{service}")
    for service, code in TARGET_CONFIG_CHECKS.items():
        result = run_check_container(
            compose,
            project,
            service,
            ["-c", code],
            kind=f"config-{service}",
            timeout=30,
        )
        if result.returncode:
            raise ValueError(f"目标配置预检失败：{service}")
    run_infra_check(compose, project)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("active")
    ports = sub.add_parser("ports")
    ports.add_argument("--project", required=True)
    candidate = sub.add_parser("candidate")
    candidate.add_argument("--release-id", required=True)
    candidate.add_argument("--source-sha", required=True)
    candidate.add_argument("--contract", type=Path, required=True)
    candidate.add_argument("--output", type=Path, required=True)
    snapshot = sub.add_parser("snapshot")
    snapshot.add_argument("--release-id", required=True)
    snapshot.add_argument("--source-sha", required=True)
    snapshot.add_argument("--candidate", type=Path, required=True)
    snapshot.add_argument("--compose-file", type=Path, required=True)
    snapshot.add_argument("--releases-dir", type=Path, required=True)
    snapshot.add_argument("--env-file", type=Path, required=True)
    for command in ("load", "verify-running", "preflight"):
        action = sub.add_parser(command)
        action.add_argument("--snapshot", type=Path, required=True)
        action.add_argument("--env-file", type=Path, required=True)
        if command != "load":
            action.add_argument("--project", required=True)
        if command == "preflight":
            action.add_argument("--allow-missing-backends", action="store_true")
            action.add_argument("--require-open-backends", action="store_true")
    args = parser.parse_args()
    try:
        if args.command == "active":
            print(parse_control_state(sys.stdin.read(), maintaining=True)["active"])
        elif args.command == "ports":
            check_ports(args.project)
        elif args.command == "candidate":
            data = {
                "release_id": args.release_id,
                "source_sha": args.source_sha,
                "deployment_contract_version": read_json(args.contract)[
                    "deployment_contract_version"
                ],
                "applications": {
                    app: {
                        "image": f"insightweave/{app}:{args.release_id}",
                        "image_id": docker(
                            "image",
                            "inspect",
                            "--format",
                            "{{.Id}}",
                            f"insightweave/{app}:{args.release_id}",
                        ),
                    }
                    for app in APPS
                },
            }
            validate_candidate(data, args.release_id)
            write_json(args.output, data)
        elif args.command == "snapshot":
            create_snapshot(args)
        else:
            path = args.snapshot.resolve()
            data = load_snapshot(path, args.env_file)
            if args.command == "load":
                for app in APPS:
                    print(data["applications"][app]["image"])
                print(path / "compose.yaml")
            elif args.command == "preflight":
                # 只影响本辅助进程；一次性检查使用目标镜像和当前 env，不用旧标签。
                for app in APPS:
                    os.environ[app.upper() + "_IMAGE"] = data["applications"][app][
                        "image"
                    ]
                compose = [
                    "compose",
                    "--project-directory",
                    str(path),
                    "-f",
                    str(path / "compose.yaml"),
                    "--env-file",
                    str(args.env_file),
                    "--project-name",
                    args.project,
                ]
                preflight(
                    data,
                    compose,
                    args.project,
                    args.allow_missing_backends,
                    args.require_open_backends,
                )
            else:
                for app in APPS:
                    cid = docker(
                        "compose",
                        "--project-directory",
                        str(path),
                        "-f",
                        str(path / "compose.yaml"),
                        "--env-file",
                        str(args.env_file),
                        "--project-name",
                        args.project,
                        "ps",
                        "-q",
                        app,
                    )
                    if (
                        not cid
                        or docker("inspect", "--format", "{{.Image}}", cid)
                        != data["applications"][app]["image_id"]
                    ):
                        raise ValueError(f"运行容器 image ID 不匹配：{app}")
    except (ValueError, OSError, KeyError, TypeError) as exc:
        label = "宿主端口预检失败" if args.command == "ports" else "发布快照核验失败"
        print(f"{label}：{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
