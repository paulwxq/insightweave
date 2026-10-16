"""doc 20：受管输入、CLI 定位及 Web 发布；不使用真实外部服务。"""

import json
from types import SimpleNamespace
from uuid import uuid4
from unittest.mock import MagicMock

import pytest
from click.testing import CliRunner

from metaweave.cli import loader_cli
from metaweave.core.loaders.table_schema_loader import TableSchemaLoader
from metaweave.core.table_schema.managed_input import (
    configure_managed_loader,
    read_managed_pairs,
)
from metaweave.utils.time_utils import utc_isoformat
from tests.unit.test_json_publish_loader import sample_document
from tests.unit.test_publish_service import (
    SOURCE_ID,
    JOB_ID,
    RUN_MARKER,
    install_session,
)
from tests.unit.test_rag_load_cli import _write_config
from tests.unit.test_web_publishing import _client, _check, _post
from backend.publishing import publish_service
from backend.persistence.models import PublishInfo


@pytest.fixture
def managed(tmp_path, monkeypatch):
    session = install_session(monkeypatch, tmp_path)
    session.source.db_name = "ordersdb"
    session.source.updated_at = RUN_MARKER
    session.source.selected_schemas = ["public"]
    for step in ("json", "md"):
        session.steps.append(
            SimpleNamespace(
                job_id=JOB_ID,
                source_id=SOURCE_ID,
                step_name=step,
                last_status="success",
                updated_at=RUN_MARKER,
            )
        )
    root = tmp_path / "output" / str(SOURCE_ID) / str(JOB_ID)
    json_dir, md_dir = root / "json", root / "md"
    json_dir.mkdir(parents=True)
    md_dir.mkdir()
    data = sample_document()
    data["generated_timestamp"] = utc_isoformat(RUN_MARKER)
    name = "ordersdb.public.orders"
    (json_dir / f"{name}.json").write_text(json.dumps(data), encoding="utf-8")
    (json_dir / "manifest.json").write_text(json.dumps({"files": [f"{name}.json"]}))
    (md_dir / f"{name}.md").write_text(
        "# ordersdb.public.orders\n## 字段列表\n- order_id (integer) - 订单ID\n"
        "- amount (numeric(10,2)) - 金额\n",
        encoding="utf-8",
    )
    (md_dir / "manifest.json").write_text(
        json.dumps(
            {
                "files": [f"{name}.md"],
                "source_json_updated_at": utc_isoformat(RUN_MARKER),
            }
        )
    )
    return session, json_dir, md_dir


def make_loader():
    cfg = {"table_schema_loader": {"collection_name": "schemas"}}
    configure_managed_loader(cfg, SOURCE_ID, JOB_ID)
    return TableSchemaLoader(cfg, metadata_config={})


def test_current_format_and_numeric_columns(managed):
    loader = make_loader()
    groups = loader.prepare_input(utc_isoformat(RUN_MARKER), "ordersdb")
    assert loader.collection_name == "schemas"  # 共用集合，不再按源拆分
    assert len(groups) == 1 and len(groups[0]) == 3
    assert groups[0][0].table_category == "fact"
    assert groups[0][2].object_desc == "金额"
    assert groups[0][0].object_id == "ordersdb.public.orders"


@pytest.mark.parametrize(
    "issue",
    ["old_marker", "rerun", "missing_md", "missing_column", "identity", "extra_json"],
)
def test_reject_bad_input_before_services(managed, issue):
    _, json_dir, md_dir = managed
    if issue in ("old_marker", "rerun"):
        path = md_dir / "manifest.json"
        data = json.loads(path.read_text())
        data["source_json_updated_at"] = (
            RUN_MARKER.isoformat()
            if issue == "old_marker"
            else "2026-09-28T10:00:00.000000Z"
        )
        path.write_text(json.dumps(data))
    elif issue == "missing_md":
        next(md_dir.glob("*.md")).unlink()
    elif issue == "missing_column":
        next(md_dir.glob("*.md")).write_text(
            "# x\n## 字段列表\n- order_id (integer) - ID\n"
        )
    elif issue == "identity":
        path = json_dir / "ordersdb.public.orders.json"
        data = json.loads(path.read_text())
        data["object_info"]["database"] = "other"
        path.write_text(json.dumps(data))
    else:
        (json_dir / "extra.json").write_text("{}")
    loader = make_loader()
    with pytest.raises(ValueError):
        loader.prepare_input(utc_isoformat(RUN_MARKER), "ordersdb")
    assert loader._milvus_client is None and loader._embedding_service is None


