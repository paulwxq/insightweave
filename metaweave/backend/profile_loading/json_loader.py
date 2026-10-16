"""受管 JSON 画像产物读取与行映射（doc 19.2 §7）。

只读校验受管目录中的对象 JSON，并映射为平台库两表行；不自行提交、
不写平台库。发布事务由 backend.publishing.publish_service 负责。
"""

import json
import logging
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy.orm import Session

from backend.data_sources.repository import get_db_source
from backend.jobs.repository import assert_workspace_owner, get_step_row
from backend.profile_loading.db_loader import (
    MetaLoadSourceDisabledError,
    MetaLoadSourceNotFoundError,
)
from metaweave.core.clean_targets import resolve_json_output_dir
from metaweave.core.metadata.json_loader import parse_strict_stem
from metaweave.core.metadata.metadata_document import (
    MetadataDocument,
    MetadataDocumentError,
)

logger = logging.getLogger("backend.profile_loading")

JSON_STEP = "json"
# 受管 JSON 目录内的采集清单文件名，与采集侧
# backend.data_sources.json_step_service 的同名常量必须一致（测试锁定）。
JSON_MANIFEST_NAME = "manifest.json"
_SUPPORTED_OBJECT_TYPES = {"table", "view", "materialized_view"}


class JsonPublishInputError(ValueError):
    """JSON 产物不满足画像两表的发布输入契约（doc 19.2 §7.2）。"""


@dataclass(frozen=True)
class RegistrationSnapshot:
    """发布预检时捕获的登记行快照（doc 19.2 §7.4）。"""

    updated_at: datetime
    db_name: str
    selected_schemas: tuple[str, ...]


def inspect_json_profile_context(
    session: Session, source_id: UUID, job_id: UUID
) -> tuple[str | None, datetime | None]:
    """检查数据源、工作区归属和 json 步骤状态；不读文件、不写库。

    语义与 inspect_ddl_definition_context 一致：返回
    (最近状态, 运行标识)。
    """
    source = get_db_source(session, source_id)
    if source is None:
        raise MetaLoadSourceNotFoundError("未找到指定 source_id 的数据源")
    if not source.is_active:
        raise MetaLoadSourceDisabledError("指定数据源已禁用，不能加载画像")
    assert_workspace_owner(session, job_id, source_id)
    step = get_step_row(session, job_id, JSON_STEP)
    if step is None:
        return None, None
    return step.last_status, step.updated_at


def read_registration_snapshot(
    session: Session, source_id: UUID
) -> RegistrationSnapshot:
    """读取启用状态登记行的库名、Schema 范围与版本标识（doc 19.2 §7.4）。"""
    source = get_db_source(session, source_id)
    if source is None:
        raise MetaLoadSourceNotFoundError("未找到指定 source_id 的数据源")
    if not source.is_active:
        raise MetaLoadSourceDisabledError("指定数据源已禁用，不能加载画像")
    return RegistrationSnapshot(
        updated_at=source.updated_at,
        db_name=source.db_name,
        selected_schemas=tuple(source.selected_schemas),
    )


def _verify_json_manifest(json_dir, database_name: str) -> list[str]:
    """清单与目录结构性校验（doc 19.2 §7.2，规则与采集侧一致）。

    返回清单声明的文件名列表；失败抛 JsonPublishInputError，消息只含
    文件名与错误类别，不含 JSON 内容。
    """
    manifest_path = json_dir / JSON_MANIFEST_NAME
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise JsonPublishInputError("JSON 采集清单缺失或不是普通文件")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        raise JsonPublishInputError("JSON 采集清单不可读") from None
    files = manifest.get("files") if isinstance(manifest, dict) else None
    if (
        not isinstance(files, list)
        or not files
        or any(
            not isinstance(name, str) or not name.endswith(".json") for name in files
        )
    ):
        raise JsonPublishInputError("JSON 采集清单形状不合法")
    if len(set(files)) != len(files):
        raise JsonPublishInputError("JSON 采集清单存在重复条目")

    actual: set[str] = set()
    for path in json_dir.glob("*.json"):
        if path.name == JSON_MANIFEST_NAME:
            continue
        if path.is_symlink() or not path.is_file():
            raise JsonPublishInputError(f"JSON 产物目录存在非常规文件: {path.name}")
        actual.add(path.name)
    if actual != set(files):
        raise JsonPublishInputError("JSON 采集清单与目录实际文件不一致")

    for name in files:
        parsed = parse_strict_stem(name.removesuffix(".json"))
        if parsed is None:
            raise JsonPublishInputError(f"JSON 文件名不是严格三段式: {name}")
        if parsed[0] != database_name:
            raise JsonPublishInputError(f"JSON 清单包含不属于当前数据库的文件: {name}")
    return files


