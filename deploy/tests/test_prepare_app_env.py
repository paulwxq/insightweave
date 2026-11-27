"""prepare_app_env.py 的离线测试（doc 16 §4「读取源文件」「生成和注入」，§5「解析和合并」）。

运行真实脚本；输入全部是临时目录里的哑值，断言退出码、候选文件内容、权限和错误输出。
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

TOOL = Path(__file__).resolve().parents[1] / "scripts" / "prepare_app_env.py"


def write(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


class Site:
    """开发工作区与部署根目录的哑值布局（doc 16 §2）。"""

    def __init__(self, tmp_path: Path):
        self.dev = tmp_path / "dev"
        self.deploy = tmp_path / "runtime"
        self.dev_env = {svc: self.dev / svc / ".env" for svc in ("metaweave", "nl2sql")}
        self.override = {
            svc: self.deploy / "config" / "overrides" / f"{svc}.env"
            for svc in ("metaweave", "nl2sql")
        }
        write(self.dev_env["metaweave"], ["APP_NAME=metaweave", "KEEP=from-dev"])
        write(self.dev_env["nl2sql"], ["APP_NAME=nl2sql"])
        write(self.override["metaweave"], ["DEPLOY_CONTROL_TOKEN=t0k-metaweave"])
        write(self.override["nl2sql"], ["DEPLOY_CONTROL_TOKEN=t0k-nl2sql"])

    def run(self, release_id="rel-1", extra_env=None, umask=0o022):
        env = {**os.environ, **(extra_env or {})}
        cmd = [
            sys.executable,
            str(TOOL),
            "--dev-root",
            str(self.dev),
            "--deploy-root",
            str(self.deploy),
            f"--release-id={release_id}",
        ]
        return subprocess.run(
            cmd, capture_output=True, text=True, timeout=60, check=False,
            env=env, cwd="/", umask=umask,
        )

    def candidate(self, release_id="rel-1") -> Path:
        return self.deploy / "config" / ".pending" / release_id


def assert_stopped_before_writing(result, site: Site, *needles: str) -> str:
    assert result.returncode != 0, result.stdout + result.stderr
    assert result.stdout == ""  # 失败时标准输出不输出任何内容
    for needle in needles:
        assert needle in result.stderr, result.stderr
    assert not site.candidate().exists()
    pending = site.deploy / "config" / ".pending"
    assert not pending.exists() or list(pending.iterdir()) == []  # 不留临时目录
    return result.stderr


class TestGenerate:
    def test_values_are_written_literally_and_override_wins(self, tmp_path):
        """开发文件里的各种写法按 python-dotenv 语义取值，再逐行原样写入 raw 文件。"""
        site = Site(tmp_path)
        write(
            site.dev_env["metaweave"],
            [
                "# 开发文件里的各种写法",
                "DOLLAR=$HOME",
                "HASH=value # 行内注释",
                'QUOTED="a # not comment"',
                "QUOTED_SINGLE='single $VALUE'",
                "BACKSLASH=C:\\dir\\",
                'PADDED="  padded  "',
                "EQUALS=a=b=c",
                "EMPTY=",
                "HASH_FIRST=#not-a-comment",
                'INNER="say \\"hi\\""',
                "KEEP=from-dev",
            ],
        )
        write(
            site.override["metaweave"],
            ["# Docker 覆盖：同名变量优先", "HASH=overridden", "ADDED_BY_OVERRIDE=1",
             "DEPLOY_CONTROL_TOKEN=t0k-metaweave"],
        )
        result = site.run(umask=0)  # 宽松 umask 下也必须收紧权限
        assert result.returncode == 0, result.stderr
        expected = "\n".join(
            [
                "ADDED_BY_OVERRIDE=1",
                "BACKSLASH=C:\\dir\\",
                "DEPLOY_CONTROL_TOKEN=t0k-metaweave",
                "DOLLAR=$HOME",
                "EMPTY=",
                "EQUALS=a=b=c",
                "HASH=overridden",
                "HASH_FIRST=#not-a-comment",
                'INNER=say "hi"',
                "KEEP=from-dev",
                "PADDED=  padded  ",
                "QUOTED=a # not comment",
                "QUOTED_SINGLE=single $VALUE",
            ]
        ) + "\n"
        candidate = site.candidate()
        assert (candidate / "metaweave.env").read_text(encoding="utf-8") == expected

    def test_services_do_not_share_variables(self, tmp_path):
        site = Site(tmp_path)
        write(site.dev_env["nl2sql"], ["ONLY_NL=1", "DEPLOY_CONTROL_TOKEN=from-dev"])
        write(site.override["nl2sql"], ["DEPLOY_CONTROL_TOKEN=t0k-nl2sql"])
        assert site.run().returncode == 0
        candidate = site.candidate()
        assert (candidate / "nl2sql.env").read_text(encoding="utf-8") == (
            "DEPLOY_CONTROL_TOKEN=t0k-nl2sql\nONLY_NL=1\n"
        )
        assert (candidate / "metaweave.env").read_text(encoding="utf-8") == (
            "APP_NAME=metaweave\nDEPLOY_CONTROL_TOKEN=t0k-metaweave\nKEEP=from-dev\n"
        )

    def test_last_assignment_wins_including_an_empty_one(self, tmp_path):
        site = Site(tmp_path)
        write(
            site.dev_env["metaweave"],
            ["A=first", "B=keep", "A=second", "B=", "C=dev"],
        )
        write(site.override["metaweave"], ["A="])
        assert site.run().returncode == 0
        assert (site.candidate() / "metaweave.env").read_text(encoding="utf-8") == (
            "A=\nB=\nC=dev\n"
        )

    def test_removed_keys_stay_removed_and_override_keys_survive(self, tmp_path):
        """每次发布都从两份源文件重建：开发文件里删掉的变量不出现在候选里（上一份正式配置
        里还有它也不继承）；覆盖文件定义的变量不依赖开发文件，同名变量仍按覆盖值生效。"""
        site = Site(tmp_path)
        write(
            site.dev_env["metaweave"],
            ["PINNED=changed-in-dev", "KEEP=from-dev"],
        )
        write(
            site.override["metaweave"],
            ["PINNED=from-override", "OVERRIDE_ONLY=1", "DEPLOY_CONTROL_TOKEN=t0k-metaweave"],
        )
        # 上一份正式配置：开发文件后来删掉了 DEV_ONLY，KEEP 被改名
        write(
            site.deploy / "config" / "metaweave.env",
            ["DEV_ONLY=1", "KEEP=old-name", "STALE_IN_FORMAL=1"],
        )
        assert site.run().returncode == 0
        assert (site.candidate() / "metaweave.env").read_text(encoding="utf-8") == (
            "DEPLOY_CONTROL_TOKEN=t0k-metaweave\n"
            "KEEP=from-dev\n"
            "OVERRIDE_ONLY=1\n"
            "PINNED=from-override\n"
        )

    def test_candidate_is_private_even_under_a_permissive_umask(self, tmp_path):
        site = Site(tmp_path)
        # 父目录已存在且权限过宽时，也要收紧到 0700
        pending = site.deploy / "config" / ".pending"
        pending.mkdir(parents=True)
        pending.chmod(0o755)
        assert site.run(umask=0).returncode == 0
        assert mode(pending) == 0o700
        assert mode(site.candidate()) == 0o700
        for svc in ("metaweave", "nl2sql"):
            assert mode(site.candidate() / f"{svc}.env") == 0o600

    def test_values_come_only_from_files_never_from_the_environment(self, tmp_path):
        site = Site(tmp_path)
        write(site.dev_env["metaweave"], ["DOLLAR=$HOME", "ENV_ONLY="])
        assert site.run(extra_env={"HOME": "/elsewhere", "ENV_ONLY": "from-env"}).returncode == 0
        text = (site.candidate() / "metaweave.env").read_text(encoding="utf-8")
        assert "DOLLAR=$HOME\n" in text and "ENV_ONLY=\n" in text

    def test_success_output_reports_counts_but_no_values(self, tmp_path):
        site = Site(tmp_path)
        result = site.run()
        assert result.returncode == 0, result.stderr
        assert str(site.candidate()) in result.stdout
        for svc in ("metaweave", "nl2sql"):
            assert svc in result.stdout
        for value in ("t0k-metaweave", "t0k-nl2sql", "from-dev"):
            assert value not in result.stdout + result.stderr


class TestStopsBeforeWriting:
    def test_missing_dev_file_names_service_and_file(self, tmp_path):
        site = Site(tmp_path)
        site.dev_env["nl2sql"].unlink()
        assert_stopped_before_writing(
            site.run(), site, "nl2sql", str(site.dev_env["nl2sql"])
        )

    def test_missing_override_file_names_service_and_file(self, tmp_path):
        site = Site(tmp_path)
        site.override["metaweave"].unlink()
        assert_stopped_before_writing(
            site.run(), site, "metaweave", str(site.override["metaweave"])
        )

    @pytest.mark.skipif(os.geteuid() == 0, reason="root 无视文件权限")
    def test_unreadable_dev_file_is_reported(self, tmp_path):
        site = Site(tmp_path)
        site.dev_env["metaweave"].chmod(0)
        try:
            assert_stopped_before_writing(
                site.run(), site, "metaweave", str(site.dev_env["metaweave"])
            )
        finally:
            site.dev_env["metaweave"].chmod(0o600)

    @pytest.mark.parametrize(
        ("bad_lines", "needle"),
        [
            (["A=1", "BAREKEY", "C=2"], "BAREKEY"),  # 没有 = 的裸键（第 2 行）
            # 未闭合的引号：报错给出等号前的变量名，但不带出等号后的值
            (["A=1", "BROKEN='s3cret-value-two", "C=2"], "BROKEN"),
            (["A=1", "BAD.KEY=1"], "BAD.KEY"),  # 不是合法的环境变量名
        ],
    )
    def test_invalid_line_reports_file_line_and_name_only(self, tmp_path, bad_lines, needle):
        site = Site(tmp_path)
        write(site.dev_env["metaweave"], bad_lines)
        stderr = assert_stopped_before_writing(
            site.run(), site, str(site.dev_env["metaweave"]), "2"
        )
        assert needle in stderr
        assert "s3cret" not in stderr

    @pytest.mark.parametrize(
        ("line", "name"),
        [
            ("MULTI='first\nsecond'", "MULTI"),  # 文件里真实的换行
            ('ESCAPED="first\\nsecond"', "ESCAPED"),  # 双引号里的 \n 被解析成换行
            ("CARRIED='first\r\nsecond'", "CARRIED"),  # 回车
        ],
    )
    def test_multiline_values_are_rejected_before_any_candidate_exists(self, tmp_path, line, name):
        site = Site(tmp_path)
        write(site.dev_env["metaweave"], ["A=1", line])
        assert_stopped_before_writing(
            site.run(), site, name, str(site.dev_env["metaweave"])
        )

    def test_another_variable_value_never_appears_in_the_error(self, tmp_path):
        site = Site(tmp_path)
        write(
            site.dev_env["nl2sql"],
            ["SECRET_ONE=s3cret-value-one", "BROKEN='unterminated"],
        )
        stderr = assert_stopped_before_writing(
            site.run(), site, "BROKEN", str(site.dev_env["nl2sql"])
        )
        assert "s3cret-value-one" not in stderr

    @pytest.mark.parametrize("release_id", ["../escape", "has space", "-leading", ".dot"])
    def test_release_id_outside_the_allowed_charset_writes_nothing(self, tmp_path, release_id):
        site = Site(tmp_path)
        result = site.run(release_id=release_id)
        assert result.returncode != 0
        assert "--release-id" in result.stderr
        assert not (site.deploy / "config" / ".pending").exists()
        assert (tmp_path / "escape").exists() is False

    @pytest.mark.parametrize("occupied", [True, False])
    def test_existing_candidate_directory_is_never_overwritten(self, tmp_path, occupied):
        """目录里有东西时操作系统会拒绝改名；空目录则能被改名覆盖，必须由脚本自己拦住。"""
        site = Site(tmp_path)
        site.candidate().mkdir(parents=True)
        keep = site.candidate() / "keep.txt"
        if occupied:
            write(keep, ["previous attempt"])
        result = site.run()
        assert result.returncode != 0
        assert str(site.candidate()) in result.stderr
        names = [p.name for p in site.candidate().iterdir()]
        assert names == (["keep.txt"] if occupied else [])
        if occupied:
            assert keep.read_text(encoding="utf-8") == "previous attempt\n"

    @pytest.mark.parametrize("fault", ["missing", "version"])
    def test_missing_or_pinned_mismatched_python_dotenv_stops(self, tmp_path, fault):
        """同步工具固定 python-dotenv==1.2.4：导入不了或版本不符都在写任何文件之前停止。"""
        site = Site(tmp_path)
        shadow = tmp_path / "shadow"
        if fault == "missing":
            write(shadow / "dotenv.py", ['raise ImportError("shadowed for the test")'])
        else:
            info = shadow / "python_dotenv-0.0.0.dist-info"
            info.mkdir(parents=True)
            (info / "METADATA").write_text(
                "Metadata-Version: 2.1\nName: python-dotenv\nVersion: 0.0.0\n",
                encoding="utf-8",
            )
        assert_stopped_before_writing(
            site.run(extra_env={"PYTHONPATH": str(shadow)}), site, "python-dotenv"
        )

    @pytest.mark.parametrize("arg", ["--dev-root", "--deploy-root", "--release-id"])
    def test_each_argument_is_required(self, tmp_path, arg):
        site = Site(tmp_path)
        argv = [
            "--dev-root", str(site.dev), "--deploy-root", str(site.deploy), "--release-id=rel-1",
        ]
        del argv[argv.index(next(item for item in argv if item.startswith(arg))) :]
        result = subprocess.run(
            [sys.executable, str(TOOL), *argv],
            capture_output=True, text=True, check=False, cwd="/",
        )
        assert result.returncode != 0
        assert arg in result.stderr
        assert not (site.deploy / "config" / ".pending").exists()
