"""工作区文件的两类备份（doc 6 §6、6.2 §5.2）。

- **生成用整目录备份** `backup_directory_before_update`：把步骤目录中除
  `.backup/` 外的全部文件和目录（含点号文件、旧 marker）**移入**
  `.backup/bak_yyyyMMdd_HHmmss_SSS/`。备份失败记录告警、删除未移走的
  残留旧内容后继续生成；不完整备份不作为完整版本使用。
- **人工编辑用文件级备份** `backup_files_before_edit`：只把本次将被覆盖
  文件的旧版本**复制**进备份目录，不移动原文件、不触碰采集 marker 或
  其他产物；任一复制失败则本次保存整体失败，不删除任何现有内容。

两类备份共用时间戳与目录命名工具，但移动/复制与失败策略严格分开。
"""

import logging
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from backend.workspace_files.timestamps import backup_dir_name, shanghai_now

logger = logging.getLogger("backend.workspace_files")

BACKUP_DIR_NAME = ".backup"


class EditBackupFailedError(RuntimeError):
    """文件级备份失败；本次保存必须终止且不改动任何现有内容。"""


@dataclass(frozen=True)
class DirectoryBackupResult:
    """整目录备份结果。"""

    backup_dir: Path | None
    complete: bool
    message: str | None


def _unique_backup_dir(backup_root: Path, moment: datetime) -> Path:
    """时间戳重名时生成不覆盖既有目录的唯一后缀（doc 6.2 §5.2）。"""
    candidate = backup_root / backup_dir_name(moment)
    serial = 2
    while candidate.exists():
        tail = f"_{serial}"
        base = backup_dir_name(moment)
        candidate = backup_root / (base[: 63 - len(tail)] + tail)
        serial += 1
    return candidate


def backup_directory_before_update(
    directory: Path,
    *,
    moment: datetime | None = None,
) -> DirectoryBackupResult:
    """生成场景的整目录备份（doc 6 §6）。

    没有旧内容时不创建空备份。备份单元是「一次目录更新」，调用方在写
    入新内容前调用一次；仅新增 marker 不应再触发本函数。

    失败策略：记录目录与失败原因的告警，删除未成功移入备份的残留旧内容
    （保留 `.backup/`），调用方继续生成；返回 `complete=False`。
    """
    moment = moment or shanghai_now()
    directory.mkdir(parents=True, exist_ok=True)
    entries = [child for child in directory.iterdir() if child.name != BACKUP_DIR_NAME]
    if not entries:
        return DirectoryBackupResult(None, True, None)

    backup_root = directory / BACKUP_DIR_NAME
    backup_dir = _unique_backup_dir(backup_root, moment)
    backup_dir.mkdir(parents=True)
    failures: list[str] = []
    moved: list[Path] = []
    for child in entries:
        target = backup_dir / child.name
        try:
            shutil.move(str(child), str(target))
            moved.append(child)
        except (OSError, shutil.Error) as exc:
            failures.append(f"{child.name}: {type(exc).__name__}")
    if failures:
        logger.warning(
            "整目录备份不完整 directory=%s backup=%s failures=%s",
            directory,
            backup_dir,
            "; ".join(failures),
        )
        # 删除未成功移走的残留旧内容后继续生成（doc 6 §6）；不完整备份
        # 不作为完整版本使用。
        for child in entries:
            if child in moved:
                continue
            try:
                if child.is_dir() and not child.is_symlink():
                    shutil.rmtree(child)
                else:
                    child.unlink()
            except OSError as exc:
                logger.error(
                    "备份失败后清理残留旧内容失败 directory=%s entry=%s type=%s",
                    directory,
                    child.name,
                    type(exc).__name__,
                )
        return DirectoryBackupResult(
            backup_dir,
            False,
            "备份不完整，已删除未移走的旧内容并继续生成（" + "; ".join(failures) + "）",
        )
    return DirectoryBackupResult(backup_dir, True, None)


def backup_files_before_edit(
    directory: Path,
    relative_paths: list[str],
    *,
    moment: datetime | None = None,
) -> Path | None:
    """人工编辑的文件级备份（doc 6.2 §5.2）。

    只**复制**本次将被覆盖且当前存在的文件（保留文件名）；不移动原文件、
    不扫描或备份其他产物，不触碰采集 marker。全部复制成功才返回备份目录；
    任一失败抛 `EditBackupFailedError`，已完成的部分复制保留在本次备份目录
    中供人工核对，但不作为完整备份使用，调用方不得进入写回。

    `relative_paths` 为相对 `directory` 的文件路径；不存在的文件跳过
    （无旧版本可备份）；越界（绝对路径/`..` 逃逸）视为失败。
    """
    moment = moment or shanghai_now()
    if not relative_paths:
        return None
    resolved_root = directory.resolve()
    to_backup: list[tuple[Path, Path]] = []
    for relative in relative_paths:
        candidate = Path(relative)
        if candidate.is_absolute() or ".." in candidate.parts:
            raise EditBackupFailedError(
                f"编辑备份路径必须是目录内相对路径: {relative!r}"
            )
        if BACKUP_DIR_NAME in candidate.parts:
            raise EditBackupFailedError(f"编辑备份路径不得指向备份目录: {relative!r}")
        source = directory / candidate
        if not source.is_file():
            continue
        try:
            source.resolve().relative_to(resolved_root)
        except ValueError:
            raise EditBackupFailedError(f"编辑备份路径越界: {relative!r}") from None
        to_backup.append((source, candidate))
    if not to_backup:
        return None

    backup_root = directory / BACKUP_DIR_NAME
    backup_dir = _unique_backup_dir(backup_root, moment)
    backup_dir.mkdir(parents=True)
    failures: list[str] = []
    for source, relative in to_backup:
        target = backup_dir / relative
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        except OSError as exc:
            failures.append(f"{relative.as_posix()}: {type(exc).__name__}")
    if failures:
        # 不删除任何现有内容（包括已复制进备份目录的部分），明确失败
        raise EditBackupFailedError(
            "文件级备份失败，本次保存终止；不完整的备份保留于 "
            f"{backup_dir.name}（{'；'.join(failures)}）"
        )
    return backup_dir
