"""版本清单/快照：真实文件与 CLI，只有 Docker 边界使用受控替身。"""

import errno
import hashlib
import importlib.util
import json
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/release_manifest.py"
SHA = "a" * 40
IDS = {
    "metaweave": "sha256:" + "1" * 64,
    "nl2sql": "sha256:" + "2" * 64,
    "frontend": "sha256:" + "3" * 64,
}
FILES = (
    "compose.yaml",
    "contract.json",
    "postgres/01-create-databases.sh",
    "milvus/embedEtcd.yaml",
    "milvus/user.yaml",
)


@pytest.fixture
def release(tmp_path):
    source = tmp_path / "source"
    for name in FILES:
        p = source / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            '{"deployment_contract_version":1}'
            if name == "contract.json"
            else f"original {name}\n"
        )
    env_file = tmp_path / "deploy.env"
    env_file.write_text(
        "POSTGRES_IMAGE=p@1\nNEO4J_IMAGE=n@1\nMILVUS_IMAGE=m@1\nATTU_IMAGE=a@1\n"
        "METAWEAVE_IMAGE=insightweave/metaweave:OLD\nSECRET=must-not-copy\n"
    )
    state = tmp_path / "docker.json"
    state.write_text(
        json.dumps(
            {
                "images": {
                    **{f"insightweave/{app}:R1": value for app, value in IDS.items()},
                    **{
                        ref: "sha256:" + digit * 64
                        for ref, digit in (
                            ("p@1", "4"),
                            ("n@1", "5"),
                            ("m@1", "6"),
                            ("a@1", "7"),
                        )
                    },
                },
                "containers": {
                    **{f"cid-{app}": value for app, value in IDS.items()},
                    **{
                        f"cid-{app}": "sha256:" + digit * 64
                        for app, digit in (
                            ("postgres", "4"),
                            ("neo4j", "5"),
                            ("milvus", "6"),
                            ("attu", "7"),
                        )
                    },
                },
            }
        )
    )
    docker = tmp_path / "docker"
    docker.write_text(f"#!{sys.executable}\n" + """import json,os,sys,time
a=sys.argv[1:]; d=json.load(open(os.environ['DOCKER_STATE']))
with open(os.environ['DOCKER_CALLS'],'a') as f: f.write(json.dumps(a)+'\\n')
if a[:2]==['image','inspect']:
 print(d['images'][a[-1]])
elif a[0]=='inspect':
 if a[2]=='{{.State.Running}}': print('false' if a[-1] in d.get('stopped',[]) else 'true')
 else: print(d['containers'][a[-1]])
elif a[0]=='compose' and 'ps' in a:
 print('' if a[-1] in d.get('absent',[]) else 'cid-'+a[-1])
elif a[0]=='compose' and 'exec' in a:
 app=a[a.index('exec')+2]
 if app in d.get('control_failure',[]): sys.exit(8)
 value=d.get('control',{}).get(app,{'deployment_contract_version':1,'maintaining':False,'active':3})
 print(value if isinstance(value,str) else json.dumps(value))
elif a[0]=='compose' and 'run' in a:
 service=a[a.index('--entrypoint')+2]
 if '-c' in a:
  if service in d.get('config_failure',[]):
   print('private config details',file=sys.stderr); sys.exit(1)
  print('configuration parsed'); sys.exit(0)
 time.sleep(d.get('infra_sleep',0))
 if d.get('infra_failure'):
  print('postgres: do-not-leak-password',file=sys.stderr); sys.exit(1)
 print('infra passed')
elif a[0]=='rm': pass
else:
 sys.exit(9)
""")
    docker.chmod(0o755)
    env = {
        **os.environ,
        "DOCKER_BIN": str(docker),
        "DOCKER_STATE": str(state),
        "DOCKER_CALLS": str(tmp_path / "calls.jsonl"),
    }
    # 不允许调用者已有的镜像变量覆盖受控 fixture。
    for key in ("POSTGRES_IMAGE", "NEO4J_IMAGE", "MILVUS_IMAGE", "ATTU_IMAGE"):
        env.pop(key, None)
    candidate = tmp_path / "candidate.json"
    snapshots = tmp_path / "releases"

    def run(command, *args):
        return subprocess.run(
            [sys.executable, str(SCRIPT), command, *map(str, args)],
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )

    def build():
        r = run(
            "candidate",
            "--release-id",
            "R1",
            "--source-sha",
            SHA,
            "--contract",
            source / "contract.json",
            "--output",
            candidate,
        )
        assert r.returncode == 0, r.stderr

    def snapshot():
        return run(
            "snapshot",
            "--release-id",
            "R1",
            "--source-sha",
            SHA,
            "--candidate",
            candidate,
            "--compose-file",
            source / "compose.yaml",
            "--env-file",
            env_file,
            "--releases-dir",
            snapshots,
        )

    return {
        "run": run,
        "build": build,
        "snapshot": snapshot,
        "source": source,
        "candidate": candidate,
        "target": snapshots / "R1",
        "state": state,
        "env_file": env_file,
        "tmp": tmp_path,
        "env": env,
    }


