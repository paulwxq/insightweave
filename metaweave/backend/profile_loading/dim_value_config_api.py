"""doc 22.2 §5：dim_value 配置编辑 API（读取/校验/原子写回）与发布拦截共享读取函数。

- GET/PUT 四端点的服务函数（§5.1/§5.2）；
- `classify_dim_tables_yaml`：三态判定（无效/零表/有表，§5.3），发布拦截（§6.2）与
  GET 共用同一宽容口径——`databases: {}` 与 `databases.<db>.tables: {}` 统一归零表态；
- 异常在 `backend/api/errors.py` 注册 handler（审核修订 36、49）。
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

from backend.data_sources.repository import get_db_source
from backend.profile_loading.file_utils import read_regular_text

from backend.persistence.platform_db import PlatformSettings, platform_session
from metaweave.core.clean_targets import (
    resolve_dim_output_dir,
    resolve_json_output_dir,
)
from metaweave.core.dim_value.models import DimTableConfig, DimTablesConfig
from metaweave.core.dim_value.recommendation import (
    validate_dimension_value_recommendation,
)
from metaweave.utils.source_id import normalize_workspace_id

logger = logging.getLogger(__name__)

DIM_CONFIG_ARTIFACT_NAME = "dim_tables.yaml"
DIM_CONFIG_STEP_NAME = "dim_config"
JSON_MANIFEST_NAME = "manifest.json"

_WRITE_HEADER = (
    "# 维度表加载配置\n"
    "# 说明：此文件由 Web dim_value 配置编辑保存（doc 22.2；doc 7.2 起人工可为每表配置\n"
    "# 一个或多个 embedding_col 字段，写作单列字符串或字段名列表均可）。\n"
    "# 含逗号的字符串读取时视为未指定（多字段请改用列表），空值视为未配置。\n"
    "# 生成时的模型推荐及置信度注释未随保存保留，请在配置弹窗查看（doc 7.1 §4.4）。\n"
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


class DimConfigBackupFailedError(Exception):
    """500 dim_config_backup_failed：文件级备份失败，本次保存终止。"""

    def __init__(self, message: str = "dim_tables.yaml 备份失败，本次保存未执行"):
        super().__init__(message)
        self.details: Dict[str, Any] = {}


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


def _normalize_embedding_col(value: Any) -> Any:
    """读取口径（doc 7.2 §3.1.1）：单列字符串与字段名列表都合法，原样保留。

    - str：去空白后非空且不含逗号 → 保留该字符串；含逗号或空串 → None；
    - list：元素全为去空白后非空、不含逗号的字符串 → 按首次出现顺序去重
      后保留列表；任一元素非法（非字符串/空/含逗号）→ 整体归 None；
    - 其他形态（None/数字/对象等）→ None（未指定）。
    宽容读取不拆逗号、不截取首项，也不把列表归为未配置。
    """

    if isinstance(value, str):
        text = value.strip()
        if not text or "," in text:
            return None
        return text
    if isinstance(value, list):
        columns: List[str] = []
        for item in value:
            if not isinstance(item, str):
                return None
            text = item.strip()
            if not text or "," in text:
                return None
            if text not in columns:
                columns.append(text)
        return columns or None
    return None


def _comma_string_tables(data: Any, db_name: str) -> List[str]:
    """收集 embedding_col 为含逗号字符串的表全名（读取归一化将其归为未指定，
    doc 7.2 §3.1.1）。供文件 GET 向显式初始化页返回独立警告：提示改用字段
    列表格式；不拆分、不截取，也不影响归一化结果本身。"""

    if not isinstance(data, dict):
        return []
    databases = data.get("databases")
    if not isinstance(databases, dict):
        return []
    db_cfg = databases.get(db_name)
    if not isinstance(db_cfg, dict):
        return []
    tables_cfg = db_cfg.get("tables", {})
    if not isinstance(tables_cfg, dict):
        return []
    names = []
    for full_name, cfg in tables_cfg.items():
        if not isinstance(full_name, str) or not isinstance(cfg, dict):
            continue
        value = cfg.get("embedding_col")
        if isinstance(value, str) and "," in value:
            names.append(full_name)
    return sorted(names)


def classify_dim_tables_yaml(
    yaml_path: Path, db_name: str
) -> Tuple[str, Optional[str], Optional[DimTablesConfig]]:
    """发布拦截三态判定（§5.3）：返回 (state, reason, config)。

    state ∈ {"invalid", "empty", "tables"}；invalid 时 config=None，
    empty/config 时返回归一后的 DimTablesConfig（每表 embedding_col 为字符串、
    字符串列表或 None，doc 7.2 §3.1.1）。
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


