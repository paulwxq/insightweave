"""sql 发布（doc 25.2、6.2 §8）：产物就绪、清单枚举与执行链。

就绪检查与 input_manifest 共用 `collect_sql_example_input_files` 枚举
（忽略 `.tmp*` 与点号文件、仅取普通 `*.json`）；失败文案分派覆盖
doc 25.2 §11 的六类出口。
"""

import json
from uuid import UUID

import pytest

from backend.publishing import publish_service
from backend.publishing.precheck import check_step
from backend.publishing.publish_service import (
    _sql_failure_summary,
    create_publish_run,
    execute_publish_run,
)
from metaweave.core.sql_rag.loader import collect_sql_example_input_files
from tests.unit.test_publish_service import (
    FakeSession,
    PUBLISH_JOB_ID,
    RUN,
    SETTINGS,
    SOURCE_ID,
    WORKSPACE_ID,
    install,
    run_step_row,
    step_dir,
    write_marker,
)


def sql_dir(tmp_path):
    return step_dir(tmp_path, "sql")


def sql_ready(tmp_path, names=("q1.json", "q2.json")):
    """写 sql 产物与成功 marker；names 传空列表表示只有目录。"""
    directory = sql_dir(tmp_path)
    directory.mkdir(parents=True, exist_ok=True)
    for name in names:
        (directory / name).write_text(
            json.dumps({"question": "q", "sql": "SELECT 1"}), encoding="utf-8"
        )
    write_marker(tmp_path, "sql")
    return directory


# ---- 就绪检查与 input_manifest 枚举 ----


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    install(monkeypatch, tmp_path)


class TestCheckStepSql:
    def test_ready_lists_json_files_in_order(self, tmp_path):
        sql_ready(tmp_path)
        result = check_step(None, SOURCE_ID, WORKSPACE_ID, "sql", db_name="orders")
        assert result.ready is True and result.reason is None
        assert result.input_files == (
            ("sql/q1.json", str(RUN)),
            ("sql/q2.json", str(RUN)),
        )

    @pytest.mark.parametrize(
        ("prepare", "ready"),
        [
            ("empty-dir", False),
            ("only-tmp", False),
            ("only-dotfile-no-json", False),
            ("missing-dir", False),
        ],
    )
    def test_not_ready_variants(self, tmp_path, prepare, ready):
        if prepare == "empty-dir":
            sql_ready(tmp_path, names=[])
        elif prepare == "only-tmp":
            sql_ready(tmp_path, names=[])
            (sql_dir(tmp_path) / ".tmp123.json").write_text("x", encoding="utf-8")
        elif prepare == "only-dotfile-no-json":
            sql_ready(tmp_path, names=[])
            (sql_dir(tmp_path) / ".hidden").write_text("x", encoding="utf-8")
        result = check_step(None, SOURCE_ID, WORKSPACE_ID, "sql", db_name="orders")
        assert result.ready is ready
        if not ready:
            if prepare == "missing-dir":
                # 无 marker：目录未采集的原因优先于文件枚举
                assert result.reason == "sql/ 目录没有采集 marker，产物不可用"
            else:
                assert result.reason == "sql/ 目录没有可发布的 SQL 样例文件"

    def test_symlink_ignored_in_enumeration(self, tmp_path):
        sql_ready(tmp_path, names=["q1.json"])
        target = sql_dir(tmp_path) / "q1.json"
        link = sql_dir(tmp_path) / "link.json"
        link.symlink_to(target)
        result = check_step(None, SOURCE_ID, WORKSPACE_ID, "sql", db_name="orders")
        assert result.ready is True
        assert [path for path, _run in result.input_files] == ["sql/q1.json"]

    def test_collect_helper_sorts_by_name(self, tmp_path):
        sql_ready(tmp_path, names=["b.json", "a.json"])
        files = collect_sql_example_input_files(sql_dir(tmp_path))
        assert [path.name for path in files] == ["a.json", "b.json"]


# ---- 失败文案分派（doc 25.2 §11） ----


class TestSqlFailureSummary:
    def test_schema_mismatch_hint(self):
        assert "手动删除该集合" in _sql_failure_summary(
            {"error_kind": "schema_mismatch"}
        )

    def test_input_failure_with_file(self):
        message = _sql_failure_summary({"error_kind": "input", "error_file": "q1.json"})
        assert message == "SQL 样例产物不可读或结构非法（q1.json），请重跑 sql 采集步骤"

    def test_input_failure_without_file_omits_parens(self):
        assert _sql_failure_summary({"error_kind": "input"}) == (
            "SQL 样例产物不可读或结构非法，请重跑 sql 采集步骤"
        )

    def test_unconfirmed_message(self):
        message = _sql_failure_summary(
            {
                "success": False,
                "records_loaded": 5,
                "records_failed": 2,
                "records_unconfirmed": 3,
            }
        )
        assert message == (
            "SQL 样例未全部发布到 Milvus（成功 5 条 / 失败 2 条，"
            "另有 3 条写入结果未确认），请重新发布"
        )

    def test_failed_only_message(self):
        assert (
            _sql_failure_summary(
                {"success": False, "records_loaded": 5, "records_failed": 2}
            )
            == "SQL 样例未全部发布到 Milvus（成功 5 条 / 失败 2 条），请重新发布"
        )

    def test_mixed_failed_appends_reason(self):
        message = _sql_failure_summary(
            {
                "success": False,
                "records_loaded": 5,
                "records_failed": 2,
                "error_summary": "unexpected mid-flight",
            }
        )
        assert message == (
            "SQL 样例未全部发布到 Milvus（成功 5 条 / 失败 2 条），请重新发布"
            "；原因：unexpected mid-flight"
        )

    def test_cleanup_message(self):
        message = _sql_failure_summary(
            {
                "success": False,
                "error_kind": "cleanup",
                "records_loaded": 5,
                "error_summary": "delete rpc failed",
            }
        )
        assert message == (
            "SQL 样例已写入 Milvus（共 5 条），但本源旧记录未清理，请重新发布"
            "；原因：delete rpc failed"
        )

    def test_unclassified_message(self):
        assert (
            _sql_failure_summary({"success": False, "error_summary": "boom"})
            == "SQL 样例发布失败（boom），请重新发布"
        )


