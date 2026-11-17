"""采集结果 marker 读写（doc 6 §6、6.2 §5.3）。

文件名：`.coll_run_id_<collection_run_id>_<yyyyMMdd-HHmmssSSS>_success|failed`。
每个采集步骤目录顶层最多一个；写入前清除顶层旧采集 marker（只清理
`.coll_run_id_` 前缀文件，不动其他点号文件）。`pending`/`running`/`blocked`
不写 marker。发布只读 marker，不写发布 marker。
"""

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from uuid import UUID

from backend.workspace_files.timestamps import (
    marker_timestamp,
    parse_marker_timestamp,
    shanghai_now,
)

_MARKER_RE = re.compile(
    r"^\.coll_run_id_([0-9a-fA-F-]{36})_(\d{8}-\d{9})_(success|failed)$"
)
_MARKER_PREFIX = ".coll_run_id_"


@dataclass(frozen=True)
class CollectionMarker:
    """一个采集步骤目录顶层的当前采集结果 marker。"""

    run_id: str
    status: str  # success | failed
    finished_at: datetime  # 上海时区 aware
    file_name: str


def list_collection_markers(directory: Path) -> list[Path]:
    """顶层全部采集 marker 文件（不含 `.backup/` 内的历史 marker）。"""
    if not directory.is_dir():
        return []
    return sorted(
        child
        for child in directory.iterdir()
        if child.is_file() and child.name.startswith(_MARKER_PREFIX)
    )


def parse_collection_marker(path: Path) -> CollectionMarker:
    match = _MARKER_RE.match(path.name)
    if match is None:
        raise ValueError(f"采集 marker 文件名不合法: {path.name!r}")
    run_id, stamp, status = match.groups()
    return CollectionMarker(
        run_id=str(UUID(run_id)),
        status=status,
        finished_at=parse_marker_timestamp(stamp),
        file_name=path.name,
    )


def read_collection_marker(
    directory: Path,
) -> tuple[CollectionMarker | None, str | None]:
    """读取目录顶层唯一采集 marker。

    返回 (marker, error)：
    - 无 marker → (None, None)；
    - 唯一合法 marker → (marker, None)；
    - 多个 marker 或文件名不合法 → (None, 原因)。发布侧据此把目录判为
      不可用/状态不明，不按时间排序挑选（doc 6 §4.1）。
    """
    markers = list_collection_markers(directory)
    if not markers:
        return None, None
    if len(markers) > 1:
        return None, "目录存在多个采集 marker，产物状态不明"
    try:
        return parse_collection_marker(markers[0]), None
    except ValueError as exc:
        return None, str(exc)


def clear_collection_markers(directory: Path) -> int:
    """删除顶层全部采集 marker；返回删除数量。不动其他点号文件。"""
    removed = 0
    for path in list_collection_markers(directory):
        path.unlink()
        removed += 1
    return removed


def write_collection_marker(
    directory: Path,
    run_id: UUID | str,
    status: str,
    *,
    moment: datetime | None = None,
) -> CollectionMarker:
    """写入本次采集结果 marker；写入前清除顶层旧采集 marker（doc 6 §6）。

    `status` 只允许 `success` / `failed`；blocked/pending/running 不落盘。
    """
    if status not in ("success", "failed"):
        raise ValueError(f"不写采集 marker 的状态: {status!r}")
    run_text = str(run_id if isinstance(run_id, UUID) else UUID(str(run_id)))
    moment = moment or shanghai_now()
    directory.mkdir(parents=True, exist_ok=True)
    clear_collection_markers(directory)
    name = f".coll_run_id_{run_text}_{marker_timestamp(moment)}_{status}"
    path = directory / name
    path.touch()
    return CollectionMarker(
        run_id=run_text,
        status=status,
        finished_at=moment,
        file_name=name,
    )