def test_candidate_records_actual_images_and_full_source(release):
    release["build"]()
    d = json.loads(release["candidate"].read_text())
    assert d["source_sha"] == SHA
    assert d["release_id"] == "R1"
    assert d["deployment_contract_version"] == 1
    assert d["applications"]["metaweave"] == {
        "image": "insightweave/metaweave:R1",
        "image_id": IDS["metaweave"],
    }


def test_snapshot_uses_candidate_not_stale_env_and_survives_source_removal(release):
    release["build"]()
    assert release["snapshot"]().returncode == 0
    d = json.loads((release["target"] / "manifest.json").read_text())
    assert d["applications"]["metaweave"]["image"] == "insightweave/metaweave:R1"
    assert d["upstream_images"]["ATTU_IMAGE"] == "a@1"
    assert set(d["config_hashes"]) == set(FILES)
    assert "must-not-copy" not in (release["target"] / "manifest.json").read_text()
    for name in FILES:
        (release["source"] / name).unlink()
    r = release["run"](
        "load", "--snapshot", release["target"], "--env-file", release["env_file"]
    )
    assert r.returncode == 0, r.stderr
    assert str(release["target"] / "compose.yaml") in r.stdout
    assert not (release["target"] / "deploy.env").exists()


@pytest.mark.parametrize(
    "mutation", ["missing", "source", "release", "id", "contract", "application"]
)
def test_bad_candidate_fails_before_snapshot_publication(release, mutation):
    release["build"]()
    d = json.loads(release["candidate"].read_text())
    if mutation == "missing":
        release["candidate"].unlink()
    else:
        if mutation == "source":
            d["source_sha"] = "b" * 40
        if mutation == "release":
            d["release_id"] = "OTHER"
        if mutation == "id":
            d["applications"]["nl2sql"]["image_id"] = "sha256:" + "9" * 64
        if mutation == "contract":
            d["deployment_contract_version"] = 99
        if mutation == "application":
            del d["applications"]["frontend"]
        release["candidate"].write_text(json.dumps(d))
    r = release["snapshot"]()
    assert r.returncode != 0
    assert not release["target"].exists()


@pytest.mark.parametrize("mutation", ["config", "image", "upstream"])
def test_load_rejects_modified_snapshot_or_retagged_image(release, mutation):
    release["build"]()
    assert release["snapshot"]().returncode == 0
    if mutation == "config":
        (release["target"] / "compose.yaml").write_text("changed")
    elif mutation == "image":
        d = json.loads(release["state"].read_text())
        d["images"]["insightweave/nl2sql:R1"] = "sha256:" + "9" * 64
        release["state"].write_text(json.dumps(d))
    else:
        with release["env_file"].open("a") as f:
            f.write("ATTU_IMAGE=changed\n")
    assert (
        release["run"](
            "load", "--snapshot", release["target"], "--env-file", release["env_file"]
        ).returncode
        != 0
    )


# contract.json 的 deployment_contract_version 只接受整数 1（doc 15 §7.4）。
# bool 是 int 的子类、1.0 与 1 相等，只写 != 1 会把 true 和 1.0 放行。
_MISSING = object()
CONTRACT_VERSION_INPUTS = (
    pytest.param(1, True, id="int-1"),
    pytest.param(True, False, id="true"),
    pytest.param(1.0, False, id="float-1"),
    pytest.param("1", False, id="string-1"),
    pytest.param(False, False, id="false"),
    pytest.param(0, False, id="zero"),
    pytest.param(2, False, id="two"),
    pytest.param(None, False, id="null"),
    pytest.param(_MISSING, False, id="missing"),
)


