"""发布并行准入与计划冻结实库验证（doc 10.2 §5.1–§5.3、§8、§9.5/§9.13）。

覆盖：同源互斥 409（跨工作区）、全局名额满额 409、同工作区采集互斥 409、
execution_plan 创建时冻结（serial/parallel）、采集创建被活动发布拒绝、
publish_target_guard 的建立/同 owner 拒绝/按 owner 清除，以及并行计划的
三通道调度（替身 publisher + 实库状态机）。

沿用既有 skipif 模式：缺少平台库环境变量时跳过；在演练库
（PLATFORM_DB_NAME 指向 metaweave_rehearsal_final）运行。
"""

from __future__ import annotations

import threading
import uuid
from contextlib import contextmanager

import os

import pytest

from backend.jobs.job_service import JobConfigPayload, create_job as create_job_service
from backend.jobs.run_repository import (
    PUBLISH_RUN,
    STATUS_BLOCKED,
    STATUS_SUCCESS,
    get_run,
    list_run_steps,
)
from backend.persistence.platform_db import (
    load_platform_settings_from_env,
    load_project_env,
    platform_session,
)
from backend.publishing import publish_service as _ps
from backend.publishing.publish_service import (
    create_publish_run,
    execute_publish_run,
)
from backend.publishing.repository import (
    PublishCapacityExhaustedError,
    SourcePublishActiveError,
    WorkspaceCollectionActiveError,
    WorkspacePublishActiveError,
)
from backend.publishing.scheduling import build_execution_plan
from backend.workspaces.service import create_workspace


def _settings():
    load_project_env()
    try:
        return load_platform_settings_from_env()
    except Exception:
        return None


SETTINGS = _settings()
pytestmark = pytest.mark.skipif(
    SETTINGS is None
    or os.environ.get("METAWEAVE_RUN_LIVE_TESTS") != "1"
    or SETTINGS.name != "metaweave_rehearsal_final",
    reason="发布准入实库断言仅允许指向演练库 metaweave_rehearsal_final"
    "（需先 export PLATFORM_DB_NAME=metaweave_rehearsal_final）",
)


