"""db 当前态画像的读取与校验（doc 6.1 §14；提交由发布服务完成）。"""

import json
import logging
from datetime import datetime
from pathlib import Path
from uuid import UUID

from sqlalchemy import select

from backend.data_sources.repository import get_db_source
from backend.persistence.models import DbProfile
from metaweave.core.clean_targets import resolve_db_output_dir

logger = logging.getLogger("backend.profile_loading")

DB_STEP = "db"


class MetaLoadConfigError(ValueError):
    """参数或工作区归属错误。"""


class MetaLoadSourceNotFoundError(MetaLoadConfigError):
    """指定 source_id 在平台库中不存在。"""


class MetaLoadSourceDisabledError(MetaLoadConfigError):
    """数据源已禁用，不能加载或发布画像。"""


class ProfileInputError(ValueError):
    """database.json 不满足 db_profile 的输入契约。"""


_COUNT_KEYS = (
    "ordinary_table_count",
    "partitioned_table_count",
    "partition_child_count",
    "view_count",
    "materialized_view_count",
    "object_count",
)


def _require_object(value, name: str) -> dict:
    if not isinstance(value, dict):
        raise ProfileInputError(f"{name} 必须是对象")
    return value


def _require_nonnegative_int(value, name: str) -> int:
    if type(value) is not int or value < 0:
        raise ProfileInputError(f"{name} 必须是非负整数")
    return value