def read_managed_json_documents(source_id, job_id, database_name: str) -> list[dict]:
    """读取并校验受管目录中的全部对象 JSON，返回内存快照（doc 19.2 §7.2）。

    v3 校验经 MetadataDocument.from_dict，身份与文件名逐字核对；逐文件
    读取而非依赖 JsonLoader 的库名前缀枚举。错误只报文件名与类别。
    """
    json_dir = resolve_json_output_dir(source_id, job_id)
    files = _verify_json_manifest(json_dir, database_name)
    documents: list[dict] = []
    for name in files:
        path = json_dir / name
        if path.is_symlink() or not path.is_file():
            raise JsonPublishInputError(f"JSON 产物不是普通文件: {name}")
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            raise JsonPublishInputError(
                f"JSON 产物不可读或不是 UTF-8 JSON: {name}"
            ) from None
        try:
            MetadataDocument.from_dict(document)
        except MetadataDocumentError:
            raise JsonPublishInputError(f"JSON 产物未通过 v3 校验: {name}") from None
        info = document["object_info"]
        parsed = parse_strict_stem(name.removesuffix(".json"))
        if (
            info.get("database") != parsed[0]
            or info.get("schema_name") != parsed[1]
            or info.get("object_name") != parsed[2]
        ):
            raise JsonPublishInputError(f"JSON 对象身份与文件名不一致: {name}")
        documents.append(document)
    return documents


def check_json_output_ready(source_id, job_id, database_name: str | None) -> bool:
    """发布预检：受管 JSON 目录、清单与对象文件就绪（只读，不解析映射）。

    步骤未成功时调用方传 None：产物核对对 publishable 已无影响，直接
    返回 False，不读登记快照。
    """
    if database_name is None:
        return False
    try:
        read_managed_json_documents(source_id, job_id, database_name)
        return True
    except JsonPublishInputError as exc:
        logger.warning(
            "JSON 预检清单核对未通过 source_id=%s job_id=%s reason=%s",
            source_id,
            job_id,
            str(exc),
        )
        return False
    except (ValueError, OSError) as exc:
        logger.warning(
            "JSON 预检产物目录不可用 source_id=%s job_id=%s type=%s",
            source_id,
            job_id,
            type(exc).__name__,
        )
        return False


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_ratio(value) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and 0.0 <= value <= 1.0
    )


def _is_str_array(value) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _object_label(document: dict) -> str:
    info = document["object_info"]
    return f"{info.get('schema_name')}.{info.get('object_name')}"


