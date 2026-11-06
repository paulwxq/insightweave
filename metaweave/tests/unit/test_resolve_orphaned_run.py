"""doc 8.1 §5.4 离线收尾脚本：预览只读、apply 幂等、message 含原因与未核验说明。"""

from datetime import datetime, timezone
from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4

import pytest

from backend.jobs.run_repository import COLLECTION_RUN
from backend.jobs.run_resolution import apply_resolution, preview_resolution

NOW = datetime(2026, 10, 29, tzinfo=timezone.utc)
RUN_ID = uuid4()


class OrphanFakeSession:
    def __init__(self):
        self.source = SimpleNamespace(source_id="db_src_1")
        self.workspaces = [SimpleNamespace(workspace_id="ws_1", source_id="db_src_1")]
        self.collection_jobs = [
            SimpleNamespace(collection_job_id="cj_1", workspace_id="ws_1")
        ]
        run = SimpleNamespace(
            collection_run_id=RUN_ID,
            collection_job_id="cj_1",
            status="pending",
            message=None,
            started_at=None,
            finished_at=None,
        )
        self.collection_runs = [run]
        self.run_steps = [
            SimpleNamespace(
                collection_run_id=RUN_ID,
                step_name="db",
                status="success",
                message="",
                input_manifest=None,
                output_manifest=None,
            ),
            SimpleNamespace(
                collection_run_id=RUN_ID,
                step_name="ddl",
                status="running",
                message="",
                input_manifest=None,
                output_manifest=None,
            ),
            SimpleNamespace(
                collection_run_id=RUN_ID,
                step_name="json",
                status="pending",
                message="",
                input_manifest=None,
                output_manifest=None,
            ),
        ]
        self.commits = 0

    def get(self, model, key, **_kwargs):
        pools = {
            "db_source_info": [self.source],
            "workspace_info": self.workspaces,
            "collection_job_info": self.collection_jobs,
            "collection_run_info": self.collection_runs,
            "collection_run_step": self.run_steps,
        }
        pool = pools.get(model.__tablename__, [])
        for row in pool:
            for attr in (
                "source_id",
                "workspace_id",
                "collection_job_id",
                "collection_run_id",
            ):
                if hasattr(row, attr) and getattr(row, attr) == key:
                    return row
        return None

    def scalars(self, statement):
        columns = [c["entity"] for c in statement.column_descriptions]
        pool = {
            "CollectionRunStep": self.run_steps,
        }.get(columns[0].__name__ if columns else "", [])
        return iter(list(pool))

    def execute(self, statement):
        if getattr(statement, "is_update", False):
            # doc 10.2 §5.3：finish_step 条件 UPDATE（同 run_resolution 替身）
            where = statement.whereclause
            pairs = []
            if where is not None:
                for item in where.clauses:
                    value = item.right
                    value = getattr(value, "value", value)
                    pairs.append((item.left.key, value))
            matched = [
                row
                for row in self.run_steps
                if all(
                    (
                        getattr(row, k, None) in v
                        if isinstance(v, (tuple, list, set, frozenset))
                        else getattr(row, k, None) == v
                    )
                    for k, v in pairs
                )
            ]
            from datetime import datetime, timezone

            for column, clause in statement._values.items():
                name = getattr(column, "key", None) or getattr(
                    column, "name", None
                )
                raw = getattr(clause, "value", None)
                value = (
                    raw
                    if isinstance(raw, (str, dict, list))
                    else datetime.now(timezone.utc)
                    if name in ("finished_at", "started_at")
                    else None
                )
                if value is None:
                    continue
                for row in matched:
                    if hasattr(row, name):
                        setattr(row, name, value)
            return SimpleNamespace(rowcount=len(matched))
        columns = [c["entity"] for c in statement.column_descriptions]
        pool = {
            "CollectionRunStep": self.run_steps,
        }.get(columns[0].__name__ if columns else "", [])

        def _filtered():
            where = getattr(statement, "whereclause", None)
            if where is None:
                return list(pool)
            pairs = []
            for item in where.clauses:
                value = item.right
                pairs.append((item.left.key, getattr(value, "value", value)))
            return [
                row for row in pool if all(getattr(row, k, None) == v for k, v in pairs)
            ]

        return SimpleNamespace(
            fetchall=lambda: list(pool),
            all=lambda: list(pool),
            scalar_one_or_none=lambda: (_filtered()[0] if _filtered() else None),
        )

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass


