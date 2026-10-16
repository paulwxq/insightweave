"""doc 22.2 §5：dim_value 配置编辑 API（读取/校验/原子写回）与发布拦截共享读取函数。

- GET/PUT 四端点的服务函数（§5.1/§5.2）；
- `classify_dim_tables_yaml`：三态判定（无效/零表/有表，§5.3），发布拦截（§6.2）与
  GET 共用同一宽容口径——`databases: {}` 与 `databases.<db>.tables: {}` 统一归零表态；
- 异常在 `backend/api/errors.py` 注册 handler（审核修订 36、49）。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from uuid import UUID

import yaml

from backend.data_sources.repository import get_db_source
from backend.profile_loading.file_utils import read_regular_text
from backend.jobs.repository import get_step_row
from backend.persistence.platform_db import PlatformSettings, platform_session
from metaweave.core.clean_targets import (
    resolve_dim_config_output_dir,
    resolve_json_output_dir,
)
from metaweave.core.dim_value.models import DimTableConfig, DimTablesConfig
from metaweave.utils.source_id import normalize_source_id

DIM_CONFIG_ARTIFACT_NAME = "dim_tables.yaml"
DIM_CONFIG_STEP_NAME = "dim_config"
JSON_MANIFEST_NAME = "manifest.json"

_WRITE_HEADER = (
    "# 维度表加载配置\n"
    "# 说明：此文件由 Web dim_value 配置编辑保存（doc 22.2）；Web 编辑仅支持单列 embedding_col，\n"
    "# 手工写入的多列值（列表/逗号串）在读取时视为未指定，保存后收敛为单列字符串。\n"
    "#\n"
)


class DimConfigOutputMissingError(Exception):
    """404 dim_config_output_missing：产物文件不存在或 dim_config 步骤行缺失。"""

    def __init__(self, message: str = "dim_config 产物或步骤记录不存在"):
        super().__init__(message)
        self.details: Dict[str, Any] = {}


class DimValueConfigInvalidError(ValueError):
    """400 dim_value_config_invalid：配置文件无效（GET 读取与发布拦截共用，审核修订 27）。"""

    def __init__(self, message: str, reason: str):
        super().__init__(message)
        self.details = {"reason": reason}


class DimValueEmptyTablesError(ValueError):
    """400 dim_value_empty_tables：发布拦截零表态（§6.2）。"""

    def __init__(
        self,
        message: str = "dim_value 未配置任何表：请为 dim_value 添加表并指定值向量化字段，或取消勾选 dim_value",
    ):
        super().__init__(message)
        self.details: Dict[str, Any] = {}


class DimValueNotConfiguredError(ValueError):
    """400 dim_value_not_configured：存在未指定 embedding_col 的表（§6.2）。"""

    def __init__(self, tables: List[str]):
        super().__init__(
            "dim_tables.yaml 中存在未指定 embedding_col 的表，请编辑配置或删除对应表"
        )
        self.details = {"tables": tables}


class DimValueColumnMissingError(ValueError):
    """400 dim_value_column_missing：PUT 存在空 embedding_col（§5.2）。"""

    def __init__(self, tables: List[str]):
        super().__init__("存在未指定值向量化字段的表，请为每张表指定 embedding_col")
        self.details = {"tables": tables}


class DimConfigRegeneratedError(Exception):
    """409 dim_config_regenerated：PUT 版本复查不匹配（§5.2）。"""

    def __init__(
        self, message: str = "配置已被重新生成（作业可能已重跑），请刷新后重新编辑"
    ):
        super().__init__(message)
        self.details: Dict[str, Any] = {}


# ---- 共享读取（O_NOFOLLOW 加固，domain_loader 同款口径） ----


def _normalize_embedding_col(value: Any) -> Optional[str]:
    """§5.3 读取口径：非单列字符串（None/空串/列表/含逗号）一律归 None。"""

    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or "," in text:
        return None
    return text


def classify_dim_tables_yaml(
    yaml_path: Path, db_name: str
) -> Tuple[str, Optional[str], Optional[DimTablesConfig]]:
    """发布拦截三态判定（§5.3）：返回 (state, reason, config)。

    state ∈ {"invalid", "empty", "tables"}；invalid 时 config=None，
    empty/config 时返回归一后的 DimTablesConfig（每表 embedding_col 为单列字符串或 None）。
    文件不存在/不可读归 invalid（审核修订 27：与手工损坏同态）。
    """

    status, content = read_regular_text(yaml_path, DIM_CONFIG_ARTIFACT_NAME)
    if status == "missing":
        return "invalid", "dim_tables.yaml 文件不存在", None
    if status == "invalid":
        return "invalid", content, None

    if not content.strip():
        return "invalid", "dim_tables.yaml 为空白文件", None
    try:
        data = yaml.safe_load(content)
    except yaml.YAMLError as exc:
        return "invalid", f"YAML 无法解析: {exc}", None
    if not isinstance(data, dict):
        return "invalid", "YAML 顶层不是对象（mapping）", None
    databases = data.get("databases")
    if not isinstance(databases, dict):
        return "invalid", "缺少 databases 配置或格式错误", None

    db_cfg = databases.get(db_name)
    if not isinstance(db_cfg, dict):
        # 采集原生 databases: {}（无本库键）与他库键形态均归零表态（审核修订 3/33）
        return "empty", None, DimTablesConfig(database=db_name, tables={})

    tables_cfg = db_cfg.get("tables", {})
    if not isinstance(tables_cfg, dict):
        tables_cfg = {}

    tables: Dict[str, DimTableConfig] = {}
    for full_name, cfg in tables_cfg.items():
        if not isinstance(full_name, str) or "." not in full_name:
            continue  # 与 from_yaml 一致忽略非法表名
        schema, table = full_name.split(".", 1)
        embedding_col = _normalize_embedding_col(
            cfg.get("embedding_col") if isinstance(cfg, dict) else None
        )
        tables[full_name] = DimTableConfig(
            schema=schema, table=table, embedding_col=embedding_col
        )

    if not tables:
        return "empty", None, DimTablesConfig(database=db_name, tables={})
    return "tables", None, DimTablesConfig(database=db_name, tables=tables)


# ---- 平台库读取 ----


def _resolve_identifiers(source_id, job_id) -> Tuple[UUID, UUID]:
    return normalize_source_id(source_id), normalize_source_id(job_id)


def _require_step_row(session, job_uuid: UUID):
    row = get_step_row(session, job_uuid, DIM_CONFIG_STEP_NAME)
    if row is None:
        raise DimConfigOutputMissingError()
    return row


def _require_registration(session, source_uuid: UUID):
    source = get_db_source(session, source_uuid)
    if source is None:
        raise DimConfigOutputMissingError("数据源不存在")
    return source


# ---- GET dim-config（§5.1） ----


def read_dim_value_config(
    settings: PlatformSettings, source_id, job_id
) -> Dict[str, Any]:
    source_uuid, job_uuid = _resolve_identifiers(source_id, job_id)
    with platform_session(settings) as session:
        step_row = _require_step_row(session, job_uuid)
        source = _require_registration(session, source_uuid)

        yaml_path = (
            resolve_dim_config_output_dir(source_uuid, job_uuid)
            / DIM_CONFIG_ARTIFACT_NAME
        )
        status, content = read_regular_text(yaml_path, DIM_CONFIG_ARTIFACT_NAME)
        if status == "missing":
            raise DimConfigOutputMissingError()
        if status == "invalid":
            raise DimValueConfigInvalidError(
                "dim_tables.yaml 配置文件无效或不存在，请重新生成后再发布", content
            )

        state, reason, cfg = classify_dim_tables_yaml(yaml_path, source.db_name)
        if state == "invalid":
            raise DimValueConfigInvalidError(
                "dim_tables.yaml 配置文件无效或不存在，请重新生成后再发布",
                reason or "配置文件无效",
            )

        tables = [
            {
                "schema_name": c.schema,
                "object_name": c.table,
                "embedding_col": c.embedding_col,
            }
            for c in cfg.tables.values()
        ]
        return {"tables": tables, "version": step_row.updated_at.isoformat()}


# ---- GET objects（§5.1） ----


def _iter_json_products(json_dir: Path):
    """枚举受管 json 产物（排除 manifest.json，仅普通文件），逐个产出 (path, object_info)。"""

    if not json_dir.is_dir():
        return
    for path in sorted(json_dir.glob("*.json")):
        if path.name == JSON_MANIFEST_NAME or path.is_symlink() or not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        info = data.get("object_info") if isinstance(data, dict) else None
        if isinstance(info, dict):
            yield path, info


def list_dim_value_objects(
    settings: PlatformSettings, source_id, job_id
) -> Dict[str, Any]:
    source_uuid, job_uuid = _resolve_identifiers(source_id, job_id)
    with platform_session(settings) as session:
        _require_step_row(session, job_uuid)
        json_dir = resolve_json_output_dir(source_uuid, job_uuid)
        if not json_dir.is_dir():
            raise DimConfigOutputMissingError("受管 json 产物目录不存在")

        objects = []
        for _, info in _iter_json_products(json_dir):
            schema_name = info.get("schema_name")
            object_name = info.get("object_name")
            object_type = info.get("object_type")
            if not isinstance(schema_name, str) or not isinstance(object_name, str):
                continue
            if "." in schema_name:
                # 审核修订 18：schema 含点全链不可表达，候选即排除
                continue
            objects.append(
                {
                    "schema_name": schema_name,
                    "object_name": object_name,
                    "object_type": (
                        object_type if isinstance(object_type, str) else "table"
                    ),
                }
            )
        return {"objects": objects}


# ---- GET object-columns（§5.1） ----


def _format_sample_cell(value: Any) -> str:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _first_sample_value(records: Any, column_name: str) -> str:
    if not isinstance(records, list):
        return ""
    for record in records:
        if not isinstance(record, dict) or column_name not in record:
            continue
        value = record[column_name]
        if value is None:
            continue
        return _format_sample_cell(value)
    return ""


def read_dim_value_object_columns(
    settings: PlatformSettings, source_id, job_id, schema_name: str, object_name: str
) -> Dict[str, Any]:
    source_uuid, job_uuid = _resolve_identifiers(source_id, job_id)
    with platform_session(settings) as session:
        _require_step_row(session, job_uuid)
        json_dir = resolve_json_output_dir(source_uuid, job_uuid)

        data = None
        for path, info in _iter_json_products(json_dir):
            if (
                info.get("schema_name") == schema_name
                and info.get("object_name") == object_name
            ):
                data = json.loads(path.read_text(encoding="utf-8"))
                break
        if data is None:
            raise DimConfigOutputMissingError(
                f"产物中不存在对象 {schema_name}.{object_name}"
            )

        sample_records: List[Any] = []
        raw_samples = data.get("sample_records")
        if isinstance(raw_samples, dict) and isinstance(
            raw_samples.get("records"), list
        ):
            sample_records = raw_samples["records"]

        columns: List[Dict[str, str]] = []
        raw_profiles = data.get("column_profiles")
        if isinstance(raw_profiles, dict):
            profiles = [
                (name, prof)
                for name, prof in raw_profiles.items()
                if isinstance(prof, dict)
            ]
            profiles.sort(key=lambda item: item[1].get("ordinal_position", 0))
            columns = [
                {
                    "name": name,
                    "data_type": (
                        prof.get("data_type")
                        if isinstance(prof.get("data_type"), str)
                        else ""
                    ),
                    "comment": (
                        prof.get("comment")
                        if isinstance(prof.get("comment"), str)
                        else ""
                    ),
                    "sample_value": _first_sample_value(sample_records, name),
                }
                for name, prof in profiles
            ]
        return {"columns": columns}


# ---- PUT（§5.2） ----


def _validate_payload(payload: Any) -> List[Dict[str, str]]:
    """结构校验（第 1 步）与 embedding_col 强校验（第 2 步），返回规范化表清单。"""

    if not isinstance(payload, dict):
        raise DimValueConfigInvalidError(
            "请求体必须是对象（mapping）", "payload 不是对象"
        )
    tables = payload.get("tables")
    if not isinstance(tables, list):
        raise DimValueConfigInvalidError("tables 必须是列表", "tables 不是列表")

    seen = set()
    missing: List[str] = []
    normalized: List[Dict[str, str]] = []
    for item in tables:
        if not isinstance(item, dict):
            raise DimValueConfigInvalidError(
                "tables 每项必须是对象", "tables 项不是对象"
            )
        schema_name = item.get("schema_name")
        object_name = item.get("object_name")
        embedding_col = item.get("embedding_col")
        if not isinstance(schema_name, str) or not schema_name.strip():
            raise DimValueConfigInvalidError(
                "schema_name 必须是非空字符串", "schema_name 缺失或为空"
            )
        if "." in schema_name:
            raise DimValueConfigInvalidError(
                f"schema 名不支持含点: {schema_name}",
                "schema 名含点（yaml 键格式限制）",
            )
        if not isinstance(object_name, str) or not object_name.strip():
            raise DimValueConfigInvalidError(
                "object_name 必须是非空字符串", "object_name 缺失或为空"
            )
        full_name = f"{schema_name}.{object_name}"
        if full_name in seen:
            raise DimValueConfigInvalidError(f"表重复: {full_name}", "表清单存在重复键")
        seen.add(full_name)

        if not isinstance(embedding_col, str) or not embedding_col.strip():
            missing.append(full_name)
            continue
        if "," in embedding_col:
            raise DimValueConfigInvalidError(
                f"embedding_col 仅支持单列: {full_name}",
                "embedding_col 含逗号（多列形态）",
            )
        normalized.append(
            {
                "schema_name": schema_name,
                "object_name": object_name,
                "embedding_col": embedding_col,
            }
        )

    if missing:
        raise DimValueColumnMissingError(sorted(missing))
    return normalized


def write_dim_value_config(
    settings: PlatformSettings, source_id, job_id, payload: Dict[str, Any]
) -> Dict[str, Any]:
    source_uuid, job_uuid = _resolve_identifiers(source_id, job_id)
    normalized = _validate_payload(payload)

    with platform_session(settings) as session:
        step_row = _require_step_row(session, job_uuid)
        source = _require_registration(session, source_uuid)

        # 版本复查（第 3 步）
        version = payload.get("version")
        if version != step_row.updated_at.isoformat():
            raise DimConfigRegeneratedError()

        # 写回（第 4 步）：与生成器同款 yaml.dump（审核修订 45，不手写引号）
        dim_config_dir = resolve_dim_config_output_dir(source_uuid, job_uuid)
        yaml_path = dim_config_dir / DIM_CONFIG_ARTIFACT_NAME
        config_dict = {
            "databases": {
                source.db_name: {
                    "tables": {
                        f"{t['schema_name']}.{t['object_name']}": {
                            "embedding_col": t["embedding_col"]
                        }
                        for t in sorted(
                            normalized,
                            key=lambda t: f"{t['schema_name']}.{t['object_name']}",
                        )
                    }
                }
            }
        }
        body = yaml.dump(
            config_dict, allow_unicode=True, default_flow_style=False, sort_keys=False
        )
        content = _WRITE_HEADER + body

        fd, tmp_name = _make_temp_file(dim_config_dir)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(content)
            os.chmod(tmp_name, 0o644)
            # 核验可解析后原子替换（dim_config_step_service 同款加固）
            yaml.safe_load(tmp_name.read_text(encoding="utf-8"))
            os.replace(tmp_name, yaml_path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

        return {
            "tables": [
                {
                    "schema_name": t["schema_name"],
                    "object_name": t["object_name"],
                    "embedding_col": t["embedding_col"],
                }
                for t in normalized
            ]
        }


def _make_temp_file(directory: Path) -> Tuple[int, Path]:
    import tempfile

    directory.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(
        prefix=".dim_tables.", suffix=".tmp", dir=str(directory)
    )
    return fd, Path(name)
