"""dim_config 步骤服务：错误类型、准备链判定与生成核验（doc 22.1 §5）。

覆盖 §11 批次 1 + 批次 3：入口/平台库/本地三段失败分类、json 依赖判定
（含 manifest.json 排除）、生成器 `json_files` 透传、临时文件 + os.replace
落盘、核验各分支、状态写回失败；错误映射按 MRO 先命中。
"""

import json
import logging
import os
import stat
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
from backend.data_sources import dim_config_step_service as service
from backend.data_sources.db_step_service import DbStepConfigError, SourceDisabledError
from backend.data_sources.dim_config_step_service import DimConfigPreparationError
from backend.data_sources.registration_service import PlatformUnavailableError
from backend.jobs.repository import (
    StepAlreadyRunningError,
    WorkspaceMissingError,
    WorkspaceSourceMismatchError,
)

SOURCE_ID = uuid4()
JOB_ID = uuid4()

JSON_NOT_READY = "同一作业的 JSON 产物未就绪；请先执行 JSON 或指定已有 job_id"

DEFAULT_CONFIG = {
    "databases": {"demo": {"tables": {"public.dim_a": {"embedding_col": None}}}}
}


def _dim_profile(table: str) -> str:
    return json.dumps(
        {
            "object_info": {
                "database": "demo",
                "schema_name": "public",
                "object_name": table,
            },
            "table_profile": {"table_category": "dim"},
        }
    )


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
    json_status="success",
    json_tables=("dim_a",),
    with_manifest=False,
    source=None,
):
    """记录型替身；文件操作走真实实现，项目根指向 tmp_path。"""
    from metaweave.utils import file_utils

    monkeypatch.setattr(file_utils, "get_project_root", lambda: tmp_path)
    source = source if source is not None else _source()
    workspace = tmp_path / "output" / str(SOURCE_ID) / str(JOB_ID)
    json_dir = workspace / "json"
    dim_config_dir = workspace / "dim_config"
    if json_tables or with_manifest:
        json_dir.mkdir(parents=True, exist_ok=True)
        for table in json_tables:
            (json_dir / f"demo.public.{table}.json").write_text(
                _dim_profile(table), encoding="utf-8"
            )
        if with_manifest:
            (json_dir / "manifest.json").write_text(
                json.dumps({"files": ["demo.public.dim_a.json"]}), encoding="utf-8"
            )

    rec = SimpleNamespace(
        source=source,
        json_dir=json_dir,
        dim_config_dir=dim_config_dir,
        json_status=json_status,
        json_step_error=None,
        session1_error=None,
        events=[],
        started=[],
        finished=[],
        locked=[],
        generator_kwargs=[],
        clean_calls=[],
        write_payload=None,
        raw_output=None,
        on_generate=None,
        readable_count=None,
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
        if step_name == "json":
            if rec.json_step_error is not None:
                raise rec.json_step_error
            if rec.json_status is None:
                return None
            return SimpleNamespace(last_status=rec.json_status, updated_at=None)
        return None

    monkeypatch.setattr(service, "get_step_row", fake_get_step_row)

    def fake_start(_session, job_id, source_id, step, **kwargs):
        rec.events.append(("start", step))
        rec.started.append((job_id, source_id, step))

    monkeypatch.setattr(service, "start_step", fake_start)

    def fake_finish(_session, job_id, step, status):
        rec.events.append(("finish", status))
        rec.finished.append((job_id, step, status))

    monkeypatch.setattr(service, "finish_step", fake_finish)

    def clean_with_record(path):
        rec.events.append(("clean", Path(path)))
        rec.clean_calls.append(Path(path))
        file_utils.clean_top_level_files(path)

    monkeypatch.setattr(service, "clean_top_level_files", clean_with_record)

    class FakeDimConfigGenerator:
        def __init__(self, json_dir=None, output_path=None, json_files=None):
            rec.events.append(("init", Path(output_path)))
            rec.generator_kwargs.append(
                {
                    "json_dir": json_dir,
                    "output_path": output_path,
                    "json_files": json_files,
                }
            )
            self.output_path = Path(output_path)
            self._json_files = json_files
            self.last_readable_count = None

        def generate(self):
            rec.events.append(("generate",))
            if rec.on_generate is not None:
                rec.on_generate(self)
            # 缺省视为全部可读；测试可经 rec.readable_count 覆盖（审核修订 7）
            self.last_readable_count = (
                rec.readable_count
                if rec.readable_count is not None
                else len(self._json_files or [])
            )
            payload = (
                rec.write_payload if rec.write_payload is not None else DEFAULT_CONFIG
            )
            text = (
                rec.raw_output
                if rec.raw_output is not None
                else yaml.safe_dump(payload, allow_unicode=True)
            )
            self.output_path.write_text(text, encoding="utf-8")
            return payload

    monkeypatch.setattr(service, "DimTableConfigGenerator", FakeDimConfigGenerator)
    return rec


def _run(**overrides):
    return service.run_dim_config_step(
        "cfg.yaml", str(SOURCE_ID), str(JOB_ID), **overrides
    )


def _leftover_tmp_files(dim_config_dir: Path):
    if not dim_config_dir.is_dir():
        return []
    return list(dim_config_dir.glob(".dim_tables.*.tmp"))


# ---- 错误类型与 API 映射（§11 批次 3） ----


def test_dim_config_preparation_error_is_platform_unavailable_subclass():
    """编排层复用既有 except PlatformUnavailableError 分支的前提（doc 22.1 §5.2）。"""
    assert issubclass(DimConfigPreparationError, PlatformUnavailableError)


def test_dim_config_preparation_error_handler_maps_before_parent():
    """子类 handler 按 MRO 先于 platform_unavailable 命中（doc 22.1 §7/§9）。"""
    app = FastAPI()
    register_exception_handlers(app)

    @app.get("/child")
    def _child():
        raise DimConfigPreparationError(
            "Dim Config 准备阶段故障（json 目录枚举：PermissionError）"
        )

    @app.get("/parent")
    def _parent():
        raise PlatformUnavailableError("平台库暂不可用")

    client = TestClient(app, raise_server_exceptions=False)
    child = client.get("/child")
    assert child.status_code == 503
    assert child.json()["code"] == "dim_config_preparation_error"
    assert child.json()["message"] == "Dim Config 准备阶段故障，请查看日志"
    assert "PermissionError" not in child.text  # 不回显异常原文

    parent = client.get("/parent")
    assert parent.status_code == 503
    assert parent.json()["code"] == "platform_unavailable"


# ---- a) 入口与平台库段的受控拒绝 ----


def test_job_id_none_rejects_without_side_effect(monkeypatch, tmp_path):
    from metaweave.utils import file_utils

    monkeypatch.setattr(file_utils, "get_project_root", lambda: tmp_path)
    with pytest.raises(DbStepConfigError, match="JSON 产物未就绪"):
        service.run_dim_config_step("cfg.yaml", str(SOURCE_ID), None)
    assert not (tmp_path / "output").exists()


def test_invalid_uuid_rejected(monkeypatch, tmp_path):
    _install(monkeypatch, tmp_path)
    with pytest.raises(DbStepConfigError):
        service.run_dim_config_step("cfg.yaml", "not-a-uuid", str(JOB_ID))


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


def test_json_row_read_error_returns_platform_result(monkeypatch, tmp_path):
    """json 步骤行读取属平台库段：查询异常同样按平台库故障返回（§5.2）。"""
    rec = _install(monkeypatch, tmp_path)
    rec.json_step_error = OperationalError("SELECT", {}, Exception("down"))

    result = _run()

    assert result["success"] is False
    assert result["message"].startswith("平台库连接或读取失败（")


# ---- b) 依赖判定与本地段的受控/故障分类 ----


def test_json_not_success_rejected(monkeypatch, tmp_path):
    rec = _install(monkeypatch, tmp_path, json_status="failed")
    with pytest.raises(DbStepConfigError, match="JSON 产物未就绪"):
        _run()
    assert rec.started == []


def test_json_row_missing_rejected(monkeypatch, tmp_path):
    _install(monkeypatch, tmp_path, json_status=None)
    with pytest.raises(DbStepConfigError, match="JSON 产物未就绪"):
        _run()


def test_json_dir_missing_rejected(monkeypatch, tmp_path):
    rec = _install(monkeypatch, tmp_path, json_tables=())
    with pytest.raises(DbStepConfigError, match="JSON 产物未就绪"):
        _run()
    assert rec.started == []


def test_json_dir_only_manifest_rejected(monkeypatch, tmp_path):
    """仅剩清单（审核修订 6）：manifest.json 不是画像产物，不算就绪。"""
    rec = _install(monkeypatch, tmp_path, json_tables=(), with_manifest=True)
    with pytest.raises(DbStepConfigError, match="JSON 产物未就绪"):
        _run()
    assert rec.started == []


def test_json_dir_manifest_plus_fifo_rejected(monkeypatch, tmp_path):
    """清单 + 非常规画像文件而无普通画像 .json：同样不就绪（审核修订 6）。"""
    rec = _install(monkeypatch, tmp_path, json_tables=(), with_manifest=True)
    os.mkfifo(rec.json_dir / "demo.public.dim_x.json")
    with pytest.raises(DbStepConfigError, match="JSON 产物未就绪"):
        _run()
    assert rec.started == []


def test_json_dir_with_symlink_among_regular_files_continues(
    monkeypatch, tmp_path, caplog
):
    """混合目录：符号链接跳过并告警、不进入生成输入（A04 f / A07）。"""
    rec = _install(monkeypatch, tmp_path)
    outside = tmp_path / "outside.json"
    outside.write_text(_dim_profile("dim_outside"), encoding="utf-8")
    (rec.json_dir / "linked.json").symlink_to(outside)

    with caplog.at_level(logging.WARNING):
        result = _run()

    assert result["success"] is True
    assert rec.started != []
    assert "跳过非常规 json 文件" in caplog.text
    expected = [rec.json_dir / "demo.public.dim_a.json"]
    assert rec.generator_kwargs[0]["json_files"] == expected


def test_json_dir_with_only_symlink_rejected(monkeypatch, tmp_path):
    rec = _install(monkeypatch, tmp_path, json_tables=())
    rec.json_dir.mkdir(parents=True, exist_ok=True)
    outside = tmp_path / "outside.json"
    outside.write_text(_dim_profile("dim_outside"), encoding="utf-8")
    (rec.json_dir / "linked.json").symlink_to(outside)

    with pytest.raises(DbStepConfigError, match="JSON 产物未就绪"):
        _run()
    assert rec.started == []


def test_json_dir_symlink_rejected_as_config_error(monkeypatch, tmp_path):
    rec = _install(monkeypatch, tmp_path, json_tables=())
    outside = tmp_path / "outside-json"
    outside.mkdir()
    rec.json_dir.parent.mkdir(parents=True, exist_ok=True)
    rec.json_dir.symlink_to(outside, target_is_directory=True)
    with pytest.raises(DbStepConfigError, match="符号链接"):
        _run()


def test_dim_config_dir_symlink_rejected_without_side_effects(monkeypatch, tmp_path):
    rec = _install(monkeypatch, tmp_path)
    outside = tmp_path / "outside-dim-config"
    outside.mkdir()
    rec.dim_config_dir.symlink_to(outside, target_is_directory=True)
    with pytest.raises(DbStepConfigError, match="符号链接"):
        _run()
    assert rec.started == []
    assert rec.generator_kwargs == [] and rec.clean_calls == []


def test_json_glob_permission_error_is_preparation_error(monkeypatch, tmp_path, caplog):
    """本地段非 ValueError：DimConfigPreparationError，
    日志只记阶段+类型、不写异常原文（§5.2、doc 15 §8）。"""
    rec = _install(monkeypatch, tmp_path, json_tables=())

    def boom(_sid, _jid):
        raise PermissionError("denied")

    monkeypatch.setattr(service, "resolve_json_output_dir", boom)
    with caplog.at_level(logging.ERROR):
        with pytest.raises(DimConfigPreparationError) as excinfo:
            _run()
    assert "json 目录枚举" in str(excinfo.value)
    assert "PermissionError" in str(excinfo.value)
    assert rec.started == []
    assert "json 目录枚举" in caplog.text and "PermissionError" in caplog.text
    assert "denied" not in caplog.text


def test_dim_config_dir_resolution_error_is_preparation_error(monkeypatch, tmp_path):
    rec = _install(monkeypatch, tmp_path)

    def boom(_sid, _jid):
        raise PermissionError("denied")

    monkeypatch.setattr(service, "resolve_dim_config_output_dir", boom)
    with pytest.raises(DimConfigPreparationError) as excinfo:
        _run()
    assert "dim_config 目录解析" in str(excinfo.value)
    assert "PermissionError" in str(excinfo.value)
    assert rec.started == []


# ---- c) 会话 2 与成功路径 ----


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


def test_session2_json_recheck_failure_rejected(monkeypatch, tmp_path):
    """会话 2 锁下复查 json 不再是 success：抛配置错误、不调用 start_step（§5.2）。"""
    rec = _install(monkeypatch, tmp_path)
    calls = {"n": 0}

    def flaky_get_step_row(_session, job_id, step_name):
        if step_name == "json":
            calls["n"] += 1
            status = "success" if calls["n"] == 1 else "failed"
            return SimpleNamespace(last_status=status, updated_at=None)
        return None

    monkeypatch.setattr(service, "get_step_row", flaky_get_step_row)
    with pytest.raises(DbStepConfigError, match="JSON 产物未就绪"):
        _run()
    assert rec.started == []


def test_step_already_running_returns_failure_with_job_id(monkeypatch, tmp_path):
    rec = _install(monkeypatch, tmp_path)

    def boom(*_a, **_k):
        raise StepAlreadyRunningError("当前步骤可能已有作业正在执行")

    monkeypatch.setattr(service, "start_step", boom)
    result = _run()

    assert result["success"] is False
    assert result["job_id"] == str(JOB_ID)
    assert result["message"] == "当前步骤可能已有作业正在执行"
    assert rec.generator_kwargs == []


def test_success_path_writes_yaml_via_tmp_and_replace(monkeypatch, tmp_path):
    """成功路径（A05/A06）：清理 → 构造（output_path 为临时文件）→ 生成 →
    核验临时文件 → os.replace 落最终路径；产物权限 0644，无 manifest。"""
    rec = _install(monkeypatch, tmp_path, json_tables=("dim_a",), with_manifest=True)
    rec.dim_config_dir.mkdir(parents=True, exist_ok=True)
    (rec.dim_config_dir / "old-artifact.yaml").write_text("old", encoding="utf-8")
    # 指向现有文件的符号链接会被清理（unlink 链接本身）
    elsewhere = tmp_path / "elsewhere.yaml"
    elsewhere.write_text("x", encoding="utf-8")
    (rec.dim_config_dir / "linked-old.yaml").symlink_to(elsewhere)
    # 悬空链接与 FIFO 幸存
    (rec.dim_config_dir / "dim_tables.yaml").symlink_to(
        tmp_path / "nonexistent-target.yaml"
    )
    os.mkfifo(rec.dim_config_dir / "pipe.fifo")

    result = _run()

    assert result["success"] is True
    assert result["job_id"] == str(JOB_ID)
    assert "1 个数据库、1 个维度表" in result["message"]
    clean_idx = next(i for i, e in enumerate(rec.events) if e[0] == "clean")
    init_idx = next(i for i, e in enumerate(rec.events) if e[0] == "init")
    assert clean_idx < init_idx
    assert rec.started and rec.started[0][2] == "dim_config"
    assert rec.finished == [(JOB_ID, "dim_config", "success")]

    kwargs = rec.generator_kwargs[0]
    assert kwargs["json_dir"] == rec.json_dir
    tmp_path_used = Path(kwargs["output_path"])
    assert tmp_path_used.parent == rec.dim_config_dir
    assert tmp_path_used.name != "dim_tables.yaml"
    assert tmp_path_used.name.startswith(".dim_tables.")
    assert tmp_path_used.name.endswith(".tmp")
    # 生成输入 = 准备链筛出的普通画像列表（排除 manifest 与非常规文件）
    assert kwargs["json_files"] == [rec.json_dir / "demo.public.dim_a.json"]

    # 旧普通文件与指向现有文件的链接被清理；悬空链接被替换；FIFO 保留
    assert not (rec.dim_config_dir / "old-artifact.yaml").exists()
    assert not (rec.dim_config_dir / "linked-old.yaml").exists()
    assert elsewhere.read_text(encoding="utf-8") == "x"  # 链接目标未被触碰
    assert (rec.dim_config_dir / "pipe.fifo").exists() or os.path.lexists(
        rec.dim_config_dir / "pipe.fifo"
    )

    final = rec.dim_config_dir / "dim_tables.yaml"
    assert final.is_file() and not final.is_symlink()
    assert not (tmp_path / "nonexistent-target.yaml").exists()  # 未顺链接写目录外
    parsed = yaml.safe_load(final.read_text(encoding="utf-8"))
    assert (
        parsed["databases"]["demo"]["tables"]["public.dim_a"]["embedding_col"] is None
    )
    assert stat.S_IMODE(final.stat().st_mode) == 0o644
    assert not (rec.dim_config_dir / "manifest.json").exists()
    assert _leftover_tmp_files(rec.dim_config_dir) == []


def test_preplanted_fifo_does_not_block_generation(monkeypatch, tmp_path):
    """预置 FIFO（含旧固定名 .dim_tables.yaml.tmp）不影响 mkstemp 唯一名（A05，审核修订 4）。

    超时守护（终审 minor 1）：若实现回归为固定名写 FIFO，本用例在超时上限
    内失败，而不是永久挂起整个套件。线程用 daemon——即使真被 FIFO 卡住也
    不阻止进程退出。
    """
    import threading

    rec = _install(monkeypatch, tmp_path)
    rec.dim_config_dir.mkdir(parents=True, exist_ok=True)
    os.mkfifo(rec.dim_config_dir / ".dim_tables.yaml.tmp")
    os.mkfifo(rec.dim_config_dir / "other.fifo")

    box: dict = {}

    def target():
        try:
            box["result"] = _run()
        except Exception as exc:  # noqa: BLE001 —— 结果统一回收后断言
            box["error"] = exc

    worker = threading.Thread(target=target, daemon=True)
    worker.start()
    worker.join(timeout=10)
    assert not worker.is_alive(), "Dim Config 生成疑似在 FIFO 上阻塞"
    assert "error" not in box
    result = box["result"]

    assert result["success"] is True
    tmp_path_used = Path(rec.generator_kwargs[0]["output_path"])
    assert tmp_path_used != rec.dim_config_dir / ".dim_tables.yaml.tmp"
    assert os.path.lexists(rec.dim_config_dir / ".dim_tables.yaml.tmp")  # 未被打开/删除


# ---- d) 生成、核验与状态写回失败 ----


def test_clean_failure_marks_generation_failed(monkeypatch, tmp_path):
    rec = _install(monkeypatch, tmp_path)

    def boom(_path):
        raise OSError("denied")

    monkeypatch.setattr(service, "clean_top_level_files", boom)
    result = _run()

    assert result["success"] is False
    assert result["message"] == "Dim Config 生成失败（OSError）"
    assert rec.finished == [(JOB_ID, "dim_config", "failed")]
    assert rec.generator_kwargs == []


def test_generation_failure_marks_failed(monkeypatch, tmp_path):
    rec = _install(monkeypatch, tmp_path)

    def boom(_self):
        raise RuntimeError("write down")

    rec.on_generate = boom
    result = _run()

    assert result["success"] is False
    assert result["message"] == "Dim Config 生成失败（RuntimeError）"
    assert rec.finished == [(JOB_ID, "dim_config", "failed")]


def test_rerun_failure_does_not_restore_old_artifact(monkeypatch, tmp_path):
    """重跑失败（审核修订 5）：旧普通产物已随清理删除、不恢复，临时文件不残留。"""
    rec = _install(monkeypatch, tmp_path)
    rec.dim_config_dir.mkdir(parents=True, exist_ok=True)
    old = rec.dim_config_dir / "dim_tables.yaml"
    old.write_text("databases: {old: {}}\n", encoding="utf-8")

    def boom(_self):
        raise RuntimeError("write down")

    rec.on_generate = boom
    result = _run()

    assert result["success"] is False
    assert not old.exists()
    assert _leftover_tmp_files(rec.dim_config_dir) == []
    assert rec.finished == [(JOB_ID, "dim_config", "failed")]


def test_verify_bad_yaml_fails_and_tmp_cleaned(monkeypatch, tmp_path):
    rec = _install(monkeypatch, tmp_path)
    rec.raw_output = "databases: [not: valid"
    result = _run()

    assert result["success"] is False
    assert "无法解析" in result["message"]
    assert rec.finished == [(JOB_ID, "dim_config", "failed")]
    assert not (rec.dim_config_dir / "dim_tables.yaml").exists()
    assert _leftover_tmp_files(rec.dim_config_dir) == []


def test_verify_databases_not_mapping_fails(monkeypatch, tmp_path):
    rec = _install(monkeypatch, tmp_path)
    rec.raw_output = "- a\n- b\n"
    result = _run()

    assert result["success"] is False
    assert "databases" in result["message"]
    assert rec.finished == [(JOB_ID, "dim_config", "failed")]


def test_verify_symlink_tmp_output_fails(monkeypatch, tmp_path):
    """核验发现临时产物被换成符号链接 → failed，且不替换最终路径（A06）。"""
    rec = _install(monkeypatch, tmp_path)
    outside = tmp_path / "outside.yaml"
    outside.write_text(
        yaml.safe_dump({"databases": {"demo": {"tables": {}}}}), encoding="utf-8"
    )

    def on_generate(gen):
        gen.output_path.unlink(missing_ok=True)
        gen.output_path.symlink_to(outside)

    rec.on_generate = on_generate
    result = _run()

    assert result["success"] is False
    assert "普通文件" in result["message"]
    assert not (rec.dim_config_dir / "dim_tables.yaml").exists()


def test_zero_dim_tables_is_success(monkeypatch, tmp_path):
    """零 dim 表（databases: {}）→ success，消息含 0 计数（A06、§13.4）。"""
    rec = _install(monkeypatch, tmp_path)
    rec.write_payload = {"databases": {}}
    result = _run()

    assert result["success"] is True
    assert "0 个数据库、0 个维度表" in result["message"]
    final = rec.dim_config_dir / "dim_tables.yaml"
    assert yaml.safe_load(final.read_text(encoding="utf-8")) == {"databases": {}}


def test_all_profiles_unreadable_fails(monkeypatch, tmp_path):
    """可读性底线（审核修订 7）：json_files 全部无法解析 → failed，
    清理临时文件、不替换最终路径，区别于"有画像但无维表"的零 dim 成功。"""
    rec = _install(monkeypatch, tmp_path)
    rec.readable_count = 0
    result = _run()

    assert result["success"] is False
    assert "无法解析" in result["message"]
    assert rec.finished == [(JOB_ID, "dim_config", "failed")]
    assert not (rec.dim_config_dir / "dim_tables.yaml").exists()
    assert _leftover_tmp_files(rec.dim_config_dir) == []


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


def test_tmp_cleanup_failure_after_generate_error_still_marks_failed(
    monkeypatch, tmp_path
):
    """生成异常后的临时文件清理自身失败：不得越过 finish_step 留在 running（终审 minor 2）。"""

    def boom_unlink(self, missing_ok=False):
        raise OSError("unlink denied")

    monkeypatch.setattr(Path, "unlink", boom_unlink)
    rec = _install(monkeypatch, tmp_path)

    def boom(_self):
        raise RuntimeError("write down")

    rec.on_generate = boom
    result = _run()

    assert result["success"] is False
    assert result["message"] == "Dim Config 生成失败（RuntimeError）"
    assert rec.finished == [(JOB_ID, "dim_config", "failed")]


def test_tmp_cleanup_failure_after_verify_error_still_marks_failed(
    monkeypatch, tmp_path
):
    """核验失败后的临时文件清理自身失败：同样记 failed 而不是抛出（终审 minor 2）。"""

    def boom_unlink(self, missing_ok=False):
        raise OSError("unlink denied")

    monkeypatch.setattr(Path, "unlink", boom_unlink)
    rec = _install(monkeypatch, tmp_path)
    rec.raw_output = "databases: [not: valid"

    result = _run()

    assert result["success"] is False
    assert "无法解析" in result["message"]
    assert rec.finished == [(JOB_ID, "dim_config", "failed")]
