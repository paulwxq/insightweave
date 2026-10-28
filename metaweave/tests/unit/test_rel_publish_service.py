"""rel 发布（doc 23.2、6 §4.3、6.2 §8）：输入检查、锚点快照与执行链。

新鲜度检查已按 doc 6 §4.3 取消：Rel marker 早于 JSON/Domain 不再阻断
（显式断言）；内容、端点与指纹检查保留，指纹复核覆盖"预检后有编辑保存"。
"""

import hashlib
import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest

import backend.profile_loading.rel_loader as rel_loader
from backend.profile_loading.rel_loader import (
    RelPrecheckError,
    build_rel_input_manifest,
    capture_rel_precheck_snapshot,
    check_rel_output_ready,
    inspect_rel_inputs,
)
from backend.publishing import publish_service
from backend.publishing.precheck import check_step
from backend.publishing.publish_service import (
    create_publish_run,
    execute_publish_run,
)
from metaweave.core.cql_generator import generator as cql_generator_module
from tests.unit.test_json_publish_service import sample_document, write_json_dir
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
import yaml

JSON_RUN = UUID("11111111-1111-4111-8111-111111111111")
DOMAIN_RUN = UUID("22222222-2222-4222-8222-222222222222")
REL_RUN = UUID("33333333-3333-4333-8333-333333333333")

MAIN_CONFIG = {
    "loaders": {
        "cql_loader": {
            "input_file": "output/cql/x.cypher",
            "neo4j": {
                "uri": "bolt://x",
                "user": "u",
                "password": "p",
                "database": "neo4j",
            },
        }
    }
}

REL_ARTIFACT = {
    "generated_by": "rel",
    "database": DB_NAME,
    "relationships": [{"relationship_id": "rel_x"}],
    "statistics": {"total_relationships_found": 1},
}


def registration():
    from backend.profile_loading.json_loader import RegistrationSnapshot

    return RegistrationSnapshot(
        updated_at=datetime(2026, 10, 20, tzinfo=datetime.now().astimezone().tzinfo),
        db_name=DB_NAME,
        selected_schemas=("public",),
    )


