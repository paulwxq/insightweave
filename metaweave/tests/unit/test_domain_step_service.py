"""domain 步骤服务：错误类型、准备链判定与生成核验（doc 21.1 §5）。

覆盖批次 2：入口/平台库/本地三段失败分类、描述解析四组合与锁下 strip、
会话 2 的固定顺序、生成与核验各分支、状态写回失败；错误映射按 MRO 先命中。
"""

import json
import logging
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from backend.api.errors import register_exception_handlers
from backend.data_sources import domain_step_service as service
from backend.data_sources.db_step_service import DbStepConfigError, SourceDisabledError
from backend.data_sources.domain_step_service import DomainPreparationError
from backend.data_sources.registration_service import PlatformUnavailableError
from backend.jobs.repository import (
    StepAlreadyRunningError,
    WorkspaceMissingError,
    WorkspaceSourceMismatchError,
)

SOURCE_ID = uuid4()
JOB_ID = uuid4()

MD_READY = "同一作业的 MD 产物未就绪；请先执行 MD 或指定已有 job_id"

MAIN_CONFIG = {
    "database": {"database": "cfgdb"},
    "output": {"output_dir": "output"},
    "domain_generation": {"md_context_limit": 100},
    "llm": {"active": "qwen", "providers": {"qwen": {"model": "m"}}},
}


