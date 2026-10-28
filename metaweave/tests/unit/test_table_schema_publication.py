"""table_schema 发布（doc 20.1、6.2 §8）：受管输入、加载器构造与执行链。

受管输入校验（清单、身份、JSON 采集运行 UUID 一致性、字段对齐）经真实
`prepare_managed_table_schema_input`；预检与执行复查共用同一函数。
Web 发布链经假 vector loader 验证固定选项、成功/失败语义与 input_manifest。
"""

import json
from uuid import UUID

import pytest

from backend.profile_loading import table_schema_loader as web_loader
from backend.publishing import publish_service
from backend.publishing.precheck import check_step
from backend.publishing.publish_service import (
    create_publish_run,
    execute_publish_run,
)
from metaweave.core.loaders.table_schema_loader import TableSchemaLoader
from metaweave.core.table_schema.managed_input import (
    configure_managed_loader,
    prepare_managed_table_schema_input,
    read_managed_pairs,
)
from tests.unit.test_json_publish_service import sample_document
from tests.unit.test_publish_service import (
    DB_NAME,
    FakeSession,
    PUBLISH_JOB_ID,
    SETTINGS,
    SOURCE_ID,
    WORKSPACE_ID,
    install,
    run_step_row,
    step_dir,
    write_marker,
)

# MD 清单的 JSON 运行标识契约：json 目录采集 marker 的 collection_run_id
# （UUID 文本，与采集侧 md_step_service 写入口径一致，doc 6 §4.2）。
JSON_RUN = UUID("55555555-5555-4555-8555-555555555555")
MD_RUN = UUID("44444444-4444-4444-8444-444444444444")
JSON_MARKER = str(JSON_RUN)


def write_managed(tmp_path, *, marker=JSON_MARKER):
    """写 json/md 受管产物（含清单与成功 marker）。"""
    json_dir = step_dir(tmp_path, "json")
    md_dir = step_dir(tmp_path, "md")
    json_dir.mkdir(parents=True, exist_ok=True)
    md_dir.mkdir(parents=True, exist_ok=True)
    document = sample_document(database=DB_NAME)
    stem = f"{DB_NAME}.public.orders"
    (json_dir / f"{stem}.json").write_text(
        json.dumps(document, ensure_ascii=False), encoding="utf-8"
    )
    (json_dir / "manifest.json").write_text(
        json.dumps({"files": [f"{stem}.json"]}), encoding="utf-8"
    )
    (md_dir / f"{stem}.md").write_text(
        f"# {stem}\n## 字段列表\n- order_id (integer) - 订单ID\n"
        "- amount (numeric(10,2)) - 金额\n",
        encoding="utf-8",
    )
    (md_dir / "manifest.json").write_text(
        json.dumps({"files": [f"{stem}.md"], "source_collection_run_id": marker}),
        encoding="utf-8",
    )
    write_marker(tmp_path, "json", run_id=JSON_RUN)
    write_marker(tmp_path, "md", run_id=MD_RUN)
    return json_dir, md_dir, stem


def make_loader(tmp_path):
    cfg = {"table_schema_loader": {"collection_name": "schemas"}}
    configure_managed_loader(cfg, SOURCE_ID, WORKSPACE_ID)
    return TableSchemaLoader(cfg, metadata_config={})


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    install(monkeypatch, tmp_path)


# ---- 受管输入解析（prepare_managed_table_schema_input） ----


class TestManagedInput:
    def test_current_format_builds_table_and_column_objects(self, tmp_path):
        json_dir, md_dir, stem = write_managed(tmp_path)
        prepared = prepare_managed_table_schema_input(
            json_dir,
            md_dir,
            SOURCE_ID,
            web_loader.WEB_LOADER_OPTIONS,
            JSON_MARKER,
            DB_NAME,
        )
        assert len(prepared.groups) == 1
        group = prepared.groups[0]
        # Web 固定加载字段级对象：表级 1 + 字段级 2
        assert len(group) == 3
        assert group[0].object_id == stem
        assert group[0].table_category == "fact"
        assert group[2].object_desc == "金额"

    @pytest.mark.parametrize(
        "issue",
        ["marker-mismatch", "missing-md", "missing-column", "identity", "extra-json"],
    )
    def test_reject_bad_input(self, tmp_path, issue):
        json_dir, md_dir, _stem = write_managed(tmp_path)
        if issue == "marker-mismatch":
            path = md_dir / "manifest.json"
            data = json.loads(path.read_text(encoding="utf-8"))
            data["source_collection_run_id"] = str(UUID(int=123))
            path.write_text(json.dumps(data), encoding="utf-8")
        elif issue == "missing-md":
            next(md_dir.glob("*.md")).unlink()
        elif issue == "missing-column":
            next(md_dir.glob("*.md")).write_text(
                "# x\n## 字段列表\n- order_id (integer) - ID\n", encoding="utf-8"
            )
        elif issue == "identity":
            path = json_dir / f"{DB_NAME}.public.orders.json"
            data = json.loads(path.read_text(encoding="utf-8"))
            data["object_info"]["database"] = "other"
            path.write_text(json.dumps(data), encoding="utf-8")
        else:
            (json_dir / "extra.json").write_text("{}", encoding="utf-8")
        with pytest.raises(ValueError):
            prepare_managed_table_schema_input(
                json_dir,
                md_dir,
                SOURCE_ID,
                web_loader.WEB_LOADER_OPTIONS,
                JSON_MARKER,
                DB_NAME,
            )

    def test_marker_must_be_valid_uuid(self, tmp_path):
        json_dir, md_dir, _stem = write_managed(
            tmp_path, marker="2026-10-21T10:00:00.000000Z"
        )
        with pytest.raises(ValueError, match="不是合法 UUID"):
            read_managed_pairs(json_dir, md_dir, JSON_MARKER, DB_NAME)

    def test_foreign_database_rejected(self, tmp_path):
        json_dir, md_dir, _stem = write_managed(tmp_path)
        with pytest.raises(ValueError, match="不属于当前登记数据库"):
            read_managed_pairs(json_dir, md_dir, JSON_MARKER, "otherdb")


