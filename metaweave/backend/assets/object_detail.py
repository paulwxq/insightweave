"""对象详情只读查询（doc 19.2 §8、6.1 §16–§17）。

新模型下 `table_profile`/`table_column_profile` 行即当前内容；对象行与
字段行的 `publish_run_id` 一致性由本模块复核（doc 6.1 §17：服务须校验），
发布来源取行上的 `publish_run_id`。
"""

import logging

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from metaweave.utils.time_utils import utc_isoformat

from backend.assets.ddl_display import (
    extract_sample,
    split_definition_sql,
)
from backend.data_sources.registration_service import (
    PlatformUnavailableError,
    SourceNotFoundError,
)
from backend.data_sources.repository import get_db_source
from backend.assets.read_tx import begin_readonly_repeatable_read
from backend.persistence.models import (
    PublishRunInfo,
    TableColumnProfile,
    TableDefinition,
    TableProfile,
)
from backend.persistence.platform_db import PlatformSettings, platform_session
from metaweave.utils.source_id import normalize_source_id

logger = logging.getLogger("backend.assets")

DDL_STEP = "ddl"
DEFINE_TYPE_DDL = "ddl"


class ProfileInconsistentError(RuntimeError):
    """对象行与字段行发布来源不一致。"""


class ObjectNotFoundError(RuntimeError):
    """对象不在当前 JSON 发布的对象集合中。"""


def _source_rows(session, source_id: str) -> list:
    return list(
        session.scalars(select(TableProfile).where(TableProfile.source_id == source_id))
    )


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
    return {
        "source_id": row.source_id,
        "schema_name": row.schema_name,
        "object_name": row.object_name,
        "db_name": row.db_name,
        "object_type": row.object_type,
        "publish_run_id": str(row.publish_run_id),
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
    return {
        "source_id": row.source_id,
        "schema_name": row.schema_name,
        "object_name": row.object_name,
        "publish_run_id": str(column.publish_run_id),
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
    source_id: str,
    *,
    db_name: str,
    object_type: str,
    schema_name: str,
    object_name: str,
):
    """同身份 DDL 当前态摘要（doc 6.1 §15），不解析 define_text。"""
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
    if row is None:
        return "not_published", None
    if row.db_name != db_name or row.object_type != object_type:
        return "incompatible", None
    return (
        "ready",
        {
            "publish_run_id": str(row.publish_run_id),
            "loaded_at": utc_isoformat(row.loaded_at),
        },
    )


def _not_published_detail(source_id, schema_name, object_name) -> dict:
    return {
        "state": "not_published",
        "source_id": source_id,
        "schema_name": schema_name,
        "object_name": object_name,
        "json_publish": None,
        "ddl_state": "not_published",
        "ddl_publish": None,
        "profile": None,
        "columns": [],
    }


def _load_rows_checked(session, source_id: str):
    """读取源全部对象行；源内对象行版本不一致直接抛受控错误。"""
    rows = _source_rows(session, source_id)
    if not rows:
        return []
    run_ids = {row.publish_run_id for row in rows}
    if len(run_ids) > 1:
        logger.error("JSON 对象行存在多个发布来源 source_id=%s", source_id)
        raise ProfileInconsistentError("对象行发布来源不一致")
    return rows


def get_object_detail(
    settings: PlatformSettings,
    source_id: str,
    schema_name: str,
    object_name: str,
) -> dict:
    """一次返回基本信息/字典/画像所需的对象行、字段行与 DDL 摘要。"""
    source_id = normalize_source_id(source_id)
    try:
        with platform_session(settings) as session:
            begin_readonly_repeatable_read(session)
            # doc 8.1 §8.1：资产只读守卫（已删源 410；删除中仍可看画像）
            from backend.access_guards import require_source

            require_source(session, source_id)
            source = get_db_source(session, source_id)
            if source is None:
                raise SourceNotFoundError("数据源不存在")
            rows = _load_rows_checked(session, source_id)
            if not rows:
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
                column.publish_run_id != row.publish_run_id for column in columns
            ):
                logger.error(
                    "字段行版本或数量不符 source_id=%s schema=%s object=%s",
                    source_id,
                    schema_name,
                    object_name,
                )
                raise ProfileInconsistentError("字段行版本或数量不符")
            run = session.get(PublishRunInfo, row.publish_run_id)
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
                "source_id": source_id,
                "schema_name": schema_name,
                "object_name": object_name,
                "json_publish": {
                    "publish_run_id": str(row.publish_run_id),
                    "published_at": (
                        utc_isoformat(run.created_at) if run is not None else None
                    ),
                },
                "ddl_state": ddl_state,
                "ddl_publish": ddl_publish,
                "profile": _profile_block(row),
                "columns": [_column_block(row, column) for column in columns],
            }
    except SQLAlchemyError:
        raise PlatformUnavailableError("平台库暂不可用") from None


def get_object_ddl(
    settings: PlatformSettings,
    source_id: str,
    schema_name: str,
    object_name: str,
) -> dict:
    """从同一条当前 DDL 行拆出对象定义 SQL 与最多 5 条样例（doc 19.2 §8.1）。"""
    source_id = normalize_source_id(source_id)
    try:
        with platform_session(settings) as session:
            begin_readonly_repeatable_read(session)
            # doc 8.1 §8.1：资产只读守卫（已删源 410；删除中仍可看画像）
            from backend.access_guards import require_source

            require_source(session, source_id)
            source = get_db_source(session, source_id)
            if source is None:
                raise SourceNotFoundError("数据源不存在")
            rows = _load_rows_checked(session, source_id)
            if not rows:
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
            payload = {
                "state": ddl_state,
                "json_publish_run_id": str(row.publish_run_id),
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
