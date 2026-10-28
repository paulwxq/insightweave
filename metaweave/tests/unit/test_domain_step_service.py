"""受管 Domain 步骤 worker 用例（doc 21.1 §5、6.2 §7.2）。

断代改造适配：上游就绪判定读 `md/` 顶层唯一成功采集 marker；描述来源
改为 `step_params.domain.description`（显式空白=无补充描述）→ 数据源描述；
不再有 sync_domain_description/job_info.db_desc。
"""

import json
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api.errors import register_exception_handlers
from backend.data_sources import domain_step_service as service
from backend.data_sources.domain_step_service import DomainPreparationError
from backend.data_sources.registration_service import PlatformUnavailableError
from backend.data_sources.step_context import DbStepConfigError, StepContext
from backend.persistence.models import DbSourceInfo

SOURCE_ID = "db_src_1"
WORKSPACE_ID = "ws_1"
RUN_ID = UUID("11111111-2222-4333-8444-555555555555")
MD_RUN_ID = UUID("aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee")
DB_NAME = "demo"

MAIN_CONFIG = {
    "database": {"database": "cfgdb"},
    "output": {"output_dir": "output"},
}


def _source(**overrides):
    values = dict(
        source_id=SOURCE_ID,
        display_name="测试源",
        description="源描述",
        db_type="postgresql",
        host="localhost",
        port=5432,
        db_name=DB_NAME,
        username="reader",
        password_ciphertext="cipher",
        sslmode=None,
        db_version=None,
        selected_schemas=["public"],
        is_active=True,
    )
    values.update(overrides)
    return DbSourceInfo(**values)


def _install(
    monkeypatch,
    tmp_path,
    *,
    md_files=("demo.public.t0.md",),
    existing_yaml=None,
    source=None,
):
    """项目根指向 tmp_path；LLM/生成器换替身；备份走真实实现。"""
    from metaweave.utils import file_utils
    from backend.workspace_files.markers import write_collection_marker

    monkeypatch.setattr(file_utils, "get_project_root", lambda: tmp_path)
    import backend.security.source_password as crypto

    monkeypatch.setattr(crypto, "decrypt_password", lambda _cipher: "secret")

    source = source if source is not None else _source()
    workspace = tmp_path / "output" / SOURCE_ID / WORKSPACE_ID
    md_dir = workspace / "md"
    domain_dir = workspace / "domain"
    if md_files:
        md_dir.mkdir(parents=True, exist_ok=True)
        for name in md_files:
            (md_dir / name).write_text("# t\n", encoding="utf-8")
        write_collection_marker(md_dir, MD_RUN_ID, "success")
    if existing_yaml is not None:
        domain_dir.mkdir(parents=True, exist_ok=True)
        (domain_dir / "db_domains.yaml").write_text(existing_yaml, encoding="utf-8")

    rec = SimpleNamespace(
        source=source,
        md_dir=md_dir,
        domain_dir=domain_dir,
        events=[],
        generator_kwargs=[],
        generate_calls=[],
        backups=[],
        write_payload=None,
        raw_output=None,
        on_generate=None,
        on_write=None,
    )
    monkeypatch.setattr(
        service, "resolve_module_llm_config", lambda _c, _p: {"active": "qwen"}
    )

    class FakeLLMService:
        def __init__(self, _config):
            pass

    monkeypatch.setattr(service, "LLMService", FakeLLMService)

    real_backup = service.backup_directory_before_update

    def recorded_backup(directory, **kwargs):
        rec.events.append(("backup", Path(directory)))
        rec.backups.append(Path(directory))
        return real_backup(directory, **kwargs)

    monkeypatch.setattr(service, "backup_directory_before_update", recorded_backup)

    class FakeDomainGenerator:
        def __init__(self, config=None, yaml_path=None, md_context_dir=None):
            rec.events.append(("init", Path(yaml_path)))
            rec.generator_kwargs.append(
                {"yaml_path": yaml_path, "md_context_dir": md_context_dir}
            )
            self.yaml_path = Path(yaml_path)

        def generate_from_context(self, user_description=None):
            rec.events.append(("generate", user_description))
            rec.generate_calls.append(user_description)
            if rec.on_generate is not None:
                rec.on_generate(self)
            return {
                "database": {"description": "d"},
                "domains": [{"name": "订单", "tables": []}],
            }

        def write_to_yaml(self, generated):
            if rec.on_write is not None:
                rec.on_write(rec, generated)
                return
            self.yaml_path.parent.mkdir(parents=True, exist_ok=True)
            if rec.raw_output is not None:
                self.yaml_path.write_text(rec.raw_output, encoding="utf-8")
            else:
                payload = (
                    rec.write_payload if rec.write_payload is not None else generated
                )
                self.yaml_path.write_text(
                    yaml.safe_dump(payload, allow_unicode=True), encoding="utf-8"
                )

    monkeypatch.setattr(service, "DomainGenerator", FakeDomainGenerator)
    return rec


