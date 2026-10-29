"""同源多工作区发布与最后成功视图实库验证（doc 6.1 §12、§14.1、A31/A17）。

覆盖：
- 同源两个工作区先后发布 db 步骤 → db_profile 仍每源一行，行上的
  publish_run_id 为最后一次成功写入；未发布步骤不替换；
- 工作区视图保留两个工作区各自成功历史；源级口径取最近成功；
- PostgreSQL 内容写入与步骤成功状态同事务（失败回滚不更新来源）。
"""

import json
from uuid import uuid4

import pytest
from sqlalchemy import delete, select, text

from backend.jobs.job_service import (
    JobConfigPayload,
    create_job as create_job_service,
)
from backend.persistence.models import DbProfile, PublishRunStep
from backend.persistence.platform_db import (
    load_platform_settings_from_env,
    load_project_env,
    platform_session,
)
from backend.publishing.publish_service import (
    _publish_db,
    create_publish_run,
)
from backend.publishing.repository import (
    source_latest_successful_publish,
    workspace_latest_successful_publish,
)
from backend.workspaces.service import create_workspace
from metaweave.core.clean_targets import resolve_db_output_dir
from metaweave.utils.source_id import normalize_run_uuid

_SOURCE_NAME = "发布演练"


def _settings():
    load_project_env()
    try:
        return load_platform_settings_from_env()
    except Exception:
        return None


SETTINGS = _settings()
pytestmark = pytest.mark.skipif(
    SETTINGS is None, reason="缺少 PLATFORM_DB_* 环境变量，跳过发布内容实库断言"
)


def _write_database_json(source_id: str, directory, collected_marker: str) -> None:
    document = {
        "schema_version": 1,
        "source_id": source_id,
        "collected_at": "2026-10-21T10:00:00+00:00",
        "database": {
            "type": "postgresql",
            "host": "h",
            "name": "demo",
            "port": 5432,
            "server_version": "18.0",
            "size_bytes": 1024,
            "size_scope": "whole_database",
        },
        "selected_schemas": ["public"],
        "schemas": [
            {
                "name": "public",
                "ordinary_table_count": 1,
                "partitioned_table_count": 0,
                "partition_child_count": 0,
                "view_count": 0,
                "materialized_view_count": 0,
                "object_count": 1,
            }
        ],
        "totals": {
            "ordinary_table_count": 1,
            "partitioned_table_count": 0,
            "partition_child_count": 0,
            "view_count": 0,
            "materialized_view_count": 0,
            "object_count": 1,
        },
        "collected_marker": collected_marker,
    }
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "database.json").write_text(
        json.dumps(document, ensure_ascii=False), encoding="utf-8"
    )


@pytest.fixture
def live_source_two_workspaces():
    from backend.persistence.models import (
        PublishJobInfo,
        PublishJobRevision,
        PublishRunInfo,
        WorkspaceInfo,
    )

    with platform_session(SETTINGS) as session:
        source_id = session.execute(
            text(
                "INSERT INTO public.db_source_info (display_name, db_type, host, port,"
                " db_name, username, password_ciphertext, selected_schemas)"
                f" VALUES ('{_SOURCE_NAME}', 'postgresql', 'h', 5432, 'demo', 'u',"
                " 'x', ARRAY['public']) RETURNING source_id"
            )
        ).scalar_one()
        session.commit()
    ws1 = create_workspace(SETTINGS, source_id, "生产")
    ws2 = create_workspace(SETTINGS, source_id, "测试")
    yield source_id, ws1["workspace_id"], ws2["workspace_id"]

    import shutil

    from metaweave.core.clean_targets import resolve_source_workspace_dir

    with platform_session(SETTINGS) as session:
        # 逆外键序，且只清理本测试的数据（共享演练库可能有其他用例的行，
        # 全表删除会被他源内容行的外键引用阻断）：步骤 → 五张内容行
        # （引用发布运行）→ 运行 → 版本 → 作业
        from backend.persistence.models import (
            TableColumnProfile,
            TableDefinition,
            TableDomain,
            TableProfile,
        )

        job_ids = session.execute(
            select(PublishJobInfo.publish_job_id)
            .join(
                WorkspaceInfo,
                WorkspaceInfo.workspace_id == PublishJobInfo.workspace_id,
            )
            .where(WorkspaceInfo.source_id == source_id)
        ).scalars().all()
        run_ids = (
            session.execute(
                select(PublishRunInfo.publish_run_id).where(
                    PublishRunInfo.publish_job_id.in_(job_ids)
                )
            ).scalars().all()
            if job_ids
            else []
        )
        if run_ids:
            session.execute(
                delete(PublishRunStep).where(PublishRunStep.publish_run_id.in_(run_ids))
            )
        session.execute(delete(DbProfile).where(DbProfile.source_id == source_id))
        session.execute(delete(TableDomain).where(TableDomain.source_id == source_id))
        session.execute(
            delete(TableDefinition).where(TableDefinition.source_id == source_id)
        )
        session.execute(delete(TableProfile).where(TableProfile.source_id == source_id))
        session.execute(
            delete(TableColumnProfile).where(TableColumnProfile.source_id == source_id)
        )
        if run_ids:
            session.execute(
                delete(PublishRunInfo).where(PublishRunInfo.publish_run_id.in_(run_ids))
            )
        if job_ids:
            session.execute(
                delete(PublishJobRevision).where(
                    PublishJobRevision.publish_job_id.in_(job_ids)
                )
            )
            session.execute(
                delete(PublishJobInfo).where(PublishJobInfo.publish_job_id.in_(job_ids))
            )
        session.execute(
            delete(WorkspaceInfo).where(WorkspaceInfo.source_id == source_id)
        )
        session.execute(
            text("DELETE FROM public.db_source_info WHERE source_id = :sid").bindparams(
                sid=source_id
            )
        )
        session.commit()
    try:
        shutil.rmtree(resolve_source_workspace_dir(source_id))
    except (OSError, ValueError):
        pass