@pytest.fixture()
def admission_env(tmp_path_factory):
    """两个同源工作区 + 各自发布作业；结束后清理。"""
    from sqlalchemy import text

    from metaweave.utils.file_utils import get_project_root

    # 让工作区目录落在临时根，避免污染真实 output/
    import backend.workspaces.service as workspace_service

    original_root = get_project_root
    import metaweave.utils.file_utils as file_utils

    monkeypatch_root = tmp_path_factory.mktemp("publish_admission")
    file_utils.get_project_root = lambda: monkeypatch_root
    try:
        with platform_session(SETTINGS) as session:
            source_id = session.execute(
                text(
                    "INSERT INTO public.db_source_info (display_name, db_type, host,"
                    " port, db_name, username, password_ciphertext, selected_schemas)"
                    f" VALUES ('adm_{uuid.uuid4().hex[:8]}', 'postgresql', 'h', 5432,"
                    " 'd', 'u', 'x', ARRAY['public']) RETURNING source_id"
                )
            ).scalar_one()
            session.commit()
        ws1 = create_workspace(SETTINGS, source_id, "准入一")["workspace_id"]
        ws2 = create_workspace(SETTINGS, source_id, "准入二")["workspace_id"]
        job1 = create_job_service(
            SETTINGS,
            "publish",
            ws1,
            "准入作业一",
            None,
            JobConfigPayload(steps=["db"], step_config=None),
        )
        job2 = create_job_service(
            SETTINGS,
            "publish",
            ws2,
            "准入作业二",
            None,
            JobConfigPayload(steps=["db"], step_config=None),
        )
        yield source_id, ws1, ws2, job1["job_id"], job2["job_id"]
    finally:
        file_utils.get_project_root = original_root
        from sqlalchemy import delete as sa_delete, select

        from backend.persistence.models import (
            CollectionJobInfo,
            CollectionJobRevision,
            CollectionRunInfo,
            CollectionRunStep,
            PublishJobInfo,
            PublishJobRevision,
            PublishRunInfo,
            PublishRunStep,
            WorkspaceInfo,
        )

        # 按工作区驱动全量清理（测试内可能创建采集作业/额外发布作业）
        with platform_session(SETTINGS) as session:
            for ws_id in (ws1, ws2):
                coll_job_ids = [
                    row[0]
                    for row in session.execute(
                        select(CollectionJobInfo.collection_job_id).where(
                            CollectionJobInfo.workspace_id == ws_id
                        )
                    )
                ]
                for jid in coll_job_ids:
                    run_ids = [
                        row[0]
                        for row in session.execute(
                            select(CollectionRunInfo.collection_run_id).where(
                                CollectionRunInfo.collection_job_id == jid
                            )
                        )
                    ]
                    for rid in run_ids:
                        session.execute(
                            sa_delete(CollectionRunStep).where(
                                CollectionRunStep.collection_run_id == rid
                            )
                        )
                        session.execute(
                            sa_delete(CollectionRunInfo).where(
                                CollectionRunInfo.collection_run_id == rid
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
                pub_job_ids = [
                    row[0]
                    for row in session.execute(
                        select(PublishJobInfo.publish_job_id).where(
                            PublishJobInfo.workspace_id == ws_id
                        )
                    )
                ]
                for jid in pub_job_ids:
                    run_ids = [
                        row[0]
                        for row in session.execute(
                            select(PublishRunInfo.publish_run_id).where(
                                PublishRunInfo.publish_job_id == jid
                            )
                        )
                    ]
                    for rid in run_ids:
                        session.execute(
                            sa_delete(PublishRunStep).where(
                                PublishRunStep.publish_run_id == rid
                            )
                        )
                        session.execute(
                            sa_delete(PublishRunInfo).where(
                                PublishRunInfo.publish_run_id == rid
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
                    sa_delete(WorkspaceInfo).where(WorkspaceInfo.workspace_id == ws_id)
                )
            session.execute(
                text("DELETE FROM public.db_source_info WHERE source_id = :sid"),
                {"sid": source_id},
            )
            session.commit()


def _make_ready(admission_env, tmp_path, workspace_idx=0):
    """为工作区写 db/ddl 预检输入（marker + database.json）。"""
    import json

    from backend.workspace_files.markers import write_collection_marker
    from metaweave.core.clean_targets import resolve_workspace_root
    from metaweave.utils.file_utils import resolve_project_path

    source_id, ws1, ws2, _j1, _j2 = admission_env
    ws = (ws1, ws2)[workspace_idx]
    root = resolve_workspace_root(source_id, ws)
    db_dir = root / "db"
    db_dir.mkdir(parents=True, exist_ok=True)
    (db_dir / "database.json").write_text(
        json.dumps(
            {
                "database": "d",
                "schema_version": 1,
                "size_bytes": 1,
                "object_count": 1,
            }
        ),
        encoding="utf-8",
    )
    write_collection_marker(db_dir, uuid.uuid4(), "success")
    ddl_dir = root / "ddl"
    ddl_dir.mkdir(parents=True, exist_ok=True)
    (ddl_dir / "d.public.orders.sql").write_text("-- managed ddl\n", encoding="utf-8")
    write_collection_marker(ddl_dir, uuid.uuid4(), "success")
    return root


def test_execution_plan_frozen_at_creation(admission_env, tmp_path, monkeypatch):
    """§8：计划在创建时冻结进 execution_plan（并行唯一模式）。"""
    source_id, ws1, _ws2, job1, _job2 = admission_env
    _make_ready(admission_env, tmp_path)

    def _stub_db(settings, run_id, workspace_id, *, input_manifest, payload):
        _ps._record_terminal(
            settings,
            run_id,
            "db",
            STATUS_SUCCESS,
            "stub db",
            input_manifest=input_manifest,
        )

    monkeypatch.setitem(_ps._PUBLISHERS, "db", _stub_db)
    result = create_publish_run(
        SETTINGS, job1, revision_no=1, mode="default", steps=None, step_config=None
    )
    run_id = uuid.UUID(result["publish_run_id"])
    with platform_session(SETTINGS) as session:
        run = get_run(PUBLISH_RUN, session, run_id)
        plan = run.execution_plan
    assert plan["plan_version"] == 1
    assert plan["selected_steps"] == ["db"]
    assert plan["order_predecessors"] == {"db": []}
    assert plan["channel_limits"] == {"pg": 1, "milvus": 1, "neo4j": 1}
    assert plan["run_concurrency"] == 3
    # 终态释放名额（默认上限 1）
    execute_publish_run(SETTINGS, run_id)
    with platform_session(SETTINGS) as session:
        assert get_run(PUBLISH_RUN, session, run_id).status == STATUS_SUCCESS


def test_same_source_second_workspace_rejected_409(admission_env, tmp_path):
    """§5.1：同源另一工作区的活动发布（pending 占用）阻断创建。"""
    source_id, ws1, ws2, job1, job2 = admission_env
    _make_ready(admission_env, tmp_path, workspace_idx=0)
    _make_ready(admission_env, tmp_path, workspace_idx=1)
    result = create_publish_run(
        SETTINGS, job1, revision_no=1, mode="default", steps=None, step_config=None
    )
    with pytest.raises(SourcePublishActiveError) as exc_info:
        create_publish_run(
            SETTINGS, job2, revision_no=1, mode="default", steps=None, step_config=None
        )
    assert exc_info.value.blocking["run_id"] == result["publish_run_id"]
    assert exc_info.value.blocking["status"] == "pending"


def test_capacity_exhausted_409(admission_env, tmp_path):
    """§5.3：全局名额 1 时，另一源的活动发布使创建满额 409（不排队）。"""
    from sqlalchemy import text

    source_id, ws1, _ws2, job1, _j2 = admission_env
    _make_ready(admission_env, tmp_path)
    create_publish_run(
        SETTINGS, job1, revision_no=1, mode="default", steps=None, step_config=None
    )
    # 第二个独立源 + 工作区 + 作业（同源检查不会命中，只能被名额挡住）
    with platform_session(SETTINGS) as session:
        other_source = session.execute(
            text(
                "INSERT INTO public.db_source_info (display_name, db_type, host,"
                " port, db_name, username, password_ciphertext, selected_schemas)"
                f" VALUES ('cap_{uuid.uuid4().hex[:8]}', 'postgresql', 'h', 5432,"
                " 'd', 'u', 'x', ARRAY['public']) RETURNING source_id"
            )
        ).scalar_one()
        session.commit()
    try:
        other_ws = create_workspace(SETTINGS, other_source, "名额工作区")[
            "workspace_id"
        ]
        other_job = create_job_service(
            SETTINGS,
            "publish",
            other_ws,
            "名额作业",
            None,
            JobConfigPayload(steps=["db"], step_config=None),
        )
        with pytest.raises(PublishCapacityExhaustedError) as exc_info:
            create_publish_run(
                SETTINGS,
                other_job["job_id"],
                revision_no=1,
                mode="default",
                steps=None,
                step_config=None,
            )
        assert exc_info.value.blocking == {"limit": 1}
    finally:
        from sqlalchemy import delete as sa_delete

        from backend.persistence.models import (
            PublishJobInfo,
            PublishJobRevision,
            WorkspaceInfo,
        )

        with platform_session(SETTINGS) as session:
            session.execute(
                sa_delete(PublishJobRevision).where(
                    PublishJobRevision.publish_job_id == other_job["job_id"]
                )
            )
            session.execute(
                sa_delete(PublishJobInfo).where(
                    PublishJobInfo.publish_job_id == other_job["job_id"]
                )
            )
            session.execute(
                sa_delete(WorkspaceInfo).where(WorkspaceInfo.workspace_id == other_ws)
            )
            session.execute(
                text("DELETE FROM public.db_source_info WHERE source_id = :sid"),
                {"sid": other_source},
            )
            session.commit()


def test_workspace_collection_active_409(admission_env, tmp_path):
    """§5.2：同工作区 pending 采集运行阻断发布创建（pending 也占用）。"""
    from sqlalchemy import text

    source_id, ws1, _ws2, job1, _j2 = admission_env
    _make_ready(admission_env, tmp_path)
    coll_job = create_job_service(
        SETTINGS,
        "collection",
        ws1,
        "准入采集",
        None,
        JobConfigPayload(steps=["ddl"], step_config=None),
    )
    with platform_session(SETTINGS) as session:
        session.execute(
            text(
                "INSERT INTO public.collection_run_info (collection_run_id,"
                " collection_job_id, revision_no, trigger_type, status)"
                " VALUES (:rid, :jid, 1, 'manual', 'pending')"
            ).bindparams(rid=uuid.uuid4(), jid=coll_job["job_id"])
        )
        session.commit()
    with pytest.raises(WorkspaceCollectionActiveError):
        create_publish_run(
            SETTINGS, job1, revision_no=1, mode="default", steps=None, step_config=None
        )


def test_collection_creation_blocked_by_active_publish(admission_env, tmp_path):
    """§5.2：活动发布（pending）阻断采集创建（409 workspace_publish_active）。"""
    from backend.jobs.collection_run import create_collection_run

    source_id, ws1, _ws2, job1, _j2 = admission_env
    _make_ready(admission_env, tmp_path)
    create_publish_run(
        SETTINGS, job1, revision_no=1, mode="default", steps=None, step_config=None
    )
    coll_job = create_job_service(
        SETTINGS,
        "collection",
        ws1,
        "准入采集二",
        None,
        JobConfigPayload(steps=["ddl"], step_config=None),
    )
    with pytest.raises(WorkspacePublishActiveError):
        create_collection_run(
            SETTINGS,
            coll_job["job_id"],
            revision_no=1,
            mode="default",
            steps=None,
            step_config=None,
        )


def test_target_guard_reserve_refuse_same_owner_and_release():
    """§5.1：保留记录建立→任何 owner（含同 owner_id）拒绝→按 owner 清除。

    直接经 ORM 操作演练库 publish_target_guard（表由升级脚本创建）。
    """
    from backend.persistence.models import PublishTargetGuard
    from backend.publishing.target_lock import (
        read_target_guard,
        release_target_guard,
        reserve_target_guard,
    )

    target_key = f"test|{uuid.uuid4().hex}"
    owner = ("publish_run", str(uuid.uuid4()))
    try:
        with platform_session(SETTINGS) as session:
            assert reserve_target_guard(session, target_key, *owner) is None
            session.commit()
        with platform_session(SETTINGS) as session:
            existing = reserve_target_guard(session, target_key, *owner)
            assert existing is not None, "同 owner_id 重入也必须被拒"
            assert existing["owner_kind"] == owner[0]
            # 他 owner 同样被拒
            other = reserve_target_guard(
                session, target_key, "deletion_task", str(uuid.uuid4())
            )
            assert other is not None
            session.rollback()
        with platform_session(SETTINGS) as session:
            assert release_target_guard(session, target_key, *owner) is True
            # 不匹配 owner 不清除
            reserve_target_guard(session, target_key, *owner)
            session.commit()
            assert (
                release_target_guard(
                    session, target_key, "deletion_task", "someone-else"
                )
                is False
            )
            session.commit()
        with platform_session(SETTINGS) as session:
            assert read_target_guard(session, target_key) is not None
    finally:
        with platform_session(SETTINGS) as session:
            guard = session.get(PublishTargetGuard, target_key)
            if guard is not None:
                session.delete(guard)
                session.commit()


def test_parallel_mode_dispatches_three_channels(admission_env, tmp_path, monkeypatch):
    """§9.1（替身 publisher + 实库状态机）：并行计划下 PG 通道串行、
    首步骤可重叠；状态写回与汇总走真实运行表。"""
    import json as _json

    from backend.workspace_files.markers import write_collection_marker
    from metaweave.core.clean_targets import resolve_workspace_root

    source_id, ws1, _ws2, job1, _j2 = admission_env
    root = resolve_workspace_root(source_id, ws1)
    db_dir = root / "db"
    db_dir.mkdir(parents=True, exist_ok=True)
    (db_dir / "database.json").write_text(
        _json.dumps(
            {"database": "d", "schema_version": 1, "size_bytes": 1, "object_count": 1}
        ),
        encoding="utf-8",
    )
    write_collection_marker(db_dir, uuid.uuid4(), "success")
    sql_dir = root / "sql"
    sql_dir.mkdir(parents=True, exist_ok=True)
    (sql_dir / "q.json").write_text(
        _json.dumps([{"question": "q", "sql": "select 1"}]), encoding="utf-8"
    )
    write_collection_marker(sql_dir, uuid.uuid4(), "success")

    parallel_job = create_job_service(
        SETTINGS,
        "publish",
        ws1,
        "并行计划",
        None,
        JobConfigPayload(steps=["db", "sql"], step_config=None),
    )

    lock = threading.Lock()
    timeline: list[tuple[str, str]] = []
    db_entered = threading.Event()
    db_release = threading.Event()

    def _stub(step_name):
        def _run(settings, run_id, workspace_id, *, input_manifest, payload):
            with lock:
                timeline.append((step_name, "start"))
            if step_name == "db":
                db_entered.set()
                db_release.wait(timeout=10)
            with lock:
                timeline.append((step_name, "end"))
            _ps._record_terminal(
                settings,
                run_id,
                step_name,
                STATUS_SUCCESS,
                f"stub {step_name}",
                input_manifest=input_manifest,
            )

        return _run

    monkeypatch.setattr(
        _ps,
        "_PUBLISHERS",
        {
            "db": _stub("db"),
            "sql": _stub("sql"),
        },
    )
    result = create_publish_run(
        SETTINGS,
        parallel_job["job_id"],
        revision_no=1,
        mode="default",
        steps=None,
        step_config=None,
    )
    run_id = uuid.UUID(result["publish_run_id"])
    import threading as _threading

    executor_thread = _threading.Thread(
        target=execute_publish_run, args=(SETTINGS, run_id)
    )
    executor_thread.start()
    assert db_entered.wait(timeout=10), "db 未启动"
    # sql（Milvus 通道首步骤）可与 db 重叠
    deadline_overlap = None
    import time

    for _ in range(200):
        with lock:
            started = [s for s, phase in timeline if phase == "start"]
        if "sql" in started:
            deadline_overlap = True
            break
        time.sleep(0.02)
    assert deadline_overlap, f"sql 未与 db 重叠: {timeline}"
    db_release.set()
    executor_thread.join(timeout=15)
    with platform_session(SETTINGS) as session:
        run = get_run(PUBLISH_RUN, session, run_id)
        assert run.status == STATUS_SUCCESS
        statuses = {
            row.step_name: row.status
            for row in list_run_steps(PUBLISH_RUN, session, run_id)
        }
    assert statuses == {"db": STATUS_SUCCESS, "sql": STATUS_SUCCESS}


def test_target_lock_real_connection_acquire_timeout_release():
    """§5.1 真实路径回归（Codex R1 意见 1）：set_config 语句超时 + advisory
    lock 取得/竞争超时/释放 + 存活探测，全部走真实演练库连接。"""
    import time

    from backend.publishing.target_lock import (
        TargetLockTimeoutError,
        acquire_target_lock,
        ensure_target_lock_alive,
        neo4j_target_lock_key,
        release_target_lock,
    )

    # 键值只须彼此一致且不与真实目标冲突；URI 须能通过规范化校验
    key = neo4j_target_lock_key("neo4j://real-lock-test:7687", "guard")
    conn = acquire_target_lock(SETTINGS, key, wait_timeout_s=5)
    try:
        ensure_target_lock_alive(conn)
        # 第二个连接竞争同一锁：短超时等待必须以受控异常结束（而非语法错误）
        start = time.monotonic()
        with pytest.raises(TargetLockTimeoutError):
            acquire_target_lock(SETTINGS, key, wait_timeout_s=1)
        elapsed = time.monotonic() - start
        assert elapsed < 10, f"竞争超时应快速受控失败，实际 {elapsed:.1f}s"
    finally:
        release_target_lock(conn, key)
    # 释放后可立即重新取得
    conn2 = acquire_target_lock(SETTINGS, key, wait_timeout_s=5)
    release_target_lock(conn2, key)


def test_concurrent_same_source_creation_single_winner(admission_env, tmp_path):
    """§9.13（DeepSeek R1 #4）：两个独立数据库连接经 Barrier 对齐同时创建
    同源发布——advisory 准入锁 + 行锁下恰好一个成功、另一个 409，无额外
    运行残留。（真实跨进程语义以独立连接近似；advisory lock 为库级。）"""
    import threading

    source_id, ws1, ws2, job1, job2 = admission_env
    _make_ready(admission_env, tmp_path, workspace_idx=0)
    _make_ready(admission_env, tmp_path, workspace_idx=1)
    barrier = threading.Barrier(2, timeout=15)
    outcomes = {}

    def _create(job_id, tag):
        barrier.wait()
        try:
            result = create_publish_run(
                SETTINGS,
                job_id,
                revision_no=1,
                mode="default",
                steps=None,
                step_config=None,
            )
            outcomes[tag] = ("ok", result["publish_run_id"])
        except SourcePublishActiveError as exc:
            outcomes[tag] = ("409", exc.blocking)
        except Exception as exc:  # noqa: BLE001
            outcomes[tag] = ("other", f"{type(exc).__name__}: {exc}")

    threads = [
        threading.Thread(target=_create, args=(job1, "a")),
        threading.Thread(target=_create, args=(job2, "b")),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    statuses = sorted(tag for tag, (kind, _v) in outcomes.items() if kind == "ok")
    rejected = [v for _t, (k, v) in outcomes.items() if k == "409"]
    others = [(t, v) for t, (k, v) in outcomes.items() if k == "other"]
    assert not others, f"出现非预期异常: {others}"
    assert len(statuses) == 1, f"恰好一个成功，实际: {outcomes}"
    assert len(rejected) == 1, f"恰好一个 409，实际: {outcomes}"
    # 409 指向成功者的运行
    assert rejected[0]["run_id"] == outcomes[statuses[0]][1]