def _validate_document(document: dict, selected_schemas) -> None:
    """§7.2 静态校验 + 结构引用核对；畸形非空值不得静默改写。"""
    label = _object_label(document)
    info = document["object_info"]
    for key in ("schema_name", "object_name"):
        if not isinstance(info.get(key), str) or not info[key].strip():
            raise JsonPublishInputError(f"{label}: object_info.{key} 为空")
    if info.get("object_type") not in _SUPPORTED_OBJECT_TYPES:
        raise JsonPublishInputError(f"{label}: 对象类型不受支持")
    if info["schema_name"] not in selected_schemas:
        raise JsonPublishInputError(f"{label}: schema 不在当前登记范围内")

    columns = document["column_profiles"]
    if not columns:
        raise JsonPublishInputError(f"{label}: column_profiles 为空对象")
    seen_ordinals: set[int] = set()
    for name, column in columns.items():
        if not isinstance(name, str) or not name.strip():
            raise JsonPublishInputError(f"{label}: 字段名为空")
        if not isinstance(column, dict):
            raise JsonPublishInputError(f"{label}.{name}: 字段画像必须是对象")
        ordinal = column.get("ordinal_position")
        if not _is_int(ordinal) or ordinal < 1:
            raise JsonPublishInputError(f"{label}.{name}: ordinal_position 非正整数")
        if ordinal in seen_ordinals:
            raise JsonPublishInputError(f"{label}.{name}: ordinal_position 重复")
        seen_ordinals.add(ordinal)
        if not isinstance(column.get("is_nullable"), bool):
            raise JsonPublishInputError(f"{label}.{name}: is_nullable 非布尔")
        if not isinstance(column.get("data_type"), str) or not column["data_type"]:
            raise JsonPublishInputError(f"{label}.{name}: data_type 为空")
        semantic = column.get("semantic_analysis")
        if semantic is None:
            semantic = {}
        elif not isinstance(semantic, dict):
            # 畸形非空值不得静默改写为缺失（doc 19.2 §7.2）：[] 等必须拒绝
            raise JsonPublishInputError(f"{label}.{name}: semantic_analysis 非对象")
        confidence = semantic.get("semantic_confidence")
        if confidence is not None and not _is_ratio(confidence):
            raise JsonPublishInputError(f"{label}.{name}: semantic_confidence 非比例")
        basis = semantic.get("inference_basis")
        if basis is not None and not _is_str_array(basis):
            raise JsonPublishInputError(f"{label}.{name}: inference_basis 非字符串数组")
        statistics = column.get("statistics")
        if statistics is None:
            statistics = {}
        elif not isinstance(statistics, dict):
            raise JsonPublishInputError(f"{label}.{name}: statistics 非对象")
        for key in ("null_count", "unique_count"):
            value = statistics.get(key)
            if value is not None and (not _is_int(value) or value < 0):
                raise JsonPublishInputError(f"{label}.{name}: {key} 非非负整数")
        for key in ("null_rate", "uniqueness"):
            value = statistics.get(key)
            if value is not None and not _is_ratio(value):
                raise JsonPublishInputError(f"{label}.{name}: {key} 非比例")
        distribution = statistics.get("value_distribution")
        if distribution is not None and not isinstance(distribution, dict):
            raise JsonPublishInputError(f"{label}.{name}: value_distribution 非对象")

    total_rows = info.get("total_rows")
    if total_rows is not None and (not _is_int(total_rows) or total_rows < 0):
        raise JsonPublishInputError(f"{label}: total_rows 非非负整数")
    profiling = document.get("profiling")
    if profiling is None:
        profiling = {}
    elif not isinstance(profiling, dict):
        raise JsonPublishInputError(f"{label}: profiling 非对象")
    sample_count = profiling.get("sample_count")
    if sample_count is not None and (not _is_int(sample_count) or sample_count < 0):
        raise JsonPublishInputError(f"{label}: profiling.sample_count 非非负整数")

    timestamp = document.get("generated_timestamp")
    if timestamp is not None:
        try:
            parsed_timestamp = datetime.fromisoformat(timestamp)
        except (TypeError, ValueError):
            raise JsonPublishInputError(
                f"{label}: generated_timestamp 不可解析"
            ) from None
        if parsed_timestamp.tzinfo is None or parsed_timestamp.utcoffset() is None:
            # 列语义为带时区的时间点（doc 19.3 §2.2.4）：无时区输入不猜测
            # 其墙钟含义，必须拒绝；带偏移的输入按同一瞬间换算入库
            raise JsonPublishInputError(f"{label}: generated_timestamp 必须带时区")

    table_profile = document.get("table_profile")
    if table_profile is None:
        return
    if not isinstance(table_profile, dict):
        raise JsonPublishInputError(f"{label}: table_profile 非对象或 null")
    confidence = table_profile.get("confidence")
    if confidence is not None and not _is_ratio(confidence):
        raise JsonPublishInputError(f"{label}: table_profile.confidence 非比例")
    for key in ("inference_basis",):
        value = table_profile.get(key)
        if value is not None and not _is_str_array(value):
            raise JsonPublishInputError(f"{label}: table_profile.{key} 非字符串数组")
    rule = table_profile.get("rule_based_classification")
    if rule is not None:
        if not isinstance(rule, dict):
            raise JsonPublishInputError(f"{label}: rule_based_classification 非对象")
        rule_confidence = rule.get("confidence")
        if rule_confidence is not None and not _is_ratio(rule_confidence):
            raise JsonPublishInputError(f"{label}: 规则分类置信度非比例")
        rule_basis = rule.get("inference_basis")
        if rule_basis is not None and not _is_str_array(rule_basis):
            raise JsonPublishInputError(f"{label}: 规则分类依据非字符串数组")

    constraints = table_profile.get("physical_constraints")
    if constraints is None:
        constraints = {}
    elif not isinstance(constraints, dict):
        raise JsonPublishInputError(f"{label}: physical_constraints 非对象")
    primary_key = constraints.get("primary_key")
    if primary_key is not None:
        if not isinstance(primary_key, dict) or not _is_str_array(
            primary_key.get("columns")
        ):
            raise JsonPublishInputError(f"{label}: primary_key.columns 非字符串数组")
        unknown = set(primary_key["columns"]) - set(columns)
        if unknown:
            raise JsonPublishInputError(f"{label}: primary_key 引用未知字段")
    for key in ("foreign_keys", "unique_constraints"):
        items = constraints.get(key)
        if items is None:
            continue
        if not isinstance(items, list) or not all(
            isinstance(item, dict) for item in items
        ):
            raise JsonPublishInputError(f"{label}: {key} 非对象数组")
        ref_key = "source_columns" if key == "foreign_keys" else "columns"
        for item in items:
            refs = item.get(ref_key)
            if not _is_str_array(refs):
                raise JsonPublishInputError(f"{label}: {key}.{ref_key} 非字符串数组")
            if key == "foreign_keys":
                targets = item.get("target_columns")
                if not _is_str_array(targets) or not refs or len(refs) != len(targets):
                    raise JsonPublishInputError(f"{label}: 外键来源/目标列数不等")
            unknown = set(refs) - set(columns)
            if unknown:
                raise JsonPublishInputError(f"{label}: {key} 引用未知字段")

    indexes = table_profile.get("indexes")
    if indexes is not None:
        if not isinstance(indexes, list) or not all(
            isinstance(item, dict) for item in indexes
        ):
            raise JsonPublishInputError(f"{label}: indexes 非对象数组")
        for index in indexes:
            for key in ("columns", "included_columns"):
                refs = index.get(key)
                if refs is None:
                    continue
                if not _is_str_array(refs):
                    raise JsonPublishInputError(f"{label}: indexes.{key} 非字符串数组")
                unknown = set(refs) - set(columns)
                if unknown:
                    raise JsonPublishInputError(f"{label}: indexes.{key} 引用未知字段")

    column_sets = table_profile.get("unique_column_sets")
    if column_sets is not None:
        if not isinstance(column_sets, list) or not all(
            isinstance(item, dict) for item in column_sets
        ):
            raise JsonPublishInputError(f"{label}: unique_column_sets 非对象数组")
        for item in column_sets:
            refs = item.get("columns")
            if not _is_str_array(refs) or not refs:
                raise JsonPublishInputError(f"{label}: 候选唯一列组 columns 非法")
            unknown = set(refs) - set(columns)
            if unknown:
                raise JsonPublishInputError(f"{label}: 候选唯一列组引用未知字段")
            for key in ("confidence_score", "uniqueness", "null_rate"):
                value = item.get(key)
                if value is not None and not _is_ratio(value):
                    raise JsonPublishInputError(f"{label}: 候选列组 {key} 非比例")