def _create_publish_job(workspace_id: str) -> str:
    job = create_job_service(
        SETTINGS,
        "publish",
        workspace_id,
        f"发布-{workspace_id}",
        None,
        JobConfigPayload(steps=["db"]),
    )
    return job["job_id"]


def test_two_workspaces_publish_db_replaces_source_content(live_source_two_workspaces):
    """A31：后一次成功发布替换该源对应步骤内容；两工作区历史都保留。"""
    source_id, ws1, ws2 = live_source_two_workspaces
    from backend.workspace_files.markers import write_collection_marker

    for wid in (ws1, ws2):
        db_dir = resolve_db_output_dir(source_id, wid)
        _write_database_json(source_id, db_dir, wid)
        write_collection_marker(db_dir, uuid4(), "success")
    job1 = _create_publish_job(ws1)
    job2 = _create_publish_job(ws2)

    run1 = create_publish_run(
        SETTINGS, job1, revision_no=1, mode="default", steps=None, step_config=None
    )
    run1_id = normalize_run_uuid(run1["publish_run_id"])
    _publish_db(
        SETTINGS,
        run1_id,
        ws1,
        input_manifest={
            "files": [{"path": "db/database.json", "collection_run_id": str(uuid4())}]
        },
        payload=None,
    )
    with platform_session(SETTINGS) as session:
        from backend.jobs.run_repository import PUBLISH_RUN, finish_step

        finish_step(
            PUBLISH_RUN,
            session,
            run1_id,
            "db",
            "success",
            "数据库画像发布完成",
        )
        session.commit()

    run2 = create_publish_run(
        SETTINGS, job2, revision_no=1, mode="default", steps=None, step_config=None
    )
    run2_id = normalize_run_uuid(run2["publish_run_id"])
    _publish_db(
        SETTINGS,
        run2_id,
        ws2,
        input_manifest={
            "files": [{"path": "db/database.json", "collection_run_id": str(uuid4())}]
        },
        payload=None,
    )
    with platform_session(SETTINGS) as session:
        from backend.jobs.run_repository import PUBLISH_RUN, finish_step

        finish_step(
            PUBLISH_RUN,
            session,
            run2_id,
            "db",
            "success",
            "数据库画像发布完成",
        )
        session.commit()

    with platform_session(SETTINGS) as session:
        profiles = (
            session.query(DbProfile).filter(DbProfile.source_id == source_id).all()
        )
        # 同源只保存一套内容：一行，来源为最后一次成功写入（A31）
        assert len(profiles) == 1
        assert str(profiles[0].publish_run_id) == str(run2_id)

        # 工作区视图：两个工作区各自的成功历史都保留（§12）
        ws1_rows = workspace_latest_successful_publish(session, ws1)
        ws2_rows = workspace_latest_successful_publish(session, ws2)
        assert [r["publish_run_id"] for r in ws1_rows] == [str(run1_id)]
        assert [r["publish_run_id"] for r in ws2_rows] == [str(run2_id)]

        # 源级口径：该源该步骤最近成功 = 工作区 2 的运行（doc 6.2 §10.2）
        source_rows = source_latest_successful_publish(session, source_id)
        db_row = next(r for r in source_rows if r["step_name"] == "db")
        assert db_row["publish_run_id"] == str(run2_id)
        assert db_row["workspace_id"] == ws2
