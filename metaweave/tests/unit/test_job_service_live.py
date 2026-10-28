"""作业保存/版本与运行状态机的实库验证（doc 6.2 §4.3、§6.3–§6.4、A02/A03/A04）。

沿用 `test_read_tx_isolation.py` 的 skipif 模式：缺少平台库环境变量时跳过。
建议在隔离测试库上运行（PLATFORM_DB_NAME 指向演练库）。用例自建临时数据、
结束后清理，不改动既有数据。
"""

import pytest

from backend.jobs.job_params import StepConfigError
from backend.jobs.job_repository import COLLECTION_JOB, PUBLISH_JOB
from backend.jobs.job_service import (
    JobConfigPayload,
    JobNameExistsError,
    add_revision,
    create_job,
)
from backend.jobs.run_repository import (
    COLLECTION_RUN,
    STATUS_BLOCKED,
    STATUS_PARTIAL,
    STATUS_PENDING,
    STATUS_SUCCESS,
    create_run,
    finalize_run,
    finish_step,
    list_runs_for_workspace,
    mark_run_running,
    mark_step_running,
    new_run_id,
)
from backend.persistence.platform_db import (
    load_platform_settings_from_env,
    load_project_env,
    platform_session,
)
from backend.workspaces.service import (
    WorkspaceNameExistsError,
    create_workspace,
)
from sqlalchemy import delete

from backend.persistence.models import (
    CollectionJobInfo,
    CollectionJobRevision,
    CollectionRunInfo,
    CollectionRunStep,
    WorkspaceInfo,
)


def _settings():
    load_project_env()
    try:
        return load_platform_settings_from_env()
    except Exception:
        return None


SETTINGS = _settings()
pytestmark = pytest.mark.skipif(
    SETTINGS is None, reason="缺少 PLATFORM_DB_* 环境变量，跳过作业实库断言"
)


@pytest.fixture
def live_source_and_workspace():
    """临时数据源 + 工作区；结束时按外键逆序清理临时数据。"""
    from sqlalchemy import text as sql_text

    with platform_session(SETTINGS) as session:
        source = session.execute(
            sql_text(
                "INSERT INTO public.db_source_info (display_name, db_type, host, port,"
                " db_name, username, password_ciphertext, selected_schemas)"
                " VALUES ('临时演练', 'postgresql', 'h', 5432, 'd', 'u', 'x', ARRAY['public'])"
                " RETURNING source_id"
            )
        ).scalar_one()
        session.commit()
    yield str(source)
    with platform_session(SETTINGS) as session:
        from sqlalchemy import select

        from backend.persistence.models import (
            PublishJobInfo,
            PublishJobRevision,
        )

        ws_ids = (
            select(WorkspaceInfo.workspace_id)
            .where(WorkspaceInfo.source_id == source)
            .scalar_subquery()
        )
        job_ids = (
            select(CollectionJobInfo.collection_job_id)
            .where(CollectionJobInfo.workspace_id.in_(ws_ids))
            .scalar_subquery()
        )
        run_ids = (
            select(CollectionRunInfo.collection_run_id)
            .where(CollectionRunInfo.collection_job_id.in_(job_ids))
            .scalar_subquery()
        )
        session.execute(
            delete(CollectionRunStep).where(
                CollectionRunStep.collection_run_id.in_(run_ids)
            )
        )
        session.execute(
            delete(CollectionRunInfo).where(
                CollectionRunInfo.collection_job_id.in_(job_ids)
            )
        )
        session.execute(
            delete(CollectionJobRevision).where(
                CollectionJobRevision.collection_job_id.in_(job_ids)
            )
        )
        session.execute(
            delete(CollectionJobInfo).where(CollectionJobInfo.workspace_id.in_(ws_ids))
        )
        pub_job_ids = (
            select(PublishJobInfo.publish_job_id)
            .where(PublishJobInfo.workspace_id.in_(ws_ids))
            .scalar_subquery()
        )
        session.execute(
            delete(PublishJobRevision).where(
                PublishJobRevision.publish_job_id.in_(pub_job_ids)
            )
        )
        session.execute(
            delete(PublishJobInfo).where(PublishJobInfo.workspace_id.in_(ws_ids))
        )
        session.execute(delete(WorkspaceInfo).where(WorkspaceInfo.source_id == source))
        session.execute(
            sql_text(
                "DELETE FROM public.db_source_info WHERE source_id = :sid"
            ).bindparams(sid=source)
        )
        session.commit()
    # 清理临时工作区目录（output/<source_id>/）
    import shutil

    from metaweave.core.clean_targets import resolve_source_workspace_dir

    try:
        shutil.rmtree(resolve_source_workspace_dir(source))
    except (OSError, ValueError):
        pass


