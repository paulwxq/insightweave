"""DDL 当前态定义的读取与校验（doc 16 §5、6.1 §15）。

只负责把当前工作区受管目录中的 `.sql` 产物完整读取并验证为整批
`table_definition` 行，供 Web 发布事务使用；不自行提交，不写平台库。
"""

import json
import logging
import re
from pathlib import Path

from metaweave.core.clean_targets import resolve_ddl_output_dir
from metaweave.core.metadata.ddl_loader import DDLLoader, DDLLoaderError

logger = logging.getLogger("backend.profile_loading")

DDL_STEP = "ddl"
DEFINE_TYPE_DDL = "ddl"
# 受管 DDL 目录内的采集清单文件名（doc 16 §4/§5），与采集侧
# backend.data_sources.ddl_step_service 的同名常量必须一致（测试锁定）。
DDL_MANIFEST_NAME = "manifest.json"
_SUPPORTED_OBJECT_TYPES = {"table", "view", "materialized_view"}
_DATABASE_HEADER = re.compile(r"^--\s*Database:\s*(?P<name>.*?)\s*$")
_HEADER_SCAN_LINES = 10


class DDLInputError(ValueError):
    """DDL 产物不满足 table_definition 的发布输入契约。"""


def check_ddl_output_ready(source_id: str, workspace_id: str) -> bool:
    """预检：受管 DDL 目录可读、已有 `.sql` 文件且与采集清单一致。

    只判断文件就绪，不解析内容；清单核对失败时记 WARNING（含原因类别），
    页面仍只显示固定的不可发布原因。
    """
    try:
        ddl_dir = resolve_ddl_output_dir(source_id, workspace_id)
        if not ddl_dir.is_dir():
            return False
        names = {path.name for path in ddl_dir.glob("*.sql")}
        if not names:
            return False
        _verify_manifest(ddl_dir, names)
        return True
    except DDLInputError as exc:
        logger.warning(
            "DDL 预检清单核对未通过 source_id=%s workspace_id=%s reason=%s",
            source_id,
            workspace_id,
            str(exc),
        )
        return False
    except (ValueError, OSError) as exc:
        logger.warning(
            "DDL 预检产物目录不可用 source_id=%s workspace_id=%s type=%s",
            source_id,
            workspace_id,
            type(exc).__name__,
        )
        return False


def _verify_manifest(ddl_dir: Path, actual_names: set[str]) -> None:
    """采集清单与目录现状核对：缺件、多出或清单不可用都拒绝（doc 16 §5）。

    目录只反映"现存"文件；采集成功后缺一份文件时，若无清单凭据，
    剩余文件会被当作完整集合发布，无提示缩小当前表定义。
    """
    manifest_path = ddl_dir / DDL_MANIFEST_NAME
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise DDLInputError("DDL 采集清单缺失")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        raise DDLInputError("DDL 采集清单不可读") from None
    files_field = manifest.get("files") if isinstance(manifest, dict) else None
    if (
        not isinstance(files_field, list)
        or not files_field
        or any(not isinstance(name, str) or not name for name in files_field)
    ):
        raise DDLInputError("DDL 采集清单内容无效")
    listed = set(files_field)
    missing = sorted(listed - actual_names)
    if missing:
        raise DDLInputError(f"DDL 文件缺失: {', '.join(missing)}")
    extra = sorted(actual_names - listed)
    if extra:
        raise DDLInputError(f"存在采集清单之外的 DDL 文件: {', '.join(extra)}")


def _header_database(content: str, file_name: str) -> str:
    """从文件头注释块取 `-- Database:` 库名；缺失或多个都拒绝。"""
    matches = []
    for line in content.splitlines()[:_HEADER_SCAN_LINES]:
        if not line.startswith("--"):
            break
        match = _DATABASE_HEADER.match(line)
        if match and match.group("name"):
            matches.append(match.group("name"))
    if len(matches) != 1:
        raise DDLInputError(f"DDL 文件缺少唯一的 Database 头: {file_name}")
    return matches[0]