def _parse_aware_timestamp(value):
    """doc 19.3 §2.2.4：带时区输入归一为 UTC；无时区输入拒绝。"""
    if value is None:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise JsonPublishInputError("generated_timestamp 必须带时区")
    return parsed.astimezone(timezone.utc)


def _table_row(document: dict, *, source_id, job_id, publish_id) -> dict:
    """§7.3/§6.3 映射：显式空数组与缺失节点入库后仍可区分。"""
    info = document["object_info"]
    columns = document["column_profiles"]
    profiling = document.get("profiling")
    profiling = profiling if isinstance(profiling, dict) else {}

    table_profile = document.get("table_profile")
    has_table_profile = table_profile is not None
    if has_table_profile:
        rule = table_profile.get("rule_based_classification")
        rule = rule if isinstance(rule, dict) else {}
        constraints = table_profile.get("physical_constraints")
        constraints = constraints if isinstance(constraints, dict) else {}
        if "primary_key" in constraints and constraints["primary_key"] is not None:
            primary_key_name = constraints["primary_key"].get("constraint_name")
            primary_key_columns = list(constraints["primary_key"].get("columns", []))
        elif "primary_key" in constraints:
            # 明确 null：无主键，与缺节点区分（doc 19.2 §6.3）
            primary_key_name = None
            primary_key_columns = []
        else:
            primary_key_name = None
            primary_key_columns = None

        def array_node(key):
            if key not in table_profile and key not in constraints:
                return None
            if key == "foreign_keys" or key == "unique_constraints":
                value = constraints.get(key)
            else:
                value = table_profile.get(key)
            return value if isinstance(value, list) else None

        foreign_keys = array_node("foreign_keys")
        unique_constraints = array_node("unique_constraints")
        indexes = array_node("indexes")
        unique_column_sets = array_node("unique_column_sets")
        parts_known = (
            "primary_key" in constraints
            and foreign_keys is not None
            and unique_constraints is not None
        )
        constraint_count = (
            (1 if constraints.get("primary_key") is not None else 0)
            + len(foreign_keys)
            + len(unique_constraints)
            if parts_known
            else None
        )
    else:
        primary_key_name = None
        primary_key_columns = None
        foreign_keys = unique_constraints = None
        indexes = unique_column_sets = None
        constraint_count = None
        rule = {}

    return {
        "source_id": source_id,
        "schema_name": info["schema_name"],
        "object_name": info["object_name"],
        "db_name": info["database"],
        "object_type": info["object_type"],
        "publish_id": publish_id,
        "job_id": job_id,
        "metadata_version": document["metadata_version"],
        "generated_timestamp": _parse_aware_timestamp(
            document.get("generated_timestamp")
        ),
        "object_comment": info.get("comment"),
        "object_comment_source": info.get("comment_source"),
        "total_rows": info.get("total_rows"),
        "column_count": len(columns),
        "sample_method": profiling.get("sample_method"),
        "sample_count": profiling.get("sample_count"),
        "has_table_profile": has_table_profile,
        "table_category": (
            table_profile.get("table_category") if has_table_profile else None
        ),
        "category_confidence": (
            table_profile.get("confidence") if has_table_profile else None
        ),
        "classification_source": (
            table_profile.get("classification_source") if has_table_profile else None
        ),
        "classification_reason": (
            table_profile.get("classification_reason") if has_table_profile else None
        ),
        "classification_basis": (
            table_profile.get("inference_basis") if has_table_profile else None
        ),
        "rule_table_category": rule.get("table_category"),
        "rule_confidence": rule.get("confidence"),
        "rule_inference_basis": rule.get("inference_basis"),
        "primary_key_name": primary_key_name,
        "primary_key_columns": primary_key_columns,
        "foreign_key_count": len(foreign_keys) if foreign_keys is not None else None,
        "index_count": len(indexes) if indexes is not None else None,
        "constraint_count": constraint_count,
        "foreign_keys": foreign_keys,
        "unique_constraints": unique_constraints,
        "indexes": indexes,
        "unique_column_sets": unique_column_sets,
        "json_sample_records": document.get("sample_records"),
    }


