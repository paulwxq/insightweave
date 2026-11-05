"""doc 10.1 §6：采集步骤依赖并行调度验证。

假 worker 用事件/屏障控制完成时机，不依赖固定休眠判断并发：
- 独立步骤（DB/DDL、MD/Dim）必须可以重叠；
- 下游只在其同选上游成功且完成结果提交（marker+状态）后启动；
- 步骤并发上限 2：第三个就绪步骤等待空位，空位释放后推进；
- 分支失败按既有文案优先级阻断后继，独立分支继续；
- 独立分支仍在执行时不提前汇总；
- 并行步骤任务持有各自 step 日志上下文，任务结束恢复。
"""

import threading
from contextlib import contextmanager
from types import SimpleNamespace
from uuid import UUID

from backend.data_sources.step_context import StepOutcome
from backend.jobs import collection_run, run_repository
from metaweave.utils.logger import get_current_step, set_current_step

RUN_ID = UUID("22222222-2222-4222-8222-222222222222")
ALL_STEPS = ["db", "ddl", "json", "md", "domain", "dim", "rel", "sql"]


class Harness:
    """采集运行替身：会话、认领、marker、状态写回全部就地生效。

    默认为每个已选步骤安装假 worker：启动前断言同选上游已 success
    且 marker 已写入（doc 10.1 §4.1"完成结果提交后才放行下游"）。
    """

    def __init__(self, monkeypatch, selected_steps):
        self.run = SimpleNamespace(status="pending", collection_job_id="coll_job_1")
        self.job = SimpleNamespace(workspace_id="ws_1")
        self.workspace = SimpleNamespace(source_id="db_src_1", workspace_id="ws_1")
        self.rows = {
            step: SimpleNamespace(
                step_name=step, step_config=None, status="pending", message=None
            )
            for step in selected_steps
        }
        self.calls = []
        self.markers = []
        harness = self

        class Session:
            def get(self, model, key):
                return harness.run if model.__name__.endswith("RunInfo") else harness.job

            def commit(self):
                pass

        @contextmanager
        def session_scope(settings):
            yield Session()

        def start(*args):
            harness.run.status = "running"
            return True

        def finish(settings, run_id, step, status, message, **kwargs):
            prior = harness.rows[step].status
            degraded = prior == "success" and status == "failed"
            assert prior in ("running", "pending") or degraded, (
                f"步骤 {step} 重复写回或非法迁移: {prior} -> {status}"
            )
            assert prior == "running" or status == "blocked" or degraded, (
                f"步骤 {step} 未经 running 即写终态 {status}"
            )
            harness.rows[step].status = status
            harness.rows[step].message = message
            return True  # 写回成功（doc 10.1 §4.1：_record_terminal 返回是否成功）

        def finalize(*args):
            assert all(
                row.status in ("success", "failed", "blocked")
                for row in harness.rows.values()
            ), "存在未终态步骤时提前汇总"
            harness.run.status = run_repository.rollup_run_status(
                [row.status for row in harness.rows.values()]
            )
            return harness.run.status

        monkeypatch.setattr(collection_run, "platform_session", session_scope)
        monkeypatch.setattr(collection_run, "mark_run_running", start)
        monkeypatch.setattr(
            collection_run,
            "list_run_steps",
            lambda *args: [harness.rows[s] for s in sorted(harness.rows)],
        )
        monkeypatch.setattr(collection_run, "_record_terminal", finish)
        monkeypatch.setattr(collection_run, "finalize_run", finalize)
        monkeypatch.setattr(
            collection_run, "get_workspace", lambda *args: harness.workspace
        )
        monkeypatch.setattr(collection_run, "_claim_step", self._claim)
        monkeypatch.setattr(
            collection_run,
            "load_step_context",
            lambda settings, **kwargs: SimpleNamespace(**kwargs),
        )
        monkeypatch.setattr(
            collection_run,
            "_write_marker_for_step",
            lambda source, workspace, step, run_id, **kw: harness.markers.append(step)
            or True,
        )
        for step in selected_steps:
            monkeypatch.setitem(collection_run._WORKERS, step, self.default_worker())

    def _claim(self, settings, workspace_id, step_name, run_id):
        self.rows[step_name].status = "running"
        return None

    def default_worker(self):
        harness = self

        def worker(ctx):
            for upstream in collection_run._STEP_DEPS.get(ctx.step_name, ()):
                if upstream in harness.rows:
                    assert harness.rows[upstream].status == "success", (
                        f"{ctx.step_name} 在上游 {upstream} 终态前启动"
                    )
                    assert upstream in harness.markers, (
                        f"{ctx.step_name} 启动时上游 {upstream} marker 未写入"
                    )
            harness.calls.append(ctx.step_name)
            return StepOutcome(True, "ok")

        return worker

    def install_worker(self, monkeypatch, step, fn):
        monkeypatch.setitem(collection_run._WORKERS, step, fn)

    def run_steps(self, steps):
        plan = collection_run.normalize_collection_steps(steps)
        collection_run.execute_collection_run(None, RUN_ID, plan)
        return plan