def write_contract(path, value):
    data = {} if value is _MISSING else {"deployment_contract_version": value}
    path.write_text(json.dumps(data), encoding="utf-8")


@pytest.mark.parametrize("value, accepted", CONTRACT_VERSION_INPUTS)
def test_candidate_contract_version_accepts_only_integer_one(release, value, accepted):
    """入口一 validate_candidate：候选清单的版本取自 contract.json。"""
    contract = release["source"] / "contract.json"
    write_contract(contract, value)
    r = release["run"](
        "candidate",
        "--release-id",
        "R1",
        "--source-sha",
        SHA,
        "--contract",
        contract,
        "--output",
        release["candidate"],
    )
    assert (r.returncode == 0) is accepted, r.stderr
    if accepted:
        version = json.loads(release["candidate"].read_text())[
            "deployment_contract_version"
        ]
        assert version == 1 and type(version) is int


@pytest.mark.parametrize("value, accepted", CONTRACT_VERSION_INPUTS)
def test_snapshot_contract_version_accepts_only_integer_one(release, value, accepted):
    """入口二 create_snapshot：直接读取源码目录的 contract.json，失败时不得发布快照。"""
    release["build"]()
    write_contract(release["source"] / "contract.json", value)
    r = release["snapshot"]()
    assert (r.returncode == 0) is accepted, r.stderr
    assert release["target"].exists() is accepted


@pytest.mark.parametrize("value, accepted", CONTRACT_VERSION_INPUTS)
def test_load_contract_version_accepts_only_integer_one(release, value, accepted):
    """入口三 load_snapshot：改写快照内的 contract.json 并同步配置哈希，测到的才是版本校验。"""
    release["build"]()
    assert release["snapshot"]().returncode == 0
    contract = release["target"] / "contract.json"
    write_contract(contract, value)
    manifest_path = release["target"] / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["config_hashes"]["contract.json"] = hashlib.sha256(
        contract.read_bytes()
    ).hexdigest()
    manifest_path.write_text(json.dumps(manifest))
    r = release["run"](
        "load", "--snapshot", release["target"], "--env-file", release["env_file"]
    )
    assert (r.returncode == 0) is accepted, r.stderr


def test_snapshot_reentry_is_immutable(release):
    release["build"]()
    assert release["snapshot"]().returncode == 0
    before = (release["target"] / "manifest.json").read_bytes()
    assert release["snapshot"]().returncode == 0
    (release["source"] / "compose.yaml").write_text("new configuration")
    assert release["snapshot"]().returncode != 0
    assert (release["target"] / "manifest.json").read_bytes() == before


def test_running_container_id_must_match_even_when_http_would_be_healthy(release):
    release["build"]()
    assert release["snapshot"]().returncode == 0
    args = (
        "--snapshot",
        release["target"],
        "--env-file",
        release["env_file"],
        "--project",
        "test-project",
    )
    assert release["run"]("verify-running", *args).returncode == 0
    d = json.loads(release["state"].read_text())
    d["containers"]["cid-nl2sql"] = "sha256:" + "9" * 64
    release["state"].write_text(json.dumps(d))
    r = release["run"]("verify-running", *args)
    assert r.returncode != 0 and "nl2sql" in r.stderr


def test_legacy_snapshot_is_not_silently_trusted(release):
    release["target"].mkdir(parents=True)
    (release["target"] / "manifest.env").write_text("METAWEAVE_IMAGE=old\n")
    r = release["run"](
        "load", "--snapshot", release["target"], "--env-file", release["env_file"]
    )
    assert r.returncode != 0
    assert "旧快照" in r.stderr or "快照缺失" in r.stderr


def preflight(release, **changes):
    release["build"]()
    assert release["snapshot"]().returncode == 0
    data = json.loads(release["state"].read_text())
    data.update(changes)
    release["state"].write_text(json.dumps(data))
    return (
        "--snapshot",
        release["target"],
        "--env-file",
        release["env_file"],
        "--project",
        "test-project",
    )