class TestCheckOutputReady:
    def test_ready_with_matching_marker(self, tmp_path):
        write_managed(tmp_path)
        assert (
            web_loader.check_table_schema_output_ready(
                SOURCE_ID, WORKSPACE_ID, JSON_MARKER, DB_NAME
            )
            is True
        )

    def test_input_changed_after_check_is_rejected(self, tmp_path):
        _json_dir, md_dir, _stem = write_managed(tmp_path)
        assert (
            web_loader.check_table_schema_output_ready(
                SOURCE_ID, WORKSPACE_ID, JSON_MARKER, DB_NAME
            )
            is True
        )
        next(md_dir.glob("*.md")).write_text(
            "# x\n## 字段列表\n- order_id (integer) - ID\n", encoding="utf-8"
        )
        assert (
            web_loader.check_table_schema_output_ready(
                SOURCE_ID, WORKSPACE_ID, JSON_MARKER, DB_NAME
            )
            is False
        )

    def test_none_marker_or_db_not_ready(self, tmp_path):
        write_managed(tmp_path)
        assert (
            web_loader.check_table_schema_output_ready(
                SOURCE_ID, WORKSPACE_ID, None, DB_NAME
            )
            is False
        )
        assert (
            web_loader.check_table_schema_output_ready(
                SOURCE_ID, WORKSPACE_ID, JSON_MARKER, None
            )
            is False
        )

    def test_precheck_and_execution_share_one_preparation_function(
        self, tmp_path, monkeypatch
    ):
        """A10：预检与加载器 prepare_input 经同一函数解析；预检不触碰外部服务。"""
        from metaweave.core.table_schema import managed_input

        write_managed(tmp_path)
        calls = []
        real = managed_input.prepare_managed_table_schema_input

        def spy(*args, **kwargs):
            calls.append(args[3])  # LoaderOptions
            return real(*args, **kwargs)

        monkeypatch.setattr(web_loader, "prepare_managed_table_schema_input", spy)
        monkeypatch.setattr(managed_input, "prepare_managed_table_schema_input", spy)

        assert web_loader.check_table_schema_output_ready(
            SOURCE_ID, WORKSPACE_ID, JSON_MARKER, DB_NAME
        )
        loader = make_loader(tmp_path)
        loader.prepare_input(JSON_MARKER, DB_NAME)

        assert len(calls) == 2
        assert calls[0].include_columns is True and calls[0].skip_empty_desc is True
        assert loader._milvus_client is None and loader._embedding_service is None


# ---- 加载器构造（固定选项 + 快照不被改写，doc 20.1 §4.2） ----


class TestBuildTableSchemaLoader:
    def _snapshot(self):
        return {
            "vector_database": {"providers": {"milvus": {}}},
            "loaders": {
                "table_schema_loader": {
                    "collection_name": "schemas",
                    "options": {
                        "include_columns": False,
                        "skip_empty_desc": False,
                        "batch_size": 7,
                    },
                }
            },
        }

    def test_forces_fixed_options_and_keeps_snapshot(self, monkeypatch, tmp_path):
        snapshot = self._snapshot()
        monkeypatch.setattr(
            web_loader, "load_main_config_snapshot", lambda *_: snapshot
        )
        publish_run_id = UUID(int=1)
        loader = web_loader.build_table_schema_loader(
            SOURCE_ID, WORKSPACE_ID, publish_run_id
        )
        assert loader.options.include_columns is True
        assert loader.options.skip_empty_desc is True
        assert loader.options.batch_size == 7  # 其他配置不被覆盖
        # 原配置快照不被改写
        assert snapshot["loaders"]["table_schema_loader"]["options"] == {
            "include_columns": False,
            "skip_empty_desc": False,
            "batch_size": 7,
        }
        assert loader.collection_name == "schemas"
        # 受管目录注入指向工作区
        assert loader.md_directory == step_dir(tmp_path, "md")
        assert loader.json_llm_directory == step_dir(tmp_path, "json")

    def test_cli_loader_keeps_config_driven_options(self, tmp_path):
        cfg = {
            "table_schema_loader": {
                "collection_name": "schemas",
                "options": {"include_columns": False, "skip_empty_desc": False},
            }
        }
        configure_managed_loader(cfg, SOURCE_ID, WORKSPACE_ID)
        loader = TableSchemaLoader(cfg, metadata_config={})
        assert loader.options.include_columns is False
        assert loader.options.skip_empty_desc is False