def _require_workspace(session, workspace_id: str):
    """工作区归属核对 + dim 目录成功 marker（doc 6 §4.1）。"""
    from backend.workspaces.repository import get_workspace
    from backend.workspace_files.markers import read_collection_marker

    workspace = get_workspace(session, workspace_id)
    if workspace is None:
        raise DimConfigOutputMissingError("工作区不存在")
    marker, error = read_collection_marker(
        resolve_dim_output_dir(workspace.source_id, workspace_id)
    )
    if marker is None:
        raise DimConfigOutputMissingError(
            error or "该工作区没有 Dim 采集 marker，请先执行 Dim 采集"
        )
    if marker.status != "success":
        raise DimConfigOutputMissingError("该工作区的 Dim 采集未成功，请先重跑后再编辑")
    return workspace


def _require_registration(session, source_id: str):
    source = get_db_source(session, source_id)
    if source is None:
        raise DimConfigOutputMissingError("数据源不存在")
    return source


# ---- GET dim-config（§5.1） ----


def _load_recommendations(json_dir: Path) -> Dict[Tuple[str, str], Dict[str, Any]]:
    """读取同工作区画像的维度值字段推荐（doc 7.1 §4.4）。

    仅返回画像可读且经 §5.2 校验后的推荐；目录缺失或画像不可读的表
    不进入映射，调用方据此展示"推荐信息不可用"。任何失败都只降级，
    不影响配置本身的读取。
    """
    recommendations: Dict[Tuple[str, str], Dict[str, Any]] = {}
    if not json_dir.is_dir():
        logger.warning("读取维度值推荐：json 目录不存在: %s", json_dir)
        return recommendations
    for path, info in _iter_json_products(json_dir):
        schema_name = info.get("schema_name")
        object_name = info.get("object_name")
        if not isinstance(schema_name, str) or not isinstance(object_name, str):
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            logger.warning("读取维度值推荐：画像不可读: %s", path.name)
            continue
        # 推荐读取段整体降级（doc 7.1 §4.4）：结构异常或未预期错误
        # 只把该表标记为推荐信息不可用（不进入映射），不阻断配置读取
        try:
            table_profile = (
                data.get("table_profile") if isinstance(data, dict) else None
            )
            column_profiles = (
                data.get("column_profiles") if isinstance(data, dict) else None
            )
            if (table_profile is not None and not isinstance(table_profile, dict)) or (
                column_profiles is not None and not isinstance(column_profiles, dict)
            ):
                logger.warning(
                    "读取维度值推荐：画像结构异常，按推荐信息不可用处理: %s",
                    path.name,
                )
                continue
            columns, confidence, reason = validate_dimension_value_recommendation(
                (table_profile or {}).get("dimension_value_columns"),
                (table_profile or {}).get("dimension_value_confidence"),
                column_profiles.keys() if isinstance(column_profiles, dict) else (),
            )
            if reason:
                logger.warning(
                    "维度值字段推荐整组不采用（%s.%s）: %s",
                    schema_name,
                    object_name,
                    reason,
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "读取维度值推荐失败（%s，%s），按推荐信息不可用处理",
                path.name,
                type(exc).__name__,
            )
            continue
        recommendations[(schema_name, object_name)] = {
            "dimension_value_columns": columns,
            "dimension_value_confidence": confidence,
        }
    return recommendations