@pytest.mark.parametrize(
    "args",
    [
        ["--type", "table_schema", "--source_id", str(SOURCE_ID)],
        ["--type", "table_schema", "--job_id", str(JOB_ID)],
        ["--type", "table_schema", "--source_id", "bad", "--job_id", str(JOB_ID)],
    ],
)
def test_cli_bad_identifiers(tmp_path, args):
    result = CliRunner().invoke(
        loader_cli.rag_load_command, args + ["-c", _write_config(tmp_path)]
    )
    assert result.exit_code == 2


def test_cli_managed_paths_and_clean_target(managed, tmp_path, monkeypatch):
    loader = MagicMock()
    loader.validate.return_value = True
    loader.load.return_value = {"success": True}
    factory = MagicMock(return_value=loader)
    monkeypatch.setattr(loader_cli.LoaderFactory, "create", factory)
    result = CliRunner().invoke(
        loader_cli.rag_load_command,
        [
            "--type",
            "table_schema",
            "--source_id",
            str(SOURCE_ID),
            "--job_id",
            str(JOB_ID),
            "-c",
            _write_config(tmp_path),
            "--clean",
        ],
    )
    assert result.exit_code == 0, result.output
    cfg = factory.call_args.args[1]["table_schema_loader"]
    assert cfg["md_directory"] == str(managed[2])
    assert cfg["json_llm_directory"] == str(managed[1])
    assert cfg["collection_name"] == "ts_c"  # 共用集合，不再按源追加 UUID
    loader.load.assert_called_once_with(clean=False)


def fake_loader(monkeypatch, success=True, started=None, validate=True):
    loader = make_loader()
    monkeypatch.setattr(loader, "validate", lambda: validate)
    result = {
        "success": success,
        "replacement_started": success if started is None else started,
    }
    load = MagicMock(return_value=result)
    monkeypatch.setattr(loader, "load", load)
    monkeypatch.setattr(publish_service, "build_table_schema_loader", lambda *_: loader)
    return load


def test_api_precheck_and_success_record(managed, monkeypatch):
    load = fake_loader(monkeypatch)
    with _client() as client:
        response = _check(client)
        assert response.status_code == 200
        step = next(
            s for s in response.json()["steps"] if s["step_name"] == "table_schema"
        )
        assert step["publishable"] is True
        assert not load.called
        response = _post(client, ["table_schema"])
    assert response.status_code == 200, response.text
    assert response.json()["results"][0]["status"] == "success"
    load.assert_called_once_with()
    assert managed[0].publishes[0].step_name == "table_schema"
    assert managed[0].publishes[0].is_current_publish


def test_api_failure_and_stale_md(managed, monkeypatch):
    load = fake_loader(monkeypatch, success=False)
    with _client() as client:
        response = _post(client, ["table_schema"])
        assert response.status_code == 200
        assert response.json()["results"][0]["status"] == "failed"
        assert not managed[0].publishes[0].is_current_publish
        managed[0].steps[-2].updated_at = RUN_MARKER.replace(day=28)
        response = _check(client)
        step = next(
            s for s in response.json()["steps"] if s["step_name"] == "table_schema"
        )
        assert not step["publishable"]
        response = _post(client, ["table_schema"])
    assert response.status_code == 200
    assert response.json()["results"][0]["status"] == "failed"
    assert load.call_count == 1


def _old_current(managed):
    old = PublishInfo(
        publish_id=uuid4(),
        source_id=SOURCE_ID,
        job_id=JOB_ID,
        step_name="table_schema",
        status="success",
        is_current_publish=True,
        created_at=RUN_MARKER,
        updated_at=RUN_MARKER,
    )
    managed[0].publishes.append(old)
    return old