def test_full_run_overlaps_independent_steps(monkeypatch):
    """§6.1：DB 与 DDL 可重叠；MD 与 Dim 可重叠；Domain 不等 Dim；
    JSON 等 DDL；Rel 等 JSON+Domain；SQL 等 MD+Domain+Rel。"""
    harness = Harness(monkeypatch, ALL_STEPS)
    overlap_root = threading.Barrier(2, timeout=10)  # db 与 ddl 必须同时在跑
    overlap_after_json = threading.Barrier(2, timeout=10)  # md 与 dim 必须同时在跑
    dim_release = threading.Event()  # Domain 完成前 Dim 不得结束

    def db_worker(ctx):
        overlap_root.wait()
        return harness.default_worker()(ctx)

    def ddl_worker(ctx):
        overlap_root.wait()
        return harness.default_worker()(ctx)

    def md_worker(ctx):
        overlap_after_json.wait()
        return harness.default_worker()(ctx)

    def dim_worker(ctx):
        overlap_after_json.wait()
        harness.calls.append("dim")  # 已启动（未结束）
        dim_release.wait(timeout=10)
        return StepOutcome(True, "ok")

    def domain_worker(ctx):
        assert "dim" in harness.calls, "Domain 已启动但 Dim 尚未启动（MD/Dim 未重叠）"
        dim_release.set()  # Domain 无须等 Dim：在此释放
        return harness.default_worker()(ctx)

    for step, fn in [
        ("db", db_worker),
        ("ddl", ddl_worker),
        ("md", md_worker),
        ("dim", dim_worker),
        ("domain", domain_worker),
    ]:
        harness.install_worker(monkeypatch, step, fn)

    harness.run_steps(ALL_STEPS)

    calls = harness.calls
    assert set(calls) == set(ALL_STEPS)
    assert calls.index("ddl") < calls.index("json")
    assert calls.index("json") < calls.index("md")
    assert calls.index("json") < calls.index("dim")
    assert calls.index("md") < calls.index("domain")
    assert calls.index("domain") < calls.index("rel")
    assert calls.index("rel") < calls.index("sql")
    assert all(row.status == "success" for row in harness.rows.values())
    assert harness.run.status == "success"


def test_step_limit_two_defers_ready_step(monkeypatch):
    """§6.5：DB 长时间运行占一个名额，JSON 完成后 MD、Dim 均就绪，
    上限 2 只允许再启动一个（按请求顺序先 MD）；空位释放后 Dim 推进。"""
    selected = ["db", "ddl", "json", "md", "dim"]
    harness = Harness(monkeypatch, selected)
    release_db = threading.Event()
    md_running = threading.Event()
    release_md = threading.Event()
    observed = {}

    def db_worker(ctx):
        release_db.wait(timeout=10)
        return harness.default_worker()(ctx)

    def md_worker(ctx):
        md_running.set()
        release_md.wait(timeout=10)
        return harness.default_worker()(ctx)

    def dim_worker(ctx):
        result = harness.default_worker()(ctx)
        release_db.set()  # Dim 是最后一个非 DB 步骤，结束后释放 DB
        return result

    harness.install_worker(monkeypatch, "db", db_worker)
    harness.install_worker(monkeypatch, "md", md_worker)
    harness.install_worker(monkeypatch, "dim", dim_worker)

    def watcher():
        md_running.wait(timeout=10)
        # db 与 md 占满两个名额：dim 不得启动
        observed["dim_deferred"] = "dim" not in harness.calls
        release_md.set()

    guard = threading.Thread(target=watcher)
    guard.start()
    try:
        harness.run_steps(selected)
    finally:
        release_db.set()
        release_md.set()
        guard.join(timeout=10)

    assert observed.get("dim_deferred") is True, "上限 2 未生效：第三个步骤提前启动"
    assert "dim" in harness.calls, "空位释放后 Dim 未推进"
    assert harness.calls.index("md") < harness.calls.index("dim")
    assert harness.run.status == "success"