def _ctx(rec, step_params=None):
    return StepContext(
        settings=SimpleNamespace(),
        source=rec.source,
        main_config=json.loads(json.dumps(MAIN_CONFIG)),
        config_path=Path("configs/metadata_config.yaml"),
        source_id=SOURCE_ID,
        workspace_id=WORKSPACE_ID,
        run_id=RUN_ID,
        step_name="domain",
        step_params=step_params,
    )


def _run(monkeypatch, tmp_path, *, step_params=None, **kwargs):
    rec = _install(monkeypatch, tmp_path, **kwargs)
    return rec, service.run_domain_step(_ctx(rec, step_params=step_params))


# ---- 错误类型与 API 映射 ----


def test_domain_preparation_error_maps_to_dedicated_503():
    """准备阶段非受控故障映射 503 domain_preparation_error，不回显原文。"""
    app = FastAPI()
    register_exception_handlers(app)

    @app.get("/boom")
    def _boom():
        raise DomainPreparationError("Domain 准备阶段故障（LLM 预检：ImportError）")

    @app.get("/platform")
    def _platform():
        raise PlatformUnavailableError("平台库暂不可用")

    client = TestClient(app, raise_server_exceptions=False)
    child = client.get("/boom")
    assert child.status_code == 503
    assert child.json()["code"] == "domain_preparation_error"
    assert child.json()["message"] == "Domain 准备阶段故障，请查看日志"
    assert "ImportError" not in child.text  # 不回显异常原文
    parent = client.get("/platform")
    assert parent.status_code == 503
    assert parent.json()["code"] == "platform_unavailable"


# ---- 上游就绪判定（md marker 与目录内容） ----


def test_no_md_marker_rejects_without_side_effect(monkeypatch, tmp_path):
    from metaweave.utils import file_utils

    monkeypatch.setattr(file_utils, "get_project_root", lambda: tmp_path)
    with pytest.raises(DbStepConfigError, match="MD 产物未就绪"):
        service.run_domain_step(
            StepContext(
                settings=SimpleNamespace(),
                source=_source(),
                main_config=dict(MAIN_CONFIG),
                config_path=Path("cfg.yaml"),
                source_id=SOURCE_ID,
                workspace_id=WORKSPACE_ID,
                run_id=RUN_ID,
                step_name="domain",
                step_params=None,
            )
        )
    assert not (tmp_path / "output" / SOURCE_ID / WORKSPACE_ID / "domain").exists()


def test_failed_md_marker_rejects(monkeypatch, tmp_path):
    from backend.workspace_files.markers import write_collection_marker

    rec = _install(monkeypatch, tmp_path)
    write_collection_marker(rec.md_dir, MD_RUN_ID, "failed")
    with pytest.raises(DbStepConfigError, match="MD 产物未就绪"):
        service.run_domain_step(_ctx(rec))
    assert rec.generate_calls == []


def test_md_dir_without_regular_files_rejected(monkeypatch, tmp_path, caplog):
    """目录无常规 md 文件（仅符号链接）视为未就绪（doc 21.1 §5.2）。"""
    from backend.workspace_files.markers import write_collection_marker

    rec = _install(monkeypatch, tmp_path, md_files=())
    rec.md_dir.mkdir(parents=True, exist_ok=True)
    write_collection_marker(rec.md_dir, MD_RUN_ID, "success")
    outside = tmp_path / "outside.md"
    outside.write_text("# x", encoding="utf-8")
    (rec.md_dir / "demo.public.link.md").symlink_to(outside)
    with caplog.at_level("WARNING"):
        with pytest.raises(DbStepConfigError, match="MD 产物未就绪"):
            service.run_domain_step(_ctx(rec))
    assert any("跳过非常规" in record.message for record in caplog.records)