# ---- 受管加载器构造（doc 25.2 §6.2） ----


class TestBuildSqlExampleLoader:
    def test_injects_workspace_input_dir_and_unique_alias(self, monkeypatch, tmp_path):
        from backend.profile_loading import sql_loader as web_loader

        snapshot = {
            "vector_database": {"providers": {"milvus": {}}},
            "loaders": {"sql_loader": {}},
        }
        monkeypatch.setattr(
            web_loader, "load_main_config_snapshot", lambda *_: snapshot
        )
        first = web_loader.build_sql_example_loader(
            SOURCE_ID, WORKSPACE_ID, UUID(int=1)
        )
        assert first.loader_cfg["input_dir"] == str(sql_dir(tmp_path))
        assert snapshot["vector_database"]["providers"]["milvus"]["alias"].startswith(
            "sql_"
        )
        second = web_loader.build_sql_example_loader(
            SOURCE_ID, WORKSPACE_ID, UUID(int=2)
        )
        assert second.loader_cfg["input_dir"] == first.loader_cfg["input_dir"]
        # 主配置快照不被改写（loader 拿 deepcopy）
        assert snapshot["loaders"]["sql_loader"] == {}


# ---- 执行链 ----


class FakeSqlLoader:
    def __init__(self):
        self.validate_result = True
        self.load_result = {"success": True, "records_loaded": 2}
        self.closed = 0
        self.load_calls = 0

    def validate(self):
        return self.validate_result

    def load(self):
        self.load_calls += 1
        return dict(self.load_result)

    def close(self):
        self.closed += 1


def _publish_sql(monkeypatch, tmp_path, loader):
    sql_ready(tmp_path)
    session = install(monkeypatch, tmp_path, FakeSession(steps=("sql",)))
    monkeypatch.setattr(publish_service, "build_sql_example_loader", lambda *a: loader)
    # target_manifest 的集合名来自主配置；单测注入固定值，不读真实配置文件
    monkeypatch.setattr(
        publish_service, "_milvus_collection", lambda _key: "sql_example_embeddings"
    )
    result = create_publish_run(
        SETTINGS,
        PUBLISH_JOB_ID,
        revision_no=1,
        mode="custom",
        steps=["sql"],
        step_config=None,
    )
    run_id = UUID(result["publish_run_id"])
    execute_publish_run(SETTINGS, run_id)
    return session, run_id


class TestPublishSqlExecution:
    def test_success_message_and_close(self, monkeypatch, tmp_path):
        loader = FakeSqlLoader()
        session, run_id = _publish_sql(monkeypatch, tmp_path, loader)
        step = run_step_row(session, run_id, "sql")
        assert step.status == "success"
        assert step.message == "SQL 样例已发布到 Milvus（共 2 条）"
        assert loader.load_calls == 1 and loader.closed == 1
        assert step.input_manifest == {
            "files": [
                {"path": "sql/q1.json", "collection_run_id": str(RUN)},
                {"path": "sql/q2.json", "collection_run_id": str(RUN)},
            ]
        }
        assert step.target_manifest["targets"][0]["type"] == "milvus"

    def test_validate_failure_message(self, monkeypatch, tmp_path):
        loader = FakeSqlLoader()
        loader.validate_result = False
        session, run_id = _publish_sql(monkeypatch, tmp_path, loader)
        step = run_step_row(session, run_id, "sql")
        assert step.status == "failed"
        assert step.message == "SQL 样例发布失败：Milvus 或 Embedding 服务不可用"
        assert loader.closed == 1

    def test_load_failure_message_from_summary(self, monkeypatch, tmp_path):
        loader = FakeSqlLoader()
        loader.load_result = {
            "success": False,
            "error_kind": "cleanup",
            "records_loaded": 5,
            "error_summary": "delete rpc failed",
        }
        session, run_id = _publish_sql(monkeypatch, tmp_path, loader)
        step = run_step_row(session, run_id, "sql")
        assert step.status == "failed"
        assert step.message == (
            "SQL 样例已写入 Milvus（共 5 条），但本源旧记录未清理，请重新发布"
            "；原因：delete rpc failed"
        )

    def test_files_deleted_after_precheck_is_blocked(self, monkeypatch, tmp_path):
        sql_ready(tmp_path)
        session = install(monkeypatch, tmp_path, FakeSession(steps=("sql",)))
        loader = FakeSqlLoader()
        monkeypatch.setattr(
            publish_service, "build_sql_example_loader", lambda *a: loader
        )
        monkeypatch.setattr(
            publish_service, "_milvus_collection", lambda _key: "sql_example_embeddings"
        )
        result = create_publish_run(
            SETTINGS,
            PUBLISH_JOB_ID,
            revision_no=1,
            mode="custom",
            steps=["sql"],
            step_config=None,
        )
        run_id = UUID(result["publish_run_id"])
        for path in sql_dir(tmp_path).glob("*.json"):
            path.unlink()
        execute_publish_run(SETTINGS, run_id)
        step = run_step_row(session, run_id, "sql")
        assert step.status == "blocked"
        assert step.started_at is None
        assert loader.load_calls == 0