def read_dim_value_config(settings: PlatformSettings, workspace_id) -> Dict[str, Any]:
    workspace_id = normalize_workspace_id(workspace_id)
    with platform_session(settings) as session:
        # doc 8.1 §8.1：只读守卫（已删工作区 410；删除中仍可查看）
        from backend.access_guards import require_workspace

        require_workspace(session, workspace_id)
        workspace = _require_workspace(session, workspace_id)
        source = _require_registration(session, workspace.source_id)

        yaml_path = (
            resolve_dim_output_dir(workspace.source_id, workspace_id)
            / DIM_CONFIG_ARTIFACT_NAME
        )
        status, content = read_regular_text(yaml_path, DIM_CONFIG_ARTIFACT_NAME)
        if status == "missing":
            raise DimConfigOutputMissingError()
        if status == "invalid":
            raise DimValueConfigInvalidError(
                "dim_tables.yaml 配置文件无效或不存在，请重新生成后再发布", content
            )
        # 版本锚点改为文件内容指纹（编辑冲突检查，doc 6.2 §5.2）
        import hashlib as _hashlib

        version = _hashlib.sha256(content.encode("utf-8")).hexdigest()

        state, reason, cfg = classify_dim_tables_yaml(yaml_path, source.db_name)
        if state == "invalid":
            raise DimValueConfigInvalidError(
                "dim_tables.yaml 配置文件无效或不存在，请重新生成后再发布",
                reason or "配置文件无效",
            )

        # 逗号串警告（doc 7.2 §3.1.1/验收 25）：归一化已将其归为未指定，
        # 这里独立告知显式初始化页改用字段列表格式；state 非法时已在上方
        # 抛错，走到此处 YAML 必可解析，防御性失败降级为空列表
        try:
            comma_tables = _comma_string_tables(yaml.safe_load(content), source.db_name)
        except yaml.YAMLError:
            comma_tables = []

        # 推荐信息在既有校验全部通过后附加；失败只降级为不可用（doc 7.1
        # §4.4）。json 路径解析（含符号链接拒绝等安全检查）与推荐加载
        # 整体降级：异常时全部表按"推荐信息不可用"处理，不阻断配置读取，
        # 也不绕过路径安全检查读取文件
        try:
            recommendations = _load_recommendations(
                resolve_json_output_dir(workspace.source_id, workspace_id)
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "读取维度值推荐失败（%s），全部按推荐信息不可用处理",
                type(exc).__name__,
            )
            recommendations = {}
        tables = []
        for c in cfg.tables.values():
            rec = recommendations.get((c.schema, c.table))
            tables.append(
                {
                    "schema_name": c.schema,
                    "object_name": c.table,
                    "embedding_col": c.embedding_col,
                    "dimension_value_columns": (
                        rec["dimension_value_columns"] if rec else []
                    ),
                    "dimension_value_confidence": (
                        rec["dimension_value_confidence"] if rec else None
                    ),
                    "recommendation_available": rec is not None,
                }
            )
        return {
            "tables": tables,
            "version": version,
            "comma_string_tables": comma_tables,
        }


# ---- 独立推荐读取（doc 7.2 §4：与被编辑 YAML 解耦的只读能力） ----


def read_dim_value_recommendations(
    settings: PlatformSettings, workspace_id
) -> Dict[str, Any]:
    """返回当前工作区全部可读画像的维度值字段推荐（与 dim-config GET 同校验口径）。

    草稿弹窗据此对 props 中的表逐表展示推荐；画像不可读或不存在的表不进入
    结果，调用方按"推荐信息不可用"展示。任何读取失败只降级为空映射。
    """
    workspace_id = normalize_workspace_id(workspace_id)
    with platform_session(settings) as session:
        from backend.workspaces.repository import get_workspace

        workspace = get_workspace(session, workspace_id)
        if workspace is None:
            raise DimConfigOutputMissingError("工作区不存在")
        try:
            recommendations = _load_recommendations(
                resolve_json_output_dir(workspace.source_id, workspace_id)
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "读取维度值推荐失败（%s），全部按推荐信息不可用处理",
                type(exc).__name__,
            )
            recommendations = {}
        tables = [
            {
                "schema_name": schema_name,
                "object_name": object_name,
                "dimension_value_columns": rec["dimension_value_columns"],
                "dimension_value_confidence": rec["dimension_value_confidence"],
                "recommendation_available": True,
            }
            for (schema_name, object_name), rec in sorted(recommendations.items())
        ]
        return {"tables": tables}


# ---- 运行/版本路径：有效清单校验与配置对象构造（doc 7.2 §5.2.1/§5.3） ----


def _in_registration_scope(
    info: Dict[str, Any], db_name: str, selected_schemas
) -> bool:
    """登记边界核对（doc 7.2 §5.2.1 保留"登记边界"检查）：画像库名须等于
    登记库名且 schema 在登记范围内。目录位置不证明归属——错误或残留画像
    不得成为候选。"""

    return info.get("database") == db_name and info.get("schema_name") in set(
        selected_schemas
    )


