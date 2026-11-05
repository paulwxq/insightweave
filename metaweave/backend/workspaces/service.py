"""工作区用例：创建（含目录与回滚）、列表、详情、基本信息修改。

doc 6 §2 / 6.2 §5.1：创建工作区先验证来源存在，保存记录并创建
`output/<source_id>/<workspace_id>/`；目录创建失败时回滚本次新增记录
（短 ID 序列出现空号可以接受）。工作区创建后不可更换来源。
"""

from sqlalchemy.exc import SQLAlchemyError

from backend.data_sources.registration_service import (
    PlatformUnavailableError,
    SourceNotFoundError,
)
from backend.data_sources.repository import get_db_source
from backend.paging import validate_page
from backend.persistence.models import WorkspaceInfo
from backend.persistence.platform_db import PlatformSettings, platform_session
from backend.workspaces.repository import (
    count_workspace_jobs,
    get_workspace,
    insert_workspace,
    list_workspaces_by_source,
    page_workspaces_by_filter,
    update_workspace,
)
from metaweave.core.clean_targets import resolve_workspace_root
from metaweave.utils.source_id import normalize_source_id, normalize_workspace_id
from metaweave.utils.time_utils import utc_isoformat


class WorkspaceNotFoundError(LookupError):
    """指定的 workspace_id 不存在。"""


class WorkspaceNameExistsError(ValueError):
    """同一数据源下已存在同名工作区（doc 6.1 §1 唯一约束）。"""


class WorkspaceValidationError(ValueError):
    """工作区名称或说明不合法。"""


class WorkspaceDirectoryError(RuntimeError):
    """工作区目录创建失败；记录已回滚。"""


_NAME_MAX = 200
_DESC_MAX = 1000


def _item(row: WorkspaceInfo) -> dict:
    return {
        "workspace_id": row.workspace_id,
        "source_id": row.source_id,
        "workspace_name": row.workspace_name,
        "workspace_desc": row.workspace_desc,
        "created_at": utc_isoformat(row.created_at),
        "updated_at": utc_isoformat(row.updated_at),
    }


def _deletion_view(session, row: WorkspaceInfo) -> dict:
    """doc 8.1 §7.0/§3.3.1：工作区响应附带删除任务摘要与父级有效状态。"""
    from backend.deletion.repository import task_summary

    state = getattr(row, "deletion_state", "active") or "active"
    effective, blocked_by, blocked_reason = state, None, None
    source_state = "active"
    if state == "active" and hasattr(session, "get"):
        # 生产 Session 恒有 get；部分测试假会话不建模数据源行
        source = get_db_source(session, row.source_id)
        source_state = (
            getattr(source, "deletion_state", "active") or "active"
            if source is not None
            else "deleted"
        )
    if state == "active" and source_state != "active":
        if source_state != "active":
            effective = f"parent_{source_state}"
            blocked_by = {"parent_type": "source", "source_id": row.source_id}
            blocked_reason = (
                "数据源正在删除或删除失败"
                if source_state in ("deleting", "delete_failed")
                else "数据源已删除"
            )
    task = task_summary(session, source_id=row.source_id, workspace_id=row.workspace_id)
    return {
        "deletion_task": ({**task, "task_id": str(task["task_id"])} if task else None),
        "effective_state": effective,
        "blocked_by": blocked_by,
        "blocked_reason": blocked_reason,
    }


def _validate_name(name: object) -> str:
    if not isinstance(name, str):
        raise WorkspaceValidationError("工作区名称必须是字符串")
    name = name.strip()
    if not name:
        raise WorkspaceValidationError("工作区名称不能为空")
    if len(name) > _NAME_MAX:
        raise WorkspaceValidationError(f"工作区名称不能超过 {_NAME_MAX} 个字符")
    return name


def _validate_desc(desc: object) -> str | None:
    if desc is None:
        return None
    if not isinstance(desc, str):
        raise WorkspaceValidationError("工作区说明必须是字符串")
    desc = desc.strip()
    if len(desc) > _DESC_MAX:
        raise WorkspaceValidationError(f"工作区说明不能超过 {_DESC_MAX} 个字符")
    return desc or None


def create_workspace(
    settings: PlatformSettings, source_id: str, name: object, desc: object = None
) -> dict:
    """创建工作区记录与目录；目录失败回滚记录（doc 6.2 §5.1）。"""
    source_id = normalize_source_id(source_id)
    workspace_name = _validate_name(name)
    workspace_desc = _validate_desc(desc)
    try:
        with platform_session(settings) as session:
            source = get_db_source(session, source_id)
            if source is None:
                raise SourceNotFoundError("数据源不存在")
            # doc 8.1 §5.1.1：锁源后在锁内复查未删除/未冻结，再插入工作区
            from backend.access_guards import require_source
            from backend.deletion.locks import lock_source_row

            source = lock_source_row(session, source_id)
            require_source(session, source_id, for_write=True)
            existing = list_workspaces_by_source(session, source_id)
            if any(row.workspace_name == workspace_name for row in existing):
                raise WorkspaceNameExistsError("同一数据源下已存在同名工作区")
            row = insert_workspace(
                session,
                {
                    "source_id": source_id,
                    "workspace_name": workspace_name,
                    "workspace_desc": workspace_desc,
                    "created_by_user_id": None,
                },
            )
            workspace_id = row.workspace_id
            try:
                directory = resolve_workspace_root(source_id, workspace_id)
                directory.mkdir(parents=True, exist_ok=True)
            except (OSError, ValueError):
                # 目录创建失败：回滚本次新增记录，序列空号可接受
                session.rollback()
                raise WorkspaceDirectoryError(
                    "工作区目录创建失败，已回滚工作区记录"
                ) from None
            session.commit()
            return _item(row)
    except SQLAlchemyError:
        raise PlatformUnavailableError("平台库暂不可用") from None


