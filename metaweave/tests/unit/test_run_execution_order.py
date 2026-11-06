"""验证后台实际执行顺序，数据库返回的步骤名称顺序不得成为执行计划。

会话、worker、文件写入使用替身，不连接平台库、源库或 LLM。
"""

import asyncio
from contextlib import contextmanager
from types import SimpleNamespace
from uuid import UUID

import pytest
from fastapi import BackgroundTasks

from backend.api.routers import workspaces
from backend.data_sources.step_context import DbStepConfigError, StepOutcome
from backend.jobs import collection_run, run_repository
from backend.publishing import publish_service
from metaweave.utils.logger import get_current_step, set_current_step


RUN_ID = UUID("11111111-1111-4111-8111-111111111111")
ALL_COLLECTION_STEPS = ["db", "ddl", "json", "md", "domain", "dim", "rel", "sql"]
ALL_PUBLISH_STEPS = [
    "db",
    "ddl",
    "json",
    "domain",
    "table_schema",
    "dim_value",
    "rel",
    "sql",
]


@pytest.fixture(autouse=True)
def restore_log_step():
    previous = get_current_step()
    yield
    set_current_step(previous)


def install_run(monkeypatch, module, selected_steps, *, collection):
    """模拟新运行：开始时间全为空，数据库按名称返回步骤。"""
    run = SimpleNamespace(
        status="pending", collection_job_id="coll_job_1", publish_job_id="pub_job_1"
    )
    job = SimpleNamespace(workspace_id="ws_1")
    workspace = SimpleNamespace(source_id="db_src_1", workspace_id="ws_1")
    rows = {
        step: SimpleNamespace(
            step_name=step, step_config=None, status="pending", message=None
        )
        for step in selected_steps
    }
    calls = []
    markers = []

    class Session:
        def get(self, model, key):
            return run if model.__name__.endswith("RunInfo") else job

        def commit(self):
            pass

    @contextmanager
    def session_scope(settings):
        yield Session()

    def start(*args):
        run.status = "running"
        return True

    def finish(settings, run_id, step, status, message, **kwargs):
        rows[step].status = status
        rows[step].message = message
        return True  # 写回成功（doc 10.1 §4.1：_record_terminal 返回是否成功）

    def finalize(*args):
        assert all(
            row.status in ("success", "failed", "blocked") for row in rows.values()
        )
        run.status = run_repository.rollup_run_status(
            [row.status for row in rows.values()]
        )
        return run.status

    monkeypatch.setattr(module, "platform_session", session_scope)
    monkeypatch.setattr(module, "mark_run_running", start)
    monkeypatch.setattr(
        module, "list_run_steps", lambda *args: [rows[step] for step in sorted(rows)]
    )
    monkeypatch.setattr(module, "_record_terminal", finish)
    # 发布在函数内部导入汇总函数，采集使用模块引用。
    monkeypatch.setattr(run_repository, "finalize_run", finalize)
    if collection:
        # 保留真实日志映射，覆盖从 Web 步骤名到 SQL 日志段的完整启动路径。
        monkeypatch.setattr(module, "finalize_run", finalize)
        monkeypatch.setattr(module, "get_workspace", lambda *args: workspace)
        monkeypatch.setattr(module, "_claim_step", lambda *args: None)
        monkeypatch.setattr(
            module,
            "load_step_context",
            lambda settings, **kwargs: SimpleNamespace(**kwargs),
        )
        monkeypatch.setattr(
            module,
            "_write_marker_for_step",
            lambda source, workspace, step, run_id, **kwargs: markers.append(step)
            or True,
        )

        def worker(ctx):
            if ctx.step_name == "sql":
                assert get_current_step() == "sql-rag-generate"
            # 上游仍 pending 时不能提前启动下游。
            for upstream in module._STEP_DEPS.get(ctx.step_name, ()):
                if upstream in rows:
                    assert rows[upstream].status == "success"
            calls.append(ctx.step_name)
            return StepOutcome(True, "ok")

        for step in selected_steps:
            monkeypatch.setitem(module._WORKERS, step, worker)
    else:

        def execute_step(settings, run_id, workspace_id, step):
            calls.append(step)
            rows[step].status = "success"
            return {"step": step, "status": "success"}

        monkeypatch.setattr(module, "_execute_step", execute_step)
    return run, rows, calls, markers