def read_ddl_definitions(source_id: str, workspace_id: str) -> tuple[list[dict], Path]:
    """完整读取并校验本次作业的全部 DDL 文件，形成整批行。

    先核对目录现状与采集清单（manifest.json）完全一致：缺件、多出或
    清单不可用都整批拒绝（doc 16 §5）。逐文件要求普通、非符号链接、
    UTF-8、非空、严格三段式文件名，且
    `-- Database:` 头与文件名一致；复用 `DDLLoader` 解析身份并与文件名
    比对，拒绝重复对象、混库与解析失败。解析身份与保存正文来自**同一
    次读取**，避免两次读取之间文件被并发重写造成身份与正文错位。任一
    文件失败都抛 `DDLInputError`，不返回部分结果。日志只写文件名、
    对象名与错误类别，不写 DDL 原文或样例值（doc 16 §5）。
    """
    ddl_dir = resolve_ddl_output_dir(source_id, workspace_id)
    if not ddl_dir.is_dir():
        raise DDLInputError(f"DDL 目录不存在: {ddl_dir}")
    files = sorted(path for path in ddl_dir.glob("*.sql"))
    if not files:
        raise DDLInputError(f"未找到可发布的 DDL 文件: {ddl_dir}")

    # 先做不读内容的检查：符号链接与非普通文件在读内容之前拒绝，
    # 否则链接目标（可能在受管目录之外）的内容会先被 load_all 读进
    # 内存，坏内容还会先报"解析失败"而不是"不是普通文件"。
    for path in files:
        if path.is_symlink() or not path.is_file():
            raise DDLInputError(f"DDL 文件不是普通文件: {path.name}")
        parts = path.stem.split(".")
        if len(parts) != 3 or not all(part.strip() for part in parts):
            raise DDLInputError(f"DDL 文件名不符合严格三段式: {path.name}")

    # 目录现状必须与采集清单完全一致（doc 16 §5）：缺一份文件时剩余
    # 文件不能被当作完整集合发布。
    _verify_manifest(ddl_dir, {path.name for path in files})

    try:
        loader = DDLLoader(ddl_dir)
    except DDLLoaderError as exc:
        raise DDLInputError(f"DDL 目录不可用: {exc}") from None

    rows: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    for path in files:
        file_db, schema_name, object_name = path.stem.split(".")
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            raise DDLInputError(f"DDL 文件不是有效 UTF-8: {path.name}") from None
        if not content.strip():
            raise DDLInputError(f"DDL 文件为空: {path.name}")
        if _header_database(content, path.name) != file_db:
            raise DDLInputError(f"DDL 文件头 Database 与文件名不一致: {path.name}")

        try:
            parsed = loader.parse_content(content, path)
        except DDLLoaderError as exc:
            raise DDLInputError(f"DDL 解析失败: {path.name}: {exc}") from None
        meta = parsed.metadata
        if (
            meta.schema_name.lower() != schema_name.lower()
            or meta.table_name.lower() != object_name.lower()
        ):
            raise DDLInputError(f"DDL 解析身份与文件名不一致: {path.name}")
        object_type = (meta.object_type or "table").lower()
        if object_type not in _SUPPORTED_OBJECT_TYPES:
            raise DDLInputError(f"DDL 对象类型不受支持: {path.name}")
        key = (file_db, schema_name, object_name)
        if key in seen:
            raise DDLInputError(f"DDL 对象重复: {schema_name}.{object_name}")
        seen.add(key)
        rows.append(
            {
                "source_id": source_id,
                "schema_name": schema_name,
                "object_name": object_name,
                "define_type": DEFINE_TYPE_DDL,
                "db_name": file_db,
                "object_type": object_type,
                "define_text": content,
            }
        )
    if len({row["db_name"] for row in rows}) != 1:
        raise DDLInputError("DDL 文件混入多个数据库")
    return rows, ddl_dir