def test_preview_is_readonly_with_transition_plan():
    session = OrphanFakeSession()
    result = preview_resolution(session, COLLECTION_RUN, RUN_ID)
    assert result["actionable"] is True
    assert result["run_status"] == "pending"
    transitions = {item["step"]: item["transition"] for item in result["steps"]}
    assert transitions == {
        "db": "保持不变",
        "ddl": "running→failed",
        "json": "pending→blocked",
    }
    assert session.commits == 0


def test_apply_resolves_multiple_leftover_running_steps():
    """doc 10.1 §6.10/§4.5：并行中断可遗留多个 running——收尾须逐一切
    failed、pending 置 blocked、已有终态保留并汇总；预览仍只读。"""
    session = OrphanFakeSession()

    def row(step, status):
        return SimpleNamespace(
            collection_run_id=RUN_ID,
            step_name=step,
            status=status,
            message="",
            input_manifest=None,
            output_manifest=None,
        )

    session.run_steps = [
        row("db", "success"),
        row("ddl", "running"),
        row("json", "running"),
        row("md", "pending"),
        row("dim", "success"),
    ]
    report = preview_resolution(session, COLLECTION_RUN, RUN_ID)
    assert report["actionable"] is True
    transitions = {item["step"]: item["transition"] for item in report["steps"]}
    assert transitions == {
        "db": "保持不变",
        "ddl": "running→failed",
        "json": "running→failed",
        "md": "pending→blocked",
        "dim": "保持不变",
    }
    assert session.commits == 0  # 预览只读

    result = apply_resolution(session, COLLECTION_RUN, RUN_ID, "并行中断人工收尾")
    assert result["changed"] is True
    assert result["final_status"] == "partial_success"
    statuses = {s.step_name: s.status for s in session.run_steps}
    assert statuses == {
        "db": "success",
        "ddl": "failed",
        "json": "failed",
        "md": "blocked",
        "dim": "success",
    }
    assert session.commits == 1


def test_apply_resolves_steps_and_rolls_up():
    session = OrphanFakeSession()
    result = apply_resolution(session, COLLECTION_RUN, RUN_ID, "维护窗口收尾")
    assert result["changed"] is True
    # 成功步骤保留、running→failed、pending→blocked；有成功有失败 → partial_success
    assert result["final_status"] == "partial_success"
    statuses = {s.step_name: s.status for s in session.run_steps}
    assert statuses == {"db": "success", "ddl": "failed", "json": "blocked"}
    run = session.collection_runs[0]
    assert "执行者已停止" in run.message
    assert "维护窗口收尾" in run.message
    assert "外部写入结果未核验" in run.message
    assert session.commits == 1


def test_apply_all_pending_zombie_blocks_all_and_fails_run():
    # doc 11 §6.1.5：pending 僵尸（进程崩溃于创建后/执行前）主路径——
    # 全部步骤 pending→blocked、无成功步骤 → 汇总 failed
    session = OrphanFakeSession()
    for step in session.run_steps:
        step.status = "pending"
    result = apply_resolution(session, COLLECTION_RUN, RUN_ID, "进程崩溃于创建后")
    assert result["changed"] is True
    assert result["final_status"] == "failed"
    assert {change["step"]: change["to"] for change in result["steps"]} == {
        "db": "blocked",
        "ddl": "blocked",
        "json": "blocked",
    }
    assert all(step.status == "blocked" for step in session.run_steps)
    assert session.collection_runs[0].status == "failed"
    assert session.commits == 1


def test_apply_is_idempotent_for_terminal_run():
    session = OrphanFakeSession()
    session.collection_runs[0].status = "failed"
    result = apply_resolution(session, COLLECTION_RUN, RUN_ID, "再次收尾")
    assert result["changed"] is False
    assert "无需处理" in result["message"]
    assert session.commits == 0


