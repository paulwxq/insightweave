"""rel 发布（doc 23.2、6 §4.3、6.2 §8）：输入检查、锚点快照与执行链。

新鲜度检查已按 doc 6 §4.3 取消：Rel marker 早于 JSON/Domain 不再阻断
（显式断言）；内容、端点与指纹检查保留，指纹复核覆盖"预检后有编辑保存"。
"""

import hashlib
import json
from contextlib import contextmanager
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

    def __init__(self, config, source_id=None, lock_probe=None):
        FakeLoader.ctor_config = config
        FakeLoader.ctor_source_id = source_id
        FakeLoader.ctor_lock_probe = lock_probe

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


class _AnyKey:
    """guard 记录断言中的 target_key 通配（键值随配置快照变化）。"""

    def __eq__(self, other):
        return isinstance(other, str) and other.startswith("neo4j|")


ANY_KEY = _AnyKey()


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
        # doc 10.2 §5.1（Kimi R1 意见 3）：成功路径 guard 恰好清除一次；
        # DeepSeek R2 #2：先清 guard 后放锁（顺序回归）
        assert session.guard_releases == [(ANY_KEY, "publish_run", str(run_id))]
        assert session.target_mutex_events == ["guard", "lock"]
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
        if FakeGenerator.gen_result is None:
            FakeGenerator.gen_result = _gen_result()
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
        if FakeGenerator.gen_result is None:
            FakeGenerator.gen_result = _gen_result()
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
            "Rel 发布失败（图未完成替换，旧图保留）：table_count 期望 4/实际 3"
            "；目标保留记录已保留，重跑前须人工核验并清除"
        )
        # doc 10.2 §5.1（Kimi R1 意见 3）：load 失败路径零清除调用；
        # 事件序只有放锁（DeepSeek R3 非阻断 2）
        assert session.guard_releases == []
        assert session.target_mutex_events == ["lock"]

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


# ---- 事务边界拆分（doc 8.1 §5.1.1：不跨 Neo4j 持行锁；运行中互斥） ----


