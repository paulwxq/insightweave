"""doc 11 §4.1：收尾核心逻辑抽取为共享模块，新增 origin 参数。

CLI 传「离线收尾」（默认，行为不变）、Web 传「Web 终止」；note 其余结构
与状态迁移语义不变（由 test_resolve_orphaned_run.py 既有用例覆盖）。
"""

from types import SimpleNamespace
from uuid import uuid4

from backend.jobs.run_repository import COLLECTION_RUN
from backend.jobs.run_resolution import apply_resolution

RUN_ID = uuid4()


class FakeSession:
    """apply_resolution 最小替身：一条 pending 运行 + 三步骤行。"""

    def __init__(self, *, run_status="pending", steps=None):
        self.source = SimpleNamespace(source_id="db_src_1")
        self.workspaces = [SimpleNamespace(workspace_id="ws_1", source_id="db_src_1")]
        self.collection_jobs = [
            SimpleNamespace(collection_job_id="cj_1", workspace_id="ws_1")
        ]
        self.collection_runs = [
            SimpleNamespace(
                collection_run_id=RUN_ID,
                collection_job_id="cj_1",
                status=run_status,
                message=None,
            )
        ]
        self.run_steps = steps or [
            SimpleNamespace(
                collection_run_id=RUN_ID,
                step_name="db",
                status="success",
                message="",
            ),
            SimpleNamespace(
                collection_run_id=RUN_ID,
                step_name="ddl",
                status="running",
                message="",
            ),
            SimpleNamespace(
                collection_run_id=RUN_ID,
                step_name="json",
                status="pending",
                message="",
            ),
        ]
        self.commits = 0

    def get(self, model, key, **_kwargs):
        pools = {
            "db_source_info": [self.source],
            "workspace_info": self.workspaces,
            "collection_job_info": self.collection_jobs,
            "collection_run_info": self.collection_runs,
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
        pool = (
            self.run_steps
            if columns and columns[0].__name__ == "CollectionRunStep"
            else []
        )
        return iter(list(pool))

    def execute(self, statement):
        if getattr(statement, "is_update", False):
            # doc 10.2 §5.3：finish_step 条件 UPDATE（running→failed /
            # pending→blocked），按 where 匹配步骤行应用字面量值。
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
        pool = (
            self.run_steps
            if columns and columns[0].__name__ == "CollectionRunStep"
            else []
        )

        def _filtered():
            where = getattr(statement, "whereclause", None)
            if where is None:
                return list(pool)
            pairs = []
            for item in where.clauses:
                value = item.right
                pairs.append((item.left.key, getattr(value, "value", value)))
            return [
                row
                for row in pool
                if all(getattr(row, k, None) == v for k, v in pairs)
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


def test_apply_resolution_origin_prefix_marks_web():
    session = FakeSession()
    result = apply_resolution(
        session, COLLECTION_RUN, RUN_ID, "后端进程被回收", origin="Web 终止"
    )
    assert result["changed"] is True
    assert result["note"].startswith("Web 终止：")
    assert "离线收尾" not in result["note"]


def test_apply_resolution_origin_defaults_to_offline():
    session = FakeSession()
    result = apply_resolution(session, COLLECTION_RUN, RUN_ID, "维护窗口收尾")
    assert result["changed"] is True
    assert result["note"].startswith("离线收尾：")


def test_apply_resolution_finalize_only_prefix_unchanged():
    session = FakeSession()
    session.run_steps = session.run_steps[:1]
    result = apply_resolution(
        session, COLLECTION_RUN, RUN_ID, "仅汇总", finalize_only=True
    )
    assert result["changed"] is True
    assert result["note"].startswith("仅汇总修复：")