def _column_rows(document: dict, table_row: dict) -> list[dict]:
    rows = []
    for name, column in document["column_profiles"].items():
        semantic = column.get("semantic_analysis")
        semantic = semantic if isinstance(semantic, dict) else {}
        statistics = column.get("statistics")
        statistics = statistics if isinstance(statistics, dict) else {}
        rows.append(
            {
                "source_id": table_row["source_id"],
                "schema_name": table_row["schema_name"],
                "object_name": table_row["object_name"],
                "publish_id": table_row["publish_id"],
                "column_name": name,
                "ordinal_position": column["ordinal_position"],
                "data_type": column["data_type"],
                "is_nullable": column["is_nullable"],
                "column_default": column.get("column_default"),
                "column_comment": column.get("comment"),
                "comment_source": column.get("comment_source"),
                "semantic_role": semantic.get("semantic_role"),
                "semantic_confidence": semantic.get("semantic_confidence"),
                "semantic_basis": semantic.get("inference_basis"),
                "null_count": statistics.get("null_count"),
                "null_rate": statistics.get("null_rate"),
                "unique_count": statistics.get("unique_count"),
                "uniqueness": statistics.get("uniqueness"),
                "sample_min": statistics.get("min"),
                "sample_max": statistics.get("max"),
                "value_distribution": statistics.get("value_distribution"),
            }
        )
    return rows


def build_profile_rows(
    source_id,
    job_id,
    publish_id,
    documents: list[dict],
    selected_schemas,
) -> tuple[list[dict], list[dict]]:
    """校验并映射文档快照为两表行（doc 19.2 §7.2/§7.3）。

    返回 (table_profile 行, table_column_profile 行)；任一对象校验失败
    整批拒绝。错误消息只含对象/字段与类别，不含 JSON 内容。
    """
    table_rows: list[dict] = []
    column_rows: list[dict] = []
    for document in documents:
        _validate_document(document, selected_schemas)
    for document in documents:
        row = _table_row(
            document, source_id=source_id, job_id=job_id, publish_id=publish_id
        )
        table_rows.append(row)
        column_rows.extend(_column_rows(document, row))
    return table_rows, column_rows