def build_dim_tables_from_params(
    tables_param: Any,
    source_id: str,
    workspace_id: str,
    registration,
) -> Tuple[Optional[DimTablesConfig], Optional[str], Optional[str], list]:
    """把规范化前的 dim_value.tables 参数校验并构造为可注入的 DimTablesConfig。

    registration 为登记快照（需 db_name / selected_schemas 属性，见
    json_loader.RegistrationSnapshot）。

    返回 (config, reason, code, input_files)：
    - config 为 None 时 reason 为失败文案；code 仅在显式零表时为
      "dim_value_empty_tables"（沿用既有错误码，doc 7.2 §3.2），其余为 None；
    - 结构校验与版本/运行参数共用 job_params._validate_dim_tables_param；
    - 准入核对（§5.2.1）：每张表必须存在于当前工作区 JSON 画像，字段必须
      在该表 column_profiles 键中（按原始大小写），失效项逐条列出；
      画像库名须等于登记库名、schema 须在登记范围内，越界画像按失效处理；
    - 成功时 input_files 登记实际读取的 JSON 画像文件（不再登记未读取的
      dim_tables.yaml）；embedding_col 以数组形态直接注入，不经核心模型的
      字符串逗号拆分路径。
    """
    from backend.jobs.job_params import StepConfigError, _validate_dim_tables_param

    try:
        normalized = _validate_dim_tables_param(tables_param)
    except StepConfigError as exc:
        return None, str(exc), None, []
    if not normalized:
        return None, str(DimValueEmptyTablesError()), "dim_value_empty_tables", []

    db_name = registration.db_name
    json_dir = resolve_json_output_dir(source_id, workspace_id)
    # {(schema, object): (path, column_names)}；越界画像单独记录以区分文案
    profiles: Dict[Tuple[str, str], Tuple[Path, set]] = {}
    out_of_scope: set = set()
    for path, info in _iter_json_products(json_dir):
        schema_name = info.get("schema_name")
        object_name = info.get("object_name")
        if not isinstance(schema_name, str) or not isinstance(object_name, str):
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            data = None
        raw_profiles = data.get("column_profiles") if isinstance(data, dict) else None
        column_names = (
            {name for name in raw_profiles if isinstance(name, str)}
            if isinstance(raw_profiles, dict)
            else set()
        )
        key = (schema_name, object_name)
        if not _in_registration_scope(info, db_name, registration.selected_schemas):
            out_of_scope.add(key)
            continue
        profiles[key] = (path, column_names)

    invalid_items: List[str] = []
    tables: Dict[str, DimTableConfig] = {}
    used_paths: Dict[str, Path] = {}
    for item in normalized:
        key = (item["schema_name"], item["object_name"])
        full_name = f"{item['schema_name']}.{item['object_name']}"
        profile = profiles.get(key)
        if profile is None:
            if key in out_of_scope:
                invalid_items.append(f"{full_name}（画像库名或 schema 超出登记范围）")
            else:
                invalid_items.append(f"{full_name}（表不在当前工作区 JSON 画像中）")
            continue
        path, column_names = profile
        unknown = [c for c in item["embedding_col"] if c not in column_names]
        if unknown:
            invalid_items.append(f"{full_name}（字段不存在: {'、'.join(unknown)}）")
            continue
        tables[full_name] = DimTableConfig(
            schema=item["schema_name"],
            table=item["object_name"],
            embedding_col=list(item["embedding_col"]),
        )
        used_paths[path.name] = path
    if invalid_items:
        return (
            None,
            "dim_value 配置引用已失效的表或字段: "
            + "；".join(invalid_items)
            + "；请编辑作业创建新版本，或在 custom 模式修正本次清单",
            None,
            [],
        )

    from backend.workspace_files.markers import read_collection_marker

    marker, _error = read_collection_marker(json_dir)
    run_id = marker.run_id if marker else None
    input_files = [
        (f"json/{name}", run_id) for name in sorted(used_paths, key=str.lower)
    ]
    return DimTablesConfig(database=db_name, tables=tables), None, None, input_files


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


