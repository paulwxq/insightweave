"""采集/发布运行生命周期实库验证（doc 6.2 §6.3–§6.4、§8.2）。

沿用既有 skipif 模式：缺少平台库环境变量时跳过；建议在隔离测试库
（PLATFORM_DB_NAME 指向演练库）运行。worker 用替身，验证的是编排层
状态机、marker 写入与 manifest——真实生成逻辑由各步骤服务测试覆盖。
"""

import json
from uuid import uuid4

import pytest
from sqlalchemy import text

from backend.jobs.collection_run import (
    _WORKERS,
    create_collection_run,
    execute_collection_run,
    normalize_collection_steps,
)
from backend.jobs.job_service import JobConfigPayload, create_job as create_job_service
from backend.jobs.run_repository import (
    COLLECTION_RUN,
    STATUS_BLOCKED,
    STATUS_PARTIAL,
    STATUS_PENDING,
    STATUS_SUCCESS,
    get_run,
    list_run_steps,
)
from backend.persistence.platform_db import (
    load_platform_settings_from_env,
    load_project_env,
    platform_session,
)
from backend.publishing.precheck import PublishPrecheckError
from backend.publishing.publish_service import create_publish_run
from backend.workspace_files.markers import read_collection_marker
from backend.workspaces.service import create_workspace
from metaweave.core.clean_targets import (
    resolve_db_output_dir,
    resolve_ddl_output_dir,
    resolve_json_output_dir,
    resolve_workspace_root,
)
from metaweave.utils.source_id import normalize_run_uuid


def _settings():
    load_project_env()
    try:
        return load_platform_settings_from_env()
    except Exception:
        return None


SETTINGS = _settings()
pytestmark = pytest.mark.skipif(
    SETTINGS is None, reason="缺少 PLATFORM_DB_* 环境变量，跳过运行生命周期实库断言"
)

_SOURCE_NAME = "生命周期演练"