def test_md_dir_with_symlink_among_regular_files_continues(
    monkeypatch, tmp_path, caplog
):
    rec = _install(monkeypatch, tmp_path)
    outside = tmp_path / "outside.md"
    outside.write_text("# x", encoding="utf-8")
    (rec.md_dir / "demo.public.link.md").symlink_to(outside)
    with caplog.at_level("WARNING"):
        outcome = service.run_domain_step(_ctx(rec))
    assert outcome.success is True
    assert any("跳过非常规" in record.message for record in caplog.records)
    # 符号链接不进入输入清单
    assert all("link" not in path for path, _ in outcome.input_files)


def test_domain_dir_symlink_rejected_without_side_effects(monkeypatch, tmp_path):
    rec = _install(monkeypatch, tmp_path)
    rec.domain_dir.parent.mkdir(parents=True, exist_ok=True)
    outside = tmp_path / "outside-domain"
    outside.mkdir()
    (rec.domain_dir.parent / "domain").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="符号链接"):
        service.run_domain_step(_ctx(rec))
    assert rec.generate_calls == []


# ---- 描述生命周期（doc 6.2 §7.2） ----


def test_first_generation_uses_explicit_step_param(monkeypatch, tmp_path):
    rec, outcome = _run(
        monkeypatch,
        tmp_path,
        step_params={"domain": {"description": "  订单库  "}},
    )
    assert outcome.success is True
    assert rec.generate_calls == ["订单库"]  # 明确输入剥除首尾空白


def test_explicit_blank_description_means_no_description(monkeypatch, tmp_path):
    """显式空白 = 无补充描述首次生成，不落回数据源描述（doc 6.2 §7.2）。"""
    rec, outcome = _run(
        monkeypatch,
        tmp_path,
        step_params={"domain": {"description": "   "}},
    )
    assert outcome.success is True
    assert rec.generate_calls == [None]


def test_first_generation_falls_back_to_source_description(monkeypatch, tmp_path):
    rec, outcome = _run(monkeypatch, tmp_path)
    assert outcome.success is True
    assert rec.generate_calls == ["源描述"]


def test_blank_source_description_generates_without_description(monkeypatch, tmp_path):
    rec, outcome = _run(monkeypatch, tmp_path, source=_source(description="   "))
    assert outcome.success is True
    assert rec.generate_calls == [None]


def test_existing_yaml_description_preserved_on_rerun(monkeypatch, tmp_path):
    """已有 YAML：重跑保留 database.description，不写回数据源描述。"""
    rec, outcome = _run(
        monkeypatch,
        tmp_path,
        existing_yaml=yaml.safe_dump(
            {
                "database": {"name": DB_NAME, "description": "  历史描述  "},
                "domains": [{"name": "旧域", "tables": []}],
            },
            allow_unicode=True,
        ),
        step_params={"domain": {"description": "本次输入不覆盖"}},
    )
    assert outcome.success is True
    # 先读需保留的描述，再以 None（保留值传给 LLM 之外的写入路径）生成
    assert rec.generate_calls == ["历史描述"]  # _strip_or_none 归一
    written = yaml.safe_load(
        (rec.domain_dir / "db_domains.yaml").read_text(encoding="utf-8")
    )
    assert written["database"]["description"] == "历史描述"


def test_corrupt_existing_yaml_rejected_not_overwritten(monkeypatch, tmp_path):
    """已有 YAML 损坏：明确报文件无效，不得当作未生成过而覆盖。"""
    rec = _install(monkeypatch, tmp_path, existing_yaml="{ not: valid: yaml:")
    with pytest.raises(DbStepConfigError, match="无法解析"):
        service.run_domain_step(_ctx(rec))
    # 原文件保持原状，未被备份移动、未被覆盖
    assert (rec.domain_dir / "db_domains.yaml").read_text(encoding="utf-8") == (
        "{ not: valid: yaml:"
    )
    assert not (rec.domain_dir / ".backup").exists()


def test_existing_yaml_missing_domains_rejected(monkeypatch, tmp_path):
    rec = _install(
        monkeypatch, tmp_path, existing_yaml=yaml.safe_dump({"database": {}})
    )
    with pytest.raises(DbStepConfigError, match="结构无效"):
        service.run_domain_step(_ctx(rec))


def test_existing_yaml_symlink_rejected(monkeypatch, tmp_path):
    rec = _install(monkeypatch, tmp_path)
    rec.domain_dir.mkdir(parents=True, exist_ok=True)
    outside = tmp_path / "outside.yaml"
    outside.write_text("database: {}\n", encoding="utf-8")
    (rec.domain_dir / "db_domains.yaml").symlink_to(outside)
    with pytest.raises(DbStepConfigError, match="不是普通文件"):
        service.run_domain_step(_ctx(rec))


