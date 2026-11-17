"""全局作业列表 SQL 装配的实库冒烟（设计稿 35/37）。

沿用 test_job_service_live.py 的 skipif 模式：缺少平台库环境变量时跳过；
建议在演练库（PLATFORM_DB_NAME 指向隔离库）上运行。只读既有数据，
不创建、不修改。
"""

import os

import pytest

from backend.jobs.job_service import list_jobs_global_page
from backend.persistence.platform_db import (
    load_platform_settings_from_env,
    load_project_env,
)


def _settings():
    load_project_env()
    try:
        return load_platform_settings_from_env()
    except Exception:
        return None


SETTINGS = _settings()
pytestmark = pytest.mark.skipif(
    SETTINGS is None or os.environ.get("METAWEAVE_RUN_LIVE_TESTS") != "1",
    reason="缺少 PLATFORM_DB_* 环境变量，跳过全局列表实库断言",
)


def test_global_list_orders_by_last_run_and_joins_names():
    # 采集全局：默认页
    page = list_jobs_global_page(SETTINGS, "collection")
    assert page["job_type"] == "collection"
    assert page["limit"] == 10 and page["offset"] == 0
    assert page["total"] >= 1
    rows = page["items"]
    assert rows, "演练库应有采集作业"
    for row in rows:
        # join 装配字段（工作区/源必然登记，名称不应为空）
        assert row["workspace_name"]
        assert row["source_id"].startswith("db_src_")
        assert row["source_name"]
        assert "last_run_at" in row
    # 排序：最后一次运行时间倒序，无运行的排后
    times = [row["last_run_at"] for row in rows]
    known = [t for t in times if t is not None]
    assert known == sorted(known, reverse=True)
    assert times[len(known) :] == [None] * (len(times) - len(known))

    # 发布全局：按工作区过滤收窄
    some_workspace = rows[0]["workspace_id"]
    publish_page = list_jobs_global_page(
        SETTINGS, "publish", workspace_id=some_workspace
    )
    assert publish_page["workspace_id"] == some_workspace
    for row in publish_page["items"]:
        assert row["workspace_id"] == some_workspace

    # 关键字过滤（作业名或 ID，ILIKE）
    keyword = rows[0]["job_id"]
    hit = list_jobs_global_page(SETTINGS, "collection", q=keyword)
    assert hit["total"] >= 1
    assert any(keyword in (row["job_id"], row["job_name"]) for row in hit["items"])
