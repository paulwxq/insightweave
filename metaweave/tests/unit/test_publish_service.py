"""发布集中预检、运行创建与后台执行（doc 6.2 §6.3、§8）。

平台库用假会话（FakeSession 覆盖 run/job/内容表的最小语义），产物按
`output/<db_src_N>/<ws_N>/<step>/` 在临时目录构造，采集 marker 用真实
`write_collection_marker` 落盘。其他发布步骤测试文件复用本文件的假件。
"""

import json
from contextlib import contextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import UUID

import pytest
from sqlalchemy.exc import SQLAlchemyError

from backend.persistence.models import (
    DbProfile,
    DbSourceInfo,
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
from backend.persistence.platform_db import PlatformSettings
from backend.profile_loading.db_loader import (
    MetaLoadSourceDisabledError,
    MetaLoadSourceNotFoundError,
)
from backend.publishing import publish_service
from backend.publishing.precheck import (
    PUBLISH_STEP_ORDER,
    PublishPrecheckError,
    check_step,
    collect_input_dir_times,
    normalize_publish_steps,
    precheck_steps,
    require_success_markers,
)
from backend.publishing.publish_service import (
    create_publish_run,
    execute_publish_run,
    check_publication,
)
from backend.workspace_files.markers import write_collection_marker
from backend.workspace_files.timestamps import SHANGHAI_TZ

SOURCE_ID = "db_src_1"
OTHER_SOURCE_ID = "db_src_2"
WORKSPACE_ID = "ws_1"
PUBLISH_JOB_ID = "pub_job_1"
DB_NAME = "orders"
RUN = UUID("11111111-1111-4111-8111-111111111111")
RUN2 = UUID("22222222-2222-4222-8222-222222222222")
SETTINGS = PlatformSettings(
    host="platform", port=5432, name="metaweave", user="admin", password="secret"
)
MOMENT = datetime(2026, 10, 21, 15, 0, 0, tzinfo=SHANGHAI_TZ)


def profile_document(source_id=SOURCE_ID):
    """合法 database.json（doc 9 契约；source_id 为短 ID）。"""
    return {
        "schema_version": 1,
        "source_id": source_id,
        "collected_at": "2026-10-20T08:00:00Z",
        "database": {
            "type": "postgresql",
            "host": "example.internal",
            "port": 5432,
            "name": DB_NAME,
            "server_version": "18.0",
            "size_bytes": 1234,
            "size_scope": "whole_database",
            "record_count": None,
        },
        "selected_schemas": ["public"],
        "schemas": [
            {
                "name": "public",
                "ordinary_table_count": 2,
                "partitioned_table_count": 1,
                "partition_child_count": 3,
                "view_count": 1,
                "materialized_view_count": 0,
                "object_count": 4,
            }
        ],
        "totals": {
            "ordinary_table_count": 2,
            "partitioned_table_count": 1,
            "partition_child_count": 3,
            "view_count": 1,
            "materialized_view_count": 0,
            "object_count": 4,
        },
    }


def step_dir(tmp_path, step, source_id=SOURCE_ID, workspace_id=WORKSPACE_ID):
    return tmp_path / "output" / source_id / workspace_id / step


def write_marker(tmp_path, step, run_id=RUN, status="success", moment=MOMENT):
    return write_collection_marker(
        step_dir(tmp_path, step), run_id, status, moment=moment
    )


def write_profile(tmp_path, document=None, source_id=SOURCE_ID):
    directory = step_dir(tmp_path, "db", source_id=source_id)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "database.json"
    path.write_text(json.dumps(document or profile_document()), encoding="utf-8")
    return directory, path


class FakeSession:
    """覆盖发布链（预检/创建/执行）的最小假平台库会话。

    - `get` 支持主键取行（含 with_for_update 等被忽略的参数）；
    - `scalars`/`execute` 对普通 select 按 where 等值/不等条件过滤；
    - `execute` 对 delete 按表名池删除（table_profile 级联删字段行）；
    - `_claim_publish_step` 的三表 join 冲突查询按语义直接计算。
    """

    def __init__(self, *, steps=("db", "ddl"), revision_no=1, step_config=None):
        self.source = SimpleNamespace(
            source_id=SOURCE_ID,
            is_active=True,
            updated_at=datetime(2026, 10, 20, tzinfo=timezone.utc),
            db_name=DB_NAME,
            selected_schemas=["public"],
            host="h",
            port=5432,
            username="u",
            password_ciphertext="cipher",
            sslmode=None,
        )
        self.workspace = SimpleNamespace(workspace_id=WORKSPACE_ID, source_id=SOURCE_ID)
        self.publish_jobs = [
            PublishJobInfo(
                publish_job_id=PUBLISH_JOB_ID,
                workspace_id=WORKSPACE_ID,
                job_name="整包发布",
                job_desc=None,
                created_by_user_id=None,
            )
        ]
        self.publish_revisions = [
            PublishJobRevision(
                publish_job_id=PUBLISH_JOB_ID,
                revision_no=revision_no,
                steps=list(steps),
                step_config=step_config,
                change_note=None,
            )
        ]
        self.runs: list = []
        self.run_steps: list = []
        self.profiles: list = []
        self.definitions: list = []
        self.table_profiles: list = []
        self.table_column_profiles: list = []
        self.table_domains: list = []
        self.pending: list = []
        self.guard_releases: list = []
        # doc 10.2 §5.1（DeepSeek R2 #2）：目标互斥事件顺序（先清 guard
        # 后放锁）的回归断言载体
        self.target_mutex_events: list = []
        self.commit_errors: list = []
        self.rolled_back = 0
        self.commits = 0
        self.flush_calls = 0

    # ---- 池路由 ----

    def _pool(self, entity):
        mapping = {
            DbSourceInfo: [self.source] if self.source is not None else [],
            WorkspaceInfo: [self.workspace] if self.workspace is not None else [],
            PublishJobInfo: self.publish_jobs,
            PublishJobRevision: self.publish_revisions,
            PublishRunInfo: self.runs,
            PublishRunStep: self.run_steps,
            DbProfile: self.profiles,
            TableDefinition: self.definitions,
            TableProfile: self.table_profiles,
            TableColumnProfile: self.table_column_profiles,
            TableDomain: self.table_domains,
        }
        return mapping.get(entity, [])

    _TABLE_POOLS = {
        "db_profile": "profiles",
        "table_definition": "definitions",
        "table_profile": "table_profiles",
        "table_domain": "table_domains",
    }

    # ---- Session 接口 ----

    def get(self, model, key, **_kwargs):
        if model is DbSourceInfo:
            if self.source is None:
                return None
            return self.source if key == self.source.source_id else None
        if model is WorkspaceInfo:
            if self.workspace is None:
                return None
            return self.workspace if key == self.workspace.workspace_id else None
        if model is PublishJobInfo:
            return next(
                (row for row in self.publish_jobs if row.publish_job_id == key), None
            )
        if model is PublishJobRevision:
            return next(
                (
                    row
                    for row in self.publish_revisions
                    if row.publish_job_id == key and row.revision_no == key
                ),
                None,
            )
        if model is PublishRunInfo:
            return next((row for row in self.runs if row.publish_run_id == key), None)
        raise AssertionError(model)

    @staticmethod
    def _clause_pairs(clause):
        clauses = getattr(clause, "clauses", None) or [clause]
        for item in clauses:
            left = item.left
            right = item.right
            value = getattr(right, "value", right)
            yield getattr(left, "key", None) or getattr(left, "name", None), (
                item.operator,
                value,
            )

    def _select_rows(self, statement):
        entity = statement.column_descriptions[0]["entity"]
        rows = list(self._pool(entity))
        where = statement.whereclause
        if where is None:
            return rows
        for name, (op, value) in self._clause_pairs(where):
            matched = []
            for row in rows:
                actual = getattr(row, name, None)
                keep = actual != value if op.__name__ == "ne" else actual == value
                if keep:
                    matched.append(row)
            rows = matched
        return rows

    def scalars(self, statement):
        return self._select_rows(statement)

    def scalar(self, statement):
        rows = self._select_rows(statement)
        return rows[0] if rows else None

    def execute(self, statement):
        from sqlalchemy.sql.elements import TextClause

        if isinstance(statement, TextClause):
            # doc 10.2：准入 advisory lock / SET LOCAL lock_timeout 真实库生效，
            # 假件不建模锁等待
            return SimpleNamespace(
                scalar_one_or_none=lambda: None,
                first=lambda: None,
                all=lambda: [],
                scalar_one=lambda: 0,
            )
        if getattr(statement, "is_update", False):
            return self._apply_update(statement)
        sql_text = str(statement)
        if getattr(statement, "_setup_joins", None):
            if "workspace_info" in sql_text and "publish_run_info" in sql_text:
                # doc 10.2 §5.1 准入查询 active_publish_runs_for_source：
                # 本假件不建模其他运行 → 无活动发布
                return SimpleNamespace(
                    all=lambda: [],
                    first=lambda: None,
                    scalar_one_or_none=lambda: None,
                )
            if (
                "publish_run_info" in sql_text
                and "publish_run_info.created_at" in sql_text.lower()
            ):
                # doc 10.2 §5.2 准入查询 active_publish_run_for_workspace
                #（带 order_by created_at；认领冲突查询无 order_by）。
                # 按池语义计算：pending/running 发布 + 作业同工作区即命中。
                params = statement.compile().params
                workspace_id = params.get("workspace_id_1")
                hit = None
                for run in self.runs:
                    if getattr(run, "status", None) not in ("pending", "running"):
                        continue
                    job = next(
                        (
                            item
                            for item in self.publish_jobs
                            if item.publish_job_id == run.publish_job_id
                        ),
                        None,
                    )
                    if job is not None and job.workspace_id == workspace_id:
                        hit = (run.publish_run_id, run.publish_job_id, run.status)
                        break
                return SimpleNamespace(
                    first=lambda: hit,
                    all=lambda: ([hit] if hit else []),
                    scalar_one_or_none=lambda: hit,
                )
            if "collection_run_info" in sql_text:
                # doc 8.1 §5.1.1：Rel 发布认领的反向检查（采集输入运行中）；
                # doc 10.2 §5.2 准入查询（.first()）——本假件不建模采集运行
                return SimpleNamespace(
                    scalar_one_or_none=lambda: None,
                    first=lambda: None,
                    all=lambda: [],
                )
        if getattr(statement, "is_delete", False):
            where = statement.whereclause
            conditions = (
                dict(
                    (name, value)
                    for name, (op, value) in self._clause_pairs(where)
                    if op.__name__ != "ne"
                )
                if where is not None
                else {}
            )
            table = statement.table.name
            if table == "table_profile":
                removed = [
                    row
                    for row in self.table_profiles
                    if all(
                        getattr(row, key, None) == value
                        for key, value in conditions.items()
                    )
                ]
                self.table_profiles[:] = [
                    row for row in self.table_profiles if row not in removed
                ]
                keys = {
                    (row.source_id, row.schema_name, row.object_name) for row in removed
                }
                self.table_column_profiles[:] = [
                    row
                    for row in self.table_column_profiles
                    if (row.source_id, row.schema_name, row.object_name) not in keys
                ]
                return None
            pool_name = self._TABLE_POOLS.get(table)
            pool = getattr(self, pool_name) if pool_name else None
            if pool is not None:
                pool[:] = [
                    row
                    for row in pool
                    if not all(
                        getattr(row, key, None) == value
                        for key, value in conditions.items()
                    )
                ]
            return None
        sql_text = str(statement)
        if getattr(statement, "_setup_joins", None):
            if "collection_run_info" in sql_text:
                # doc 8.1 §5.1.1：Rel 发布认领的反向检查（采集输入运行中）；
                # 本假件不建模采集运行 → 无冲突
                return SimpleNamespace(scalar_one_or_none=lambda: None)
            # _claim_publish_step 冲突查询：同名 running 步骤被其他运行占用
            params = statement.compile().params
            step_name = params.get("step_name_1")
            exclude_run = params.get("publish_run_id_1")
            workspace_id = params.get("workspace_id_1")
            hit = None
            for step_row in self.run_steps:
                if step_row.step_name != step_name or step_row.status != "running":
                    continue
                run = next(
                    (
                        row
                        for row in self.runs
                        if row.publish_run_id == step_row.publish_run_id
                    ),
                    None,
                )
                if run is None:
                    continue
                job = next(
                    (
                        row
                        for row in self.publish_jobs
                        if row.publish_job_id == run.publish_job_id
                    ),
                    None,
                )
                if (
                    run.status == "running"
                    and run.publish_run_id != exclude_run
                    and job is not None
                    and job.workspace_id == workspace_id
                ):
                    hit = run.publish_run_id
                    break
            return SimpleNamespace(scalar_one_or_none=lambda: hit)
        rows = self._select_rows(statement)
        return SimpleNamespace(
            scalar_one_or_none=lambda: rows[0] if rows else None,
            # doc 10.2 §5.3：count_active_publish_runs 的 func.count 查询
            scalar_one=lambda: rows[0] if rows else 0,
            scalars=lambda: rows,
            all=lambda: rows,
        )

    def _apply_update(self, statement):
        """doc 10.2 §5.3 条件状态更新（mark_*_running / finish_step）。

        按 where 条件（等值 / in / ne）过滤对应池，应用字面量 values
        （func.now() 以当前时间代替），返回 affected rowcount 语义。
        """
        table = statement.table.name
        pool = {
            "publish_run_info": self.runs,
            "publish_run_step": self.run_steps,
        }.get(table, [])
        where = statement.whereclause

        def _match(row, name, op, value):
            actual = getattr(row, name, None)
            op_name = getattr(op, "__name__", "")
            if op_name == "ne":
                return actual != value
            if isinstance(value, (tuple, list, set, frozenset)):
                return actual in value
            return actual == value

        matched = list(pool)
        if where is not None:
            for name, (op, value) in self._clause_pairs(where):
                matched = [
                    row for row in matched if _match(row, name, op, value)
                ]
        updates: dict[str, object] = {}
        for column, clause in statement._values.items():
            name = getattr(column, "key", None) or getattr(column, "name", None)
            raw = getattr(clause, "value", None)
            if isinstance(raw, (str, dict, list)):
                updates[name] = raw
            elif name in ("started_at", "finished_at"):
                updates[name] = datetime.now(timezone.utc)
        for row in matched:
            for key, value in updates.items():
                if hasattr(row, key):
                    setattr(row, key, value)
        return SimpleNamespace(rowcount=len(matched))

    def add(self, row):
        self.pending.append(row)

    def add_all(self, rows):
        self.pending.extend(rows)

    def delete(self, row):
        for pool_name in (
            "profiles",
            "definitions",
            "table_profiles",
            "table_domains",
            "runs",
            "run_steps",
            "publish_jobs",
        ):
            pool = getattr(self, pool_name)
            if row in pool:
                pool.remove(row)
                return

    def flush(self):
        self.flush_calls += 1

    def commit(self):
        if self.commit_errors:
            raise self.commit_errors.pop(0)
        for row in self.pending:
            if isinstance(row, PublishRunInfo):
                self.runs.append(row)
            elif isinstance(row, PublishRunStep):
                self.run_steps.append(row)
            elif isinstance(row, DbProfile):
                self.profiles.append(row)
            elif isinstance(row, TableDefinition):
                self.definitions.append(row)
            elif isinstance(row, TableProfile):
                self.table_profiles.append(row)
            elif isinstance(row, TableColumnProfile):
                self.table_column_profiles.append(row)
            elif isinstance(row, TableDomain):
                self.table_domains.append(row)
            else:
                raise AssertionError(type(row))
        self.pending.clear()
        self.commits += 1

    def rollback(self):
        self.pending.clear()
        self.rolled_back += 1


def install(monkeypatch, tmp_path, session=None):
    monkeypatch.setattr("metaweave.utils.file_utils.get_project_root", lambda: tmp_path)
    holder = session or FakeSession()

    @contextmanager
    def fake_platform_session(_settings):
        try:
            yield holder
        except Exception:
            holder.rollback()
            raise

    monkeypatch.setattr(publish_service, "platform_session", fake_platform_session)
    # doc 10.2 §5.1：rel 发布的 Neo4j 目标互斥——假件不建真连接；
    # 目标锁恒可取得、保留记录恒为空（个别用例自行覆盖以测互斥语义）。
    # publish_service 顶层 from-import 了这些名字，补丁打在其模块属性上。
    from types import SimpleNamespace as _NS

    monkeypatch.setattr(
        publish_service, "acquire_target_lock",
        lambda settings, key, *, wait_timeout_s: _NS(closed=False),
    )
    monkeypatch.setattr(
        publish_service, "release_target_lock", lambda conn, key: None
    )
    monkeypatch.setattr(
        publish_service, "ensure_target_lock_alive", lambda conn: None
    )
    monkeypatch.setattr(
        publish_service,
        "reserve_target_guard",
        lambda session, target_key, owner_kind, owner_id: None,
    )
    def _record_guard_release(session, target_key, owner_kind, owner_id):
        # doc 10.2 §5.1（Kimi R1 意见 3）：记录 guard 清除调用供断言
        holder.guard_releases.append((target_key, owner_kind, owner_id))
        holder.target_mutex_events.append("guard")
        return True

    def _record_lock_release(conn, key):
        holder.target_mutex_events.append("lock")
        return None

    monkeypatch.setattr(
        publish_service, "release_target_guard", _record_guard_release
    )
    monkeypatch.setattr(publish_service, "release_target_lock", _record_lock_release)
    return holder


def make_ready_db(tmp_path):
    write_marker(tmp_path, "db")
    write_profile(tmp_path)


def run_step_row(session, run_id, step):
    return next(
        row
        for row in session.run_steps
        if row.publish_run_id == run_id and row.step_name == step
    )


# ---- 步骤白名单与固定顺序（doc 6.2 §8.1） ----


class TestNormalizePublishSteps:
    def test_orders_by_fixed_sequence_and_dedups(self):
        assert normalize_publish_steps(["sql", "db", "sql"]) == ["db", "sql"]
        assert normalize_publish_steps(
            ["rel", "dim_value", "table_schema", "json", "domain", "ddl", "db"]
        ) == ["db", "ddl", "json", "domain", "table_schema", "dim_value", "rel"]

    def test_empty_selection_rejected(self):
        with pytest.raises(ValueError, match="至少选择一个发布步骤"):
            normalize_publish_steps([])

    def test_unknown_step_lists_whitelist(self):
        with pytest.raises(ValueError, match="仅支持"):
            normalize_publish_steps(["md"])
        with pytest.raises(ValueError, match="仅支持"):
            normalize_publish_steps(["db", "nope"])

    def test_step_order_constant(self):
        assert PUBLISH_STEP_ORDER == (
            "db",
            "ddl",
            "json",
            "domain",
            "table_schema",
            "dim_value",
            "rel",
            "sql",
        )


# ---- 目录 marker 判定（doc 6 §4.1） ----


class TestRequireSuccessMarkers:
    def test_success_markers_by_directory(self, monkeypatch, tmp_path):
        install(monkeypatch, tmp_path)
        write_marker(tmp_path, "db")
        write_marker(tmp_path, "json", run_id=RUN2)
        markers, failure = require_success_markers(
            SOURCE_ID, WORKSPACE_ID, ("db", "json")
        )
        assert failure is None
        assert markers["db"].run_id == str(RUN)
        assert markers["json"].run_id == str(RUN2)

    def test_missing_marker_reports_directory(self, monkeypatch, tmp_path):
        install(monkeypatch, tmp_path)
        write_marker(tmp_path, "db")
        markers, failure = require_success_markers(
            SOURCE_ID, WORKSPACE_ID, ("db", "json")
        )
        assert failure == "json/ 目录没有采集 marker，产物不可用"
        assert set(markers) == {"db"}

    def test_failed_marker_blocks_directory(self, monkeypatch, tmp_path):
        install(monkeypatch, tmp_path)
        write_marker(tmp_path, "db", status="failed")
        _markers, failure = require_success_markers(SOURCE_ID, WORKSPACE_ID, ("db",))
        assert failure == "db/ 目录最近一次采集未成功，产物不可用"

    def test_backup_markers_do_not_count(self, monkeypatch, tmp_path):
        install(monkeypatch, tmp_path)
        directory = step_dir(tmp_path, "db")
        write_collection_marker(directory, RUN, "success", moment=MOMENT)
        backup = directory / ".backup" / "bak_20260101_000000_000"
        backup.mkdir(parents=True)
        (backup / f".coll_run_id_{RUN}_20260101-000000000_success").touch()
        markers, failure = require_success_markers(SOURCE_ID, WORKSPACE_ID, ("db",))
        assert failure is None and markers["db"].run_id == str(RUN)


# ---- check_step / precheck_steps（db、ddl 两步；其余步骤见各自文件） ----


class TestCheckStep:
    def test_db_ready_lists_input_file_with_marker_run(self, monkeypatch, tmp_path):
        install(monkeypatch, tmp_path)
        make_ready_db(tmp_path)
        result = check_step(None, SOURCE_ID, WORKSPACE_ID, "db", db_name=DB_NAME)
        assert result.ready is True and result.reason is None
        assert result.input_files == (("db/database.json", str(RUN)),)

    def test_db_missing_file_reason(self, monkeypatch, tmp_path):
        install(monkeypatch, tmp_path)
        write_marker(tmp_path, "db")
        result = check_step(None, SOURCE_ID, WORKSPACE_ID, "db", db_name=DB_NAME)
        assert result.ready is False
        assert result.reason == "db/database.json 缺失或不可读"
        assert result.input_files == ()

    def test_db_marker_missing_beats_file_check(self, monkeypatch, tmp_path):
        install(monkeypatch, tmp_path)
        write_profile(tmp_path)
        result = check_step(None, SOURCE_ID, WORKSPACE_ID, "db", db_name=DB_NAME)
        assert result.ready is False
        assert result.reason == "db/ 目录没有采集 marker，产物不可用"

    def test_precheck_steps_collects_all_failures(self, monkeypatch, tmp_path):
        install(monkeypatch, tmp_path)
        write_marker(tmp_path, "db")
        write_profile(tmp_path)
        results = precheck_steps(
            None,
            SOURCE_ID,
            WORKSPACE_ID,
            ["db", "ddl"],
            db_name=DB_NAME,
        )
        assert results["db"].ready is True
        assert results["ddl"].ready is False
        assert "ddl/" in results["ddl"].reason


# ---- check_publication（固定 8 步骤 + 输入目录时间，doc 6.2 §8.2.2） ----


class TestCheckPublication:
    def _run(self, monkeypatch, tmp_path, session=None):
        session = install(monkeypatch, tmp_path, session)
        return session, check_publication(SETTINGS, WORKSPACE_ID)

    def test_fixed_eight_steps_in_order(self, monkeypatch, tmp_path):
        make_ready_db(tmp_path)
        _session, result = self._run(monkeypatch, tmp_path)
        assert result["workspace_id"] == WORKSPACE_ID
        assert result["source_id"] == SOURCE_ID
        assert [step["step_name"] for step in result["steps"]] == list(
            PUBLISH_STEP_ORDER
        )
        by_name = {step["step_name"]: step for step in result["steps"]}
        assert by_name["db"]["ready"] is True
        assert by_name["db"]["reason"] is None
        # 未采集目录：不可发布但不让整份预检 400
        assert by_name["sql"]["ready"] is False
        assert by_name["sql"]["reason"] is not None

    def test_input_directories_report_marker_times(self, monkeypatch, tmp_path):
        make_ready_db(tmp_path)
        _session, result = self._run(monkeypatch, tmp_path)
        directories = {item["directory"]: item for item in result["input_directories"]}
        # 8 个发布步骤覆盖的全部采集目录（去重后按名称排序）
        assert set(directories) == {
            "db",
            "ddl",
            "dim",
            "domain",
            "json",
            "md",
            "rel",
            "sql",
        }
        assert result["input_directories"] == sorted(
            result["input_directories"], key=lambda item: item["directory"]
        )
        db_info = directories["db"]
        assert db_info["marker_status"] == "success"
        assert db_info["collection_run_id"] == str(RUN)
        # 采集时间来自 marker 文件名时间戳（Asia/Shanghai → ISO 文本）
        assert db_info["collected_at"] == MOMENT.isoformat()
        assert directories["sql"]["marker_status"] == "none"
        assert directories["sql"]["collection_run_id"] is None
        assert directories["sql"]["collected_at"] is None

    def test_missing_workspace_is_source_not_found(self, monkeypatch, tmp_path):
        session = FakeSession()
        session.workspace = None
        install(monkeypatch, tmp_path, session)
        with pytest.raises(MetaLoadSourceNotFoundError):
            check_publication(SETTINGS, WORKSPACE_ID)

    def test_disabled_source_rejected(self, monkeypatch, tmp_path):
        session = FakeSession()
        session.source.is_active = False
        install(monkeypatch, tmp_path, session)
        with pytest.raises(MetaLoadSourceDisabledError):
            check_publication(SETTINGS, WORKSPACE_ID)

    def test_platform_failure_is_unavailable(self, monkeypatch, tmp_path):
        session = FakeSession()

        def boom(*_args, **_kwargs):
            raise SQLAlchemyError("db down")

        session.get = boom
        install(monkeypatch, tmp_path, session)
        from backend.data_sources.registration_service import PlatformUnavailableError

        with pytest.raises(PlatformUnavailableError):
            check_publication(SETTINGS, WORKSPACE_ID)

    def test_collect_input_dir_times_dedups_directories(self, monkeypatch, tmp_path):
        install(monkeypatch, tmp_path)
        make_ready_db(tmp_path)
        info = collect_input_dir_times(SOURCE_ID, WORKSPACE_ID)
        # rel/json/domain 等被多个步骤共享的目录只出现一次
        assert len(info) == 8
        assert info["db"]["marker_status"] == "success"


# ---- create_publish_run（集中预检 → 创建运行，doc 6.2 §6.3.2） ----


def _create(monkeypatch, tmp_path, **kwargs):
    session = install(monkeypatch, tmp_path)
    result = create_publish_run(
        SETTINGS,
        PUBLISH_JOB_ID,
        revision_no=1,
        mode="custom",
        steps=["db"],
        step_config=None,
        **kwargs,
    )
    return session, result


class TestCreatePublishRun:
    def test_success_creates_pending_run_and_step_rows(self, monkeypatch, tmp_path):
        make_ready_db(tmp_path)
        session, result = _create(monkeypatch, tmp_path)
        assert result["publish_job_id"] == PUBLISH_JOB_ID
        assert result["revision_no"] == 1
        assert result["steps"] == ["db"]
        assert result["status"] == "pending"
        run_id = UUID(result["publish_run_id"])
        assert len(session.runs) == 1
        assert session.runs[0].publish_run_id == run_id
        assert session.runs[0].status == "pending"
        assert session.runs[0].trigger_type == "manual"
        assert [(row.step_name, row.status) for row in session.run_steps] == [
            ("db", "pending")
        ]
        assert session.commits == 1

    def test_precheck_failure_rejects_whole_request_without_records(
        self, monkeypatch, tmp_path
    ):
        install(monkeypatch, tmp_path)
        # db 目录没有任何产物：集中预检失败，不建运行、不写业务记录
        with pytest.raises(PublishPrecheckError) as excinfo:
            create_publish_run(
                SETTINGS,
                PUBLISH_JOB_ID,
                revision_no=1,
                mode="custom",
                steps=["db"],
                step_config=None,
            )
        failures = excinfo.value.failures
        assert failures == [
            {"step": "db", "reason": "db/ 目录没有采集 marker，产物不可用"}
        ]
        assert "db:" in str(excinfo.value)

    def test_precheck_failure_leaves_no_business_records(self, monkeypatch, tmp_path):
        session = install(monkeypatch, tmp_path)
        with pytest.raises(PublishPrecheckError):
            create_publish_run(
                SETTINGS,
                PUBLISH_JOB_ID,
                revision_no=1,
                mode="custom",
                steps=["db"],
                step_config=None,
            )
        assert session.runs == [] and session.run_steps == []
        assert session.profiles == []
        assert session.pending == []

    def test_missing_job_or_revision(self, monkeypatch, tmp_path):
        from backend.publishing.publish_service import PublishRunError

        make_ready_db(tmp_path)
        install(monkeypatch, tmp_path)
        with pytest.raises(PublishRunError, match="发布作业不存在"):
            create_publish_run(
                SETTINGS,
                "pub_job_9",
                revision_no=1,
                mode="custom",
                steps=["db"],
                step_config=None,
            )
        with pytest.raises(PublishRunError, match="版本不存在"):
            create_publish_run(
                SETTINGS,
                PUBLISH_JOB_ID,
                revision_no=7,
                mode="custom",
                steps=["db"],
                step_config=None,
            )

    def test_step_outside_revision_rejected(self, monkeypatch, tmp_path):
        from backend.publishing.publish_service import PublishRunError

        make_ready_db(tmp_path)
        install(monkeypatch, tmp_path)  # 版本 steps 只有 db、ddl
        with pytest.raises(PublishRunError, match="超出引用版本允许的范围"):
            create_publish_run(
                SETTINGS,
                PUBLISH_JOB_ID,
                revision_no=1,
                mode="custom",
                steps=["sql"],
                step_config=None,
            )

    def test_default_mode_rejects_nonempty_step_config(self, monkeypatch, tmp_path):
        from backend.publishing.publish_service import PublishRunError

        make_ready_db(tmp_path)
        install(monkeypatch, tmp_path)
        with pytest.raises(PublishRunError, match="default 模式不接受非空 step_config"):
            create_publish_run(
                SETTINGS,
                PUBLISH_JOB_ID,
                revision_no=1,
                mode="default",
                steps=None,
                step_config={"db": {"x": 1}},
            )

    def test_disabled_source_rejected_before_run(self, monkeypatch, tmp_path):
        make_ready_db(tmp_path)
        session = FakeSession()
        session.source.is_active = False
        install(monkeypatch, tmp_path, session)
        with pytest.raises(MetaLoadSourceDisabledError):
            create_publish_run(
                SETTINGS,
                PUBLISH_JOB_ID,
                revision_no=1,
                mode="custom",
                steps=["db"],
                step_config=None,
            )
        assert session.runs == []


# ---- execute_publish_run 状态机（blocked / failed / partial，doc 6.2 §6.4） ----


class TestExecutePublishRun:
    def test_single_db_step_finalizes_run_and_step(self, monkeypatch, tmp_path):
        """仅选 db 时也必须完整推进整体运行，不能遗留 pending + success。"""
        make_ready_db(tmp_path)
        session = install(monkeypatch, tmp_path)
        result = create_publish_run(
            SETTINGS,
            PUBLISH_JOB_ID,
            revision_no=1,
            mode="custom",
            steps=["db"],
            step_config=None,
        )
        run_id = UUID(result["publish_run_id"])
        execute_publish_run(SETTINGS, run_id)
        run = session.runs[0]
        step = run_step_row(session, run_id, "db")
        assert run.status == step.status == "success"
        assert run.started_at is not None and run.finished_at is not None
        assert step.started_at is not None and step.finished_at is not None
        assert session.profiles[0].publish_run_id == run_id

    def test_all_success_finalizes_success(self, monkeypatch, tmp_path):
        from tests.unit.test_ddl_publish import DEFAULT_FILES, ddl_ready

        make_ready_db(tmp_path)
        ddl_ready(tmp_path, DEFAULT_FILES)
        session = install(monkeypatch, tmp_path)
        result = create_publish_run(
            SETTINGS,
            PUBLISH_JOB_ID,
            revision_no=1,
            mode="default",
            steps=None,
            step_config=None,
        )
        run_id = UUID(result["publish_run_id"])
        execute_publish_run(SETTINGS, run_id)

        run = session.runs[0]
        assert run.status == "success"
        assert run.finished_at is not None and run.started_at is not None
        db_step = run_step_row(session, run_id, "db")
        ddl_step = run_step_row(session, run_id, "ddl")
        assert db_step.status == "success"
        assert db_step.message == "数据库画像发布完成"
        assert db_step.input_manifest == {
            "files": [{"path": "db/database.json", "collection_run_id": str(RUN)}]
        }
        assert db_step.target_manifest == {
            "targets": [
                {
                    "type": "postgresql",
                    "database": "metaweave",
                    "schema": "public",
                    "table": "db_profile",
                }
            ]
        }
        assert ddl_step.status == "success"
        # 内容行与成功状态同一事务提交：行挂在本次运行上
        assert session.profiles[0].publish_run_id == run_id
        assert {row.publish_run_id for row in session.definitions} == {run_id}

    def test_marker_removed_between_create_and_execute_is_blocked(
        self, monkeypatch, tmp_path, caplog
    ):
        import logging

        from tests.unit.test_ddl_publish import DEFAULT_FILES, ddl_ready

        make_ready_db(tmp_path)
        ddl_ready(tmp_path, DEFAULT_FILES)
        session = install(monkeypatch, tmp_path)
        result = create_publish_run(
            SETTINGS,
            PUBLISH_JOB_ID,
            revision_no=1,
            mode="default",
            steps=None,
            step_config=None,
        )
        run_id = UUID(result["publish_run_id"])
        # 预检通过后、执行前：ddl 产物目录被清空（marker 一并消失）
        for path in step_dir(tmp_path, "ddl").iterdir():
            path.unlink()
        with caplog.at_level(logging.WARNING):
            execute_publish_run(SETTINGS, run_id)

        run = session.runs[0]
        assert run.status == "partial_success"
        db_step = run_step_row(session, run_id, "db")
        ddl_step = run_step_row(session, run_id, "ddl")
        assert db_step.status == "success"
        assert ddl_step.status == "blocked"
        assert "ddl/" in ddl_step.message
        # blocked 步骤未实际执行：没有开始时间，也没有输入清单
        assert ddl_step.started_at is None
        assert ddl_step.input_manifest is None
        assert ddl_step.finished_at is not None
        assert session.definitions == []  # ddl 未执行，不写定义行

    def test_publisher_crash_is_failed_with_type_name(self, monkeypatch, tmp_path):
        make_ready_db(tmp_path)
        session = install(monkeypatch, tmp_path)
        result = create_publish_run(
            SETTINGS,
            PUBLISH_JOB_ID,
            revision_no=1,
            mode="custom",
            steps=["db"],
            step_config=None,
        )
        run_id = UUID(result["publish_run_id"])

        def crashing_publisher(*_args, **_kwargs):
            raise RuntimeError("boom-with-detail")

        monkeypatch.setitem(publish_service._PUBLISHERS, "db", crashing_publisher)
        execute_publish_run(SETTINGS, run_id)

        db_step = run_step_row(session, run_id, "db")
        assert db_step.status == "failed"
        assert db_step.message == "发布执行失败（RuntimeError）"
        assert "boom-with-detail" not in db_step.message
        assert session.runs[0].status == "failed"

    def test_invalid_profile_file_fails_step_and_keeps_profiles(
        self, monkeypatch, tmp_path
    ):
        write_marker(tmp_path, "db")
        _directory, path = write_profile(tmp_path)
        path.write_text("{", encoding="utf-8")
        session = install(monkeypatch, tmp_path)
        result = create_publish_run(
            SETTINGS,
            PUBLISH_JOB_ID,
            revision_no=1,
            mode="custom",
            steps=["db"],
            step_config=None,
        )
        run_id = UUID(result["publish_run_id"])
        execute_publish_run(SETTINGS, run_id)
        db_step = run_step_row(session, run_id, "db")
        assert db_step.status == "failed"
        assert db_step.message == "发布执行失败（JSONDecodeError）"
        assert session.profiles == []

    def test_mismatched_source_in_profile_fails_without_writes(
        self, monkeypatch, tmp_path
    ):
        write_marker(tmp_path, "db")
        write_profile(tmp_path, profile_document(OTHER_SOURCE_ID))
        session = install(monkeypatch, tmp_path)
        result = create_publish_run(
            SETTINGS,
            PUBLISH_JOB_ID,
            revision_no=1,
            mode="custom",
            steps=["db"],
            step_config=None,
        )
        execute_publish_run(SETTINGS, UUID(result["publish_run_id"]))
        assert session.profiles == []
        assert run_step_row(session, UUID(result["publish_run_id"]), "db").status == (
            "failed"
        )

    def test_all_blocked_run_is_failed(self, monkeypatch, tmp_path):
        from backend.publishing.precheck import PublishPrecheckError

        write_profile(tmp_path)  # 只有文件，没有任何 marker
        session = install(monkeypatch, tmp_path)
        # 集中预检失败：不建运行；再模拟执行期才发现目录不可用的运行
        with pytest.raises(PublishPrecheckError):
            create_publish_run(
                SETTINGS,
                PUBLISH_JOB_ID,
                revision_no=1,
                mode="default",
                steps=None,
                step_config=None,
            )
        assert session.runs == []
        from backend.jobs.run_repository import PUBLISH_RUN, create_run, new_run_id

        run_id = new_run_id()
        with publish_service.platform_session(SETTINGS) as hand_session:
            create_run(
                PUBLISH_RUN,
                hand_session,
                run_id=run_id,
                job_id=PUBLISH_JOB_ID,
                revision_no=1,
                steps=["db"],
                step_params={},
            )
            hand_session.commit()
        execute_publish_run(SETTINGS, run_id)
        run = session.runs[0]
        assert run.status == "failed"
        assert run_step_row(session, run_id, "db").status == "blocked"

    def test_run_not_pending_is_skipped(self, monkeypatch, tmp_path):
        make_ready_db(tmp_path)
        session = install(monkeypatch, tmp_path)
        from backend.jobs.run_repository import PUBLISH_RUN, create_run, new_run_id

        run_id = new_run_id()
        with publish_service.platform_session(SETTINGS) as hand_session:
            create_run(
                PUBLISH_RUN,
                hand_session,
                run_id=run_id,
                job_id=PUBLISH_JOB_ID,
                revision_no=1,
                steps=["db"],
                step_params={},
            )
            hand_session.commit()
        session.runs[0].status = "running"
        execute_publish_run(SETTINGS, run_id)
        # 未执行任何步骤：步骤行保持 pending
        assert run_step_row(session, run_id, "db").status == "pending"

    def test_disabled_source_between_create_and_execute_blocks_step(
        self, monkeypatch, tmp_path
    ):
        make_ready_db(tmp_path)
        session = install(monkeypatch, tmp_path)
        result = create_publish_run(
            SETTINGS,
            PUBLISH_JOB_ID,
            revision_no=1,
            mode="custom",
            steps=["db"],
            step_config=None,
        )
        run_id = UUID(result["publish_run_id"])
        session.source.is_active = False
        execute_publish_run(SETTINGS, run_id)
        db_step = run_step_row(session, run_id, "db")
        assert db_step.status == "blocked"
        assert "禁用" in db_step.message


# ---- _claim_publish_step：同名步骤被其他运行占用 → 阻断 ----


class TestClaimPublishStep:
    def _claim(self, session, step_name="db", run_id=RUN):
        return publish_service._claim_publish_step(
            SETTINGS, WORKSPACE_ID, step_name, run_id
        )

    def test_no_conflict_marks_step_running(self, monkeypatch, tmp_path):
        import backend.jobs.run_repository as run_repository

        session = FakeSession()
        install(monkeypatch, tmp_path, session)
        from backend.jobs.run_repository import PUBLISH_RUN, create_run

        with publish_service.platform_session(SETTINGS) as hand:
            create_run(
                PUBLISH_RUN,
                hand,
                run_id=RUN,
                job_id=PUBLISH_JOB_ID,
                revision_no=1,
                steps=["db"],
                step_params={},
            )
            hand.commit()
        marked = []
        monkeypatch.setattr(
            # doc 10.2 §5.3：条件认领需返回 True 才继续执行 publisher
            run_repository, "mark_step_running", lambda *a, **k: marked.append(a) or True
        )
        assert self._claim(session) is None
        assert marked and marked[0][2] == RUN and marked[0][3] == "db"

    def test_conflicting_running_step_blocks(self, monkeypatch, tmp_path):
        session = FakeSession()
        install(monkeypatch, tmp_path, session)
        # 其他运行的同名步骤正在 running
        session.runs.append(
            SimpleNamespace(
                publish_run_id=RUN2, publish_job_id=PUBLISH_JOB_ID, status="running"
            )
        )
        session.run_steps.append(
            SimpleNamespace(publish_run_id=RUN2, step_name="db", status="running")
        )
        reason = self._claim(session, step_name="db", run_id=RUN)
        assert reason is not None
        assert "正在执行" in reason
        assert session.rolled_back == 1

    def test_missing_workspace_blocks(self, monkeypatch, tmp_path):
        session = FakeSession()
        session.workspace = None
        install(monkeypatch, tmp_path, session)
        assert self._claim(session) == "工作区不存在"


def test_finish_success_rolls_back_when_row_already_terminal(monkeypatch, tmp_path):
    """doc 10.2 §6.1（Kimi R2 发现 2）：步骤行已被并发终结时，_finish_success
    抛错使 PG 内容写入随同一事务回滚，不产生"内容已写而状态未变"。"""
    session = FakeSession()
    install(monkeypatch, tmp_path, session)
    import backend.publishing.publish_service as _ps
    from backend.publishing.publish_service import _finish_success

    monkeypatch.setattr(
        _ps, "finish_step", lambda *args, **kwargs: "failed"
    )  # 模拟条件 UPDATE 未生效（行已 failed）

    with pytest.raises(RuntimeError, match="内容写入回滚"):
        _finish_success(
            session,
            RUN,
            "db",
            "数据库画像发布完成",
            input_manifest=None,
            target_manifest=None,
        )
