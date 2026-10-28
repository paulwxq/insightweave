"""跨源工作区列表 `GET /api/workspaces` 的实库验证（doc 6.2 §6.1）。

沿用 `test_job_service_live.py` 的 skipif 模式：缺少平台库环境变量时跳过；
建议在隔离测试库上运行（PLATFORM_DB_NAME 指向演练库）。

用例直接在 workspace_info 插入临时行（绕过 create_workspace 的目录创建，
不触碰 output/ 与数据源登记），覆盖：
- created_at DESC、workspace_id DESC 排序（同一事务内 created_at 相同，
  由 workspace_id 文本序决定先后，与 doc 6.2 §6.1 的排序键一致）；
- limit/offset 翻页与 total（筛选条件下的全部行数，非当前页条数）；
- source_id 过滤与空过滤；过滤无匹配返回空页；
- 列表行 collection_job_count / publish_job_count 聚合计数（含 0 值）。
结束后清理临时行，不改动既有数据。
"""

import pytest
from sqlalchemy import delete, select

from backend.persistence.models import (
    CollectionJobInfo,
    DbSourceInfo,
    PublishJobInfo,
    WorkspaceInfo,
)
from backend.persistence.platform_db import (
    load_platform_settings_from_env,
    load_project_env,
    platform_session,
)
from backend.workspaces.service import page_workspaces


def _settings():
    load_project_env()
    try:
        return load_platform_settings_from_env()
    except Exception:
        return None


SETTINGS = _settings()
pytestmark = pytest.mark.skipif(
    SETTINGS is None, reason="缺少 PLATFORM_DB_* 环境变量，跳过工作区分页实库断言"
)


@pytest.fixture()
def temp_rows():
    """在既有数据源下插入 3 行临时工作区，返回 (settings, source_id, ids)；用后清理。"""
    settings = SETTINGS
    with platform_session(settings) as session:
        source_id = session.scalars(select(DbSourceInfo.source_id).limit(1)).first()
        if source_id is None:
            pytest.skip("平台库没有已登记数据源，跳过工作区分页实库断言")
        rows = [
            WorkspaceInfo(
                source_id=source_id,
                workspace_name=f"分页实库验证-{marker}",
                workspace_desc=None,
                created_by_user_id=None,
            )
            for marker in ("a", "b", "c")
        ]
        session.add_all(rows)
        session.flush()
        ids = [row.workspace_id for row in rows]
        # 先提交，测试内的独立会话才能看到临时行
        session.commit()
        try:
            yield settings, source_id, ids
        finally:
            session.execute(
                delete(WorkspaceInfo).where(WorkspaceInfo.workspace_id.in_(ids))
            )
            session.commit()


def test_page_orders_desc_and_paginates(temp_rows):
    settings, source_id, ids = temp_rows

    first = page_workspaces(settings, source_id=source_id, limit=2, offset=0)
    assert first["total"] >= 3
    assert first["limit"] == 2 and first["offset"] == 0
    assert len(first["items"]) == 2

    second = page_workspaces(settings, source_id=source_id, limit=2, offset=2)
    assert second["total"] == first["total"]

    # 同一事务插入 → created_at 相同 → 排序退化为 workspace_id 文本 DESC
    expected_order = sorted(ids, reverse=True)
    got = [item["workspace_id"] for item in first["items"] + second["items"]]
    # 本批三行应按期望顺序出现在返回窗内（首两页覆盖 total 内的前 4 行）
    positions = [got.index(wid) for wid in expected_order if wid in got]
    assert positions == sorted(positions)
    assert all(item["source_id"] == source_id for item in first["items"])


def test_page_without_filter_covers_all_sources(temp_rows):
    settings, _source_id, ids = temp_rows

    page = page_workspaces(settings, source_id=None, limit=100, offset=0)
    assert page["total"] >= len(ids)
    returned = {item["workspace_id"] for item in page["items"]}
    assert set(ids) <= returned


def test_page_filter_without_match_is_empty(temp_rows):
    settings, _source_id, _ids = temp_rows

    page = page_workspaces(settings, source_id="db_src_999999", limit=20, offset=0)
    assert page == {
        "items": [],
        "total": 0,
        "limit": 20,
        "offset": 0,
    }


def test_page_items_carry_job_counts(temp_rows):
    """列表行聚合：ids[0] 采 2 发 1、ids[1] 采 0 发 2、ids[2] 双 0（缺键取 0）。"""
    settings, source_id, ids = temp_rows
    jobs = [
        CollectionJobInfo(workspace_id=ids[0], job_name="实库计数-采集1"),
        CollectionJobInfo(workspace_id=ids[0], job_name="实库计数-采集2"),
        PublishJobInfo(workspace_id=ids[0], job_name="实库计数-发布1"),
        PublishJobInfo(workspace_id=ids[1], job_name="实库计数-发布2"),
        PublishJobInfo(workspace_id=ids[1], job_name="实库计数-发布3"),
    ]
    with platform_session(settings) as session:
        session.add_all(jobs)
        session.commit()
    try:
        page = page_workspaces(settings, source_id=source_id, limit=100, offset=0)
        counts = {
            item["workspace_id"]: (
                item["collection_job_count"],
                item["publish_job_count"],
            )
            for item in page["items"]
        }
        assert counts[ids[0]] == (2, 1)
        assert counts[ids[1]] == (0, 2)
        assert counts[ids[2]] == (0, 0)
    finally:
        # 作业行必须先于工作区临时行删除（外键约束）
        with platform_session(settings) as session:
            session.execute(
                delete(CollectionJobInfo).where(
                    CollectionJobInfo.workspace_id.in_(ids)
                )
            )
            session.execute(
                delete(PublishJobInfo).where(PublishJobInfo.workspace_id.in_(ids))
            )
            session.commit()