def _source(**overrides):
    values = dict(
        source_id=SOURCE_ID,
        db_type="postgresql",
        host="localhost",
        port=5432,
        db_name="demo",
        username="reader",
        password_ciphertext="cipher",
        sslmode=None,
        selected_schemas=["public"],
        is_active=True,
        description="源描述",
        updated_at=None,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def _install(
    monkeypatch,
    tmp_path,
    *,
    md_status="success",
    md_file_count=1,
    domain_row=None,
    source=None,
):
    """记录型替身；文件操作走真实实现，项目根指向 tmp_path。"""
    from metaweave.utils import file_utils

    monkeypatch.setattr(file_utils, "get_project_root", lambda: tmp_path)
    source = source if source is not None else _source()
    workspace = tmp_path / "output" / str(SOURCE_ID) / str(JOB_ID)
    md_dir = workspace / "md"
    domain_dir = workspace / "domain"
    if md_file_count:
        md_dir.mkdir(parents=True, exist_ok=True)
        for index in range(md_file_count):
            (md_dir / f"demo.public.t{index}.md").write_text("# t\n", encoding="utf-8")

    rec = SimpleNamespace(
        source=source,
        md_dir=md_dir,
        domain_dir=domain_dir,
        md_status=md_status,
        domain_row=domain_row,
        md_step_error=None,
        session1_error=None,
        events=[],
        started=[],
        finished=[],
        locked=[],
        generator_kwargs=[],
        generate_calls=[],
        clean_calls=[],
        write_payload=None,
        raw_output=None,
        on_write=None,
        on_generate=None,
    )

    monkeypatch.setattr(
        service,
        "load_main_config_snapshot",
        lambda _p: json.loads(json.dumps(MAIN_CONFIG)),
    )
    monkeypatch.setattr(service, "load_platform_settings_from_env", lambda: object())

    @contextmanager
    def fake_session(_settings):
        if rec.session1_error is not None:
            raise rec.session1_error
        yield object()

    monkeypatch.setattr(service, "platform_session", fake_session)
    monkeypatch.setattr(service, "get_db_source", lambda _s, _id: rec.source)
    monkeypatch.setattr(service, "assert_workspace_owner", lambda *a: None)

    def fake_lock(_session, source_id):
        rec.locked.append(source_id)
        return rec.source

    monkeypatch.setattr(service, "lock_db_source_row", fake_lock)

    def fake_get_step_row(_session, job_id, step_name):
        if step_name == "md":
            if rec.md_step_error is not None:
                raise rec.md_step_error
            if rec.md_status is None:
                return None
            return SimpleNamespace(last_status=rec.md_status, updated_at=None)
        if step_name == "domain":
            return rec.domain_row
        return None

    monkeypatch.setattr(service, "get_step_row", fake_get_step_row)

    def fake_start(_session, job_id, source_id, step, **kwargs):
        rec.events.append(("start", step))
        rec.started.append((job_id, source_id, step, kwargs.get("db_desc", "<未传>")))

    monkeypatch.setattr(service, "start_step", fake_start)

    def fake_finish(_session, job_id, step, status):
        rec.events.append(("finish", status))
        rec.finished.append((job_id, step, status))

    monkeypatch.setattr(service, "finish_step", fake_finish)
    monkeypatch.setattr(service, "resolve_module_llm_config", lambda _c, _p: {"active": "qwen"})

    class FakeLLMService:
        def __init__(self, _config):
            pass

    monkeypatch.setattr(service, "LLMService", FakeLLMService)

    def clean_with_record(path):
        rec.events.append(("clean", Path(path)))
        rec.clean_calls.append(Path(path))
        file_utils.clean_top_level_files(path)

    monkeypatch.setattr(service, "clean_top_level_files", clean_with_record)

    class FakeDomainGenerator:
        def __init__(self, config=None, yaml_path=None, md_context_dir=None, **kwargs):
            rec.events.append(("init", Path(yaml_path)))
            rec.generator_kwargs.append(
                {
                    "config": config,
                    "yaml_path": yaml_path,
                    "md_context_dir": md_context_dir,
                    "extra": kwargs,
                }
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


def _run(**overrides):
    return service.run_domain_step(
        "cfg.yaml", str(SOURCE_ID), str(JOB_ID), **overrides
    )


# ---- 错误类型与 API 映射（子步 2d） ----


def test_domain_preparation_error_is_platform_unavailable_subclass():
    """编排层复用既有 except PlatformUnavailableError 分支的前提（doc 21.1 §5.2）。"""
    assert issubclass(DomainPreparationError, PlatformUnavailableError)


def test_domain_preparation_error_handler_maps_before_parent():
    """子类 handler 按 MRO 先于 platform_unavailable 命中（doc 21.1 §9）。"""
    app = FastAPI()
    register_exception_handlers(app)

    @app.get("/child")
    def _child():
        raise DomainPreparationError("Domain 准备阶段故障（LLM 预检：ImportError）")

    @app.get("/parent")
    def _parent():
        raise PlatformUnavailableError("平台库暂不可用")

    client = TestClient(app, raise_server_exceptions=False)
    child = client.get("/child")
    assert child.status_code == 503
    assert child.json()["code"] == "domain_preparation_error"
    assert child.json()["message"] == "Domain 准备阶段故障，请查看日志"
    assert "ImportError" not in child.text  # 不回显异常原文

    parent = client.get("/parent")
    assert parent.status_code == 503
    assert parent.json()["code"] == "platform_unavailable"


# ---- a) 入口与平台库段的受控拒绝 ----


def test_job_id_none_rejects_without_side_effect(monkeypatch, tmp_path):
    from metaweave.utils import file_utils

    monkeypatch.setattr(file_utils, "get_project_root", lambda: tmp_path)
    with pytest.raises(DbStepConfigError, match="MD 产物未就绪"):
        service.run_domain_step("cfg.yaml", str(SOURCE_ID), None)
    assert not (tmp_path / "output").exists()


def test_sync_without_description_rejected_at_entry(monkeypatch, tmp_path):
    rec = _install(monkeypatch, tmp_path)
    with pytest.raises(DbStepConfigError, match="同步数据源描述"):
        _run(sync_domain_description=True)
    assert rec.started == [] and rec.locked == []


def test_invalid_uuid_rejected(monkeypatch, tmp_path):
    _install(monkeypatch, tmp_path)
    with pytest.raises(DbStepConfigError):
        service.run_domain_step("cfg.yaml", "not-a-uuid", str(JOB_ID))


def test_main_config_value_error_maps_to_config_error(monkeypatch, tmp_path):
    _install(monkeypatch, tmp_path)

    def boom(_p):
        raise ValueError("配置非法")

    monkeypatch.setattr(service, "load_main_config_snapshot", boom)
    with pytest.raises(DbStepConfigError, match="配置非法"):
        _run()


def test_main_config_other_error_maps_to_config_error(monkeypatch, tmp_path):
    _install(monkeypatch, tmp_path)

    def boom(_p):
        raise FileNotFoundError("missing")

    monkeypatch.setattr(service, "load_main_config_snapshot", boom)
    with pytest.raises(DbStepConfigError, match="主配置加载或校验失败（FileNotFoundError）"):
        _run()


def test_platform_settings_value_error_maps_to_config_error(monkeypatch, tmp_path):
    _install(monkeypatch, tmp_path)

    def boom():
        raise ValueError("缺少平台库环境变量: PLATFORM_DB_HOST")

    monkeypatch.setattr(service, "load_platform_settings_from_env", boom)
    with pytest.raises(DbStepConfigError, match="缺少平台库环境变量"):
        _run()


def test_source_not_found_rejected(monkeypatch, tmp_path):
    rec = _install(monkeypatch, tmp_path, source=None)
    rec.source = None
    with pytest.raises(DbStepConfigError, match="未找到指定 source_id 的数据源"):
        _run()


def test_source_disabled_raises_source_disabled_error(monkeypatch, tmp_path):
    _install(monkeypatch, tmp_path, source=_source(is_active=False))
    with pytest.raises(SourceDisabledError):
        _run()


def test_workspace_missing_maps_to_config_error(monkeypatch, tmp_path):
    """WorkspaceMissingError 是 LookupError：不得落入平台库故障兜底（§5.2）。"""
    rec = _install(monkeypatch, tmp_path)

    def boom(*_a):
        raise WorkspaceMissingError(f"指定的 job_id 不存在: {JOB_ID}")

    monkeypatch.setattr(service, "assert_workspace_owner", boom)
    with pytest.raises(DbStepConfigError, match="指定的 job_id 不存在"):
        _run()
    assert rec.started == []


def test_workspace_mismatch_maps_to_config_error(monkeypatch, tmp_path):
    rec = _install(monkeypatch, tmp_path)

    def boom(*_a):
        raise WorkspaceSourceMismatchError(f"指定的 job_id 不属于该数据源: {JOB_ID}")

    monkeypatch.setattr(service, "assert_workspace_owner", boom)
    with pytest.raises(DbStepConfigError, match="不属于该数据源"):
        _run()
    assert rec.started == []


def test_platform_read_error_returns_unregistered_result(monkeypatch, tmp_path):
    """会话 1 平台库异常：返回不带 job_id 的失败字典，由编排层分流 503（§9）。"""
    rec = _install(monkeypatch, tmp_path)
    rec.session1_error = OperationalError("SELECT 1", {}, Exception("down"))

    result = _run()

    assert result["success"] is False
    assert result["message"] == "平台库连接或读取失败（OperationalError）"
    assert "job_id" not in result
    assert rec.started == []


def test_md_row_read_error_returns_platform_result(monkeypatch, tmp_path):
    """md 步骤行读取属平台库段：查询异常同样按平台库故障返回（§5.2）。"""
    rec = _install(monkeypatch, tmp_path)
    rec.md_step_error = OperationalError("SELECT", {}, Exception("down"))

    result = _run()

    assert result["success"] is False
    assert result["message"].startswith("平台库连接或读取失败（")


# ---- b) 依赖判定与本地段的受控/故障分类 ----


def test_md_not_success_rejected(monkeypatch, tmp_path):
    rec = _install(monkeypatch, tmp_path, md_status="failed")
    with pytest.raises(DbStepConfigError, match="MD 产物未就绪"):
        _run()
    assert rec.started == []


def test_md_row_missing_rejected(monkeypatch, tmp_path):
    _install(monkeypatch, tmp_path, md_status=None)
    with pytest.raises(DbStepConfigError, match="MD 产物未就绪"):
        _run()


def test_md_dir_empty_rejected(monkeypatch, tmp_path):
    rec = _install(monkeypatch, tmp_path, md_file_count=0)
    with pytest.raises(DbStepConfigError, match="MD 产物未就绪"):
        _run()
    assert rec.started == []


def test_md_dir_symlink_rejected_as_config_error(monkeypatch, tmp_path):
    rec = _install(monkeypatch, tmp_path, md_file_count=0)
    outside = tmp_path / "outside-md"
    outside.mkdir()
    rec.md_dir.parent.mkdir(parents=True, exist_ok=True)
    rec.md_dir.symlink_to(outside, target_is_directory=True)
    with pytest.raises(DbStepConfigError, match="符号链接"):
        _run()


def test_md_dir_with_symlink_among_regular_files_continues(monkeypatch, tmp_path, caplog):
    """混合目录按 §5.2 最简口径继续：跳过非常规文件，普通 .md 仍可生成。

    读取侧另有 O_NOFOLLOW 加固（§13.2）保证被跳过文件的内容不进入提示词。"""
    rec = _install(monkeypatch, tmp_path, md_file_count=1)
    outside = tmp_path / "outside.txt"
    outside.write_text("外部文件内容\n", encoding="utf-8")
    (rec.md_dir / "linked.md").symlink_to(outside)

    with caplog.at_level(logging.WARNING):
        result = _run()

    assert result["success"] is True
    assert rec.started != [] and rec.generate_calls == ["源描述"]
    assert "跳过非常规 md 文件" in caplog.text


def test_md_dir_with_only_symlink_rejected(monkeypatch, tmp_path):
    """全部候选都非常规（无普通 .md）：按「MD 产物未就绪」拒绝。"""
    rec = _install(monkeypatch, tmp_path, md_file_count=0)
    rec.md_dir.mkdir(parents=True, exist_ok=True)
    outside = tmp_path / "outside.txt"
    outside.write_text("外部内容\n", encoding="utf-8")
    (rec.md_dir / "linked.md").symlink_to(outside)

    with pytest.raises(DbStepConfigError, match="MD 产物未就绪"):
        _run()
    assert rec.started == []


def test_md_dir_fifo_only_rejected_as_not_ready(monkeypatch, tmp_path):
    import os

    rec = _install(monkeypatch, tmp_path, md_file_count=0)
    rec.md_dir.mkdir(parents=True, exist_ok=True)
    os.mkfifo(rec.md_dir / "pipe.md")
    with pytest.raises(DbStepConfigError, match="MD 产物未就绪"):
        _run()


def test_domain_dir_symlink_rejected_without_side_effects(monkeypatch, tmp_path):
    rec = _install(monkeypatch, tmp_path)
    outside = tmp_path / "outside-domain"
    outside.mkdir()
    rec.domain_dir.symlink_to(outside, target_is_directory=True)
    with pytest.raises(DbStepConfigError, match="符号链接"):
        _run()
    assert rec.started == []
    assert rec.generator_kwargs == [] and rec.clean_calls == []


def test_llm_precheck_value_error_maps_to_config_error(monkeypatch, tmp_path):
    rec = _install(monkeypatch, tmp_path)

    class Boom:
        def __init__(self, _config):
            raise ValueError("LLM API Key 未配置: qwen")

    monkeypatch.setattr(service, "LLMService", Boom)
    with pytest.raises(DbStepConfigError, match="API Key"):
        _run()
    assert rec.started == []


def test_llm_precheck_unexpected_error_is_preparation_error(
    monkeypatch, tmp_path, caplog
):
    """非 ValueError 的 LLM 预检故障：DomainPreparationError，
    日志只记阶段+类型、不写异常原文（§5.2、doc 15 §8）。"""
    rec = _install(monkeypatch, tmp_path)

    class Boom:
        def __init__(self, _config):
            raise ImportError("cannot import name 'ChatOpenAI'")

    monkeypatch.setattr(service, "LLMService", Boom)
    with caplog.at_level(logging.ERROR):
        with pytest.raises(DomainPreparationError) as excinfo:
            _run()

    assert "LLM 预检" in str(excinfo.value)
    assert "ImportError" in str(excinfo.value)
    assert rec.started == []
    assert "LLM 预检" in caplog.text and "ImportError" in caplog.text
    assert "cannot import name" not in caplog.text


def test_md_glob_permission_error_is_preparation_error(monkeypatch, tmp_path):
    rec = _install(monkeypatch, tmp_path, md_file_count=0)

    def boom(_sid, _jid):
        raise PermissionError("denied")

    monkeypatch.setattr(service, "resolve_md_output_dir", boom)
    with pytest.raises(DomainPreparationError) as excinfo:
        _run()
    assert "md 目录枚举" in str(excinfo.value)
    assert "PermissionError" in str(excinfo.value)
    assert rec.started == []


def test_snapshot_failure_is_preparation_error(monkeypatch, tmp_path):
    rec = _install(monkeypatch, tmp_path)

    def boom(*_a, **_k):
        raise RuntimeError("snapshot broke")

    monkeypatch.setattr(service, "build_managed_output_snapshot", boom)
    with pytest.raises(DomainPreparationError) as excinfo:
        _run()
    assert "快照构造" in str(excinfo.value)


# ---- c) 描述解析四组合与锁下 strip ----


def test_carried_description_used_and_synced(monkeypatch, tmp_path):
    rec = _install(monkeypatch, tmp_path)

    result = _run(domain_description="新描述", sync_domain_description=True)

    assert result["success"] is True
    assert rec.started and rec.started[0][3] == "新描述"
    assert rec.generate_calls == ["新描述"]
    assert rec.source.description == "新描述"
    assert rec.source.updated_at is not None


def test_carried_description_without_sync_keeps_source(monkeypatch, tmp_path):
    rec = _install(monkeypatch, tmp_path)

    _run(domain_description="新描述")

    assert rec.started[0][3] == "新描述"
    assert rec.generate_calls == ["新描述"]
    assert rec.source.description == "源描述"


def test_not_carried_reuses_existing_db_desc(monkeypatch, tmp_path):
    rec = _install(
        monkeypatch, tmp_path, domain_row=SimpleNamespace(db_desc="旧定制")
    )

    _run()

    assert rec.started[0][3] == "旧定制"
    assert rec.generate_calls == ["旧定制"]
    assert rec.source.description == "源描述"


def test_not_carried_falls_back_to_source_description(monkeypatch, tmp_path):
    rec = _install(monkeypatch, tmp_path)

    _run()

    assert rec.started[0][3] == "源描述"
    assert rec.generate_calls == ["源描述"]
    assert rec.source.description == "源描述"


def test_not_carried_both_null_uses_no_description(monkeypatch, tmp_path):
    rec = _install(
        monkeypatch, tmp_path, source=_source(description=None), domain_row=None
    )

    result = _run()

    assert result["success"] is True
    assert rec.started[0][3] is None
    assert rec.generate_calls == [None]


def test_locked_db_desc_stripped(monkeypatch, tmp_path):
    rec = _install(
        monkeypatch, tmp_path, domain_row=SimpleNamespace(db_desc="  旧定制  ")
    )
    _run()
    assert rec.started[0][3] == "旧定制"


def test_locked_source_description_stripped(monkeypatch, tmp_path):
    rec = _install(
        monkeypatch, tmp_path, source=_source(description="  源描述  ")
    )
    _run()
    assert rec.started[0][3] == "源描述"


# ---- d) 会话 2 固定顺序与失败语义 ----


def test_session2_lock_missing_maps_to_not_found(monkeypatch, tmp_path):
    rec = _install(monkeypatch, tmp_path)
    calls = {"n": 0}

    def flaky_lock(_session, source_id):
        calls["n"] += 1
        return None if calls["n"] == 1 else rec.source

    monkeypatch.setattr(service, "lock_db_source_row", flaky_lock)
    with pytest.raises(DbStepConfigError, match="未找到指定 source_id 的数据源"):
        _run()
    assert rec.started == []


def test_session2_md_recheck_failure_rejected(monkeypatch, tmp_path):
    """会话 2 锁下复查 md 不再是 success：抛配置错误、不调用 start_step（§5.2）。"""
    rec = _install(monkeypatch, tmp_path)
    calls = {"n": 0}

    def flaky_get_step_row(_session, job_id, step_name):
        if step_name == "md":
            calls["n"] += 1
            status = "success" if calls["n"] == 1 else "failed"
            return SimpleNamespace(last_status=status, updated_at=None)
        return None

    monkeypatch.setattr(service, "get_step_row", flaky_get_step_row)
    with pytest.raises(DbStepConfigError, match="MD 产物未就绪"):
        _run()
    assert rec.started == []


def test_generation_runs_after_start_step(monkeypatch, tmp_path):
    """⑤ 离开锁会话后才调用 LLM：start 事件先于 generate 事件（§5.2）。"""
    rec = _install(monkeypatch, tmp_path)
    _run()
    assert rec.events.index(("start", "domain")) < next(
        i for i, event in enumerate(rec.events) if event[0] == "generate"
    )


def test_step_already_running_returns_failure_with_job_id(monkeypatch, tmp_path):
    rec = _install(monkeypatch, tmp_path)

    def boom(*_a, **_k):
        raise StepAlreadyRunningError("当前步骤可能已有作业正在执行")

    monkeypatch.setattr(service, "start_step", boom)
    result = _run()

    assert result["success"] is False
    assert result["job_id"] == str(JOB_ID)
    assert result["message"] == "当前步骤可能已有作业正在执行"
    assert rec.generate_calls == []


# ---- e) 生成、核验与状态写回 ----


def test_success_path_cleans_before_construct_and_writes_yaml(monkeypatch, tmp_path):
    rec = _install(monkeypatch, tmp_path)
    rec.domain_dir.mkdir(parents=True, exist_ok=True)
    (rec.domain_dir / "old-artifact.yaml").write_text("old", encoding="utf-8")

    result = _run()

    assert result["success"] is True
    assert result["job_id"] == str(JOB_ID)
    assert rec.events.index(("clean", rec.domain_dir)) < rec.events.index(
        ("init", rec.domain_dir / "db_domains.yaml")
    )
    assert not (rec.domain_dir / "old-artifact.yaml").exists()
    final = yaml.safe_load(
        (rec.domain_dir / "db_domains.yaml").read_text(encoding="utf-8")
    )
    assert final["domains"]
    assert not (rec.domain_dir / "manifest.json").exists()
    assert rec.finished == [(JOB_ID, "domain", "success")]
    kwargs = rec.generator_kwargs[0]
    assert kwargs["config"]["database"]["database"] == "demo"
    assert kwargs["md_context_dir"] == str(rec.md_dir)
    assert kwargs["yaml_path"] == str(rec.domain_dir / "db_domains.yaml")


def test_clean_failure_marks_generation_failed(monkeypatch, tmp_path):
    rec = _install(monkeypatch, tmp_path)

    def boom(_path):
        raise OSError("denied")

    monkeypatch.setattr(service, "clean_top_level_files", boom)
    result = _run()

    assert result["success"] is False
    assert result["message"] == "Domain 生成失败（OSError）"
    assert rec.finished == [(JOB_ID, "domain", "failed")]
    assert rec.generator_kwargs == []


def test_generation_failure_marks_failed_and_keeps_db_desc(monkeypatch, tmp_path):
    rec = _install(monkeypatch, tmp_path, domain_row=SimpleNamespace(db_desc="旧定制"))

    def boom(_self):
        raise RuntimeError("llm down")

    rec.on_generate = boom
    result = _run()

    assert result["success"] is False
    assert result["message"] == "Domain 生成失败（RuntimeError）"
    assert rec.finished == [(JOB_ID, "domain", "failed")]
    assert rec.started[0][3] == "旧定制"  # db_desc 已随 start_step 提交


def test_verify_bad_yaml_fails(monkeypatch, tmp_path):
    rec = _install(monkeypatch, tmp_path)
    rec.raw_output = "domains: [not: valid"
    result = _run()

    assert result["success"] is False
    assert "无法解析" in result["message"]
    assert rec.finished == [(JOB_ID, "domain", "failed")]


def test_verify_empty_domains_fails(monkeypatch, tmp_path):
    """空 domains 不能靠成功写盘复现（write_to_yaml 恒注入 _未分类_），
    这里直接交给写钩子构造（doc 21.1 A08）。"""
    rec = _install(monkeypatch, tmp_path)
    rec.write_payload = {"database": {}, "domains": []}
    result = _run()

    assert result["success"] is False
    assert "domains 为空" in result["message"]


def test_verify_symlink_output_fails(monkeypatch, tmp_path):
    rec = _install(monkeypatch, tmp_path)
    outside = tmp_path / "outside.yaml"
    outside.write_text(
        yaml.safe_dump({"domains": [{"name": "x"}]}, allow_unicode=True),
        encoding="utf-8",
    )

    def on_write(r, _generated):
        r.domain_dir.mkdir(parents=True, exist_ok=True)
        (r.domain_dir / "db_domains.yaml").symlink_to(outside)

    rec.on_write = on_write
    result = _run()

    assert result["success"] is False
    assert "普通文件" in result["message"]


def test_finish_step_failure_reported(monkeypatch, tmp_path):
    _install(monkeypatch, tmp_path)

    def boom(*_a):
        raise RuntimeError("db down")

    monkeypatch.setattr(service, "finish_step", boom)
    result = _run()

    assert result["success"] is False
    assert "作业状态更新失败" in result["message"]
    assert str(JOB_ID) in result["message"]


def test_finish_lock_missing_reports_registration_gone(monkeypatch, tmp_path):
    rec = _install(monkeypatch, tmp_path)
    calls = {"n": 0}

    def flaky_lock(_session, source_id):
        calls["n"] += 1
        return rec.source if calls["n"] == 1 else None

    monkeypatch.setattr(service, "lock_db_source_row", flaky_lock)
    result = _run()

    assert result["success"] is False
    assert "数据源登记已不存在" in result["message"]
    assert str(JOB_ID) in result["message"]