class TestRelPublishTxnSplit:
    def _run_with_probe(self, monkeypatch, tmp_path, probe):
        """执行 rel 发布；FakeLoader.load 开始时调用 probe（记录现场）。"""
        rel_ready(tmp_path)
        session = install(monkeypatch, tmp_path, FakeSession(steps=("rel",)))
        _install_rel_chain(monkeypatch, tmp_path)
        if FakeGenerator.gen_result is None:
            FakeGenerator.gen_result = _gen_result()

        open_sessions = {"count": 0}
        original_ps = publish_service.platform_session

        @contextmanager
        def counting_ps(_settings):
            open_sessions["count"] += 1
            try:
                with original_ps(_settings) as inner:
                    yield inner
            finally:
                open_sessions["count"] -= 1

        monkeypatch.setattr(publish_service, "platform_session", counting_ps)

        class ProbingLoader(FakeLoader):
            def load(self):
                probe(open_sessions["count"], session)
                return super().load()

        monkeypatch.setattr(publish_service, "CQLLoader", ProbingLoader)
        run_id = _publish_rel_once(monkeypatch, tmp_path, session)
        return session, run_id

    def test_no_session_open_during_neo4j_load(self, monkeypatch, tmp_path):
        """Neo4j 调用期间准备事务已提交、会话（与源/工作区行锁）已释放。"""
        observed = []
        session, run_id = self._run_with_probe(
            monkeypatch,
            tmp_path,
            lambda open_count, _s: observed.append(open_count),
        )
        assert observed == [0]
        assert run_step_row(session, run_id, "rel").status == "success"

    def test_run_stays_running_while_neo4j_in_flight(self, monkeypatch, tmp_path):
        """外部调用期间运行保持 running（删除准入据此阻断，不靠行锁）。"""
        observed = {}

        def probe(_open_count, session):
            run = session.runs[0]
            observed["run_status"] = run.status
            observed["step_status"] = run_step_row(
                session, run.publish_run_id, "rel"
            ).status

        session, _run_id = self._run_with_probe(monkeypatch, tmp_path, probe)
        assert observed == {"run_status": "running", "step_status": "running"}
        # 收尾后到终态
        assert session.runs[0].status in ("success", "partial_success")

    def test_neo4j_failure_records_failed_not_running(self, monkeypatch, tmp_path):
        rel_ready(tmp_path)
        session = install(monkeypatch, tmp_path, FakeSession(steps=("rel",)))
        _install_rel_chain(monkeypatch, tmp_path)
        if FakeGenerator.gen_result is None:
            FakeGenerator.gen_result = _gen_result()
        FakeLoader.load_result = {
            "success": False,
            "_rolled_back": True,
            "errors": [{"error_message": "图事务回滚"}],
        }
        run_id = _publish_rel_once(monkeypatch, tmp_path, session)
        step = run_step_row(session, run_id, "rel")
        assert step.status == "failed"
        # doc 10.2 §5.1（Codex R1 意见 5）：_rolled_back 无法区分 schema 阶段
        # 异常 → 一律保留目标保留记录，提示人工核验
        assert "旧图保留" in step.message and "目标保留记录已保留" in step.message
        assert session.runs[0].status == "failed"

    def test_rel_publish_in_progress_helper_scoping(self, monkeypatch, tmp_path):
        """运行中检查：本工作区 running→冲突；步骤终态/兄弟工作区→放行。"""
        from backend.publishing.repository import rel_publish_in_progress

        rel_ready(tmp_path)
        session = install(monkeypatch, tmp_path, FakeSession(steps=("rel",)))
        _install_rel_chain(monkeypatch, tmp_path)
        if FakeGenerator.gen_result is None:
            FakeGenerator.gen_result = _gen_result()
        run_id = _publish_rel_once(monkeypatch, tmp_path, session)
        # 执行已成功 → 无运行中发布
        assert rel_publish_in_progress(session, WORKSPACE_ID) is None

        # 人为构造 running 状态复查
        session.runs[0].status = "running"
        run_step_row(session, run_id, "rel").status = "running"
        assert rel_publish_in_progress(session, WORKSPACE_ID) is not None
        # 兄弟工作区不受影响
        assert rel_publish_in_progress(session, "ws_other") is None
        # 步骤到终态即放行（run 仍 running 不构成输入变更冲突）
        run_step_row(session, run_id, "rel").status = "failed"
        assert rel_publish_in_progress(session, WORKSPACE_ID) is None

    def test_write_rel_config_rejected_while_publish_running(
        self, monkeypatch, tmp_path
    ):
        """发布执行期保存 Rel 配置被拒（doc 10.2 §5.2：409 workspace_publish_active
        双向保留取代原 rel 专用 running 检查）。"""
        from backend.profile_loading.rel_config_api import write_rel_config
        from backend.publishing.repository import WorkspacePublishActiveError

        rel_ready(tmp_path)
        session = install(monkeypatch, tmp_path, FakeSession(steps=("rel",)))
        _install_rel_chain(monkeypatch, tmp_path)
        if FakeGenerator.gen_result is None:
            FakeGenerator.gen_result = _gen_result()
        run_id = _publish_rel_once(monkeypatch, tmp_path, session)
        session.runs[0].status = "running"
        run_step_row(session, run_id, "rel").status = "running"

        import backend.profile_loading.rel_config_api as rel_config_api

        @contextmanager
        def rel_config_ps(_settings):
            yield session

        monkeypatch.setattr(rel_config_api, "platform_session", rel_config_ps)
        monkeypatch.setattr(
            rel_config_api,
            "load_main_config_snapshot",
            lambda _p: MAIN_CONFIG,
        )
        with pytest.raises(WorkspacePublishActiveError):
            write_rel_config(SETTINGS, WORKSPACE_ID, {"fingerprint": "x"})

    def test_collection_rel_claim_blocked_while_publish_running(
        self, monkeypatch, tmp_path
    ):
        """Rel 重新采集在发布执行期被拒；非 rel 步骤不查此互斥。"""
        import backend.jobs.collection_run as collection_run
        from backend.publishing.repository import rel_publish_in_progress

        rel_ready(tmp_path)
        session = install(monkeypatch, tmp_path, FakeSession(steps=("rel",)))
        _install_rel_chain(monkeypatch, tmp_path)
        if FakeGenerator.gen_result is None:
            FakeGenerator.gen_result = _gen_result()
        run_id = _publish_rel_once(monkeypatch, tmp_path, session)
        session.runs[0].status = "running"
        run_step_row(session, run_id, "rel").status = "running"

        @contextmanager
        def fake_ps(_settings):
            yield session

        monkeypatch.setattr(collection_run, "platform_session", fake_ps)
        reason = collection_run._claim_step(SETTINGS, WORKSPACE_ID, "rel", UUID(int=1))
        assert reason is not None and "Rel 发布正在执行" in reason
        # helper 直接验证互斥源
        assert rel_publish_in_progress(session, WORKSPACE_ID) is not None


# ---- Rel 发布输入的双向互斥（doc 8.1 §5.1.1：json/domain/rel 输入保护） ----