@pytest.mark.parametrize(
    "scenario, old_stays_current",
    [
        ("validate_fails", True),  # 连接/配置不可用：尚未写 Milvus
        ("fails_before_first_upsert", True),  # 输入/集合/向量化失败
        ("fails_after_first_upsert", False),  # 可能新旧混合：撤销旧 current
    ],
)
def test_old_current_only_revoked_when_replacement_starts(
    managed, monkeypatch, scenario, old_stays_current
):
    old = _old_current(managed)
    load = fake_loader(
        monkeypatch,
        success=False,
        started=scenario == "fails_after_first_upsert",
        validate=scenario != "validate_fails",
    )
    with _client() as client:
        response = _post(client, ["table_schema"])
    assert response.status_code == 200
    assert response.json()["results"][0]["status"] == "failed"
    assert old.is_current_publish is old_stays_current
    assert load.called is (scenario != "validate_fails")
    failed = [p for p in managed[0].publishes if p is not old]
    assert len(failed) == 1 and failed[0].status == "failed"
    assert not failed[0].is_current_publish


def test_loader_exception_after_start_is_treated_as_started(managed, monkeypatch):
    old = _old_current(managed)
    loader = make_loader()
    monkeypatch.setattr(loader, "validate", lambda: True)
    monkeypatch.setattr(loader, "load", MagicMock(side_effect=RuntimeError("x")))
    monkeypatch.setattr(publish_service, "build_table_schema_loader", lambda *_: loader)
    with _client() as client:
        response = _post(client, ["table_schema"])
    assert response.json()["results"][0]["status"] == "failed"
    assert old.is_current_publish is False


@pytest.mark.parametrize("success", [True, False])
def test_loader_close_called_through_public_interface(managed, monkeypatch, success):
    loader = make_loader()
    monkeypatch.setattr(loader, "validate", lambda: True)
    monkeypatch.setattr(
        loader,
        "load",
        MagicMock(return_value={"success": success, "replacement_started": success}),
    )
    close = MagicMock()
    monkeypatch.setattr(loader, "close", close)
    monkeypatch.setattr(publish_service, "build_table_schema_loader", lambda *_: loader)
    with _client() as client:
        _post(client, ["table_schema"])
    close.assert_called_once_with()


def test_loader_close_called_even_when_validate_raises(managed, monkeypatch):
    loader = make_loader()
    monkeypatch.setattr(loader, "validate", MagicMock(side_effect=RuntimeError("x")))
    close = MagicMock()
    monkeypatch.setattr(loader, "close", close)
    monkeypatch.setattr(publish_service, "build_table_schema_loader", lambda *_: loader)
    with _client() as client:
        response = _post(client, ["table_schema"])
    assert response.json()["results"][0]["status"] == "failed"
    close.assert_called_once_with()