@pytest.mark.parametrize(
    ("requested", "expected"),
    [
        (ALL_COLLECTION_STEPS, ALL_COLLECTION_STEPS),
        # 独立步骤的请求优先级必须保留，不能只对字母序重新拓扑排序。
        (["dim", "md", "json", "ddl", "db"], ["ddl", "json", "dim", "md", "db"]),
        (["rel"], ["rel"]),  # 未选上游不自动补跑。
    ],
)
def test_collection_executes_dependencies_before_downstream(
    monkeypatch, requested, expected
):
    run, rows, calls, markers = install_run(
        monkeypatch, collection_run, requested, collection=True
    )
    plan = collection_run.normalize_collection_steps(requested)
    collection_run.execute_collection_run(None, RUN_ID, plan)
    # doc 10.1：独立步骤可并行，完成交错合法；只约束依赖先后。
    assert set(calls) == set(expected)
    for downstream in expected:
        for upstream in collection_run._STEP_DEPS.get(downstream, ()):
            if upstream in calls:
                assert calls.index(upstream) < calls.index(downstream)
    assert set(markers) == set(expected)
    assert run.status == "success"


@pytest.mark.parametrize(
    "failure_at", ["set_operation_step", "_claim_step", "load_step_context"]
)
def test_collection_initialization_error_finishes_run(monkeypatch, failure_at):
    run, rows, calls, markers = install_run(
        monkeypatch, collection_run, ["sql", "db"], collection=True
    )
    original = getattr(collection_run, failure_at)

    def fail_sql(*args, **kwargs):
        step = {
            "set_operation_step": lambda: args[0],
            "_claim_step": lambda: args[2],
            "load_step_context": lambda: kwargs["step_name"],
        }[failure_at]()
        if step == "sql":
            raise RuntimeError("模拟步骤初始化故障")
        return original(*args, **kwargs)

    monkeypatch.setattr(collection_run, failure_at, fail_sql)
    collection_run.execute_collection_run(None, RUN_ID, ["sql", "db"])
    # doc 10.2 §6.1（DeepSeek R1 #2）：认领前初始化异常（set_operation_step/
    # _claim_step）记 blocked——failed 的合法来源不含 pending，记 failed 会被
    # 条件 UPDATE 拒绝导致运行无法汇总；认领后（load_step_context）异常
    # 仍记 failed（步骤已 running）。
    expected_sql = "failed" if failure_at == "load_step_context" else "blocked"
    assert rows["sql"].status == expected_sql
    assert "RuntimeError" in rows["sql"].message
    assert calls == ["db"]
    assert rows["db"].status == "success"
    assert run.status == "partial_success"


def test_collection_sql_logging_error_finishes_failed_run(monkeypatch):
    run, rows, calls, markers = install_run(
        monkeypatch, collection_run, ["sql"], collection=True
    )

    def missing_mapping(step):
        raise KeyError(step)

    monkeypatch.setattr(collection_run, "set_operation_step", missing_mapping)
    collection_run.execute_collection_run(None, RUN_ID, ["sql"])
    # 认领前（日志步骤设置）异常 → blocked（DeepSeek R1 #2 新契约）
    assert rows["sql"].status == "blocked"
    assert "KeyError" in rows["sql"].message
    assert calls == []
    assert run.status == "failed"


@pytest.mark.parametrize("upstream_status", ["failed", "blocked"])
def test_collection_propagates_unsuccessful_upstream(monkeypatch, upstream_status):
    run, rows, calls, markers = install_run(
        monkeypatch, collection_run, ALL_COLLECTION_STEPS, collection=True
    )

    def unsuccessful_json(ctx):
        calls.append("json")
        if upstream_status == "blocked":
            raise DbStepConfigError("JSON 前置条件不满足")
        return StepOutcome(False, "JSON 生成失败")

    monkeypatch.setitem(collection_run._WORKERS, "json", unsuccessful_json)
    collection_run.execute_collection_run(None, RUN_ID, ALL_COLLECTION_STEPS)
    # db 与 ddl 并行启动、完成交错合法（doc 10.1）；json 依赖 ddl，须后于 ddl。
    assert set(calls) == {"db", "ddl", "json"}
    assert calls.index("ddl") < calls.index("json")
    assert rows["json"].status == upstream_status
    for step in ["md", "domain", "dim", "rel", "sql"]:
        assert rows[step].status == "blocked"
        assert step not in markers
    assert run.status == "partial_success"