class TestRelPublishInputMutualExclusion:
    def _publish_running(self, monkeypatch, tmp_path):
        rel_ready(tmp_path)
        session = install(monkeypatch, tmp_path, FakeSession(steps=("rel",)))
        _install_rel_chain(monkeypatch, tmp_path)
        run_id = _publish_rel_once(monkeypatch, tmp_path, session)
        session.runs[0].status = "running"
        run_step_row(session, run_id, "rel").status = "running"
        return session, run_id

    def test_publish_first_blocks_input_collection_claims(self, monkeypatch, tmp_path):
        """发布先开始：json/domain/rel 采集认领被拒（Rel 输入保护），其余步骤
        也被 doc 10.2 §5.2 工作区级双向保留拒绝。"""
        import backend.jobs.collection_run as collection_run
        import backend.publishing.repository as publishing_repository

        session, run_id = self._publish_running(monkeypatch, tmp_path)
        assert (
            publishing_repository.rel_publish_in_progress(session, WORKSPACE_ID)
            is not None
        )

        @contextmanager
        def fake_ps(_settings):
            yield session

        monkeypatch.setattr(collection_run, "platform_session", fake_ps)
        for step in ("json", "domain", "rel"):
            reason = collection_run._claim_step(
                SETTINGS, WORKSPACE_ID, step, UUID(int=1)
            )
            assert reason is not None and "Rel 发布正在执行" in reason, step
        # doc 10.2 §5.2：非输入步骤同样被工作区级保留拒绝（不再放行）
        reason = collection_run._claim_step(SETTINGS, WORKSPACE_ID, "md", UUID(int=1))
        assert reason is not None and "存在进行中的发布" in reason

    def test_publish_first_blocks_domain_config_save(self, monkeypatch, tmp_path):
        """发布先开始：write_domain_config 被拒（409 workspace_publish_active，
        doc 10.2 §5.2 双向保留取代原 rel 专用检查）。"""
        import backend.profile_loading.domain_config_api as domain_config_api
        from backend.publishing.repository import WorkspacePublishActiveError

        session, _run_id = self._publish_running(monkeypatch, tmp_path)

        @contextmanager
        def fake_ps(_settings):
            yield session

        monkeypatch.setattr(domain_config_api, "platform_session", fake_ps)
        with pytest.raises(WorkspacePublishActiveError):
            domain_config_api.write_domain_config(
                SETTINGS, WORKSPACE_ID, {"fingerprint": "x"}
            )

    def test_collection_first_blocks_rel_publish_claim(self, monkeypatch, tmp_path):
        """采集先开始：Rel 发布认领被拒；其他发布步骤不查此互斥。"""
        from backend.publishing.publish_service import _claim_publish_step
        import backend.publishing.repository as publishing_repository

        rel_ready(tmp_path)
        session = install(monkeypatch, tmp_path, FakeSession(steps=("rel",)))

        collection_run_id = UUID(int=7)

        @contextmanager
        def fake_ps(_settings):
            yield session

        import backend.jobs.collection_run as collection_run

        monkeypatch.setattr(collection_run, "platform_session", fake_ps)
        monkeypatch.setattr(
            publishing_repository,
            "collection_inputs_in_progress",
            lambda _s, _ws: collection_run_id,
        )
        reason = _claim_publish_step(SETTINGS, WORKSPACE_ID, "rel", UUID(int=2))
        assert reason is not None and "采集正在执行" in reason
        # 非 rel 发布步骤不做反向检查
        monkeypatch.setattr(
            publishing_repository,
            "collection_inputs_in_progress",
            lambda _s, _ws: (_ for _ in ()).throw(AssertionError("不应检查")),
        )
        reason_db = _claim_publish_step(SETTINGS, WORKSPACE_ID, "db", UUID(int=3))
        assert reason_db is None or "采集正在执行" not in (reason_db or "")

    def test_restriction_lifts_after_terminal_and_sibling_untouched(
        self, monkeypatch, tmp_path
    ):
        """结束后解除限制；兄弟工作区不受影响（运行中检查按 workspace 过滤）。"""
        session, run_id = self._publish_running(monkeypatch, tmp_path)
        from backend.publishing.repository import rel_publish_in_progress

        assert rel_publish_in_progress(session, WORKSPACE_ID) is not None
        assert rel_publish_in_progress(session, "ws_sibling") is None
        # 步骤到终态即解除（run 汇总前也不构成输入变更冲突）
        run_step_row(session, run_id, "rel").status = "success"
        assert rel_publish_in_progress(session, WORKSPACE_ID) is None