def test_preflight_accepts_busy_backends_and_runs_readonly_infra(release):
    args = preflight(release)
    result = release["run"]("preflight", *args)
    assert result.returncode == 0, result.stderr
    calls = [
        json.loads(line)
        for line in (release["tmp"] / "calls.jsonl").read_text().splitlines()
    ]
    run = next(
        c
        for c in calls
        if c[0] == "compose" and "run" in c and c[-2:] == ["--scope", "infra"]
    )
    assert "--no-deps" in run and "--rm" in run
    assert run[-2:] == ["--scope", "infra"]
    assert "initialize" not in run


@pytest.mark.parametrize(
    "state",
    [
        {"deployment_contract_version": 99, "maintaining": False, "active": 0},
        {"deployment_contract_version": 10, "maintaining": False, "active": 0},
        {"deployment_contract_version": "1", "maintaining": False, "active": 0},
        {"deployment_contract_version": True, "maintaining": False, "active": 0},
        {"deployment_contract_version": 1, "maintaining": False, "active": -1},
        {"deployment_contract_version": 1, "maintaining": False, "active": True},
        "not json",
        {},
    ],
)
def test_preflight_rejects_bad_control_before_infra(release, state):
    args = preflight(release, control={"nl2sql": state})
    result = release["run"]("preflight", *args)
    assert result.returncode != 0 and "nl2sql" in result.stderr
    assert not any(
        '"run"' in line
        for line in (release["tmp"] / "calls.jsonl").read_text().splitlines()
    )


@pytest.mark.parametrize("app", ["postgres", "neo4j", "milvus", "attu"])
def test_preflight_checks_each_upstream_container_identity(release, app):
    args = preflight(release)
    data = json.loads(release["state"].read_text())
    data["containers"][f"cid-{app}"] = "sha256:" + "9" * 64
    release["state"].write_text(json.dumps(data))
    result = release["run"]("preflight", *args)
    assert result.returncode != 0 and app in result.stderr


@pytest.mark.parametrize(
    "changes", [{"absent": ["nl2sql"]}, {"stopped": ["cid-nl2sql"]}]
)
def test_only_recovery_can_skip_confirmed_stopped_or_absent_backend(release, changes):
    args = preflight(release, **changes)
    assert release["run"]("preflight", *args).returncode != 0
    assert (
        release["run"]("preflight", *args, "--allow-missing-backends").returncode == 0
    )


def test_recovery_still_rejects_running_backend_with_unreachable_control(release):
    args = preflight(release, control_failure=["nl2sql"])
    result = release["run"]("preflight", *args, "--allow-missing-backends")
    assert result.returncode != 0


@pytest.mark.parametrize("service", ["metaweave", "nl2sql"])
def test_target_config_parse_failure_stops_before_infra_without_leaking_values(
    release, service
):
    args = preflight(release, config_failure=[service])
    result = release["run"]("preflight", *args)
    assert result.returncode != 0 and service in result.stderr
    assert "private config details" not in result.stdout + result.stderr
    calls = [
        json.loads(line)
        for line in (release["tmp"] / "calls.jsonl").read_text().splitlines()
    ]
    assert not any(c[-2:] == ["--scope", "infra"] for c in calls)


def test_both_target_configs_are_parsed_before_infra(release):
    args = preflight(release)
    result = release["run"]("preflight", *args)
    assert result.returncode == 0, result.stderr
    calls = [
        json.loads(line)
        for line in (release["tmp"] / "calls.jsonl").read_text().splitlines()
    ]
    runs = [c for c in calls if c[0] == "compose" and "run" in c]
    assert len(runs) == 3
    assert {c[c.index("--entrypoint") + 2] for c in runs[:-1]} == {
        "metaweave",
        "nl2sql",
    }
    assert all("-c" in c for c in runs[:-1])
    assert runs[-1][-2:] == ["--scope", "infra"]


@pytest.mark.parametrize("maintaining", [False, True])
def test_ready_verification_requires_open_backend_but_recovery_accepts_maintenance(
    release, maintaining
):
    args = preflight(
        release,
        control={
            "nl2sql": {
                "deployment_contract_version": 1,
                "maintaining": maintaining,
                "active": 0,
            }
        },
    )
    normal = release["run"]("preflight", *args, "--require-open-backends")
    assert (normal.returncode == 0) is (not maintaining)
    if maintaining:
        assert "维护" in normal.stderr
    recovery = release["run"]("preflight", *args, "--allow-missing-backends")
    assert recovery.returncode == 0, recovery.stderr