def test_create_workspace_and_jobs_with_immutable_revisions(live_source_and_workspace):
    source_id = live_source_and_workspace
    workspace = create_workspace(SETTINGS, source_id, "生产")
    assert workspace["workspace_id"].startswith("ws_")
    workspace2 = create_workspace(SETTINGS, source_id, "测试", "隔离演练")
    assert workspace2["workspace_id"] != workspace["workspace_id"]
    with pytest.raises(WorkspaceNameExistsError):
        create_workspace(SETTINGS, source_id, "生产")

    job = create_job(
        SETTINGS,
        "collection",
        workspace["workspace_id"],
        "完整采集",
        None,
        JobConfigPayload(
            steps=["db", "ddl", "json", "md", "domain", "dim", "rel", "sql"],
            step_config={"domain": {"description": "订单库"}},
        ),
    )
    assert job["job_id"].startswith("coll_job_")
    assert job["revision"]["revision_no"] == 1
    assert job["revision"]["step_config"] == {"domain": {"description": "订单库"}}

    # 同名拒绝；非法步骤与未知参数明确报错
    with pytest.raises(JobNameExistsError):
        create_job(
            SETTINGS,
            "collection",
            workspace["workspace_id"],
            "完整采集",
            None,
            JobConfigPayload(steps=["db"]),
        )
    with pytest.raises(StepConfigError):
        create_job(
            SETTINGS,
            "collection",
            workspace["workspace_id"],
            "坏作业",
            None,
            JobConfigPayload(steps=["dim_config"]),
        )
    with pytest.raises(StepConfigError):
        create_job(
            SETTINGS,
            "collection",
            workspace["workspace_id"],
            "坏参数",
            None,
            JobConfigPayload(steps=["rel"], step_config={"rel": {"x": 1}}),
        )

    # 新版本：max+1；旧版本不可变
    rev2 = add_revision(
        SETTINGS,
        "collection",
        job["job_id"],
        JobConfigPayload(steps=["rel"], step_config=None, change_note="只修 Rel"),
    )
    assert rev2["revision_no"] == 2
    assert rev2["change_note"] == "只修 Rel"

    with platform_session(SETTINGS) as session:
        from backend.jobs.job_repository import get_revision

        first = get_revision(COLLECTION_JOB, session, job["job_id"], 1)
        assert list(first.steps) == [
            "db",
            "ddl",
            "json",
            "md",
            "domain",
            "dim",
            "rel",
            "sql",
        ]
        assert first.step_config == {"domain": {"description": "订单库"}}


def test_run_state_machine_partial_success(live_source_and_workspace):
    source_id = live_source_and_workspace
    workspace = create_workspace(SETTINGS, source_id, "状态机")
    job = create_job(
        SETTINGS,
        "collection",
        workspace["workspace_id"],
        "状态机作业",
        None,
        JobConfigPayload(steps=["ddl", "json", "md"]),
    )
    run_id = new_run_id()
    with platform_session(SETTINGS) as session:
        create_run(
            COLLECTION_RUN,
            session,
            run_id=run_id,
            job_id=job["job_id"],
            revision_no=1,
            steps=["ddl", "json", "md"],
            step_params={},
        )
        session.commit()

        # 仅执行本次请求新建的这一个运行：pending → running
        assert mark_run_running(COLLECTION_RUN, session, run_id)
        assert mark_step_running(COLLECTION_RUN, session, run_id, "ddl")
        finish_step(
            COLLECTION_RUN,
            session,
            run_id,
            "ddl",
            STATUS_SUCCESS,
            "完成",
            output_manifest={"files": ["ddl/orders.sql"]},
        )
        finish_step(COLLECTION_RUN, session, run_id, "json", STATUS_SUCCESS, "完成")
        finish_step(
            COLLECTION_RUN,
            session,
            run_id,
            "md",
            STATUS_BLOCKED,
            "本次请求的 JSON 上游失败",
        )
        final = finalize_run(COLLECTION_RUN, session, run_id)
        session.commit()
    assert final == STATUS_PARTIAL

    # 工作区运行列表能查到该运行（A26 后端口径）
    with platform_session(SETTINGS) as session:
        rows, total = list_runs_for_workspace(
            COLLECTION_RUN, session, workspace["workspace_id"], limit=20, offset=0
        )
        assert total == 1
        assert str(rows[0].collection_run_id) == str(run_id)
        assert rows[0].status == STATUS_PARTIAL


def test_publish_job_revision_and_publish_steps_whitelist(live_source_and_workspace):
    source_id = live_source_and_workspace
    workspace = create_workspace(SETTINGS, source_id, "发布侧")
    job = create_job(
        SETTINGS,
        "publish",
        workspace["workspace_id"],
        "整包发布",
        None,
        JobConfigPayload(
            steps=[
                "db",
                "ddl",
                "json",
                "domain",
                "table_schema",
                "dim_value",
                "rel",
                "sql",
            ]
        ),
    )
    assert job["job_id"].startswith("pub_job_")
    with pytest.raises(StepConfigError):
        create_job(
            SETTINGS,
            "publish",
            workspace["workspace_id"],
            "坏发布",
            None,
            JobConfigPayload(steps=["md"]),
        )
    with pytest.raises(StepConfigError):
        create_job(
            SETTINGS,
            "publish",
            workspace["workspace_id"],
            "带参发布",
            None,
            JobConfigPayload(steps=["db"], step_config={"db": {"x": 1}}),
        )
