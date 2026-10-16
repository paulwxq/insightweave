"""对象详情与 DDL 摘要/正文读取（doc 19.2 §8.1/§8.2）。

只读当前 JSON 发布：源级检查通过后按身份取对象行与字段行，逐行核对
版本与字段数；DDL 只在详情中给摘要（不解析正文），`object-ddl` 才在
同一条当前 DDL 行上切分 SQL 与样例。
"""

import logging
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from backend.assets.ddl_display import (
    DefinitionInconsistentError,
    extract_sample,
    split_definition_sql,
)
from backend.assets.object_catalog import get_current_json_publishes
from backend.assets.read_tx import begin_readonly_repeatable_read
from backend.data_sources.registration_service import (
    PlatformUnavailableError,
    SourceNotFoundError,
)
from backend.data_sources.repository import get_db_source
from backend.persistence.models import (
    PublishInfo,
    TableColumnProfile,
    TableDefinition,
    TableProfile,
)
from backend.persistence.platform_db import PlatformSettings, platform_session
from metaweave.utils.time_utils import utc_isoformat

JSON_STEP = "json"
DDL_STEP = "ddl"
DEFINE_TYPE_DDL = "ddl"

logger = logging.getLogger("backend.assets")


class ProfileInconsistentError(RuntimeError):
    """当前 JSON 发布与画像行不一致（doc 19.2 §8.2）。"""


class ObjectNotFoundError(RuntimeError):
    """对象不在当前 JSON 发布的对象集合中。"""


def _publish_block(current) -> dict:
    return {
        "publish_id": str(current.publish_id),
        "job_id": str(current.job_id),
        "published_at": utc_isoformat(current.created_at),
    }


def _source_rows(session, source_id: UUID) -> list:
    rows = list(
        session.scalars(select(TableProfile).where(TableProfile.source_id == source_id))
    )
    return rows


def _identity_row(rows, schema_name: str, object_name: str):
    return next(
        (
            row
            for row in rows
            if row.schema_name == schema_name and row.object_name == object_name
        ),
        None,
    )


def _profile_block(row) -> dict:
    """§6 表级标量全量，排除 json_sample_records（doc 19.2 §8.1.1）。"""
    return {
        "source_id": str(row.source_id),
        "schema_name": row.schema_name,
        "object_name": row.object_name,
        "db_name": row.db_name,
        "object_type": row.object_type,
        "publish_id": str(row.publish_id),
        "job_id": str(row.job_id),
        "metadata_version": row.metadata_version,
        "generated_timestamp": (
            utc_isoformat(row.generated_timestamp)
            if row.generated_timestamp is not None
            else None
        ),
        "loaded_at": utc_isoformat(row.loaded_at),
        "object_comment": row.object_comment,
        "object_comment_source": row.object_comment_source,
        "total_rows": row.total_rows,
        "column_count": row.column_count,
        "sample_method": row.sample_method,
        "sample_count": row.sample_count,
        "has_table_profile": row.has_table_profile,
        "table_category": row.table_category,
        "category_confidence": (
            float(row.category_confidence)
            if row.category_confidence is not None
            else None
        ),
        "classification_source": row.classification_source,
        "classification_reason": row.classification_reason,
        "classification_basis": row.classification_basis,
        "rule_table_category": row.rule_table_category,
        "rule_confidence": (
            float(row.rule_confidence) if row.rule_confidence is not None else None
        ),
        "rule_inference_basis": row.rule_inference_basis,
        "primary_key_name": row.primary_key_name,
        "primary_key_columns": row.primary_key_columns,
        "foreign_key_count": row.foreign_key_count,
        "index_count": row.index_count,
        "constraint_count": row.constraint_count,
        "foreign_keys": row.foreign_keys,
        "unique_constraints": row.unique_constraints,
        "indexes": row.indexes,
        "unique_column_sets": row.unique_column_sets,
    }


def _hits(items, key, column_name):
    if items is None:
        return None
    return [item for item in items if column_name in (item.get(key) or [])]


def _column_structures(row, column_name: str) -> dict:
    """按 §3.3/§3.4 规则把表级明细投影到字段；null 与 [] 语义区分。"""
    if row.primary_key_columns is None:
        pk_state, pk = "unknown", None
    elif column_name in row.primary_key_columns:
        pk_state = "present"
        pk = {
            "constraint_name": row.primary_key_name,
            "columns": list(row.primary_key_columns),
        }
    else:
        pk_state, pk = "none", None
    return {
        "primary_key_state": pk_state,
        "primary_key": pk,
        "foreign_keys": _hits(row.foreign_keys, "source_columns", column_name),
        "unique_constraints": _hits(row.unique_constraints, "columns", column_name),
    }