@pytest.fixture
def live_workspace(tmp_path_factory):
    """临时数据源 + 工作区；结束后逆外键序清理并删除目录。"""
    from sqlalchemy import delete as sa_delete

    from backend.persistence.models import (
        CollectionJobInfo,
        CollectionJobRevision,
        CollectionRunInfo,
        CollectionRunStep,
        DbProfile,
        PublishJobInfo,
        PublishJobRevision,
        PublishRunInfo,
        PublishRunStep,
        TableColumnProfile,
        TableDefinition,
        TableDomain,
        TableProfile,
        WorkspaceInfo,
    )

    with platform_session(SETTINGS) as session:
        source_id = session.execute(
            text(
                "INSERT INTO public.db_source_info (display_name, db_type, host, port,"
                " db_name, username, password_ciphertext, selected_schemas)"
                f" VALUES ('{_SOURCE_NAME}', 'postgresql', 'h', 5432, 'd', 'u', 'x',"
                " ARRAY['public']) RETURNING source_id"
            )
        ).scalar_one()
        session.commit()
    workspace = create_workspace(SETTINGS, source_id, "生命周期")
    workspace_id = workspace["workspace_id"]
    yield source_id, workspace_id

    import shutil

    from metaweave.core.clean_targets import resolve_source_workspace_dir

    with platform_session(SETTINGS) as session:
        ws = (
            session.execute(
                text(
                    "SELECT workspace_id FROM public.workspace_info WHERE source_id = :sid"
                ).bindparams(sid=source_id)
            )
            .scalars()
            .all()
        )
        for wid in ws:
            coll_jobs = (
                session.execute(
                    text(
                        "SELECT collection_job_id FROM public.collection_job_info"
                        " WHERE workspace_id = :w"
                    ).bindparams(w=wid),
                )
                .scalars()
                .all()
            )
            for jid in coll_jobs:
                runs = (
                    session.execute(
                        text(
                            "SELECT collection_run_id FROM public.collection_run_info"
                            " WHERE collection_job_id = :j"
                        ).bindparams(j=jid),
                    )
                    .scalars()
                    .all()
                )
                for rid in runs:
                    session.execute(
                        sa_delete(CollectionRunStep).where(
                            CollectionRunStep.collection_run_id == rid
                        )
                    )
                session.execute(
                    sa_delete(CollectionRunInfo).where(
                        CollectionRunInfo.collection_job_id == jid
                    )
                )
                session.execute(
                    sa_delete(CollectionJobRevision).where(
                        CollectionJobRevision.collection_job_id == jid
                    )
                )
                session.execute(
                    sa_delete(CollectionJobInfo).where(
                        CollectionJobInfo.collection_job_id == jid
                    )
                )
            pub_jobs = (
                session.execute(
                    text(
                        "SELECT publish_job_id FROM public.publish_job_info"
                        " WHERE workspace_id = :w"
                    ).bindparams(w=wid),
                )
                .scalars()
                .all()
            )
            for jid in pub_jobs:
                runs = (
                    session.execute(
                        text(
                            "SELECT publish_run_id FROM public.publish_run_info"
                            " WHERE publish_job_id = :j"
                        ).bindparams(j=jid),
                    )
                    .scalars()
                    .all()
                )
                for rid in runs:
                    session.execute(
                        sa_delete(PublishRunStep).where(
                            PublishRunStep.publish_run_id == rid
                        )
                    )
                # 五张内容行引用发布运行，须先于运行删除（doc 6.1 §14.1）
                session.execute(
                    sa_delete(DbProfile).where(DbProfile.publish_run_id.in_(runs))
                )
                session.execute(
                    sa_delete(TableDomain).where(TableDomain.publish_run_id.in_(runs))
                )
                session.execute(
                    sa_delete(TableDefinition).where(
                        TableDefinition.publish_run_id.in_(runs)
                    )
                )
                session.execute(
                    sa_delete(TableProfile).where(TableProfile.publish_run_id.in_(runs))
                )
                session.execute(
                    sa_delete(TableColumnProfile).where(
                        TableColumnProfile.publish_run_id.in_(runs)
                    )
                )
                session.execute(
                    sa_delete(PublishRunInfo).where(
                        PublishRunInfo.publish_job_id == jid
                    )
                )
                session.execute(
                    sa_delete(PublishJobRevision).where(
                        PublishJobRevision.publish_job_id == jid
                    )
                )
                session.execute(
                    sa_delete(PublishJobInfo).where(
                        PublishJobInfo.publish_job_id == jid
                    )
                )
        session.execute(
            sa_delete(WorkspaceInfo).where(WorkspaceInfo.source_id == source_id)
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


def _fake_worker(outcome):
    def _run(ctx):
        return outcome

    return _run


def test_default_run_one_run_id_steps_and_markers(live_workspace, monkeypatch):
    """A04：默认执行多步骤 → 一个运行 UUID、逐步骤行、marker 与 manifest。"""
    source_id, workspace_id = live_workspace
    job = create_job_service(
        SETTINGS,
        "collection",
        workspace_id,
        "默认链",
        None,
        JobConfigPayload(steps=["db", "ddl", "json"]),
    )
    monkeypatch.setitem(
        _WORKERS,
        "db",
        _fake_worker(
            type(
                "O",
                (),
                {
                    "success": True,
                    "message": "ok",
                    "input_files": [],
                    "output_files": ["db/database.json"],
                },
            )()
        ),
    )
    monkeypatch.setitem(
        _WORKERS,
        "ddl",
        _fake_worker(
            type(
                "O",
                (),
                {
                    "success": True,
                    "message": "ok",
                    "input_files": [],
                    "output_files": ["ddl/a.sql"],
                },
            )()
        ),
    )
    monkeypatch.setitem(
        _WORKERS,
        "json",
        _fake_worker(
            type(
                "O",
                (),
                {
                    "success": True,
                    "message": "ok",
                    "input_files": [("ddl/a.sql", str(uuid4()))],
                    "output_files": ["json/a.json"],
                },
            )()
        ),
    )
    result = create_collection_run(
        SETTINGS,
        job["job_id"],
        revision_no=1,
        mode="default",
        steps=None,
        step_config=None,
    )
    assert result["steps"] == ["db", "ddl", "json"]
    run_id = normalize_run_uuid(result["collection_run_id"])

    execute_collection_run(SETTINGS, run_id, result["steps"])

    with platform_session(SETTINGS) as session:
        run = get_run(COLLECTION_RUN, session, run_id)
        assert run.status == STATUS_SUCCESS
        steps = list_run_steps(COLLECTION_RUN, session, run_id)
        # doc 10.1 §4.6：返回顺序为启动时间线（started_at、step_name）——
        # db 与 ddl 并行启动、次序不定，只约束依赖先后（json 晚于 ddl）。
        assert sorted(s.step_name for s in steps) == ["db", "ddl", "json"]
        assert [s.step_name for s in steps][-1] == "json"
        assert all(s.status == STATUS_SUCCESS for s in steps)
        ddl_started = next(s for s in steps if s.step_name == "ddl").started_at
        json_started = next(s for s in steps if s.step_name == "json").started_at
        assert ddl_started is not None and json_started is not None
        assert ddl_started <= json_started
        json_step = next(s for s in steps if s.step_name == "json")
        assert json_step.input_manifest["files"][0]["path"] == "ddl/a.sql"
        assert json_step.output_manifest == {"files": ["json/a.json"]}
    for step, directory in (
        ("db", resolve_db_output_dir(source_id, workspace_id)),
        ("ddl", resolve_ddl_output_dir(source_id, workspace_id)),
        ("json", resolve_json_output_dir(source_id, workspace_id)),
    ):
        marker, error = read_collection_marker(directory)
        assert error is None and marker is not None
        assert marker.run_id == str(run_id) and marker.status == "success"


def test_disordered_request_is_reordered(live_workspace):
    """A06：请求 [md, ddl, json] → 执行序 [ddl, json, md]（既有拓扑排序复用）。"""
    assert normalize_collection_steps(["md", "ddl", "json"]) == ["ddl", "json", "md"]
    source_id, workspace_id = live_workspace
    job = create_job_service(
        SETTINGS,
        "collection",
        workspace_id,
        "乱序链",
        None,
        JobConfigPayload(steps=["db", "ddl", "json", "md"]),
    )
    result = create_collection_run(
        SETTINGS,
        job["job_id"],
        revision_no=1,
        mode="custom",
        steps=["md", "ddl", "json"],
        step_config=None,
    )
    assert result["steps"] == ["ddl", "json", "md"]


def test_partial_failure_blocks_downstream(live_workspace, monkeypatch):
    """A07：json 失败 → md blocked（同请求上游失败）；整体 partial_success。"""
    source_id, workspace_id = live_workspace
    job = create_job_service(
        SETTINGS,
        "collection",
        workspace_id,
        "部分链",
        None,
        JobConfigPayload(steps=["db", "ddl", "json", "md"]),
    )
    from backend.data_sources.step_context import StepOutcome

    monkeypatch.setitem(
        _WORKERS,
        "db",
        _fake_worker(StepOutcome(True, "ok", output_files=["db/database.json"])),
    )
    monkeypatch.setitem(
        _WORKERS,
        "ddl",
        _fake_worker(StepOutcome(True, "ok", output_files=["ddl/a.sql"])),
    )
    monkeypatch.setitem(
        _WORKERS, "json", _fake_worker(StepOutcome(False, "JSON 采集失败（模拟）"))
    )
    # md 未执行：不应被调用
    monkeypatch.setitem(
        _WORKERS,
        "md",
        _fake_worker(StepOutcome(True, "不应执行")),
    )
    result = create_collection_run(
        SETTINGS,
        job["job_id"],
        revision_no=1,
        mode="custom",
        steps=["db", "ddl", "json", "md"],
        step_config=None,
    )
    run_id = normalize_run_uuid(result["collection_run_id"])
    execute_collection_run(SETTINGS, run_id, result["steps"])

    with platform_session(SETTINGS) as session:
        run = get_run(COLLECTION_RUN, session, run_id)
        assert run.status == STATUS_PARTIAL
        steps = {
            s.step_name: s for s in list_run_steps(COLLECTION_RUN, session, run_id)
        }
        assert steps["db"].status == STATUS_SUCCESS
        assert steps["json"].status == "failed"
        assert steps["md"].status == STATUS_BLOCKED
        assert "JSON 未成功" in steps["md"].message
    # blocked 不写 marker；failed 写 failed marker
    json_marker, _ = read_collection_marker(
        resolve_json_output_dir(source_id, workspace_id)
    )
    assert json_marker.status == "failed"
    md_dir = resolve_workspace_root(source_id, workspace_id) / "md"
    from backend.workspace_files.markers import read_collection_marker as _read

    assert _read(md_dir) == (None, None)


def test_stale_pending_run_not_taken_over(live_workspace, monkeypatch):
    """A25：遗留 pending 运行不被新请求的后台任务接管，状态原地保留。"""
    source_id, workspace_id = live_workspace
    job = create_job_service(
        SETTINGS,
        "collection",
        workspace_id,
        "接管链",
        None,
        JobConfigPayload(steps=["db"]),
    )
    from backend.data_sources.step_context import StepOutcome

    monkeypatch.setitem(
        _WORKERS,
        "db",
        _fake_worker(StepOutcome(True, "ok", output_files=["db/database.json"])),
    )
    stale = create_collection_run(
        SETTINGS,
        job["job_id"],
        revision_no=1,
        mode="default",
        steps=None,
        step_config=None,
    )
    fresh = create_collection_run(
        SETTINGS,
        job["job_id"],
        revision_no=1,
        mode="default",
        steps=None,
        step_config=None,
    )
    # 只执行新请求的运行
    execute_collection_run(
        SETTINGS, normalize_run_uuid(fresh["collection_run_id"]), fresh["steps"]
    )

    with platform_session(SETTINGS) as session:
        stale_run = get_run(
            COLLECTION_RUN, session, normalize_run_uuid(stale["collection_run_id"])
        )
        assert stale_run.status == STATUS_PENDING
        assert stale_run.started_at is None and stale_run.finished_at is None
        fresh_run = get_run(
            COLLECTION_RUN, session, normalize_run_uuid(fresh["collection_run_id"])
        )
        assert fresh_run.status == STATUS_SUCCESS


def test_publish_precheck_rejects_whole_request_without_records(live_workspace):
    """A12/A24：所需目录无 marker → 整单 400、不创建运行、不留业务记录。"""
    source_id, workspace_id = live_workspace
    from backend.jobs.job_service import create_job as create_pub_job

    job = create_pub_job(
        SETTINGS,
        "publish",
        workspace_id,
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
            ],
            # doc 7.2 §6/§7.3：选择 dim_value/domain 的版本必须携带完整清单
            step_config={
                "dim_value": {
                    "tables": [
                        {
                            "schema_name": "public",
                            "object_name": "dim_t",
                            "embedding_col": ["code"],
                        }
                    ]
                },
                "domain": {
                    "domains": [
                        {"name": "默认域", "description": "说明", "tables": []}
                    ]
                },
            },
        ),
    )
    with pytest.raises(PublishPrecheckError) as excinfo:
        create_publish_run(
            SETTINGS,
            job["job_id"],
            revision_no=1,
            mode="custom",
            steps=["db", "dim_value", "sql"],
            step_config=None,
        )
    failed_steps = {item["step"] for item in excinfo.value.failures}
    assert {"db", "dim_value", "sql"} <= failed_steps
    with platform_session(SETTINGS) as session:
        count = session.execute(
            text(
                "SELECT count(*) FROM public.publish_run_info pr"
                " JOIN public.publish_job_info pj ON pj.publish_job_id = pr.publish_job_id"
                " WHERE pj.workspace_id = :w"
            ).bindparams(w=workspace_id)
        ).scalar_one()
        assert count == 0
        step_count = session.execute(
            text(
                "SELECT count(*) FROM public.publish_run_step ps"
                " JOIN public.publish_run_info pr ON pr.publish_run_id = ps.publish_run_id"
                " JOIN public.publish_job_info pj ON pj.publish_job_id = pr.publish_job_id"
                " WHERE pj.workspace_id = :w"
            ).bindparams(w=workspace_id)
        ).scalar_one()
        assert step_count == 0


