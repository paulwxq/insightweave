"""对象列表与级联候选：只读当前 JSON 发布（doc 19.2 §8.1/§8.2）。

列表与候选都先做源级检查（current 标记至多一条、有标记时该源至少有
一条版本一致的画像对象行），再从同一对象集合分页或收窄；不访问采集
工作区或源库。
"""

import logging
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from backend.assets.read_tx import begin_readonly_repeatable_read
from backend.data_sources.registration_service import (
    PlatformUnavailableError,
    SourceNotFoundError,
)
from backend.data_sources.repository import get_db_source
from backend.persistence.models import PublishInfo, TableProfile
from backend.persistence.platform_db import PlatformSettings, platform_session
from metaweave.utils.time_utils import utc_isoformat

logger = logging.getLogger("backend.assets")

JSON_STEP = "json"
_TYPE_ORDER = {"table": 0, "view": 1, "materialized_view": 2}


def get_current_json_publishes(session, source_id: UUID) -> list:
    """该源 json 步骤 current 且成功的发布行（doc 19.2 §8.2）。

    失败尝试的 current 标记不构成有效当前发布；防御性返回全部，
    多条判不一致。
    """
    stmt = select(PublishInfo).where(
        PublishInfo.source_id == source_id,
        PublishInfo.step_name == JSON_STEP,
        PublishInfo.is_current_publish.is_(True),
        PublishInfo.status == "success",
    )
    return list(session.scalars(stmt))


def _publish_block(current) -> dict | None:
    if current is None:
        return None
    return {
        "publish_id": str(current.publish_id),
        "job_id": str(current.job_id),
        "published_at": utc_isoformat(current.created_at),
    }


def _primary_key_state(row) -> str:
    if row.primary_key_columns is None:
        return "unknown"
    return "present" if row.primary_key_columns else "none"


def _list_item(row) -> dict:
    return {
        "schema_name": row.schema_name,
        "object_name": row.object_name,
        "object_type": row.object_type,
        "object_comment": row.object_comment,
        "table_category": row.table_category,
        "column_count": row.column_count,
        "total_rows": row.total_rows,
        "primary_key_state": _primary_key_state(row),
        "generated_timestamp": (
            utc_isoformat(row.generated_timestamp)
            if row.generated_timestamp is not None
            else None
        ),
    }


def _empty_response(state: str, source_id, page: int, page_size: int) -> dict:
    return {
        "state": state,
        "source_id": str(source_id),
        "json_publish": None,
        "total": 0,
        "page": page,
        "page_size": page_size,
        "items": [],
    }


def _load_profile_rows(session, source_id) -> list:
    stmt = (
        select(TableProfile)
        .where(TableProfile.source_id == source_id)
        .order_by(TableProfile.schema_name.asc(), TableProfile.object_name.asc())
    )
    return list(session.scalars(stmt))


def _source_rows_consistent(rows, current) -> bool:
    """源级一致性：有对象行且全部行的 publish_id/job_id 与当前发布一致。

    doc 19.2 §8.2：对象行的 publish_id/job_id 必须与 JSON 当前发布一致。
    """
    if not rows:
        return False
    return all(
        row.publish_id == current.publish_id and row.job_id == current.job_id
        for row in rows
    )


def _filter_rows(rows, object_type, schema_name, keyword) -> list:
    filtered = rows
    if object_type:
        filtered = [row for row in filtered if row.object_type == object_type]
    if schema_name:
        filtered = [row for row in filtered if row.schema_name == schema_name]
    if keyword:
        # 不区分大小写的子串匹配；%、_ 等输入按普通字符处理（doc 19.2 §8.1.1）
        needle = keyword.lower()
        filtered = [row for row in filtered if needle in row.object_name.lower()]
    return filtered


def _resolve_state(session, source_id: UUID):
    """源级检查（doc 19.2 §8.2）。

    返回 (state, current, rows)：state 取 ok / not_published /
    inconsistent；inconsistent 时只写诊断日志，不返回部分数据。
    """
    currents = get_current_json_publishes(session, source_id)
    if not currents:
        return "not_published", None, None
    rows = _load_profile_rows(session, source_id)
    if len(currents) > 1:
        logger.error(
            "JSON 当前发布标记出现多条 source_id=%s count=%d",
            source_id,
            len(currents),
        )
        return "inconsistent", None, None
    if not _source_rows_consistent(rows, currents[0]):
        logger.error(
            "JSON 当前画像行缺失或版本不符 source_id=%s",
            source_id,
        )
        return "inconsistent", None, None
    return "ok", currents[0], rows


def list_objects(
    settings: PlatformSettings,
    source_id: UUID,
    *,
    page: int = 1,
    page_size: int = 50,
    object_type: str | None = None,
    schema_name: str | None = None,
    keyword: str | None = None,
) -> dict:
    """该源当前 JSON 发布的对象分页列表（doc 19.2 §8.1）。"""
    try:
        with platform_session(settings) as session:
            begin_readonly_repeatable_read(session)
            source = get_db_source(session, source_id)
            if source is None:
                raise SourceNotFoundError("数据源不存在")
            state, current, rows = _resolve_state(session, source_id)
            if state != "ok":
                return _empty_response(state, source_id, page, page_size)
            filtered = _filter_rows(rows, object_type, schema_name, keyword)
            start = (page - 1) * page_size
            return {
                "state": "ready",
                "source_id": str(source_id),
                "json_publish": _publish_block(current),
                "total": len(filtered),
                "page": page,
                "page_size": page_size,
                "items": [
                    _list_item(row) for row in filtered[start : start + page_size]
                ],
            }
    except SQLAlchemyError:
        raise PlatformUnavailableError("平台库暂不可用") from None


def list_object_options(
    settings: PlatformSettings,
    source_id: UUID,
    *,
    object_type: str | None = None,
    schema_name: str | None = None,
    keyword: str | None = None,
) -> dict:
    """级联搜索栏三级候选；keyword 只筛对象候选，不改变前两级（doc 19.2 §8.1）。"""
    try:
        with platform_session(settings) as session:
            begin_readonly_repeatable_read(session)
            source = get_db_source(session, source_id)
            if source is None:
                raise SourceNotFoundError("数据源不存在")
            state, current, rows = _resolve_state(session, source_id)
            if state != "ok":
                return {
                    "state": state,
                    "source_id": str(source_id),
                    "json_publish": None,
                    "object_types": [],
                    "schemas": [],
                    "objects": [],
                }
            typed = _filter_rows(rows, object_type, None, None)
            candidates = _filter_rows(rows, object_type, schema_name, keyword)
            return {
                "state": "ready",
                "source_id": str(source_id),
                "json_publish": _publish_block(current),
                "object_types": sorted(
                    {row.object_type for row in rows},
                    key=lambda name: _TYPE_ORDER.get(name, 99),
                ),
                "schemas": sorted({row.schema_name for row in typed}),
                "objects": [
                    {
                        "schema_name": row.schema_name,
                        "object_name": row.object_name,
                        "object_type": row.object_type,
                    }
                    for row in candidates
                ],
            }
    except SQLAlchemyError:
        raise PlatformUnavailableError("平台库暂不可用") from None