def _column_indexes(row, column_name: str):
    if row.indexes is None:
        return None
    return [
        index
        for index in row.indexes
        if column_name in (index.get("columns") or [])
        or column_name in (index.get("included_columns") or [])
    ]


def _column_block(row, column) -> dict:
    # §8.1.1：字段行返回 §6 的字段行标量（仅排除 value_distribution），
    # 身份四列置首对齐 §6.2 列序；publish_id 用字段行自身版本标记
    # （读取路径已校验其与对象行相同）
    return {
        "source_id": str(row.source_id),
        "schema_name": row.schema_name,
        "object_name": row.object_name,
        "publish_id": str(column.publish_id),
        "column_name": column.column_name,
        "ordinal_position": column.ordinal_position,
        "data_type": column.data_type,
        "is_nullable": column.is_nullable,
        "column_default": column.column_default,
        "column_comment": column.column_comment,
        "comment_source": column.comment_source,
        "semantic_role": column.semantic_role,
        "semantic_confidence": (
            float(column.semantic_confidence)
            if column.semantic_confidence is not None
            else None
        ),
        "semantic_basis": column.semantic_basis,
        "null_count": column.null_count,
        "null_rate": (
            float(column.null_rate) if column.null_rate is not None else None
        ),
        "unique_count": column.unique_count,
        "uniqueness": (
            float(column.uniqueness) if column.uniqueness is not None else None
        ),
        "sample_min": column.sample_min,
        "sample_max": column.sample_max,
        "physical_constraints": _column_structures(row, column.column_name),
        "indexes": _column_indexes(row, column.column_name),
        "unique_column_sets": _hits(
            row.unique_column_sets, "columns", column.column_name
        ),
    }


def _ddl_summary(
    session,
    source_id: UUID,
    *,
    db_name: str,
    object_type: str,
    schema_name: str,
    object_name: str,
):
    """同身份 DDL 当前态摘要（doc 19.2 §8.2），不解析 define_text。"""
    currents = list(
        session.scalars(
            select(PublishInfo).where(
                PublishInfo.source_id == source_id,
                PublishInfo.step_name == DDL_STEP,
                PublishInfo.is_current_publish.is_(True),
                # 失败尝试的 current 标记不构成有效当前发布（doc 19.2 §8.2）
                PublishInfo.status == "success",
            )
        )
    )
    rows = list(
        session.scalars(
            select(TableDefinition).where(
                TableDefinition.source_id == source_id,
                TableDefinition.schema_name == schema_name,
                TableDefinition.object_name == object_name,
                TableDefinition.define_type == DEFINE_TYPE_DDL,
            )
        )
    )
    row = rows[0] if rows else None
    # §8.2：标记缺失且无行、或有标记但无行 → not_published；
    # 标记缺失却有孤立行、或行版本与标记不符 → inconsistent。
    if not currents:
        if row is not None:
            logger.error(
                "DDL 孤立行（无当前发布标记）source_id=%s schema=%s object=%s",
                source_id,
                schema_name,
                object_name,
            )
            return "inconsistent", None
        return "not_published", None
    if len(currents) > 1:
        logger.error(
            "DDL 当前发布标记出现多条 source_id=%s count=%d",
            source_id,
            len(currents),
        )
        return "inconsistent", None
    current = currents[0]
    if row is None:
        return "not_published", None
    if row.publish_id != current.publish_id:
        logger.error(
            "DDL 行与当前标记版本不符 source_id=%s schema=%s object=%s",
            source_id,
            schema_name,
            object_name,
        )
        return "inconsistent", None
    if row.db_name != db_name or row.object_type != object_type:
        return "incompatible", None
    return (
        "ready",
        {
            "publish_id": str(row.publish_id),
            "job_id": str(row.job_id),
            "loaded_at": utc_isoformat(row.loaded_at),
        },
    )


def _not_published_detail(source_id, schema_name, object_name) -> dict:
    return {
        "state": "not_published",
        "source_id": str(source_id),
        "schema_name": schema_name,
        "object_name": object_name,
        "json_publish": None,
        "ddl_state": "not_published",
        "ddl_publish": None,
        "profile": None,
        "columns": [],
    }


def _load_current_and_rows(session, source_id: UUID):
    """json current（仅成功）+ 源全部对象行；源级不一致直接抛受控错误。

    与列表/候选统一口径（doc 19.2 §8.2）：源内任一对象行的
    publish_id/job_id 与当前发布不符即判当前内容不一致，不只在读取
    目标对象时才校验。
    """
    currents = get_current_json_publishes(session, source_id)
    if not currents:
        return None, None
    if len(currents) > 1:
        raise ProfileInconsistentError("JSON 当前发布标记出现多条")
    current = currents[0]
    rows = _source_rows(session, source_id)
    if not rows:
        raise ProfileInconsistentError("已有 JSON current 却没有任何对象行")
    if any(
        row.publish_id != current.publish_id or row.job_id != current.job_id
        for row in rows
    ):
        logger.error("JSON 对象行版本与当前发布不符 source_id=%s", source_id)
        raise ProfileInconsistentError("对象行版本与当前发布不符")
    return current, rows