def test_publish_execution_recheck_blocks_changed_step(live_workspace, monkeypatch):
    """A27（doc 7.2 §5.2.1 口径）：预检通过创建运行后删除 dim_value 有效清单
    引用的 JSON 画像（执行期仍复查的外部条件）→ 该步 blocked、其余成功、
    整体 partial。dim_value 不再以 dim_tables.yaml 为运行输入。"""

    from backend.jobs.job_service import create_job as create_job_service
    from backend.jobs.run_repository import PUBLISH_RUN, get_run, list_run_steps
    from backend.publishing.publish_service import (
        create_publish_run,
        execute_publish_run,
    )
    from backend.workspace_files.markers import write_collection_marker

    source_id, workspace_id = live_workspace
    root = resolve_workspace_root(source_id, workspace_id)
    db_dir = root / "db"
    dim_dir = root / "dim"
    json_dir = root / "json"
    sql_dir = root / "sql"
    db_dir.mkdir(parents=True, exist_ok=True)
    dim_dir.mkdir(parents=True, exist_ok=True)
    json_dir.mkdir(parents=True, exist_ok=True)
    sql_dir.mkdir(parents=True, exist_ok=True)
    _write_db_profile_json(source_id, db_dir)
    # dim_value 有效清单引用的表/字段必须存在于当前 JSON 画像（§5.2.1）；
    # 画像库名须与登记库名（'d'）一致，否则按超出登记范围拒绝（审核问题 2）
    (json_dir / "d.public.dim_t.json").write_text(
        json.dumps(
            {
                "object_info": {
                    "database": "d",
                    "schema_name": "public",
                    "object_name": "dim_t",
                    "object_type": "table",
                },
                "column_profiles": {"code": {"ordinal_position": 1}},
            }
        ),
        encoding="utf-8",
    )
    (sql_dir / "qs_demo_pair.json").write_text(
        json.dumps([{"question": "q", "sql": "select 1"}]), encoding="utf-8"
    )
    for directory in (db_dir, dim_dir, sql_dir):
        write_collection_marker(directory, uuid4(), "success")

    job = create_job_service(
        SETTINGS,
        "publish",
        workspace_id,
        "A27 发布",
        None,
        JobConfigPayload(
            steps=["db", "dim_value", "sql"],
            step_config={
                "dim_value": {
                    "tables": [
                        {
                            "schema_name": "public",
                            "object_name": "dim_t",
                            "embedding_col": ["code"],
                        }
                    ]
                }
            },
        ),
    )
    # db/sql 的实际下游写入用替身（本用例聚焦执行期复查语义）；
    # 替身沿用真实 publisher 契约：自行写回成功终态。
    # dim_value 走真实复查路径（其 publisher 不会被调用）。
    from backend.publishing import publish_service as _ps

    def _make_stub(step_name):
        def _run(settings, run_id, workspace_id, *, input_manifest, payload):
            _ps._record_terminal(
                settings,
                run_id,
                step_name,
                "success",
                "stub 发布完成",
                input_manifest=input_manifest,
            )

        return _run

    monkeypatch.setitem(_ps._PUBLISHERS, "db", _make_stub("db"))
    monkeypatch.setitem(_ps._PUBLISHERS, "sql", _make_stub("sql"))
    result = create_publish_run(
        SETTINGS,
        job["job_id"],
        revision_no=1,
        mode="custom",
        steps=["db", "dim_value", "sql"],
        step_config=None,
    )
    run_id = normalize_run_uuid(result["publish_run_id"])
    # 预检通过、运行已创建；启动前删除有效清单引用的 JSON 画像（A27 场景，
    # doc 7.2 口径：外部条件失效 → blocked，不静默改用其他配置）
    (json_dir / "d.public.dim_t.json").unlink()
    execute_publish_run(SETTINGS, run_id)

    with platform_session(SETTINGS) as session:
        run = get_run(PUBLISH_RUN, session, run_id)
        assert run.status == STATUS_PARTIAL
        steps = {s.step_name: s for s in list_run_steps(PUBLISH_RUN, session, run_id)}
        assert steps["db"].status == STATUS_SUCCESS
        assert steps["sql"].status == STATUS_SUCCESS
        assert steps["dim_value"].status == STATUS_BLOCKED
        assert steps["dim_value"].started_at is None
        assert steps["dim_value"].finished_at is not None
        assert (
            "dim" in steps["dim_value"].message or "缺失" in steps["dim_value"].message
        )


def _write_db_profile_json(source_id: str, db_dir) -> None:
    document = {
        "schema_version": 1,
        "source_id": source_id,
        "collected_at": "2026-10-21T10:00:00+00:00",
        "database": {
            "type": "postgresql",
            "host": "h",
            "name": "demo",
            "port": 5432,
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
    }
    (db_dir / "database.json").write_text(
        json.dumps(document, ensure_ascii=False), encoding="utf-8"
    )