@pytest.mark.parametrize(
    "statuses, expected",
    [
        (["success"], "success"),
        (["success", "failed"], "partial_success"),
        (["success", "blocked"], "partial_success"),
        (["failed", "blocked"], "failed"),
    ],
)
def test_finalize_only_preserves_steps_and_records_reason(statuses, expected):
    session = OrphanFakeSession()
    session.run_steps = session.run_steps[: len(statuses)]
    for step, status in zip(session.run_steps, statuses):
        step.status = status
        step.started_at = NOW
        step.finished_at = NOW
        step.input_manifest = {"files": ["original.json"]}
        step.message = "原步骤记录"
    before = deepcopy([vars(step) for step in session.run_steps])
    run = session.collection_runs[0]
    run.message = "历史说明"
    report = preview_resolution(session, COLLECTION_RUN, RUN_ID, finalize_only=True)
    assert report["actionable"] is True
    assert report["planned_final_status"] == expected
    assert report["source_id"] == "db_src_1"
    assert all(step["transition"] == "保持不变" for step in report["steps"])
    assert session.commits == 0
    result = apply_resolution(
        session, COLLECTION_RUN, RUN_ID, "校正汇总", finalize_only=True
    )
    assert result["final_status"] == expected
    assert run.status == expected and run.finished_at is not None
    assert run.started_at is None  # 不编造历史开始时间
    assert run.message.startswith("历史说明\n仅汇总修复")
    assert "处理时间" in run.message and "校正汇总" in run.message
    assert [vars(step) for step in session.run_steps] == before
    assert session.commits == 1
    again = apply_resolution(
        session, COLLECTION_RUN, RUN_ID, "再次修复", finalize_only=True
    )
    assert again["changed"] is False
    assert session.commits == 1


@pytest.mark.parametrize(
    "statuses", [["pending"], ["running"], ["success", "pending"], [], ["unknown"]]
)
def test_finalize_only_refuses_unfinished_empty_or_invalid_steps(statuses):
    session = OrphanFakeSession()
    session.run_steps = session.run_steps[: len(statuses)]
    for step, status in zip(session.run_steps, statuses):
        step.status = status
    before = deepcopy([vars(step) for step in session.run_steps])
    report = preview_resolution(session, COLLECTION_RUN, RUN_ID, finalize_only=True)
    assert report["actionable"] is False
    assert report["planned_final_status"] is None
    assert report["refusal_reason"]
    result = apply_resolution(
        session, COLLECTION_RUN, RUN_ID, "拒绝未结束运行", finalize_only=True
    )
    assert result["changed"] is False and result["error"]
    assert session.collection_runs[0].status == "pending"
    assert [vars(step) for step in session.run_steps] == before
    assert session.commits == 0


def test_finalize_only_rechecks_steps_after_preview():
    session = OrphanFakeSession()
    session.run_steps = session.run_steps[:1]
    assert preview_resolution(session, COLLECTION_RUN, RUN_ID, finalize_only=True)["actionable"]
    session.run_steps[0].status = "running"
    result = apply_resolution(
        session, COLLECTION_RUN, RUN_ID, "预览后状态变化", finalize_only=True
    )
    assert result["changed"] is False
    assert session.run_steps[0].status == "running"
    assert session.commits == 0


def test_resolution_rechecks_run_under_lock(monkeypatch):
    session = OrphanFakeSession()
    original_get = session.get
    calls = []

    def get(model, key, **kwargs):
        if kwargs.get("with_for_update"):
            calls.append(model.__tablename__)
            assert kwargs["populate_existing"] is True
            if model.__tablename__ == "collection_run_info":
                session.collection_runs[0].status = "success"
        return original_get(model, key, **kwargs)

    monkeypatch.setattr(session, "get", get)
    result = apply_resolution(
        session, COLLECTION_RUN, RUN_ID, "锁内复查", finalize_only=True
    )
    assert result["changed"] is False
    assert calls == [
        "db_source_info",
        "workspace_info",
        "collection_job_info",
        "collection_run_info",
    ]
    assert session.commits == 0


def test_terminal_preview_does_not_plan_step_changes():
    session = OrphanFakeSession()
    session.collection_runs[0].status = "failed"
    result = preview_resolution(session, COLLECTION_RUN, RUN_ID)
    assert result["actionable"] is False
    assert all(step["transition"] == "保持不变" for step in result["steps"])


def test_empty_reason_does_not_write():
    session = OrphanFakeSession()
    assert apply_resolution(session, COLLECTION_RUN, RUN_ID, "  ")["error"]
    assert session.commits == 0