def get_object_detail(
    settings: PlatformSettings,
    source_id: UUID,
    schema_name: str,
    object_name: str,
) -> dict:
    """一次返回基本信息/字典/画像所需的对象行、字段行与 DDL 摘要。"""
    try:
        with platform_session(settings) as session:
            begin_readonly_repeatable_read(session)
            source = get_db_source(session, source_id)
            if source is None:
                raise SourceNotFoundError("数据源不存在")
            current, rows = _load_current_and_rows(session, source_id)
            if current is None:
                return _not_published_detail(source_id, schema_name, object_name)
            row = _identity_row(rows, schema_name, object_name)
            if row is None:
                raise ObjectNotFoundError("对象不在当前 JSON 发布中")
            columns = list(
                session.scalars(
                    select(TableColumnProfile)
                    .where(
                        TableColumnProfile.source_id == source_id,
                        TableColumnProfile.schema_name == schema_name,
                        TableColumnProfile.object_name == object_name,
                    )
                    .order_by(TableColumnProfile.ordinal_position.asc())
                )
            )
            if len(columns) != row.column_count or any(
                column.publish_id != row.publish_id for column in columns
            ):
                logger.error(
                    "字段行版本或数量不符 source_id=%s schema=%s object=%s",
                    source_id,
                    schema_name,
                    object_name,
                )
                raise ProfileInconsistentError("字段行版本或数量不符")
            ddl_state, ddl_publish = _ddl_summary(
                session,
                source_id,
                db_name=row.db_name,
                object_type=row.object_type,
                schema_name=schema_name,
                object_name=object_name,
            )
            return {
                "state": "ready",
                "source_id": str(source_id),
                "schema_name": schema_name,
                "object_name": object_name,
                "json_publish": _publish_block(current),
                "ddl_state": ddl_state,
                "ddl_publish": ddl_publish,
                "profile": _profile_block(row),
                "columns": [_column_block(row, column) for column in columns],
            }
    except SQLAlchemyError:
        raise PlatformUnavailableError("平台库暂不可用") from None


def get_object_ddl(
    settings: PlatformSettings,
    source_id: UUID,
    schema_name: str,
    object_name: str,
) -> dict:
    """从同一条当前 DDL 行拆出对象定义 SQL 与最多 5 条样例（doc 19.2 §8.1）。"""
    try:
        with platform_session(settings) as session:
            begin_readonly_repeatable_read(session)
            source = get_db_source(session, source_id)
            if source is None:
                raise SourceNotFoundError("数据源不存在")
            current, rows = _load_current_and_rows(session, source_id)
            # object-ddl 只服务当前 JSON 中存在的对象；无 current 即对象
            # 不存在，不能误当成 DDL 未发布（doc 19.2 §8.1.1）
            if current is None:
                raise ObjectNotFoundError("对象不在当前 JSON 发布中")
            row = _identity_row(rows, schema_name, object_name)
            if row is None:
                raise ObjectNotFoundError("对象不在当前 JSON 发布中")
            ddl_state, ddl_publish = _ddl_summary(
                session,
                source_id,
                db_name=row.db_name,
                object_type=row.object_type,
                schema_name=schema_name,
                object_name=object_name,
            )
            if ddl_state == "inconsistent":
                raise DefinitionInconsistentError("已发布 DDL 状态不一致")
            payload = {
                "state": ddl_state,
                "json_publish_id": str(current.publish_id),
                "ddl_publish": ddl_publish,
                "definition_sql": None,
                "sample_state": None,
                "sample_columns": [],
                "sample_records": [],
            }
            if ddl_state != "ready":
                return payload
            define_rows = list(
                session.scalars(
                    select(TableDefinition).where(
                        TableDefinition.source_id == source_id,
                        TableDefinition.schema_name == schema_name,
                        TableDefinition.object_name == object_name,
                        TableDefinition.define_type == DEFINE_TYPE_DDL,
                    )
                )
            )
            sql, block = split_definition_sql(define_rows[0].define_text)
            sample_state, columns, records = extract_sample(
                block,
                schema_name=schema_name,
                object_name=object_name,
                object_type=row.object_type,
            )
            return {
                **payload,
                "definition_sql": sql,
                "sample_state": sample_state,
                "sample_columns": columns,
                "sample_records": records,
            }
    except SQLAlchemyError:
        raise PlatformUnavailableError("平台库暂不可用") from None
