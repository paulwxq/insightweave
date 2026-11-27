"""由开发 .env 与 Docker 覆盖文件生成两份后端候选 env（doc 16 §3.2、§4）；只写宿主机受限目录，不输出变量值。"""

from __future__ import annotations

import argparse
import importlib.metadata
import io
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

SERVICES = ("metaweave", "nl2sql")
DOTENV_VERSION = "1.2.4"
RELEASE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
KEY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
UNSUPPORTED = (("\n", "换行"), ("\r", "回车"), ("\0", "NUL"))


class SyncError(Exception):
    """只携带可以公开的信息：服务、文件、行号、变量名和原因。"""


def load_parser():
    """返回 python-dotenv 的解析函数；导入不了或版本不是固定值时停止。"""
    try:
        from dotenv.parser import parse_stream
    except ImportError:
        raise SyncError(
            f"当前解释器无法导入 python-dotenv；请为它安装 python-dotenv=={DOTENV_VERSION}"
        ) from None
    try:
        version = importlib.metadata.version("python-dotenv")
    except importlib.metadata.PackageNotFoundError:
        version = "未知"
    if version != DOTENV_VERSION:
        raise SyncError(f"python-dotenv 版本不符：需要 {DOTENV_VERSION}，实际 {version}")
    return parse_stream


def _name_hint(raw: str) -> str:
    """无法解析的行里，等号之前若是合法变量名则返回它；等号之后的内容可能是值，一律不看。"""
    match = re.match(r"(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=", raw.lstrip("\r\n"))
    return match.group(1) if match else ""


def read_env_file(parse_stream, service: str, path: Path, what: str) -> dict[str, str]:
    """按 dotenv 写法读取一个文件；不做插值，同一变量多次赋值以最后一次为准。"""
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        raise SyncError(f"{service} 的{what}无法读取：{path}（{type(exc).__name__}）") from None
    values: dict[str, str] = {}
    for binding in parse_stream(io.StringIO(text)):
        raw = binding.original.string
        # 前面的空行会并入本条绑定的原文：变量所在的行要加上这些空行
        leading = raw[: len(raw) - len(raw.lstrip("\r\n"))]
        line = binding.original.line + leading.count("\n")
        where = f"{path} 第 {line} 行（{service} 的{what}）"
        if binding.error:
            name = _name_hint(raw)
            raise SyncError(f"{where}无法解析" + (f"：变量 {name}" if name else ""))
        if binding.key is None:
            continue  # 空行或注释
        if binding.value is None:
            raise SyncError(f"{where}变量 {binding.key} 没有 =")
        if not KEY_RE.fullmatch(binding.key):
            raise SyncError(f"{where}变量名 {binding.key} 不是合法的环境变量名")
        for char, name in UNSUPPORTED:
            if char in binding.value:
                raise SyncError(
                    f"{where}变量 {binding.key} 的值含有{name}，首版只支持单行值"
                )
        values[binding.key] = binding.value
    return values


def merge_service(
    parse_stream, dev_root: Path, deploy_root: Path, service: str
) -> dict[str, str]:
    dev = read_env_file(parse_stream, service, dev_root / service / ".env", "开发 .env")
    override = read_env_file(
        parse_stream,
        service,
        deploy_root / "config" / "overrides" / f"{service}.env",
        "Docker 覆盖文件",
    )
    return {**dev, **override}  # Docker 覆盖文件的同名变量优先


def _write_private_file(path: Path, lines: list[str]) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
        handle.write("".join(lines))
        handle.flush()
        os.fsync(handle.fileno())


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_candidate(
    deploy_root: Path, release_id: str, merged: dict[str, dict[str, str]]
) -> Path:
    """两份文件先写进临时目录，全部成功后整组改名为候选目录。"""
    pending = deploy_root / "config" / ".pending"
    candidate = pending / release_id
    if candidate.exists() or candidate.is_symlink():
        raise SyncError(f"本次发布的候选目录已存在，不覆盖：{candidate}")
    try:
        pending.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(pending, 0o700)  # 候选目录的父目录由本工具管理
        staging = Path(tempfile.mkdtemp(prefix=f".staging-{release_id}-", dir=pending))
        try:
            for service in SERVICES:
                lines = [f"{key}={merged[service][key]}\n" for key in sorted(merged[service])]
                _write_private_file(staging / f"{service}.env", lines)
            _fsync_dir(staging)
            os.rename(staging, candidate)
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        _fsync_dir(pending)
    except OSError as exc:
        raise SyncError(f"无法写入候选目录 {candidate}（{type(exc).__name__}）") from None
    return candidate


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dev-root", required=True, type=Path, help="开发工作区绝对路径")
    parser.add_argument("--deploy-root", required=True, type=Path, help="部署根目录")
    parser.add_argument("--release-id", required=True, help="本次发布的 release_id")
    args = parser.parse_args()
    if not RELEASE_ID_RE.fullmatch(args.release_id):
        print(
            f"[prepare_app_env] 错误：--release-id 只能使用字母、数字、_ . -，"
            "并以字母或数字开头",
            file=sys.stderr,
        )
        return 1
    try:
        parse_stream = load_parser()
        merged = {
            service: merge_service(parse_stream, args.dev_root, args.deploy_root, service)
            for service in SERVICES
        }
        candidate = write_candidate(args.deploy_root, args.release_id, merged)
    except SyncError as exc:
        print(f"[prepare_app_env] 错误：{exc}", file=sys.stderr)
        return 1
    counts = "，".join(f"{service} {len(merged[service])} 个变量" for service in SERVICES)
    print(f"已生成候选配置：{candidate}（{counts}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
