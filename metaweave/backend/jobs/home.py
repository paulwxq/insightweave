"""工作区首页数据：最近运行与状态统计（新模型，doc 6.2 §9.1.7）。"""

from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

from backend.data_sources.registration_service import (
    PlatformUnavailableError,
    SourceNotFoundError,
)
from backend.data_sources.repository import get_db_source
from backend.jobs.listing import (
    list_workspace_collection_runs,
    list_workspace_publish_runs,
)
from backend.paging import DEFAULT_LIMIT
from backend.persistence.models import (
    CollectionJobInfo,
    CollectionRunInfo,
    PublishJobInfo,
    PublishRunInfo,
)
from backend.persistence.platform_db import PlatformSettings, platform_session
from backend.workspaces.repository import get_workspace
from backend.workspaces.service import WorkspaceNotFoundError

_RECENT_DAYS = 30


def get_workspace_home(
    settings: PlatformSettings, workspace_id: str, *, now=None
) -> dict:
    """工作区概览：来源信息、采集/发布运行计数与最近运行。"""
    from datetime import datetime, timezone

    from metaweave.utils.source_id import normalize_workspace_id

    workspace_id = normalize_workspace_id(workspace_id)
    moment = now or datetime.now(timezone.utc)
    try:
        with platform_session(settings) as session:
            workspace = get_workspace(session, workspace_id)
            if workspace is None:
                raise WorkspaceNotFoundError("工作区不存在")
            source = get_db_source(session, workspace.source_id)
            if source is None:
                raise SourceNotFoundError("数据源不存在")
            source_view = {
                "source_id": source.source_id,
                "display_name": source.display_name,
                "db_name": source.db_name,
                "is_active": source.is_active,
            }
            cutoff = moment - timedelta(days=_RECENT_DAYS)
            collection_stats = session.execute(
                select(
                    CollectionRunInfo.status,
                    func.count(),
                )
                .join(
                    CollectionJobInfo,
                    CollectionJobInfo.collection_job_id
                    == CollectionRunInfo.collection_job_id,
                )
                .where(
                    CollectionJobInfo.workspace_id == workspace_id,
                    CollectionRunInfo.created_at >= cutoff,
                )
                .group_by(CollectionRunInfo.status)
            ).all()
            publish_stats = session.execute(
                select(
                    PublishRunInfo.status,
                    func.count(),
                )
                .join(
                    PublishJobInfo,
                    PublishJobInfo.publish_job_id == PublishRunInfo.publish_job_id,
                )
                .where(
                    PublishJobInfo.workspace_id == workspace_id,
                    PublishRunInfo.created_at >= cutoff,
                )
                .group_by(PublishRunInfo.status)
            ).all()
    except SQLAlchemyError:
        raise PlatformUnavailableError("平台库暂不可用") from None

    def _count_rows(rows, status):
        return sum(count for row_status, count in rows if row_status == status)

    recent_collection = list_workspace_collection_runs(
        settings, workspace_id, limit=DEFAULT_LIMIT, offset=0
    )
    recent_publish = list_workspace_publish_runs(
        settings, workspace_id, limit=DEFAULT_LIMIT, offset=0
    )
    return {
        "workspace_id": workspace_id,
        "source": source_view,
        "collection_runs": {
            "running": _count_rows(collection_stats, "running"),
            "success": _count_rows(collection_stats, "success"),
            "partial_success": _count_rows(collection_stats, "partial_success"),
            "failed": _count_rows(collection_stats, "failed"),
            "recent": recent_collection["items"],
        },
        "publish_runs": {
            "running": _count_rows(publish_stats, "running"),
            "success": _count_rows(publish_stats, "success"),
            "partial_success": _count_rows(publish_stats, "partial_success"),
            "failed": _count_rows(publish_stats, "failed"),
            "recent": recent_publish["items"],
        },
    }