def list_dim_value_objects(settings: PlatformSettings, workspace_id) -> Dict[str, Any]:
    workspace_id = normalize_workspace_id(workspace_id)
    with platform_session(settings) as session:
        workspace = _require_workspace(session, workspace_id)
        source = _require_registration(session, workspace.source_id)
        json_dir = resolve_json_output_dir(workspace.source_id, workspace_id)
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
            # 登记边界（与发布预检同一规则，doc 7.2 §5.2.1）：画像库名须等于
            # 登记库名且 schema 在登记范围内
            if not _in_registration_scope(
                info, source.db_name, source.selected_schemas
            ):
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
    settings: PlatformSettings, workspace_id, schema_name: str, object_name: str
) -> Dict[str, Any]:
    workspace_id = normalize_workspace_id(workspace_id)
    with platform_session(settings) as session:
        workspace = _require_workspace(session, workspace_id)
        source = _require_registration(session, workspace.source_id)
        json_dir = resolve_json_output_dir(workspace.source_id, workspace_id)

        data = None
        for path, info in _iter_json_products(json_dir):
            if (
                info.get("schema_name") == schema_name
                and info.get("object_name") == object_name
                # 登记边界（与发布预检同一规则）：越界画像不提供字段候选
                and _in_registration_scope(
                    info, source.db_name, source.selected_schemas
                )
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


def _validate_payload(payload: Any) -> List[Dict[str, Any]]:
    """结构校验（第 1 步）与 embedding_col 强校验（第 2 步），返回规范化表清单。

    embedding_col 接受单列字符串或字段名列表（doc 7.2 §3.1.1）；列表元素须为
    非空字符串，按首次出现顺序去重；逗号串（含列表内元素含逗号）一律拒绝并
    提示改用字段列表，不拆分、不截取首项；空值/空列表按未配置处理。
    """

    if not isinstance(payload, dict):
        raise DimValueConfigInvalidError(
            "请求体必须是对象（mapping）", "payload 不是对象"
        )
    tables = payload.get("tables")
    if not isinstance(tables, list):
        raise DimValueConfigInvalidError("tables 必须是列表", "tables 不是列表")

    seen = set()
    missing: List[str] = []
    normalized: List[Dict[str, Any]] = []
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

        columns: Any = None
        if isinstance(embedding_col, str):
            text = embedding_col.strip()
            if "," in text:
                raise DimValueConfigInvalidError(
                    f"embedding_col 多字段请使用字段列表格式: {full_name}",
                    "embedding_col 含逗号（多字段请使用列表）",
                )
            if text:
                columns = text
        elif isinstance(embedding_col, list):
            items: List[str] = []
            for element in embedding_col:
                if not isinstance(element, str) or not element.strip():
                    raise DimValueConfigInvalidError(
                        f"embedding_col 列表元素必须是非空字符串: {full_name}",
                        "embedding_col 列表含非法元素",
                    )
                text = element.strip()
                if "," in text:
                    raise DimValueConfigInvalidError(
                        f"embedding_col 多字段请使用字段列表格式: {full_name}",
                        "embedding_col 列表元素含逗号（多字段请使用列表）",
                    )
                if text not in items:
                    items.append(text)
            if items:
                columns = items
        elif embedding_col is not None:
            raise DimValueConfigInvalidError(
                f"embedding_col 必须是字符串或字段名列表: {full_name}",
                "embedding_col 类型非法",
            )
        if columns is None:
            missing.append(full_name)
            continue
        normalized.append(
            {
                "schema_name": schema_name,
                "object_name": object_name,
                "embedding_col": columns,
            }
        )

    if missing:
        raise DimValueColumnMissingError(sorted(missing))
    return normalized


def write_dim_value_config(
    settings: PlatformSettings, workspace_id, payload: Dict[str, Any]
) -> Dict[str, Any]:
    workspace_id = normalize_workspace_id(workspace_id)
    normalized = _validate_payload(payload)

    with platform_session(settings) as session:
        from backend.access_guards import require_workspace
        from backend.deletion.locks import lock_source_row, lock_workspace_row
        from backend.persistence.models import WorkspaceInfo
        from backend.workspace_files.backups import (
            EditBackupFailedError,
            backup_files_before_edit,
        )

        # doc 8.1 §5.1.1：定位父级 → 锁源 → 锁工作区，锁内复查冻结
        probe = session.get(WorkspaceInfo, workspace_id)
        if probe is not None:
            lock_source_row(session, probe.source_id)
        workspace_row = lock_workspace_row(session, workspace_id)
        if workspace_row is None:
            raise DimConfigOutputMissingError("工作区不存在")
        require_workspace(session, workspace_id, for_write=True)
        _require_workspace(session, workspace_id)
        source = _require_registration(session, workspace_row.source_id)

        # 版本复查：文件内容指纹（编辑冲突检查）
        yaml_path = (
            resolve_dim_output_dir(workspace_row.source_id, workspace_id)
            / DIM_CONFIG_ARTIFACT_NAME
        )
        status, current = read_regular_text(yaml_path, DIM_CONFIG_ARTIFACT_NAME)
        if status != "ok" or not isinstance(current, str):
            raise DimConfigOutputMissingError()
        import hashlib as _hashlib

        version = payload.get("version")
        if version != _hashlib.sha256(current.encode("utf-8")).hexdigest():
            raise DimConfigRegeneratedError()

        dim_config_dir = yaml_path.parent
        # 文件级备份（doc 6.2 §5.2）：失败则本次保存终止，不写回、不删任何内容
        try:
            backup_files_before_edit(dim_config_dir, [DIM_CONFIG_ARTIFACT_NAME])
        except EditBackupFailedError as exc:
            raise DimConfigBackupFailedError(
                f"dim_tables.yaml 备份失败，本次保存未执行：{exc}"
            ) from None
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