# ---- Web 发布链（预检走真实链路；vector loader 用假件） ----


class FakeVectorLoader:
    def __init__(self):
        self.validate_result = True
        self.load_result = {"success": True, "objects_loaded": 3}
        self.closed = 0
        self.load_calls = 0

    def validate(self):
        return self.validate_result

    def load(self):
        self.load_calls += 1
        return dict(self.load_result)

    def close(self):
        self.closed += 1


def _publish_table_schema(monkeypatch, tmp_path, loader):
    write_managed(tmp_path)
    session = install(monkeypatch, tmp_path, FakeSession(steps=("table_schema",)))
    monkeypatch.setattr(publish_service, "build_table_schema_loader", lambda *a: loader)
    monkeypatch.setattr(
        publish_service, "_milvus_collection", lambda _key: "table_schema_embeddings"
    )
    result = create_publish_run(
        SETTINGS,
        PUBLISH_JOB_ID,
        revision_no=1,
        mode="custom",
        steps=["table_schema"],
        step_config=None,
    )
    run_id = UUID(result["publish_run_id"])
    execute_publish_run(SETTINGS, run_id)
    return session, run_id


class TestPublishExecution:
    def test_check_step_ready_with_real_chain(self, tmp_path):
        write_managed(tmp_path)
        result = check_step(
            FakeSession(), SOURCE_ID, WORKSPACE_ID, "table_schema", db_name=DB_NAME
        )
        assert result.ready is True
        assert result.input_files == (
            (f"json/{DB_NAME}.public.orders.json", str(JSON_RUN)),
            (f"md/{DB_NAME}.public.orders.md", str(MD_RUN)),
        )

    def test_success_message_and_manifests(self, monkeypatch, tmp_path):
        loader = FakeVectorLoader()
        session, run_id = _publish_table_schema(monkeypatch, tmp_path, loader)
        step = run_step_row(session, run_id, "table_schema")
        assert step.status == "success"
        assert step.message == "表结构已发布到 Milvus（3 条）"
        assert loader.load_calls == 1 and loader.closed == 1
        # input_manifest 覆盖 json（不含清单）与 md 文件，来源各自目录 marker
        entries = {
            item["path"]: item["collection_run_id"]
            for item in step.input_manifest["files"]
        }
        assert set(entries) == {
            f"json/{DB_NAME}.public.orders.json",
            f"md/{DB_NAME}.public.orders.md",
        }
        assert entries[f"json/{DB_NAME}.public.orders.json"] == str(JSON_RUN)
        assert entries[f"md/{DB_NAME}.public.orders.md"] == str(MD_RUN)
        assert step.target_manifest == {
            "targets": [{"type": "milvus", "collection": "table_schema_embeddings"}]
        }

    def test_validate_failure_message(self, monkeypatch, tmp_path):
        loader = FakeVectorLoader()
        loader.validate_result = False
        session, run_id = _publish_table_schema(monkeypatch, tmp_path, loader)
        step = run_step_row(session, run_id, "table_schema")
        assert step.status == "failed"
        assert step.message == "表结构发布失败：Milvus 或 Embedding 服务不可用"
        assert loader.closed == 1

    def test_partial_load_failure_message(self, monkeypatch, tmp_path):
        loader = FakeVectorLoader()
        loader.load_result = {
            "success": False,
            "objects_loaded": 1,
            "objects_skipped": 2,
        }
        session, run_id = _publish_table_schema(monkeypatch, tmp_path, loader)
        step = run_step_row(session, run_id, "table_schema")
        assert step.status == "failed"
        assert step.message == (
            "表结构未全部发布到 Milvus（成功 1 条 / 失败 2 条），请重新发布"
        )

    def test_md_deleted_after_precheck_is_blocked(self, monkeypatch, tmp_path):
        write_managed(tmp_path)
        session = install(monkeypatch, tmp_path, FakeSession(steps=("table_schema",)))
        loader = FakeVectorLoader()
        monkeypatch.setattr(
            publish_service, "build_table_schema_loader", lambda *a: loader
        )
        monkeypatch.setattr(
            publish_service,
            "_milvus_collection",
            lambda _key: "table_schema_embeddings",
        )
        result = create_publish_run(
            SETTINGS,
            PUBLISH_JOB_ID,
            revision_no=1,
            mode="custom",
            steps=["table_schema"],
            step_config=None,
        )
        run_id = UUID(result["publish_run_id"])
        # 预检通过后、执行前：MD 文件被删除 → 执行期复查（同一函数）拒绝
        next(step_dir(tmp_path, "md").glob("*.md")).unlink()
        execute_publish_run(SETTINGS, run_id)
        step = run_step_row(session, run_id, "table_schema")
        assert step.status == "blocked"
        assert step.started_at is None
        assert "缺失或不匹配" in step.message
        assert loader.load_calls == 0