def test_collection_rejects_plan_that_differs_from_run_rows(monkeypatch):
    run, rows, calls, markers = install_run(
        monkeypatch, collection_run, ["db"], collection=True
    )
    with pytest.raises(collection_run.CollectionRunError, match="步骤不一致"):
        collection_run.execute_collection_run(None, RUN_ID, ["ddl"])
    assert run.status == "pending"
    assert calls == markers == []


@pytest.mark.parametrize(
    "selected", [ALL_PUBLISH_STEPS, ["table_schema", "sql", "dim_value", "json"]]
)
def test_publish_executes_channel_order_without_global_serial(monkeypatch, selected):
    """并行唯一模式（2026-11-06 裁定移除调度开关）：跨通道无固定全局
    顺序，通道内保持相邻顺序（doc 10.2 §4）；运行行缺 execution_plan
    时按已选步骤即时重建计划（NULL 防御分支）。"""
    run, rows, calls, markers = install_run(
        monkeypatch, publish_service, selected, collection=False
    )
    publish_service.execute_publish_run(None, RUN_ID)
    assert set(calls) == set(selected)
    chains = (
        ["db", "ddl", "json", "domain"],
        ["table_schema", "dim_value", "sql"],
        ["rel"],
    )
    for chain in chains:
        positions = [calls.index(step) for step in chain if step in selected]
        assert positions == sorted(positions)
    assert run.status == "success"


def test_collection_route_passes_created_plan_to_background_task(monkeypatch):
    plan = ["ddl", "json", "dim", "md"]
    result = {"collection_run_id": str(RUN_ID), "steps": plan}
    calls = []
    # doc 11 §4.2：run_id 由路由生成（提交前注册）后传入创建函数
    monkeypatch.setattr(workspaces, "new_run_id", lambda: RUN_ID)
    monkeypatch.setattr(
        workspaces, "create_collection_run", lambda *args, **kwargs: result
    )
    monkeypatch.setattr(
        workspaces, "execute_collection_run", lambda *args: calls.append(args)
    )
    background = BackgroundTasks()
    payload = SimpleNamespace(
        revision_no=1, mode="custom", steps=plan, step_config=None
    )
    response = asyncio.run(
        workspaces.create_collection_run_route("coll_job_1", payload, background, None)
    )
    assert response is result
    assert calls == []
    asyncio.run(background())
    assert calls == [(None, RUN_ID, plan)]


def test_upstream_worker_crash_keeps_cascade_message(monkeypatch):
    """DeepSeek R2 新问题 1 回归：上游 worker 抛未捕获异常时步骤状态
    必须是终态常量（failed），同选下游的级联阻断消息保持 doc 10.1 §4.1
    既定文案，而非 bool 导致的通用兜底。"""
    import backend.jobs.collection_run as collection_run
    from backend.jobs.run_repository import STATUS_BLOCKED, STATUS_FAILED

    run, rows, calls, markers = install_run(
        monkeypatch, collection_run, ["json", "rel"], collection=True
    )

    def crash(ctx):
        raise RuntimeError("worker 崩溃")

    monkeypatch.setitem(collection_run._WORKERS, "json", crash)
    collection_run.execute_collection_run(None, RUN_ID, ["json", "rel"])
    assert rows["json"].status == STATUS_FAILED
    assert rows["rel"].status == STATUS_BLOCKED
    assert "JSON" in rows["rel"].message and "未成功" in rows["rel"].message
    assert run.status == "failed"


def test_preclaim_crash_keeps_cascade_message(monkeypatch):
    """DeepSeek R3 非阻断 1：认领前异常（set_operation_step 抛错）→
    blocked，同选下游级联文案仍为既定"JSON 未成功"口径。"""
    import backend.jobs.collection_run as collection_run
    from backend.jobs.run_repository import STATUS_BLOCKED

    run, rows, calls, markers = install_run(
        monkeypatch, collection_run, ["json", "rel"], collection=True
    )
    original = collection_run.set_operation_step

    def crash(step_name):
        if step_name == "json":
            raise RuntimeError("认领前崩溃")
        return original(step_name)

    monkeypatch.setattr(collection_run, "set_operation_step", crash)
    collection_run.execute_collection_run(None, RUN_ID, ["json", "rel"])
    assert rows["json"].status == STATUS_BLOCKED
    assert rows["rel"].status == STATUS_BLOCKED
    assert "JSON" in rows["rel"].message and "未成功" in rows["rel"].message
    assert run.status == "failed"