# ---- 准备阶段故障分类 ----


def test_llm_precheck_value_error_maps_to_config_error(monkeypatch, tmp_path):
    rec = _install(monkeypatch, tmp_path)

    def boom(_config, _path):
        raise ValueError("llm 配置非法")

    monkeypatch.setattr(service, "resolve_module_llm_config", boom)
    with pytest.raises(DbStepConfigError, match="配置非法"):
        service.run_domain_step(_ctx(rec))
    assert rec.generate_calls == []


def test_llm_precheck_unexpected_error_is_preparation_error(monkeypatch, tmp_path):
    rec = _install(monkeypatch, tmp_path)

    class _Boom(Exception):
        pass

    def boom(_config):
        raise _Boom("unexpected")

    monkeypatch.setattr(service, "LLMService", boom)
    with pytest.raises(DomainPreparationError, match="LLM 预检"):
        service.run_domain_step(_ctx(rec))
    assert rec.generate_calls == []


def test_md_glob_permission_error_is_preparation_error(monkeypatch, tmp_path):
    rec = _install(monkeypatch, tmp_path)
    real_glob = Path.glob

    def broken_glob(self, pattern):
        if self == rec.md_dir and pattern == "*.md":
            raise PermissionError("denied")
        return real_glob(self, pattern)

    monkeypatch.setattr(Path, "glob", broken_glob)
    with pytest.raises(DomainPreparationError, match="md 目录枚举"):
        service.run_domain_step(_ctx(rec))


# ---- 备份、生成与核验 ----


def test_success_backs_up_before_generation_and_reports_inputs(monkeypatch, tmp_path):
    """旧 domain 产物（含 marker）在生成前整目录备份；输入携带 md 来源运行。"""
    from backend.workspace_files.markers import write_collection_marker

    rec = _install(monkeypatch, tmp_path, existing_yaml="database: {}\ndomains: []\n")
    write_collection_marker(
        rec.domain_dir, UUID("99999999-9999-4999-8999-999999999999"), "success"
    )
    outcome = service.run_domain_step(_ctx(rec))

    assert outcome.success is True, outcome.message
    # 备份先于生成器构造（doc 6 §6：一次目录更新一个备份单元）
    backup_index = next(i for i, e in enumerate(rec.events) if e[0] == "backup")
    init_index = next(i for i, e in enumerate(rec.events) if e[0] == "init")
    assert backup_index < init_index
    (backup,) = list((rec.domain_dir / ".backup").iterdir())
    assert (backup / "db_domains.yaml").exists()
    assert list(backup.glob(".coll_run_id_*"))
    assert outcome.input_files == [("md/demo.public.t0.md", str(MD_RUN_ID))]
    assert outcome.output_files == ["domain/db_domains.yaml"]


def test_generation_failure_marks_failed(monkeypatch, tmp_path):
    rec = _install(monkeypatch, tmp_path)

    def on_generate(_self):
        raise RuntimeError("boom")

    rec.on_generate = on_generate
    outcome = service.run_domain_step(_ctx(rec))
    assert outcome.success is False
    assert "Domain 生成失败（RuntimeError）" in outcome.message
    assert "boom" not in outcome.message


def test_verify_bad_yaml_fails(monkeypatch, tmp_path):
    rec = _install(monkeypatch, tmp_path)
    rec.raw_output = "{ not: valid"
    outcome = service.run_domain_step(_ctx(rec))
    assert outcome.success is False
    assert "YAML 无法解析" in outcome.message


def test_verify_empty_domains_fails(monkeypatch, tmp_path):
    rec = _install(monkeypatch, tmp_path)
    rec.write_payload = {"database": {"description": "d"}, "domains": []}
    outcome = service.run_domain_step(_ctx(rec))
    assert outcome.success is False
    assert "domains 为空" in outcome.message


def test_verify_symlink_output_fails(monkeypatch, tmp_path):
    """生成后 YAML 被换成符号链接：核验判失败（doc 21.1 §5.4）。"""
    rec = _install(monkeypatch, tmp_path)

    def on_write(r, _generated):
        r.domain_dir.mkdir(parents=True, exist_ok=True)
        outside = r.domain_dir.parent / "outside-domains.yaml"
        outside.write_text("database: {}\ndomains: [x]\n", encoding="utf-8")
        (r.domain_dir / "db_domains.yaml").symlink_to(outside)

    rec.on_write = on_write
    outcome = service.run_domain_step(_ctx(rec))
    assert outcome.success is False
    assert "不是普通文件" in outcome.message