def test_md_failure_blocks_chain_not_dim(monkeypatch):
    """§6.2：MD 失败阻断 Domain、Rel、SQL，不阻断 Dim；文案与既有优先级一致。"""
    harness = Harness(monkeypatch, ALL_STEPS)

    def failing_md(ctx):
        harness.calls.append("md")
        return StepOutcome(False, "MD 生成失败")

    harness.install_worker(monkeypatch, "md", failing_md)
    harness.run_steps(ALL_STEPS)

    assert "md" in harness.calls and "dim" in harness.calls
    for step in ("domain", "rel", "sql"):
        assert step not in harness.calls
        assert step not in harness.markers
        assert harness.rows[step].status == "blocked"
    assert harness.rows["domain"].message == (
        "本次请求的 MD 未成功，Domain 未启动；如需使用已有 MD 产物，请单独重跑 Domain"
    )
    assert harness.rows["rel"].message == (
        "本次请求的 Domain 未成功，Rel 未启动；如需使用已有 Domain 产物，请单独重跑 Rel"
    )
    assert harness.rows["sql"].message == (
        "本次请求的 MD 未成功，SQL 未启动；请修复 MD 后重跑 SQL"
    )
    for step in ("db", "ddl", "json", "dim"):
        assert harness.rows[step].status == "success"
    assert harness.run.status == "partial_success"


def test_json_failure_blocks_downstream_with_priority_messages(monkeypatch):
    """§6.2/§4.1：JSON 失败阻断全部同选后继；Rel 文案取 JSON 优先于 Domain。"""
    harness = Harness(monkeypatch, ALL_STEPS)

    def json_fail(ctx):
        harness.calls.append("json")
        return StepOutcome(False, "JSON 生成失败")

    harness.install_worker(monkeypatch, "json", json_fail)
    harness.run_steps(ALL_STEPS)

    expected = {
        "md": "本次请求的 JSON 未成功，MD 未启动；如需使用已有 JSON 产物，请单独重跑 MD",
        "dim": "本次请求的 JSON 未成功，Dim 未启动；如需使用已有 JSON 产物，请单独重跑 Dim",
        "domain": (
            "本次请求的 MD 未成功，Domain 未启动；"
            "如需使用已有 MD 产物，请单独重跑 Domain"
        ),
        "rel": "本次请求的 JSON 未成功，Rel 未启动；如需使用已有 JSON 产物，请单独重跑 Rel",
        "sql": "本次请求的 MD 未成功，SQL 未启动；请修复 MD 后重跑 SQL",
    }
    for step, message in expected.items():
        assert harness.rows[step].status == "blocked"
        assert harness.rows[step].message == message
        assert step not in harness.calls
        assert step not in harness.markers
    assert harness.rows["json"].status == "failed"
    assert harness.rows["db"].status == "success"
    assert harness.run.status == "partial_success"


def test_no_early_finalize_while_branch_running(monkeypatch):
    """§6.6：主链全部结束时 DB 仍在执行 → 整体保持 running，最后才汇总。"""
    harness = Harness(monkeypatch, ALL_STEPS)
    chain_done = threading.Event()
    observed = []

    def db_worker(ctx):
        chain_done.wait(timeout=10)
        observed.append(harness.run.status)  # 主链完成、DB 未终态：不得汇总
        return harness.default_worker()(ctx)

    def sql_worker(ctx):
        result = harness.default_worker()(ctx)
        chain_done.set()  # 主链最后一步完成
        return result

    harness.install_worker(monkeypatch, "db", db_worker)
    harness.install_worker(monkeypatch, "sql", sql_worker)
    harness.run_steps(ALL_STEPS)

    assert observed == ["running"], "独立分支执行中发生提前汇总"
    assert harness.run.status == "success"


def test_parallel_step_tasks_isolate_log_context(monkeypatch):
    """§6.9：重叠步骤各自读到自己的 step；运行结束恢复原上下文。"""
    harness = Harness(monkeypatch, ALL_STEPS)
    overlap = threading.Barrier(2, timeout=10)
    observed = {}

    def observing_worker(step_name):
        def worker(ctx):
            if step_name in ("db", "ddl"):
                overlap.wait()
            observed[step_name] = get_current_step()
            result = harness.default_worker()(ctx)
            observed.setdefault(step_name + ":end", get_current_step())
            return result

        return worker

    for step in ALL_STEPS:
        harness.install_worker(monkeypatch, step, observing_worker(step))

    set_current_step("-")
    harness.run_steps(ALL_STEPS)

    assert observed["db"] == "db"
    assert observed["ddl"] == "ddl"
    assert observed["md"] == "md"
    assert observed["dim"] == "dim"
    assert observed["sql"] == "sql-rag-generate"  # Web sql 标签映射保留
    assert observed["db:end"] == "db"  # 任务运行期间上下文稳定
    assert get_current_step() == "-", "运行结束后步骤上下文未恢复"


