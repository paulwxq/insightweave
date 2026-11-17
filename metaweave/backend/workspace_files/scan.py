"""普通产物扫描：递归忽略点号开头的文件和目录（doc 6.1 §11.4）。

marker、`.backup/`、临时文件一律不进入普通扫描与 manifest；
marker 由 `workspace_files.markers` 的专门逻辑读取。
"""

from collections.abc import Iterator
from pathlib import Path


def is_hidden(name: str) -> bool:
    return name.startswith(".")


def iter_visible_children(directory: Path) -> Iterator[Path]:
    """目录第一层可见子项（跳过点号名称）。"""
    for child in sorted(directory.iterdir(), key=lambda item: item.name):
        if is_hidden(child.name):
            continue
        yield child


def iter_visible_paths(directory: Path) -> Iterator[Path]:
    """递归枚举可见文件与目录（每一级都跳过点号名称）。"""
    if not directory.is_dir():
        return
    for child in iter_visible_children(directory):
        yield child
        if child.is_dir() and not child.is_symlink():
            yield from iter_visible_paths(child)


def list_visible_files(
    directory: Path, *, relative_to: Path | None = None
) -> list[str]:
    """递归列出可见普通文件的相对路径（POSIX 分隔、排序）。

    `relative_to` 默认为 `directory` 本身。符号链接即使指向文件也不列出
    （受管产物不接受链接），目录链接不深入。
    """
    base = (relative_to or directory).resolve()
    results: list[str] = []
    for path in iter_visible_paths(directory):
        if path.is_symlink() or not path.is_file():
            continue
        results.append(path.resolve().relative_to(base).as_posix())
    return sorted(results)