class TestRelLockLossMasking:
    """Codex R2 意见 4 回归：失锁 + 终态写回失败 + 解锁失败三者叠加时，
    _StateWriteFailed 必须原样传播，不得被释放锁的异常遮蔽后转入
    "再记一次 failed"路径（停发语义失效）。"""

    def test_state_write_failure_survives_release_error(
        self, monkeypatch, tmp_path
    ):
        from backend.publishing.publish_service import _StateWriteFailed
        from backend.publishing.target_lock import TargetLockLostError

        rel_ready(tmp_path)
        session = install(monkeypatch, tmp_path, FakeSession(steps=("rel",)))
        _install_rel_chain(monkeypatch, tmp_path)
        if FakeGenerator.gen_result is None:
            FakeGenerator.gen_result = _gen_result()

        # 失锁：探测抛 TargetLockLostError；写回失败：_record_terminal 抛
        # _StateWriteFailed；释放锁也失败：三者叠加
        monkeypatch.setattr(
            publish_service,
            "ensure_target_lock_alive",
            lambda conn: (_ for _ in ()).throw(
                TargetLockLostError("锁连接已失效")
            ),
        )
        monkeypatch.setattr(
            publish_service,
            "release_target_lock",
            lambda conn, key: (_ for _ in ()).throw(
                RuntimeError("解锁失败：连接已断")
            ),
        )

        def _boom(*args, **kwargs):
            raise _StateWriteFailed("rel", "failed")

        monkeypatch.setattr(publish_service, "_record_terminal", _boom)

        from uuid import uuid4 as _uuid4

        run_id = _uuid4()
        with pytest.raises(_StateWriteFailed):
            publish_service._publish_rel(
                SETTINGS,
                run_id,
                WORKSPACE_ID,
                input_manifest=None,
                payload=None,
            )


class TestRelGuardBranches:
    """doc 10.2 §5.1（DeepSeek R1 #4）：guard 占用→blocked 与
    validate 失败（写入前拒绝）→ 清 guard 两分支。"""

    def test_existing_guard_blocks_without_releasing(self, monkeypatch, tmp_path):
        rel_ready(tmp_path)
        session = install(monkeypatch, tmp_path, FakeSession(steps=("rel",)))
        _install_rel_chain(monkeypatch, tmp_path)
        if FakeGenerator.gen_result is None:
            FakeGenerator.gen_result = _gen_result()
        monkeypatch.setattr(
            publish_service,
            "reserve_target_guard",
            lambda sess, tk, kind, oid: {
                "target_key": tk, "owner_kind": "deletion_task",
                "owner_id": "old-task", "reserved_at": None,
            },
        )
        run_id = _publish_rel_once(monkeypatch, tmp_path, session)
        row = run_step_row(session, run_id, "rel")
        assert row.status == "blocked"
        assert "目标保留记录存在" in row.message
        assert "old-task" in row.message
        assert session.guard_releases == [], "非本执行者的记录不得清除"

    def test_validate_failure_clears_guard_after_failed_recorded(
        self, monkeypatch, tmp_path
    ):
        rel_ready(tmp_path)
        session = install(monkeypatch, tmp_path, FakeSession(steps=("rel",)))
        _install_rel_chain(monkeypatch, tmp_path)
        if FakeGenerator.gen_result is None:
            FakeGenerator.gen_result = _gen_result()
        FakeLoader.validate_result = False
        try:
            run_id = _publish_rel_once(monkeypatch, tmp_path, session)
            row = run_step_row(session, run_id, "rel")
            assert row.status == "failed"
            assert "Neo4j 服务不可用" in row.message
            assert len(session.guard_releases) == 1, (
                "写入前拒绝（无任何目标写入）在 failed 可靠提交后清除 guard"
            )
            assert session.guard_releases[0][1:] == ("publish_run", str(run_id))
            # DeepSeek R3 非阻断 2：失败可清除路径同样先清 guard 后放锁
            assert session.target_mutex_events == ["guard", "lock"]
        finally:
            FakeLoader.validate_result = True



class TestRelLockLossDirect:
    """doc 10.2 §5.1（DeepSeek R2 #3）：目标锁失锁 → failed 可靠提交、
    保留记录不清（直测，不经 masking 用例的 _record_terminal 替换）。"""

    def test_lock_loss_records_failed_and_keeps_guard(
        self, monkeypatch, tmp_path
    ):
        from backend.publishing.target_lock import TargetLockLostError

        rel_ready(tmp_path)
        session = install(monkeypatch, tmp_path, FakeSession(steps=("rel",)))
        _install_rel_chain(monkeypatch, tmp_path)
        if FakeGenerator.gen_result is None:
            FakeGenerator.gen_result = _gen_result()
        # 第一次探测（validate 前）即失锁
        monkeypatch.setattr(
            publish_service,
            "ensure_target_lock_alive",
            lambda conn: (_ for _ in ()).throw(
                TargetLockLostError("目标锁连接已失效")
            ),
        )
        run_id = _publish_rel_once(monkeypatch, tmp_path, session)
        row = run_step_row(session, run_id, "rel")
        assert row.status == "failed"
        assert "目标保留记录已保留" in row.message
        assert "人工核验" in row.message
        assert session.guard_releases == [], "失锁路径不得清除保留记录"
        # 事件顺序：无 guard 清除，只有放锁
        assert session.target_mutex_events == ["lock"]