def test_each_step_result_recorded_once(monkeypatch):
    """§6.5：多任务同时完成时每个结果仅处理一次，不重复提交或遗漏。"""
    harness = Harness(monkeypatch, ALL_STEPS)
    harness.run_steps(ALL_STEPS)
    assert len(harness.calls) == len(ALL_STEPS)
    assert len(set(harness.calls)) == len(ALL_STEPS)
    assert len(harness.markers) == len(ALL_STEPS)
    assert sorted(row.status for row in harness.rows.values()) == ["success"] * len(
        ALL_STEPS
    )


def test_claim_conflict_blocks_step_and_downstream(monkeypatch):
    """认领失败（其他运行占用）→ 步骤 blocked，不调用 worker、不写 marker。"""
    selected = ["db", "ddl", "json"]
    harness = Harness(monkeypatch, selected)
    original_claim = collection_run._claim_step

    def claim_ddl_busy(settings, workspace_id, step_name, run_id):
        if step_name == "ddl":
            return "该工作区此步骤可能正在执行（其他运行占用），请稍后重试"
        return original_claim(settings, workspace_id, step_name, run_id)

    monkeypatch.setattr(collection_run, "_claim_step", claim_ddl_busy)
    harness.run_steps(selected)

    assert harness.rows["ddl"].status == "blocked"
    assert "ddl" not in harness.calls
    assert "ddl" not in harness.markers
    # json 的同选上游 DDL 被阻断 → json 依既有文案阻断
    assert harness.rows["json"].status == "blocked"
    assert harness.rows["json"].message == (
        "本次请求的 DDL 未成功，JSON 未启动；如需使用已有 DDL 产物，请单独重跑 JSON"
    )
    assert harness.rows["db"].status == "success"


def test_status_commit_failure_degrades_and_blocks_downstream(monkeypatch):
    """§6.4：上游 worker 成功但步骤状态写回失败 → 不得凭内存成功放行下游；
    步骤按失败处理，下游依既定优先级文案级联阻断。"""
    selected = ["db", "ddl", "json"]
    harness = Harness(monkeypatch, selected)

    def flaky_record(settings, run_id, step, status, message, **kwargs):
        if step == "ddl" and status == "success":
            return False  # 模拟写库故障被 _record_terminal 吞掉后的失败返回
        harness.rows[step].status = status
        harness.rows[step].message = message
        return True

    monkeypatch.setattr(collection_run, "_record_terminal", flaky_record)
    harness.run_steps(selected)

    assert harness.rows["ddl"].status == "failed"
    assert "结果提交失败" in harness.rows["ddl"].message
    assert "json" not in harness.calls and "json" not in harness.markers
    assert harness.rows["json"].status == "blocked"
    assert harness.rows["json"].message == (
        "本次请求的 DDL 未成功，JSON 未启动；如需使用已有 DDL 产物，请单独重跑 JSON"
    )
    assert harness.rows["db"].status == "success"
    assert harness.run.status == "partial_success"


def test_marker_commit_failure_degrades_and_blocks_downstream(monkeypatch):
    """§6.4：成功 marker 写入失败 → 同样按失败处理，不放行下游。"""
    selected = ["db", "ddl", "json"]
    harness = Harness(monkeypatch, selected)

    def flaky_marker(source, workspace, step, run_id, *, failed):
        if step == "ddl" and not failed:
            return False  # 成功 marker 写入故障
        harness.markers.append(step)
        return True

    monkeypatch.setattr(collection_run, "_write_marker_for_step", flaky_marker)
    harness.run_steps(selected)

    assert harness.rows["ddl"].status == "failed"
    assert "结果提交失败" in harness.rows["ddl"].message
    assert "ddl" in harness.markers  # 降级路径补写失败 marker
    assert "json" not in harness.calls and "json" not in harness.markers
    assert harness.rows["json"].status == "blocked"
    assert harness.run.status == "partial_success"


def test_ddl_md_without_json_blocks_md_on_ddl_failure(monkeypatch):
    """§6.3：DDL 与 MD 同选但不选 JSON，DDL 失败 → MD 看 (ddl, md) 文案阻断。"""
    selected = ["ddl", "md"]
    harness = Harness(monkeypatch, selected)

    def ddl_fail(ctx):
        harness.calls.append("ddl")
        return StepOutcome(False, "DDL 生成失败")

    harness.install_worker(monkeypatch, "ddl", ddl_fail)
    harness.run_steps(selected)

    assert harness.calls == ["ddl"]
    assert "md" not in harness.markers
    assert harness.rows["md"].status == "blocked"
    assert harness.rows["md"].message == (
        "本次请求的 DDL 未成功，MD 未启动；如需使用已有 JSON 产物，请单独重跑 MD"
    )
    assert harness.rows["ddl"].status == "failed"
    assert harness.run.status == "failed"