def _require_text(value, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProfileInputError(f"{name} 必须是非空字符串")
    return value


def _optional_text(value, name: str) -> str | None:
    """缺失或 null → None；否则必须是非空字符串。"""
    if value is None:
        return None
    return _require_text(value, name)


def _profile_fields(document: object, source_id: str) -> dict:
    """只从已校验产物提取 db_profile 字段，不读取当前源库登记值。"""
    from metaweave.utils.source_id import normalize_source_id

    root = _require_object(document, "database.json")
    if type(root.get("schema_version")) is not int or root["schema_version"] != 1:
        raise ProfileInputError("不支持的 database.json schema_version")
    try:
        file_source = normalize_source_id(root.get("source_id"))
    except ValueError:
        raise ProfileInputError("database.json 的 source_id 非法") from None
    if file_source != source_id:
        raise ProfileInputError("database.json 的 source_id 与本次数据源不一致")

    collected_text = _require_text(root.get("collected_at"), "collected_at")
    try:
        collected_at = datetime.fromisoformat(collected_text.replace("Z", "+00:00"))
    except ValueError:
        raise ProfileInputError("collected_at 不是有效时间") from None
    if collected_at.tzinfo is None or collected_at.utcoffset() is None:
        raise ProfileInputError("collected_at 必须包含时区")

    database = _require_object(root.get("database"), "database")
    db_type = _require_text(database.get("type"), "database.type")
    if db_type != "postgresql":
        raise ProfileInputError("database.type 目前只支持 postgresql")
    host = _require_text(database.get("host"), "database.host")
    db_name = _require_text(database.get("name"), "database.name")
    port = database.get("port")
    if type(port) is not int or not 1 <= port <= 65535:
        raise ProfileInputError("database.port 必须是 1–65535 的整数")
    server_version = database.get("server_version")
    if server_version is not None and not isinstance(server_version, str):
        raise ProfileInputError("database.server_version 必须是字符串或 null")
    server_version_full = _optional_text(
        database.get("server_version_full"), "database.server_version_full"
    )
    os_platform = _optional_text(database.get("os_platform"), "database.os_platform")
    size_bytes = _require_nonnegative_int(
        database.get("size_bytes"), "database.size_bytes"
    )
    if database.get("size_scope") != "whole_database":
        raise ProfileInputError("database.size_scope 必须是 whole_database")
    record_count = database.get("record_count")
    if record_count is not None:
        record_count = _require_nonnegative_int(record_count, "database.record_count")

    selected_schemas = root.get("selected_schemas")
    if (
        not isinstance(selected_schemas, list)
        or not selected_schemas
        or any(
            not isinstance(name, str) or not name.strip() for name in selected_schemas
        )
        or len(selected_schemas) != len(set(selected_schemas))
    ):
        raise ProfileInputError("selected_schemas 必须是非空、不重复的名称数组")
    schemas = root.get("schemas")
    if not isinstance(schemas, list) or len(schemas) != len(selected_schemas):
        raise ProfileInputError("schemas 必须与 selected_schemas 一一对应")
    schema_names = []
    summed_counts = dict.fromkeys(_COUNT_KEYS, 0)
    for index, raw_schema in enumerate(schemas):
        schema = _require_object(raw_schema, f"schemas[{index}]")
        schema_names.append(_require_text(schema.get("name"), f"schemas[{index}].name"))
        counts = {
            key: _require_nonnegative_int(schema.get(key), f"schemas[{index}].{key}")
            for key in _COUNT_KEYS
        }
        if counts["object_count"] != sum(
            counts[key] for key in _COUNT_KEYS[:5] if key != "partition_child_count"
        ):
            raise ProfileInputError(f"schemas[{index}].object_count 与分类计数不一致")
        for key in _COUNT_KEYS:
            summed_counts[key] += counts[key]
    if set(schema_names) != set(selected_schemas) or len(schema_names) != len(
        set(schema_names)
    ):
        raise ProfileInputError("schemas 名称与 selected_schemas 不一致")

    totals = _require_object(root.get("totals"), "totals")
    total_counts = {
        key: _require_nonnegative_int(totals.get(key), f"totals.{key}")
        for key in _COUNT_KEYS
    }
    if total_counts != summed_counts:
        raise ProfileInputError("totals 与各 schema 分类计数之和不一致")

    return {
        "source_id": source_id,
        "schema_version": 1,
        "collected_at": collected_at,
        "db_type": db_type,
        "host": host,
        "port": port,
        "db_name": db_name,
        "server_version": server_version,
        "server_version_full": server_version_full,
        "os_platform": os_platform,
        "size_bytes": size_bytes,
        "size_scope": "whole_database",
        "record_count": record_count,
        "selected_schemas": selected_schemas,
        "schemas": schemas,
        **total_counts,
    }


def require_workspace_source(session, workspace_id: str, source_id: str):
    """校验工作区存在且归属该数据源，并返回 (工作区, 数据源) 行。"""
    from backend.workspaces.repository import get_workspace

    workspace = get_workspace(session, workspace_id)
    if workspace is None:
        raise MetaLoadConfigError("工作区不存在")
    if workspace.source_id != source_id:
        raise MetaLoadConfigError("工作区不属于该数据源")
    source = get_db_source(session, source_id)
    if source is None:
        raise MetaLoadSourceNotFoundError("未找到指定 source_id 的数据源")
    if not source.is_active:
        raise MetaLoadSourceDisabledError("指定数据源已禁用，不能加载画像")
    return workspace, source


def resolve_db_profile_input_path(source_id: str, workspace_id: str) -> Path:
    """定位 database.json，并确认它是普通、非符号链接文件。"""
    try:
        target_dir = resolve_db_output_dir(source_id, workspace_id)
        input_path = target_dir / "database.json"
        if input_path.is_symlink() or not input_path.is_file():
            raise ProfileInputError(f"未找到可加载的普通文件: {input_path}")
        return input_path
    except ProfileInputError:
        raise
    except (ValueError, OSError) as exc:
        message = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
        raise ProfileInputError(message) from None


def read_db_profile_input(source_id: str, workspace_id: str) -> tuple[dict, Path]:
    """读取并校验画像；返回字段和 db 目录。不查平台库、不入库。"""
    input_path = resolve_db_profile_input_path(source_id, workspace_id)
    document = json.loads(input_path.read_text(encoding="utf-8"))
    fields = _profile_fields(document, source_id)
    return fields, input_path.parent


def replace_db_profile(session, publish_run_id: UUID, fields: dict) -> DbProfile:
    """整行替换该数据源的当前画像（doc 6.1 §14：主键 source_id，每源一行）。

    实现为事务内 DELETE + INSERT：先删除旧行并 flush，再插入新行（publish_run_id
    为本次发布运行，doc 6.1 §10.1）。只改会话不提交；调用方事务回滚则旧行不变。
    """
    stmt = select(DbProfile).where(DbProfile.source_id == fields["source_id"])
    existing_rows = list(session.scalars(stmt))
    for existing in existing_rows:
        session.delete(existing)
    session.flush()
    row = DbProfile(publish_run_id=publish_run_id, **fields)
    session.add(row)
    return row