def test_infra_failure_is_nonzero_and_does_not_echo_exception_credentials(release):
    args = preflight(release, infra_failure=True)
    result = release["run"]("preflight", *args)
    assert result.returncode != 0 and "postgres" in result.stderr
    assert "do-not-leak-password" not in result.stdout + result.stderr


def test_infra_timeout_cleans_only_its_own_probe(release, monkeypatch):
    import runpy

    module = runpy.run_path(str(SCRIPT))
    preflight(release, infra_sleep=2)
    for key, value in release["env"].items():
        if key.startswith("DOCKER_"):
            monkeypatch.setenv(key, value)
    compose = ["compose", "-f", str(release["target"] / "compose.yaml")]
    with pytest.raises(ValueError, match="超时"):
        module["run_infra_check"](compose, "test-project", timeout=0.1)
    calls = [
        json.loads(line)
        for line in (release["tmp"] / "calls.jsonl").read_text().splitlines()
    ]
    probe = next(c[c.index("--name") + 1] for c in calls if "--name" in c)
    assert ["rm", "-f", probe] in calls
    assert probe.startswith("test-project-infra-check-")


@pytest.fixture
def port_case(monkeypatch):
    spec = importlib.util.spec_from_file_location("m01_manifest", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    module.HOST_PORTS = (("postgres", port, "5432/tcp"),)
    rows = []
    calls = []

    def docker(*args):
        calls.append(args)
        if args[0] == "ps":
            return "cid" if rows else ""
        assert args[0] == "inspect" and ".Env" not in args[2]
        return "\n".join(json.dumps(row) for row in rows)

    monkeypatch.setattr(module, "docker", docker)
    monkeypatch.setattr(module, "windows_listeners", lambda ports: set(), raising=False)
    try:
        yield module, listener, port, rows, calls
    finally:
        listener.close()


def port_record(port):
    return {
        "running": True,
        "project": "insightweave-local",
        "service": "postgres",
        "ports": {"5432/tcp": [{"HostIp": "127.0.0.1", "HostPort": str(port)}]},
    }


def test_port_check_allows_free_port(port_case):
    module, listener, _, _, _ = port_case
    listener.close()
    module.check_ports("insightweave-local")


def test_port_check_detects_tcp_listener_without_http_and_sends_no_data(port_case):
    module, listener, port, _, _ = port_case
    listener.listen()
    listener.settimeout(1)
    with pytest.raises(ValueError, match=str(port)):
        module.check_ports("insightweave-local")
    peer, _ = listener.accept()
    with peer:
        assert peer.recv(1) == b""


@pytest.mark.parametrize(
    "fault", ["none", "project", "service", "internal_port", "wildcard", "stopped"]
)
def test_port_check_requires_exact_running_project_service_mapping(port_case, fault):
    module, listener, port, rows, _ = port_case
    listener.listen()
    record = port_record(port)
    if fault == "project":
        record["project"] = "another-project"
    elif fault == "service":
        record["service"] = "frontend"
    elif fault == "internal_port":
        record["ports"]["80/tcp"] = record["ports"].pop("5432/tcp")
    elif fault == "wildcard":
        record["ports"]["5432/tcp"][0]["HostIp"] = "0.0.0.0"
    elif fault == "stopped":
        record["running"] = False
    rows.append(record)
    if fault == "none":
        module.check_ports("insightweave-local")
    else:
        with pytest.raises(ValueError, match=str(port)):
            module.check_ports("insightweave-local")


def test_foreign_docker_binding_blocks_even_without_tcp_response(port_case):
    module, listener, port, rows, _ = port_case
    listener.close()
    record = port_record(port)
    record["project"] = "other"
    record["ports"]["5432/tcp"][0]["HostIp"] = "0.0.0.0"
    rows.append(record)
    with pytest.raises(ValueError, match=str(port)):
        module.check_ports("insightweave-local")


def test_windows_only_listener_blocks_when_wsl_loopback_is_free(port_case, monkeypatch):
    module, listener, port, _, _ = port_case
    listener.close()
    monkeypatch.setattr(module, "windows_listeners", lambda ports: {port})
    with pytest.raises(ValueError, match=str(port)):
        module.check_ports("insightweave-local")


@pytest.mark.parametrize(
    "failure", ["docker", "windows", "tcp_timeout", "tcp_permission", "bad_json"]
)
def test_unknown_port_state_never_counts_as_free(port_case, monkeypatch, failure):
    module, listener, _, _, _ = port_case
    listener.close()
    if failure in ("docker", "bad_json"):

        def docker(*args):
            if failure == "docker":
                raise ValueError("Docker unavailable")
            return "cid" if args[0] == "ps" else "not json"

        monkeypatch.setattr(module, "docker", docker)
    elif failure == "windows":

        def windows(ports):
            raise ValueError("Windows query failed")

        monkeypatch.setattr(module, "windows_listeners", windows)
    else:

        def connect(*args, **kwargs):
            if failure == "tcp_timeout":
                raise TimeoutError("timed out")
            raise PermissionError(errno.EACCES, "denied")

        monkeypatch.setattr(module.socket, "create_connection", connect)
    with pytest.raises(ValueError):
        module.check_ports("insightweave-local")


@pytest.mark.parametrize("failure", [False, True])
def test_windows_listener_query_is_bounded_and_checks_exit_code(monkeypatch, failure):
    spec = importlib.util.spec_from_file_location("m01_windows", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    port = 43210
    monkeypatch.setattr(module.platform, "release", lambda: "microsoft-standard-WSL2")
    monkeypatch.setattr(module.shutil, "which", lambda name: "/windows/powershell.exe")

    def run(command, **kwargs):
        assert 0 < kwargs["timeout"] <= 15
        assert "Get-NetTCPConnection" in command[-1]
        return subprocess.CompletedProcess(
            command, 1 if failure else 0, json.dumps([port]), "private diagnostic"
        )

    monkeypatch.setattr(module.subprocess, "run", run)
    if failure:
        with pytest.raises(ValueError, match="Windows"):
            module.windows_listeners([port])
    else:
        assert module.windows_listeners([port]) == {port}


def _windows_query_module(monkeypatch):
    spec = importlib.util.spec_from_file_location("m01_windows_query", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module.platform, "release", lambda: "microsoft-standard-WSL2")
    monkeypatch.setattr(module.shutil, "which", lambda name: "/windows/powershell.exe")
    return module


def test_windows_listener_query_starts_from_tmp_not_the_callers_directory(monkeypatch):
    """以 iw-runner 身份在 /home/iw-runner 下执行时，WSL 无法从调用者的目录启动 Windows
    程序（Invalid argument）。查询不读取项目文件，子进程固定从 WSL 默认用户可访问的
    /tmp 启动，而不是继承部署脚本所在目录。"""
    module = _windows_query_module(monkeypatch)
    seen = {}

    def run(command, **kwargs):
        seen["command"] = command
        seen.update(kwargs)
        return subprocess.CompletedProcess(command, 0, json.dumps([]), "")

    monkeypatch.setattr(module.subprocess, "run", run)
    monkeypatch.chdir("/")
    assert module.windows_listeners([43211]) == set()
    assert seen.get("cwd") == "/tmp"
    # 查询内容不随工作目录改变：仍由 -Command 传入并包含所问端口，不在 /tmp 落任何脚本。
    assert seen["command"][-2] == "-Command" and "43211" in seen["command"][-1]


@pytest.mark.parametrize(
    "outcome", ["timeout", "invalid-json", "not-a-list", "port-not-requested"]
)
def test_windows_listener_query_rejects_unusable_answers(monkeypatch, outcome):
    """查询失败不能解释为端口空闲：超时或响应无效一律拒绝。"""
    module = _windows_query_module(monkeypatch)
    answers = {
        "invalid-json": "not json",
        "not-a-list": json.dumps({"port": 43212}),
        "port-not-requested": json.dumps([43299]),
    }

    def run(command, **kwargs):
        if outcome == "timeout":
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        return subprocess.CompletedProcess(command, 0, answers[outcome], "")

    monkeypatch.setattr(module.subprocess, "run", run)
    with pytest.raises(ValueError, match="Windows TCP 监听查询"):
        module.windows_listeners([43212])