def test_cli_requires_stopped_executor_confirmation_before_loading_settings(
    monkeypatch,
):
    from scripts import resolve_orphaned_run as script
    from backend.persistence import platform_db

    def forbidden():
        raise AssertionError("缺少执行者停止确认，不得加载连接")

    monkeypatch.setattr(platform_db, "load_project_env", forbidden)
    assert (
        script.main(
            ["publish", str(RUN_ID), "--reason", "修复", "--finalize-only", "--apply"]
        )
        == 2
    )


def test_cli_database_override_and_preview_remain_readonly(monkeypatch, capsys):
    import json
    from contextlib import contextmanager
    from backend.persistence import platform_db
    from backend.persistence.platform_db import PlatformSettings
    from scripts import resolve_orphaned_run as script

    settings = PlatformSettings(
        host="h", port=5432, name="application", user="u", password="secret"
    )
    session = OrphanFakeSession()
    session.run_steps = session.run_steps[:1]
    opened = []

    @contextmanager
    def scope(actual):
        opened.append(actual.name)
        yield session

    monkeypatch.setattr(platform_db, "load_project_env", lambda: None)
    monkeypatch.setattr(
        platform_db, "load_platform_settings_from_env", lambda: settings
    )
    monkeypatch.setattr(platform_db, "platform_session", scope)
    result = script.main(
        [
            "collection",
            str(RUN_ID),
            "--reason",
            "只读核对",
            "--finalize-only",
            "--database",
            "repair_test",
        ]
    )
    assert result == 0
    report = json.loads(capsys.readouterr().out)
    assert report["platform_database"] == "repair_test"
    assert report["planned_final_status"] == "success"
    assert opened == ["repair_test"]
    assert settings.name == "application"
    assert session.commits == 0 and session.collection_runs[0].status == "pending"


def test_cli_target_guard_list_and_clear_flow(monkeypatch, capsys):
    """doc 10.2 §8：目标保留记录预览/人工清除入口（Kimi R1 意见 7）。

    预览列出全部记录；未带确认参数只预览不清除；带确认按 key 删除；
    不存在的 key 报告并退出码 1。
    """
    import json
    from contextlib import contextmanager

    from backend.persistence import platform_db
    from backend.persistence.platform_db import PlatformSettings
    from scripts import resolve_orphaned_run as script

    settings = PlatformSettings(
        host="h", port=5432, name="guard_test", user="u", password="secret"
    )
    store = {
        "neo4j://t:7687|neo4j": SimpleNamespace(
            target_key="neo4j://t:7687|neo4j",
            owner_kind="publish_run",
            owner_id="run-1",
            reserved_at=datetime(2026, 11, 6, tzinfo=timezone.utc),
        )
    }
    deleted = []

    class GuardSession:
        def scalars(self, statement):
            return iter(list(store.values()))

        def get(self, model, key):
            return store.get(key)

        def delete(self, guard):
            deleted.append(guard.target_key)
            store.pop(guard.target_key, None)

        def commit(self):
            pass

    @contextmanager
    def scope(_settings):
        yield GuardSession()

    monkeypatch.setattr(platform_db, "load_project_env", lambda: None)
    monkeypatch.setattr(
        platform_db, "load_platform_settings_from_env", lambda: settings
    )
    monkeypatch.setattr(platform_db, "platform_session", scope)

    assert script.main(["--list-target-guards"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert listed["guards"][0]["owner_id"] == "run-1"

    # 未确认：只预览不清除
    assert (
        script.main(["--clear-target-guard", "neo4j://t:7687|neo4j"]) == 0
    )
    preview = json.loads(capsys.readouterr().out)
    assert preview["cleared"] is False and "confirm-external-verified" in (
        preview["message"]
    )
    assert deleted == []

    # 确认后清除
    assert (
        script.main(
            [
                "--clear-target-guard",
                "neo4j://t:7687|neo4j",
                "--confirm-external-verified",
            ]
        )
        == 0
    )
    cleared = json.loads(capsys.readouterr().out)
    assert cleared == {"cleared": True, "target_key": "neo4j://t:7687|neo4j"}
    assert deleted == ["neo4j://t:7687|neo4j"]

    # 已不存在
    assert (
        script.main(
            [
                "--clear-target-guard",
                "neo4j://t:7687|neo4j",
                "--confirm-external-verified",
            ]
        )
        == 1
    )