def list_workspaces(settings: PlatformSettings, source_id: str) -> dict:
    source_id = normalize_source_id(source_id)
    try:
        with platform_session(settings) as session:
            source = get_db_source(session, source_id)
            if source is None:
                raise SourceNotFoundError("数据源不存在")
            rows = list_workspaces_by_source(session, source_id)
            return {
                "source_id": source_id,
                "items": [
                    {**_item(row), **_deletion_view(session, row)} for row in rows
                ],
            }
    except SQLAlchemyError:
        raise PlatformUnavailableError("平台库暂不可用") from None


def page_workspaces(
    settings: PlatformSettings,
    *,
    source_id: str | None = None,
    limit: int | None = None,
    offset: int | None = None,
) -> dict:
    """跨源工作区列表（doc 6.2 §6.1）：服务端分页信封 items/total/limit/offset。

    `source_id` 可选；传入时必须是合法短 ID（ShortIdError → 422），
    不要求来源存在（无匹配返回空页）。全局工作区管理页使用，
    不由前端逐源拉取后拼接分页。列表行附每工作区
    collection_job_count / publish_job_count 聚合计数。
    """
    normalized_source = None if source_id is None else normalize_source_id(source_id)
    page = validate_page(limit, offset)
    try:
        with platform_session(settings) as session:
            rows, total = page_workspaces_by_filter(
                session,
                source_id=normalized_source,
                limit=page.limit,
                offset=page.offset,
            )
            collection_counts, publish_counts = count_workspace_jobs(
                session, [row.workspace_id for row in rows]
            )
            return {
                "items": [
                    {
                        **_item(row),
                        "collection_job_count": collection_counts.get(
                            row.workspace_id, 0
                        ),
                        "publish_job_count": publish_counts.get(row.workspace_id, 0),
                        **_deletion_view(session, row),
                    }
                    for row in rows
                ],
                "total": total,
                "limit": page.limit,
                "offset": page.offset,
            }
    except SQLAlchemyError:
        raise PlatformUnavailableError("平台库暂不可用") from None


def _require_workspace(session, workspace_id: str) -> WorkspaceInfo:
    row = get_workspace(session, workspace_id)
    if row is None:
        raise WorkspaceNotFoundError("工作区不存在")
    return row


def get_workspace_detail(settings: PlatformSettings, workspace_id: str) -> dict:
    workspace_id = normalize_workspace_id(workspace_id)
    try:
        with platform_session(settings) as session:
            row = _require_workspace(session, workspace_id)
            # doc 8.1 §3.3：已删除工作区详情 410
            if getattr(row, "deleted_at", None) is not None:
                from backend.deletion.errors import ResourceDeletedError

                raise ResourceDeletedError("工作区已删除")
            return {**_item(row), **_deletion_view(session, row)}
    except SQLAlchemyError:
        raise PlatformUnavailableError("平台库暂不可用") from None


def patch_workspace(
    settings: PlatformSettings,
    workspace_id: str,
    *,
    name: object = None,
    desc: object = None,
    has_name: bool = False,
    has_desc: bool = False,
) -> dict:
    """修改工作区名称/说明；不允许改绑来源（doc 6.2 §6.1）。"""
    workspace_id = normalize_workspace_id(workspace_id)
    fields: dict = {}
    if has_name:
        fields["workspace_name"] = _validate_name(name)
    if has_desc:
        fields["workspace_desc"] = _validate_desc(desc)
    if not fields:
        raise WorkspaceValidationError("没有可修改的字段")
    try:
        with platform_session(settings) as session:
            # doc 8.1 §5.1.1/§8.1：定位父级 → 锁源 → 锁工作区 → 写守卫
            from backend.access_guards import require_workspace
            from backend.deletion.locks import lock_source_row, lock_workspace_row

            probe = get_workspace(session, workspace_id)
            if probe is None:
                raise WorkspaceNotFoundError("工作区不存在")
            lock_source_row(session, probe.source_id)
            lock_workspace_row(session, workspace_id)
            require_workspace(session, workspace_id, for_write=True)
            row = update_workspace(session, workspace_id, fields)
            if row is None:
                raise WorkspaceNotFoundError("工作区不存在")
            session.commit()
            return _item(row)
    except SQLAlchemyError:
        raise PlatformUnavailableError("平台库暂不可用") from None