def write_domain_yaml(tmp_path, tables=("orders.public.users",)):
    domain_dir = step_dir(tmp_path, "domain")
    domain_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "database": {"name": DB_NAME},
        "domains": [
            {"name": "用户管理", "description": "域", "tables": list(tables)},
        ],
    }
    (domain_dir / "db_domains.yaml").write_text(
        yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    return domain_dir


def write_rel_artifact(tmp_path, artifact=None):
    rel_dir = step_dir(tmp_path, "rel")
    rel_dir.mkdir(parents=True, exist_ok=True)
    text = json.dumps(artifact or REL_ARTIFACT, ensure_ascii=False, indent=2)
    (rel_dir / f"{DB_NAME}.relationships_global.json").write_text(
        text, encoding="utf-8"
    )
    (rel_dir / f"{DB_NAME}.relationships_global.md").write_text("# r", encoding="utf-8")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def rel_ready(tmp_path):
    """三目录 marker + 内容齐备（无任何时间先后要求）。"""
    write_marker(tmp_path, "json", run_id=JSON_RUN)
    write_marker(tmp_path, "domain", run_id=DOMAIN_RUN)
    write_marker(tmp_path, "rel", run_id=REL_RUN)
    write_json_dir(tmp_path, [sample_document(database=DB_NAME)])
    write_domain_yaml(tmp_path)
    return write_rel_artifact(tmp_path)


# ---- 宽松产物就绪（check_rel_output_ready） ----


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    install(monkeypatch, tmp_path)


class TestCheckRelOutputReady:
    def test_ignore_rules(self, tmp_path):
        rel_dir = step_dir(tmp_path, "rel")
        rel_dir.mkdir(parents=True)
        assert check_rel_output_ready(SOURCE_ID, WORKSPACE_ID) is False
        (rel_dir / ".tmp-abc").write_text("x", encoding="utf-8")
        backup = rel_dir / ".backup"
        backup.mkdir()
        (backup / f"{DB_NAME}.relationships_global.json").write_text(
            "{}", encoding="utf-8"
        )
        assert check_rel_output_ready(SOURCE_ID, WORKSPACE_ID) is False
        (rel_dir / f"{DB_NAME}.relationships_global.json").write_text(
            "{}", encoding="utf-8"
        )
        assert check_rel_output_ready(SOURCE_ID, WORKSPACE_ID) is True

    def test_marker_files_do_not_count(self, tmp_path):
        write_marker(tmp_path, "rel", run_id=REL_RUN)
        assert check_rel_output_ready(SOURCE_ID, WORKSPACE_ID) is False

    def test_symlink_file_not_ready(self, tmp_path):
        rel_dir = step_dir(tmp_path, "rel")
        rel_dir.mkdir(parents=True)
        staged = tmp_path / "staged.json"
        staged.write_text("{}", encoding="utf-8")
        (rel_dir / f"{DB_NAME}.relationships_global.json").symlink_to(staged)
        assert check_rel_output_ready(SOURCE_ID, WORKSPACE_ID) is False

    def test_directory_error_degrades_to_not_ready(self, tmp_path, monkeypatch):
        def boom(_s, _j):
            raise ValueError("路径包含符号链接")

        monkeypatch.setattr(rel_loader, "resolve_rel_output_dir", boom)
        assert check_rel_output_ready(SOURCE_ID, WORKSPACE_ID) is False


# ---- inspect_rel_inputs：marker + 内容（无新鲜度比较，doc 6 §4.3） ----


class TestInspectRelInputs:
    def test_happy_path_returns_runs_and_fingerprint(self, tmp_path):
        fingerprint = write_rel_artifact(tmp_path)
        write_marker(tmp_path, "json", run_id=JSON_RUN)
        write_marker(tmp_path, "domain", run_id=DOMAIN_RUN)
        write_marker(tmp_path, "rel", run_id=REL_RUN)
        write_json_dir(tmp_path, [sample_document(database=DB_NAME)])
        write_domain_yaml(tmp_path)
        result = inspect_rel_inputs(SOURCE_ID, WORKSPACE_ID, registration())
        assert result == (str(JSON_RUN), str(DOMAIN_RUN), str(REL_RUN), fingerprint)

    def test_rel_older_than_json_is_accepted(self, tmp_path):
        """新鲜度取消（doc 6 §4.3）：Rel marker 早于 JSON/Domain 仍可发布。"""
        from datetime import timedelta

        from backend.workspace_files.timestamps import SHANGHAI_TZ

        base = datetime(2026, 10, 21, 12, 0, 0, tzinfo=SHANGHAI_TZ)
        write_marker(tmp_path, "rel", run_id=REL_RUN, moment=base)  # Rel 最早完成
        write_marker(tmp_path, "json", run_id=JSON_RUN, moment=base + timedelta(days=1))
        write_marker(
            tmp_path, "domain", run_id=DOMAIN_RUN, moment=base + timedelta(days=2)
        )
        write_json_dir(tmp_path, [sample_document(database=DB_NAME)])
        write_domain_yaml(tmp_path)
        write_rel_artifact(tmp_path)
        result = inspect_rel_inputs(SOURCE_ID, WORKSPACE_ID, registration())
        assert result[2] == str(REL_RUN)  # 不因时间先后拒绝

    @pytest.mark.parametrize("step", ["json", "domain", "rel"])
    def test_missing_marker_rejected(self, tmp_path, step):
        rel_ready(tmp_path)
        for path in step_dir(tmp_path, step).glob(".coll_run_id_*"):
            path.unlink()
        with pytest.raises(RelPrecheckError):
            inspect_rel_inputs(SOURCE_ID, WORKSPACE_ID, registration())

    @pytest.mark.parametrize("step", ["json", "domain", "rel"])
    def test_failed_marker_rejected(self, tmp_path, step):
        rel_ready(tmp_path)
        for path in step_dir(tmp_path, step).glob(".coll_run_id_*"):
            path.unlink()
        write_marker(tmp_path, step, run_id=UUID(int=9), status="failed")
        with pytest.raises(RelPrecheckError, match="未成功"):
            inspect_rel_inputs(SOURCE_ID, WORKSPACE_ID, registration())

    def test_json_products_not_ready(self, tmp_path):
        write_marker(tmp_path, "json", run_id=JSON_RUN)
        write_marker(tmp_path, "domain", run_id=DOMAIN_RUN)
        write_marker(tmp_path, "rel", run_id=REL_RUN)
        # json 目录有 marker 但没有产物
        write_domain_yaml(tmp_path)
        write_rel_artifact(tmp_path)
        with pytest.raises(RelPrecheckError, match="JSON 产物未就绪"):
            inspect_rel_inputs(SOURCE_ID, WORKSPACE_ID, registration())

    def test_domain_artifact_invalid(self, tmp_path):
        write_marker(tmp_path, "json", run_id=JSON_RUN)
        write_marker(tmp_path, "domain", run_id=DOMAIN_RUN)
        write_marker(tmp_path, "rel", run_id=REL_RUN)
        write_json_dir(tmp_path, [sample_document(database=DB_NAME)])
        write_domain_yaml(tmp_path, tables=())
        # 空 tables 的 domains 仍非空列表 → artifact 合法；改成坏 YAML
        (step_dir(tmp_path, "domain") / "db_domains.yaml").write_text(
            "domains: [\n", encoding="utf-8"
        )
        write_rel_artifact(tmp_path)
        with pytest.raises(RelPrecheckError, match="请先执行 Domain 采集"):
            inspect_rel_inputs(SOURCE_ID, WORKSPACE_ID, registration())

    def test_extra_file_rejected(self, tmp_path):
        rel_ready(tmp_path)
        (step_dir(tmp_path, "rel") / "extra.txt").write_text("x", encoding="utf-8")
        with pytest.raises(RelPrecheckError, match="两份文件"):
            inspect_rel_inputs(SOURCE_ID, WORKSPACE_ID, registration())

    def test_tmp_files_ignored(self, tmp_path):
        rel_ready(tmp_path)
        (step_dir(tmp_path, "rel") / ".tmp-x").write_text("x", encoding="utf-8")
        inspect_rel_inputs(SOURCE_ID, WORKSPACE_ID, registration())  # 通过

    def test_database_mismatch_rejected(self, tmp_path):
        write_rel_artifact(
            tmp_path,
            {"database": "other", "relationships": [], "statistics": {}},
        )
        write_marker(tmp_path, "json", run_id=JSON_RUN)
        write_marker(tmp_path, "domain", run_id=DOMAIN_RUN)
        write_marker(tmp_path, "rel", run_id=REL_RUN)
        write_json_dir(tmp_path, [sample_document(database=DB_NAME)])
        write_domain_yaml(tmp_path)
        with pytest.raises(RelPrecheckError, match="结构不合法"):
            inspect_rel_inputs(SOURCE_ID, WORKSPACE_ID, registration())

    def test_broken_json_rejected(self, tmp_path):
        fingerprint = write_rel_artifact(tmp_path)
        assert len(fingerprint) == 16
        (step_dir(tmp_path, "rel") / f"{DB_NAME}.relationships_global.json").write_text(
            "{broken", encoding="utf-8"
        )
        write_marker(tmp_path, "json", run_id=JSON_RUN)
        write_marker(tmp_path, "domain", run_id=DOMAIN_RUN)
        write_marker(tmp_path, "rel", run_id=REL_RUN)
        write_json_dir(tmp_path, [sample_document(database=DB_NAME)])
        write_domain_yaml(tmp_path)
        with pytest.raises(RelPrecheckError, match="无法解析"):
            inspect_rel_inputs(SOURCE_ID, WORKSPACE_ID, registration())


class TestBuildRelInputManifest:
    def test_lists_all_read_files_with_runs(self, tmp_path):
        write_json_dir(tmp_path, [sample_document(database=DB_NAME)])
        write_domain_yaml(tmp_path)
        entries = build_rel_input_manifest(
            SOURCE_ID,
            WORKSPACE_ID,
            str(JSON_RUN),
            str(DOMAIN_RUN),
            str(REL_RUN),
            DB_NAME,
        )
        assert entries == [
            (f"json/{DB_NAME}.public.orders.json", str(JSON_RUN)),
            ("domain/db_domains.yaml", str(DOMAIN_RUN)),
            (f"rel/{DB_NAME}.relationships_global.json", str(REL_RUN)),
            (f"rel/{DB_NAME}.relationships_global.md", str(REL_RUN)),
        ]


class TestCaptureSnapshot:
    @pytest.fixture(autouse=True)
    def _config(self, monkeypatch):
        monkeypatch.setattr(
            rel_loader, "load_main_config_snapshot", lambda _p: MAIN_CONFIG
        )
        monkeypatch.setattr(
            cql_generator_module, "CQLGenerator", lambda *a, **k: object()
        )

    def test_snapshot_bundles_anchors(self, tmp_path):
        fingerprint = rel_ready(tmp_path)
        snapshot = capture_rel_precheck_snapshot(FakeSession(), SOURCE_ID, WORKSPACE_ID)
        assert snapshot.rel_fingerprint == fingerprint
        assert snapshot.json_run_id == str(JSON_RUN)
        assert snapshot.domain_run_id == str(DOMAIN_RUN)
        assert snapshot.rel_run_id == str(REL_RUN)
        assert snapshot.registration.db_name == DB_NAME
        assert snapshot.main_config is MAIN_CONFIG

    def test_bad_main_config_rejected(self, tmp_path, monkeypatch):
        rel_ready(tmp_path)

        def broken_generator(*_args, **_kwargs):
            raise ValueError("composite_score_threshold 非法")

        monkeypatch.setattr(cql_generator_module, "CQLGenerator", broken_generator)
        with pytest.raises(RelPrecheckError, match="composite_score_threshold"):
            capture_rel_precheck_snapshot(FakeSession(), SOURCE_ID, WORKSPACE_ID)

    def test_check_step_rel_wires_manifest_and_payload(self, tmp_path):
        rel_ready(tmp_path)
        session = FakeSession()
        result = check_step(session, SOURCE_ID, WORKSPACE_ID, "rel", db_name=DB_NAME)
        assert result.ready is True
        assert ("domain/db_domains.yaml", str(DOMAIN_RUN)) in result.input_files
        assert result.payload is not None
        assert result.payload.rel_fingerprint == result.payload.rel_fingerprint

    def test_check_step_rel_not_ready_reason(self, tmp_path):
        # rel 目录存在但 json 目录完全没有
        write_marker(tmp_path, "json", run_id=JSON_RUN)
        write_marker(tmp_path, "domain", run_id=DOMAIN_RUN)
        write_marker(tmp_path, "rel", run_id=REL_RUN)
        write_domain_yaml(tmp_path)
        write_rel_artifact(tmp_path)
        session = FakeSession()
        result = check_step(session, SOURCE_ID, WORKSPACE_ID, "rel", db_name=DB_NAME)
        assert result.ready is False
        assert "JSON 产物未就绪" in result.reason


# ---- 执行链（_publish_rel：生成 → 校验 → Neo4j） ----


class FakeGenerator:
    ctor_kwargs: dict = {}
    gen_result: object = None
    gen_raises: Exception | None = None

    def __init__(self, _config_path, **kwargs):
        FakeGenerator.ctor_kwargs = dict(kwargs)

    def generate(self):
        if FakeGenerator.gen_raises is not None:
            raise FakeGenerator.gen_raises
        result = FakeGenerator.gen_result
        cql_dir = Path(result_cql_dir())
        cql_dir.mkdir(parents=True, exist_ok=True)
        written = []
        for name in result.output_files or []:
            path = cql_dir / name
            path.write_text("// fake", encoding="utf-8")
            written.append(str(path))
        result.output_files = written
        return result


def result_cql_dir():
    return FakeGenerator.ctor_kwargs["config"]["output"]["cql_directory"]


class FakeLoader:
    ctor_config: dict = {}
    ctor_source_id = None
    validate_result: bool = True
    load_result: dict = {}
    load_raises: Exception | None = None
    calls: list = []

    def __init__(self, config, source_id=None):
        FakeLoader.ctor_config = config
        FakeLoader.ctor_source_id = source_id

    def validate(self):
        return FakeLoader.validate_result

    def load(self):
        FakeLoader.calls.append("load")
        if FakeLoader.load_raises is not None:
            raise FakeLoader.load_raises
        return dict(FakeLoader.load_result)

    def close(self):
        FakeLoader.calls.append("close")


@pytest.fixture(autouse=True)
def _reset_fakes():
    FakeGenerator.ctor_kwargs = {}
    FakeGenerator.gen_result = None
    FakeGenerator.gen_raises = None
    FakeLoader.ctor_config = {}
    FakeLoader.validate_result = True
    FakeLoader.load_result = {"success": True}
    FakeLoader.load_raises = None
    FakeLoader.calls = []
    yield


def _gen_result(success=True, files=("import_all.orders.cypher", "meta.md"), **counts):
    return SimpleNamespace(
        success=success,
        output_files=list(files),
        tables_count=counts.get("tables", 3),
        columns_count=counts.get("columns", 10),
        relationships_count=counts.get("relationships", 2),
        errors=counts.get("errors", []),
    )


def _install_rel_chain(monkeypatch, tmp_path):
    """替换主配置/生成器/加载器/受管快照；目录仍走真实 resolver。"""
    monkeypatch.setattr(rel_loader, "load_main_config_snapshot", lambda _p: MAIN_CONFIG)
    monkeypatch.setattr(cql_generator_module, "CQLGenerator", lambda *a, **k: object())
    monkeypatch.setattr(publish_service, "CQLGenerator", FakeGenerator)
    monkeypatch.setattr(publish_service, "DomainResolver", lambda _yaml: object())
    monkeypatch.setattr(publish_service, "CQLLoader", FakeLoader)
    root = tmp_path / "output" / SOURCE_ID / WORKSPACE_ID

    def fake_managed_snapshot(_cfg, _sid, _ws, _db, _schemas):
        return dict(MAIN_CONFIG) | {
            "output": {
                "json_directory": str(root / "json"),
                "rel_directory": str(root / "rel"),
                "cql_directory": str(root / "cql"),
            }
        }

    monkeypatch.setattr(
        publish_service, "build_managed_output_snapshot", fake_managed_snapshot
    )
    return root


def _publish_rel_once(monkeypatch, tmp_path, session):
    result = create_publish_run(
        SETTINGS,
        PUBLISH_JOB_ID,
        revision_no=1,
        mode="custom",
        steps=["rel"],
        step_config=None,
    )
    run_id = UUID(result["publish_run_id"])
    execute_publish_run(SETTINGS, run_id)
    return run_id


class TestPublishRelExecution:
    def _run(self, monkeypatch, tmp_path):
        rel_ready(tmp_path)
        session = install(monkeypatch, tmp_path, FakeSession(steps=("rel",)))
        _install_rel_chain(monkeypatch, tmp_path)
        if FakeGenerator.gen_result is None:
            FakeGenerator.gen_result = _gen_result()
        return session, _publish_rel_once(monkeypatch, tmp_path, session)

    def test_success_message_counts_and_provenance(self, monkeypatch, tmp_path):
        FakeGenerator.gen_result = _gen_result(tables=4, columns=30, relationships=3)
        session, run_id = self._run(monkeypatch, tmp_path)
        step = run_step_row(session, run_id, "rel")
        assert step.status == "success"
        assert step.message == "Rel 已发布到 Neo4j（4 张表 / 30 列 / 3 条关系）"
        assert FakeLoader.calls[-1] == "close"
        assert FakeLoader.ctor_source_id == SOURCE_ID
        assert FakeLoader.ctor_config["cql_loader"]["input_file"].endswith(
            "import_all.orders.cypher"
        )
        assert FakeGenerator.ctor_kwargs["provenance"] == (SOURCE_ID, str(run_id))
        # input_manifest 覆盖三目录实际读取文件
        paths = [item["path"] for item in step.input_manifest["files"]]
        assert "domain/db_domains.yaml" in paths
        assert f"rel/{DB_NAME}.relationships_global.json" in paths
        assert step.target_manifest == {
            "targets": [{"type": "neo4j", "database": "neo4j"}]
        }

    def test_old_cql_backed_up_before_generation(self, monkeypatch, tmp_path):
        cql_dir = step_dir(tmp_path, "cql")
        cql_dir.mkdir(parents=True)
        (cql_dir / "import_all.old.cypher").write_text("-- old", encoding="utf-8")
        session, run_id = self._run(monkeypatch, tmp_path)
        backups = list((cql_dir / ".backup").iterdir())
        assert len(backups) == 1
        assert (backups[0] / "import_all.old.cypher").read_text(encoding="utf-8") == (
            "-- old"
        )
        # 新产物在顶层
        assert (cql_dir / "import_all.orders.cypher").is_file()

    def test_zero_relationships_success_with_warning(self, monkeypatch, tmp_path):
        FakeGenerator.gen_result = _gen_result(relationships=0)
        session, run_id = self._run(monkeypatch, tmp_path)
        step = run_step_row(session, run_id, "rel")
        assert step.status == "success"
        assert "关系为空" in step.message

    def test_fingerprint_changed_between_capture_and_recheck_blocks(
        self, monkeypatch, tmp_path
    ):
        """锚点快照与锁下复核之间内容被编辑：指纹捕获 → blocked，不进 Neo4j。

        注：`_publish_rel` 锁内自行重新捕获快照（预检 payload 未回用），
        因此该检查实际覆盖的是"捕获与复核之间"的窗口；预检→执行的窗口
        由执行期 check_step 的内容复查兜底（已单测）。
        """
        import backend.profile_loading.file_utils as profile_file_utils

        rel_ready(tmp_path)
        session = install(monkeypatch, tmp_path, FakeSession(steps=("rel",)))
        _install_rel_chain(monkeypatch, tmp_path)
        FakeGenerator.gen_result = _gen_result()
        real_read = profile_file_utils.read_regular_text
        calls = {"n": 0}

        def flipping_read(path, label):
            status, content = real_read(path, label)
            if path.name.endswith(".relationships_global.json"):
                calls["n"] += 1
                if calls["n"] == 1:
                    # rel_loader 顶部绑定的原函数承担前三次读取（预检两次 +
                    # 锁下捕获）；本 wrapper 只会接到锁下指纹复核这一次——
                    # 返回变化后的内容，模拟捕获与复核之间的编辑保存。
                    return status, json.dumps(
                        {**REL_ARTIFACT, "statistics": {"total_relationships_found": 9}}
                    )
            return status, content

        monkeypatch.setattr(profile_file_utils, "read_regular_text", flipping_read)
        run_id = _publish_rel_once(monkeypatch, tmp_path, session)
        step = run_step_row(session, run_id, "rel")
        assert step.status == "blocked"
        assert "内容已变更" in step.message
        assert FakeLoader.calls == []  # 未进入 Neo4j 加载

    def test_generation_failure_is_failed(self, monkeypatch, tmp_path):
        FakeGenerator.gen_result = _gen_result(
            success=False, files=(), errors=["生成炸了"]
        )
        session, run_id = self._run(monkeypatch, tmp_path)
        step = run_step_row(session, run_id, "rel")
        assert step.status == "failed"
        assert "CQL 生成或产物核验未通过（生成炸了）" in step.message

    def test_outputs_outside_cql_dir_rejected(self, monkeypatch, tmp_path):
        """核验要求产物落在受管 cql 目录内：目录外文件按生成失败处理。"""
        FakeGenerator.gen_result = _gen_result()

        class EscapingGenerator(FakeGenerator):
            def generate(self):
                generated = super().generate()
                (tmp_path / "escaped.cypher").write_text("// out", encoding="utf-8")
                generated.output_files = [str(tmp_path / "escaped.cypher")]
                return generated

        rel_ready(tmp_path)
        session = install(monkeypatch, tmp_path, FakeSession(steps=("rel",)))
        _install_rel_chain(monkeypatch, tmp_path)
        monkeypatch.setattr(publish_service, "CQLGenerator", EscapingGenerator)
        run_id = _publish_rel_once(monkeypatch, tmp_path, session)
        step = run_step_row(session, run_id, "rel")
        assert step.status == "failed"
        assert "CQL 生成或产物核验未通过" in step.message

    def test_validate_failure_is_failed(self, monkeypatch, tmp_path):
        FakeGenerator.gen_result = _gen_result()
        FakeLoader.validate_result = False
        session, run_id = self._run(monkeypatch, tmp_path)
        step = run_step_row(session, run_id, "rel")
        assert step.status == "failed"
        assert step.message == "Rel 发布失败：Neo4j 服务不可用"
        assert "close" in FakeLoader.calls

    def test_rolled_back_failure_keeps_old_graph_message(self, monkeypatch, tmp_path):
        from metaweave.core.loaders.cql_loader import CQLScriptValidationError

        FakeGenerator.gen_result = _gen_result()
        FakeLoader.load_result = {
            "success": False,
            "_rolled_back": True,
            "errors": [
                {"section": "verify", "error_message": "table_count 期望 4/实际 3"}
            ],
        }
        session, run_id = self._run(monkeypatch, tmp_path)
        step = run_step_row(session, run_id, "rel")
        assert step.status == "failed"
        assert step.message == (
            "Rel 发布失败（已回滚，旧图保留）：table_count 期望 4/实际 3"
        )

    def test_unknown_commit_state_failure(self, monkeypatch, tmp_path):
        FakeGenerator.gen_result = _gen_result()
        FakeLoader.load_result = {"success": False}
        session, run_id = self._run(monkeypatch, tmp_path)
        step = run_step_row(session, run_id, "rel")
        assert step.status == "failed"
        assert "提交状态不可知" in step.message

    def test_script_validation_error_before_load(self, monkeypatch, tmp_path):
        from metaweave.core.loaders.cql_loader import CQLScriptValidationError

        FakeGenerator.gen_result = _gen_result()
        FakeLoader.load_raises = CQLScriptValidationError("bad script")
        session, run_id = self._run(monkeypatch, tmp_path)
        step = run_step_row(session, run_id, "rel")
        assert step.status == "failed"
        assert step.message == "Rel 发布失败：bad script"
        assert "close" in FakeLoader.calls

    def test_precheck_marker_missing_rejects_before_run(self, monkeypatch, tmp_path):
        from backend.publishing.precheck import PublishPrecheckError

        write_json_dir(tmp_path, [sample_document(database=DB_NAME)])
        write_domain_yaml(tmp_path)
        write_rel_artifact(tmp_path)  # 三目录都没有 marker
        session = install(monkeypatch, tmp_path, FakeSession(steps=("rel",)))
        with pytest.raises(PublishPrecheckError) as excinfo:
            create_publish_run(
                SETTINGS,
                PUBLISH_JOB_ID,
                revision_no=1,
                mode="custom",
                steps=["rel"],
                step_config=None,
            )
        assert excinfo.value.failures[0]["step"] == "rel"
        assert "marker" in excinfo.value.failures[0]["reason"]
        assert session.runs == []