def test_build_loader_forces_fixed_options_and_keeps_snapshot(monkeypatch):
    from backend.profile_loading import table_schema_loader as web_loader

    snapshot = {
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
    monkeypatch.setattr(web_loader, "load_main_config_snapshot", lambda *_: snapshot)
    publish_id = uuid4()
    loader = web_loader.build_table_schema_loader(SOURCE_ID, JOB_ID, publish_id)
    assert loader.options.include_columns is True
    assert loader.options.skip_empty_desc is True
    assert loader.options.batch_size == 7  # 其他配置不被覆盖
    assert loader.publish_id == str(publish_id) and loader.source_id == str(SOURCE_ID)
    # 原配置快照不被改写
    assert snapshot["loaders"]["table_schema_loader"]["options"] == {
        "include_columns": False,
        "skip_empty_desc": False,
        "batch_size": 7,
    }
    assert loader.collection_name == "schemas"


def test_cli_loader_keeps_config_driven_options():
    cfg = {
        "table_schema_loader": {
            "collection_name": "schemas",
            "options": {"include_columns": False, "skip_empty_desc": False},
        }
    }
    configure_managed_loader(cfg, SOURCE_ID, JOB_ID)
    loader = TableSchemaLoader(cfg, metadata_config={})
    assert loader.options.include_columns is False
    assert loader.options.skip_empty_desc is False


def _add_step(session, name, status):
    session.steps.append(
        SimpleNamespace(
            job_id=JOB_ID,
            source_id=SOURCE_ID,
            step_name=name,
            last_status=status,
            updated_at=RUN_MARKER,
        )
    )


@pytest.mark.parametrize("other_status", ["failed", "running"])
def test_job_level_admission_blocks_every_publish_item(
    managed, monkeypatch, other_status
):
    """J03：同一 Job 任一现有步骤行 failed/running，包括本次未选择的 md/ddl，都不能发布。"""
    session = managed[0]
    _add_step(session, "ddl", other_status)
    load = fake_loader(monkeypatch)
    with _client() as client:
        check = _check(client)
        assert check.status_code == 200
        by_step = {s["step_name"]: s for s in check.json()["steps"]}
        assert by_step["table_schema"]["publishable"] is False
        assert by_step["json"]["publishable"] is False
        response = _post(client, ["table_schema"])
    assert response.status_code == 409
    assert response.json()["code"] == "job_not_publishable"
    assert session.publishes == [] and not load.called


@pytest.mark.parametrize("own_status", ["failed", "running"])
def test_selected_step_itself_failed_is_job_not_publishable(
    managed, monkeypatch, own_status
):
    """所选 table_schema 依赖的 md 步骤自身 failed/running：整个 Job 不可发布，409。"""
    session = managed[0]
    for step in session.steps:
        if step.step_name == "md":
            step.last_status = own_status
    load = fake_loader(monkeypatch)
    with _client() as client:
        response = _post(client, ["table_schema"])
    assert response.status_code == 409
    assert response.json()["code"] == "job_not_publishable"
    assert session.publishes == [] and not load.called


def test_job_with_only_successful_rows_publishes_without_other_steps(
    managed, monkeypatch
):
    """J02：只有 json/md（无 db/ddl）且全部 success 时 table_schema 可发布。"""
    load = fake_loader(monkeypatch)
    with _client() as client:
        response = _post(client, ["table_schema"])
    assert response.status_code == 200 and load.called


def test_job_state_rechecked_under_source_lock_per_item(managed, monkeypatch):
    """锁下复查：创建发布 ID 后其他步骤变为 failed，该项不执行替换，按逐项失败记录。"""
    session = managed[0]
    ddl_row = SimpleNamespace(
        job_id=JOB_ID,
        source_id=SOURCE_ID,
        step_name="ddl",
        last_status="success",
        updated_at=RUN_MARKER,
    )
    session.steps.append(ddl_row)
    load = fake_loader(monkeypatch)
    original = publish_service.lock_db_source_row

    def flip_after_lock(sess, source_id):
        ddl_row.last_status = "failed"
        return original(sess, source_id)

    monkeypatch.setattr(publish_service, "lock_db_source_row", flip_after_lock)
    with _client() as client:
        response = _post(client, ["table_schema"])
    assert response.status_code == 200
    result = response.json()["results"][0]
    assert result["status"] == "failed" and not load.called
    assert session.publishes and not session.publishes[-1].is_current_publish


def test_precheck_and_execution_share_one_preparation_function(managed, monkeypatch):
    """A10：预检与加载器 prepare_input 经同一函数解析；预检不触碰 Embedding / Milvus。"""
    from backend.profile_loading import table_schema_loader as web_loader
    from metaweave.core.table_schema import managed_input

    calls = []
    real = managed_input.prepare_managed_table_schema_input

    def spy(*args, **kwargs):
        calls.append(args[3])  # LoaderOptions
        return real(*args, **kwargs)

    monkeypatch.setattr(web_loader, "prepare_managed_table_schema_input", spy)
    monkeypatch.setattr(managed_input, "prepare_managed_table_schema_input", spy)

    assert web_loader.check_table_schema_output_ready(
        SOURCE_ID, JOB_ID, RUN_MARKER, "ordersdb"
    )
    loader = make_loader()
    loader.prepare_input(utc_isoformat(RUN_MARKER), "ordersdb")

    assert len(calls) == 2
    assert calls[0].include_columns is True and calls[0].skip_empty_desc is True
    assert loader._milvus_client is None and loader._embedding_service is None


def test_precheck_rejects_input_changed_after_it(managed):
    """A10：输入在预检后变化，执行阶段重新读取并按同一规则拒绝，不沿用旧预检结果。"""
    from backend.profile_loading import table_schema_loader as web_loader

    assert web_loader.check_table_schema_output_ready(
        SOURCE_ID, JOB_ID, RUN_MARKER, "ordersdb"
    )
    md = next(managed[2].glob("*.md"))
    md.write_text("# x\n## 字段列表\n- order_id (integer) - ID\n", encoding="utf-8")
    assert not web_loader.check_table_schema_output_ready(
        SOURCE_ID, JOB_ID, RUN_MARKER, "ordersdb"
    )
    loader = make_loader()
    with pytest.raises(ValueError):
        loader.prepare_input(utc_isoformat(RUN_MARKER), "ordersdb")
