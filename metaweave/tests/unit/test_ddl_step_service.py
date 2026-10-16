"""受管 DDL 采集：快照覆盖、目录边界、成功条件核对与状态写回（doc 16 §4）。"""

import copy
import json
import shutil
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import UUID

import pytest
import yaml
from click.testing import CliRunner

from backend.data_sources import ddl_step_service as service
from backend.data_sources.db_step_service import (
    DbStepConfigError,
    SourceDisabledError,
)
from backend.jobs.repository import (
    SourceRegistrationMissingError,
    StepAlreadyRunningError,
    WorkspaceMissingError,
)
from metaweave.cli.metadata_cli import metadata_command
from metaweave.core.clean_targets import resolve_ddl_output_dir
from metaweave.core.metadata.models import GenerationResult
from metaweave.utils.file_utils import clean_top_level_files

SOURCE_ID = UUID("b9a4943d-44d6-4f35-b553-d25207f9d8c1")
OTHER_SOURCE_ID = UUID("adb15693-2dda-4e4d-8ca7-ad5164c5e85d")
JOB_ID = UUID("550e8400-e29b-41d4-a716-446655440000")

MAIN_CONFIG = {
    "database": {
        "host": "cfg-host",
        "port": 9999,
        "database": "cfgdb",
        "user": "cfguser",
        "password": "cfg-pass",
        "schemas": ["cfgschema"],
    },
    "output": {
        "output_dir": "output",
        "ddl_directory": "output/ddl",
        "markdown_directory": "output/md",
        "json_directory": "output/json",
    },
    "ddl_generation": {"comments": {"llm_enabled": False}},
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
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def _install(monkeypatch, tmp_path, source, **overrides):
    """把服务的外部依赖全部换成记录型假实现，返回记录器。

    clean 用真实实现（验证目录边界与清理语义）；resolver 用真实实现
    （项目根指向 tmp_path）。
    """
    rec = SimpleNamespace(
        source=source,
        started=[],
        finished=[],
        cleaned=[],
        owner_checks=[],
        generator_configs=[],
        generate_calls=[],
        start_error=overrides.pop("start_error", None),
        finish_error=overrides.pop("finish_error", None),
        owner_error=overrides.pop("owner_error", None),
        generate_error=overrides.pop("generate_error", None),
        gen_success=overrides.pop("gen_success", True),
        gen_failed=overrides.pop("gen_failed", 0),
        gen_processed=overrides.pop("gen_processed", 2),
        gen_files=overrides.pop(
            "gen_files", ["demo.public.a.sql", "demo.public.b.sql"]
        ),
        gen_missing=overrides.pop("gen_missing", ()),
    )
    assert not overrides, overrides
    monkeypatch.setattr("metaweave.utils.file_utils.get_project_root", lambda: tmp_path)
    monkeypatch.setattr(
        service,
        "load_main_config_snapshot",
        lambda _path: copy.deepcopy(MAIN_CONFIG),
    )
    monkeypatch.setattr(service, "load_platform_settings_from_env", lambda: object())

    @contextmanager
    def fake_session(_settings):
        yield object()

    monkeypatch.setattr(service, "platform_session", fake_session)
    monkeypatch.setattr(service, "get_db_source", lambda _session, _sid: rec.source)
    monkeypatch.setattr(service, "decrypt_password", lambda _cipher: "secret")

    def fake_owner(_session, job_id, source_id):
        rec.owner_checks.append((job_id, source_id))
        if rec.owner_error is not None:
            raise rec.owner_error

    monkeypatch.setattr(service, "assert_workspace_owner", fake_owner)

    def fake_start(_session, job_id, source_id, step_name):
        rec.started.append((job_id, source_id, step_name))
        if rec.start_error is not None:
            raise rec.start_error

    def fake_finish(_session, job_id, step_name, status):
        rec.finished.append((job_id, step_name, status))
        if rec.finish_error is not None:
            raise rec.finish_error

    monkeypatch.setattr(service, "start_step", fake_start)
    monkeypatch.setattr(service, "finish_step", fake_finish)

    def recorded_clean(directory):
        rec.cleaned.append(Path(directory))
        return clean_top_level_files(directory)

    monkeypatch.setattr(service, "clean_top_level_files", recorded_clean)

    class FakeGenerator:
        def __init__(self, config_path, config=None):
            rec.generator_configs.append(config)

        def generate(
            self,
            schemas=None,
            tables=None,
            incremental=False,
            max_workers=4,
            step="ddl",
        ):
            rec.generate_calls.append((step, max_workers, schemas, tables))
            if rec.generate_error is not None:
                raise rec.generate_error
            ddl_dir = Path(rec.generator_configs[-1]["output"]["ddl_directory"])
            # 替身真实落盘：verify_ddl_collection 以磁盘为准核对声称的输出
            # （doc 16 §4"实际输出数"）。目录外路径只服务归属检查用例，
            # gen_missing 模拟 save_text 失败被忽略——声称输出但未写盘。
            ddl_dir.mkdir(parents=True, exist_ok=True)
            for name in rec.gen_files:
                candidate = ddl_dir / name
                if candidate.parent != ddl_dir or name in rec.gen_missing:
                    continue
                candidate.write_text("-- generated stub\n", encoding="utf-8")
            return SimpleNamespace(
                success=rec.gen_success,
                failed_tables=rec.gen_failed,
                processed_tables=rec.gen_processed,
                output_files=[str(ddl_dir / name) for name in rec.gen_files],
            )

    monkeypatch.setattr(service, "MetadataGenerator", FakeGenerator)

    class _ConfigStub:
        @staticmethod
        def from_config(_config):
            return None

    monkeypatch.setattr(service, "MetadataGenerationConfig", _ConfigStub)
    return rec


# ---------- 快照与成功路径 ----------


def test_success_builds_workspace_snapshot_and_reports(monkeypatch, tmp_path):
    rec = _install(monkeypatch, tmp_path, _source())
    before = copy.deepcopy(MAIN_CONFIG)
    result = service.run_ddl_step("cfg.yaml", SOURCE_ID, job_id=JOB_ID)
    assert result["success"] is True
    assert result["job_id"] == str(JOB_ID)
    assert "2 个对象" in result["message"]
    assert MAIN_CONFIG == before  # 快照只改内存副本，不动画议配置
    workspace = tmp_path / "output" / str(SOURCE_ID) / str(JOB_ID)
    (snapshot,) = rec.generator_configs
    assert snapshot["database"]["host"] == "localhost"
    assert snapshot["database"]["port"] == 5432
    assert snapshot["database"]["database"] == "demo"
    assert snapshot["database"]["user"] == "reader"
    assert snapshot["database"]["password"] == "secret"
    assert snapshot["database"]["schemas"] == ["public"]
    # 六个输出键全部指进同一 job 工作区（doc 16 §4：不碰全局目录；
    # cql_directory 为 doc 23.2 §5.1 第 6 键，发布生成 cql 用）。
    # 生成器是录制替身、不真落盘，验收 #4 的"全局 output/{ddl,md,json}
    # 不被创建/读取/覆盖"由此快照断言代理覆盖；若生成链路改造，需补
    # 真实 Formatter 运行后的全局目录断言。
    assert snapshot["output"] == {
        "output_dir": str(workspace),
        "ddl_directory": str(workspace / "ddl"),
        "markdown_directory": str(workspace / "md"),
        "json_directory": str(workspace / "json"),
        "rel_directory": str(workspace / "rel"),
        "cql_directory": str(workspace / "cql"),
    }
    assert rec.generate_calls == [("ddl", 4, None, None)]
    assert rec.started == [(JOB_ID, SOURCE_ID, "ddl")]
    assert rec.finished == [(JOB_ID, "ddl", "success")]
    assert rec.cleaned == [workspace / "ddl"]
    # 采集成功写文件清单：发布据此核对缺件（doc 16 §4/§5）
    manifest = json.loads(
        (workspace / "ddl" / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest == {"files": ["demo.public.a.sql", "demo.public.b.sql"]}


def test_manifest_write_failure_fails_the_step(monkeypatch, tmp_path):
    """清单写不进去：本次产物不可发布，不能标 success。"""
    rec = _install(monkeypatch, tmp_path, _source())

    def broken_manifest(*_args):
        raise OSError("disk full")

    monkeypatch.setattr(service, "_write_ddl_manifest", broken_manifest)
    result = service.run_ddl_step("cfg.yaml", SOURCE_ID, job_id=JOB_ID)
    assert result["success"] is False
    assert result["message"] == "DDL 采集清单写入失败"
    assert rec.finished == [(JOB_ID, "ddl", "failed")]


def test_new_workspace_created_when_job_id_omitted(monkeypatch, tmp_path):
    rec = _install(monkeypatch, tmp_path, _source())
    result = service.run_ddl_step("cfg.yaml", SOURCE_ID)
    assert result["success"] is True
    new_job = UUID(result["job_id"])
    (job_id, _source_id, step_name) = rec.started[0]
    assert job_id == new_job and step_name == "ddl"
    workspace = tmp_path / "output" / str(SOURCE_ID) / str(new_job)
    assert rec.generator_configs[0]["output"]["ddl_directory"] == str(workspace / "ddl")


def test_clean_only_removes_top_level_of_this_jobs_ddl_dir(monkeypatch, tmp_path):
    _install(monkeypatch, tmp_path, _source())
    workspace = tmp_path / "output" / str(SOURCE_ID) / str(JOB_ID)
    ddl_dir = workspace / "ddl"
    ddl_dir.mkdir(parents=True)
    (ddl_dir / "demo.public.stale.sql").write_text("old", encoding="utf-8")
    (ddl_dir / "subdir").mkdir()
    (ddl_dir / "subdir" / "keep.txt").write_text("keep", encoding="utf-8")
    sibling = tmp_path / "output" / str(SOURCE_ID) / "other-job" / "ddl"
    sibling.mkdir(parents=True)
    (sibling / "keep.sql").write_text("keep", encoding="utf-8")

    result = service.run_ddl_step("cfg.yaml", SOURCE_ID, job_id=JOB_ID)
    assert result["success"] is True
    assert not (ddl_dir / "demo.public.stale.sql").exists()
    assert (ddl_dir / "subdir" / "keep.txt").exists()
    assert (sibling / "keep.sql").exists()


# ---------- 成功条件核对 ----------


def test_zero_objects_fails_with_explicit_reason(monkeypatch, tmp_path):
    """选中范围无对象：有意判 failed 且说明原因（doc 16 §4）。"""
    rec = _install(monkeypatch, tmp_path, _source(), gen_processed=0, gen_files=[])
    result = service.run_ddl_step("cfg.yaml", SOURCE_ID, job_id=JOB_ID)
    assert result["success"] is False
    assert result["message"] == "选中范围内没有可采集对象"
    assert result["job_id"] == str(JOB_ID)
    assert rec.finished == [(JOB_ID, "ddl", "failed")]


@pytest.mark.parametrize(
    ("overrides", "fragment"),
    [
        (dict(gen_success=False), "未全部成功"),
        (dict(gen_failed=1), "1 个对象采集失败"),
        (dict(gen_files=["demo.public.a.sql"]), "输出文件数与成功对象数不一致"),
        # 两个对象声称同一输出路径：磁盘只有一份文件，重复路径不能冒充数量
        (
            dict(gen_files=["demo.public.a.sql", "demo.public.a.sql"]),
            "输出文件数与成功对象数不一致",
        ),
        (
            dict(gen_files=["demo.public.a.sql", "../escape.sql"]),
            "不在本次受管 DDL 目录内",
        ),
    ],
)
def test_incomplete_results_are_not_marked_success(
    monkeypatch, tmp_path, overrides, fragment
):
    rec = _install(monkeypatch, tmp_path, _source(), **overrides)
    result = service.run_ddl_step("cfg.yaml", SOURCE_ID, job_id=JOB_ID)
    assert result["success"] is False
    assert fragment in result["message"]
    assert rec.finished == [(JOB_ID, "ddl", "failed")]


def test_claimed_but_missing_output_file_fails_verification(monkeypatch, tmp_path):
    """生成器声称输出但未落盘（如 save_text 失败被忽略）：核对以磁盘为准。"""
    rec = _install(
        monkeypatch,
        tmp_path,
        _source(),
        gen_missing=("demo.public.b.sql",),
    )
    result = service.run_ddl_step("cfg.yaml", SOURCE_ID, job_id=JOB_ID)
    assert result["success"] is False
    assert "未实际落盘" in result["message"]
    assert rec.finished == [(JOB_ID, "ddl", "failed")]


def test_verify_rejects_extra_sql_file_in_managed_dir(tmp_path):
    """目录混入清单外的 .sql：集合不一致判失败（发布读取时 glob 本目录）。"""
    ddl_dir = tmp_path / "ddl"
    ddl_dir.mkdir()
    listed = ddl_dir / "demo.public.a.sql"
    listed.write_text("-- x\n", encoding="utf-8")
    (ddl_dir / "demo.public.extra.sql").write_text("-- x\n", encoding="utf-8")
    result = SimpleNamespace(
        success=True,
        failed_tables=0,
        processed_tables=1,
        output_files=[str(listed)],
    )
    assert service.verify_ddl_collection(result, ddl_dir) == (
        False,
        "受管 DDL 目录的实际输出文件与清单不一致",
    )


# ---------- 准备链拒绝（登记作业之前） ----------


@pytest.mark.parametrize(
    ("source_kwargs", "owner_error", "exc_type", "fragment"),
    [
        (dict(source=None), None, DbStepConfigError, "未找到指定 source_id"),
        (dict(db_type="mysql"), None, DbStepConfigError, "仅支持 PostgreSQL"),
        (dict(username=""), None, DbStepConfigError, "必要的连接字段"),
        (dict(is_active=False), None, SourceDisabledError, "已禁用"),
        (
            dict(),
            WorkspaceMissingError(f"指定的 job_id 不存在: {JOB_ID}"),
            DbStepConfigError,
            "job_id 不存在",
        ),
    ],
)
def test_prepare_chain_rejections_write_nothing(
    monkeypatch, tmp_path, source_kwargs, owner_error, exc_type, fragment
):
    if "source" in source_kwargs and source_kwargs["source"] is None:
        source = None
    else:
        source = _source(**{k: v for k, v in source_kwargs.items() if k != "source"})
    rec = _install(monkeypatch, tmp_path, source, owner_error=owner_error)
    with pytest.raises(exc_type, match=fragment):
        service.run_ddl_step("cfg.yaml", SOURCE_ID, job_id=JOB_ID)
    assert rec.started == [] and rec.finished == []
    assert rec.generate_calls == [] and rec.cleaned == []


def test_invalid_generation_config_fails_before_registration(monkeypatch, tmp_path):
    rec = _install(monkeypatch, tmp_path, _source())

    class _BrokenConfig:
        @staticmethod
        def from_config(_config):
            raise ValueError("ddl_generation.comments 配置非法")

    monkeypatch.setattr(service, "MetadataGenerationConfig", _BrokenConfig)
    with pytest.raises(DbStepConfigError, match="配置非法"):
        service.run_ddl_step("cfg.yaml", SOURCE_ID, job_id=JOB_ID)
    assert rec.started == [] and rec.generate_calls == []


def test_bad_arguments_are_config_errors(monkeypatch, tmp_path):
    _install(monkeypatch, tmp_path, _source())
    with pytest.raises(DbStepConfigError, match="UUID"):
        service.run_ddl_step("cfg.yaml", "not-a-uuid")
    with pytest.raises(DbStepConfigError, match="job-id"):
        service.run_ddl_step("cfg.yaml", SOURCE_ID, job_id="not-a-uuid")
    for bad in (0, -1, True):
        with pytest.raises(DbStepConfigError, match="max_workers"):
            service.run_ddl_step("cfg.yaml", SOURCE_ID, max_workers=bad)


def test_explicit_max_workers_passes_through(monkeypatch, tmp_path):
    rec = _install(monkeypatch, tmp_path, _source())
    result = service.run_ddl_step("cfg.yaml", SOURCE_ID, job_id=JOB_ID, max_workers=6)
    assert result["success"] is True
    assert rec.generate_calls == [("ddl", 6, None, None)]


# ---------- 运行期失败与状态写回 ----------


def test_generator_exception_marks_step_failed(monkeypatch, tmp_path):
    rec = _install(
        monkeypatch, tmp_path, _source(), generate_error=RuntimeError("boom")
    )
    result = service.run_ddl_step("cfg.yaml", SOURCE_ID, job_id=JOB_ID)
    assert result["success"] is False
    assert "DDL 采集失败（RuntimeError）" in result["message"]
    assert "boom" not in result["message"]  # 不回显异常原文
    assert result["job_id"] == str(JOB_ID)
    assert rec.finished == [(JOB_ID, "ddl", "failed")]


def test_start_step_missing_registration_maps_to_config_error(monkeypatch, tmp_path):
    """删除先提交：start_step 查不到登记 → 报数据源不存在、不采集（验收 9）。"""
    rec = _install(
        monkeypatch,
        tmp_path,
        _source(),
        start_error=SourceRegistrationMissingError("数据源登记已不存在"),
    )
    with pytest.raises(DbStepConfigError, match="未找到指定 source_id"):
        service.run_ddl_step("cfg.yaml", SOURCE_ID, job_id=JOB_ID)
    assert rec.generate_calls == [] and rec.cleaned == [] and rec.finished == []


def test_step_already_running_is_task_failure(monkeypatch, tmp_path):
    rec = _install(
        monkeypatch,
        tmp_path,
        _source(),
        start_error=StepAlreadyRunningError("当前步骤可能已有作业正在执行"),
    )
    result = service.run_ddl_step("cfg.yaml", SOURCE_ID, job_id=JOB_ID)
    assert result["success"] is False
    assert "已有作业正在执行" in result["message"]
    assert result["job_id"] == str(JOB_ID)
    assert rec.generate_calls == [] and rec.cleaned == []


def test_state_registration_failure_skips_collection(monkeypatch, tmp_path):
    rec = _install(
        monkeypatch, tmp_path, _source(), start_error=RuntimeError("db down")
    )
    result = service.run_ddl_step("cfg.yaml", SOURCE_ID)
    assert result["success"] is False
    assert "作业状态登记失败（RuntimeError）" in result["message"]
    assert result["job_id"]
    assert rec.generate_calls == [] and rec.cleaned == []


def test_finish_failure_hides_artifact_directory(monkeypatch, tmp_path, caplog):
    _install(
        monkeypatch, tmp_path, _source(), finish_error=RuntimeError("write back 失败")
    )
    result = service.run_ddl_step("cfg.yaml", SOURCE_ID, job_id=JOB_ID)
    assert result["success"] is False
    assert "作业状态更新失败" in result["message"]
    workspace = tmp_path / "output" / str(SOURCE_ID) / str(JOB_ID)
    # 产物目录只进日志，API 回显的消息不含路径（受控路径不出错误消息）
    assert str(workspace / "ddl") not in result["message"]
    assert str(JOB_ID) in result["message"]
    assert str(workspace / "ddl") in caplog.text


# ---------- 目录解析边界 ----------


def test_resolve_ddl_output_dir_returns_guarded_path(monkeypatch, tmp_path):
    monkeypatch.setattr("metaweave.utils.file_utils.get_project_root", lambda: tmp_path)
    resolved = resolve_ddl_output_dir(SOURCE_ID, JOB_ID)
    expected = tmp_path / "output" / str(SOURCE_ID) / str(JOB_ID) / "ddl"
    assert resolved == expected
    assert resolved.is_absolute()


@pytest.mark.parametrize("linked_level", ["output", "source", "job", "ddl"])
def test_resolver_rejects_in_project_symlink(monkeypatch, tmp_path, linked_level):
    monkeypatch.setattr("metaweave.utils.file_utils.get_project_root", lambda: tmp_path)
    expected = tmp_path / "output" / str(SOURCE_ID) / str(JOB_ID) / "ddl"
    other = tmp_path / "other" / "ddl"
    other.mkdir(parents=True)
    keep = other / "keep.txt"
    keep.write_text("keep", encoding="utf-8")
    if linked_level == "output":
        (tmp_path / "output").symlink_to(tmp_path / "other", target_is_directory=True)
    elif linked_level == "source":
        expected.parents[2].mkdir()
        expected.parents[1].symlink_to(tmp_path / "other", target_is_directory=True)
    elif linked_level == "job":
        expected.parents[1].mkdir(parents=True)
        expected.parent.symlink_to(tmp_path / "other", target_is_directory=True)
    else:
        expected.parent.mkdir(parents=True)
        expected.symlink_to(other, target_is_directory=True)
    with pytest.raises(ValueError, match="符号链接"):
        resolve_ddl_output_dir(SOURCE_ID, JOB_ID)
    assert keep.read_text(encoding="utf-8") == "keep"


# ---------- CLI 受管组合 ----------


def _cli_env(monkeypatch, tmp_path, *, run_result=None, run_error=None, strict=True):
    from backend.data_sources import ddl_step_service

    cfg = tmp_path / "config.yaml"
    cfg.write_text("database: {}\n", encoding="utf-8")
    if run_result is None:
        run_result = {"success": True, "execution_time": 0, "message": "不应执行"}
    run_ddl_step = Mock(return_value=run_result)
    if run_error is not None:
        run_ddl_step = Mock(side_effect=run_error)
    monkeypatch.setattr(ddl_step_service, "run_ddl_step", run_ddl_step)
    if strict:
        monkeypatch.setattr(
            ddl_step_service,
            "load_platform_settings_from_env",
            Mock(side_effect=AssertionError("不应读取平台库配置")),
        )
    return cfg, run_ddl_step


@pytest.mark.parametrize(
    "extra",
    [
        ["--schemas", "public"],
        ["--tables", "users"],
        ["--clean"],
    ],
)
def test_cli_managed_ddl_rejects_scope_params_before_side_effects(
    tmp_path, monkeypatch, extra
):
    """受管 CLI 显式 --schemas/--tables/--clean：exit 2 且不触发服务（验收 4）。"""
    cfg, run_ddl_step = _cli_env(monkeypatch, tmp_path)
    result = CliRunner().invoke(
        metadata_command,
        ["-c", str(cfg), "--step", "ddl", "--source-id", str(SOURCE_ID), *extra],
    )
    assert result.exit_code == 2, result.output
    assert "不接受" in result.output
    run_ddl_step.assert_not_called()


@pytest.mark.parametrize(
    ("args", "fragment"),
    [
        (["--step", "ddl", "--job-id", str(JOB_ID)], "--job-id 仅在受管模式"),
        (["--step", "json", "--source-id", str(SOURCE_ID)], "--source-id 仅适用于"),
        (["--step", "md", "--source-id", str(SOURCE_ID)], "--source-id 仅适用于"),
        (
            ["--step", "ddl", "--source-id", str(SOURCE_ID), "--generate-domains"],
            "不能与 --generate-*",
        ),
    ],
)
def test_cli_rejects_invalid_ddl_combinations(tmp_path, monkeypatch, args, fragment):
    cfg, run_ddl_step = _cli_env(monkeypatch, tmp_path)
    result = CliRunner().invoke(metadata_command, ["-c", str(cfg), *args])
    assert result.exit_code == 2, result.output
    assert fragment in result.output
    run_ddl_step.assert_not_called()


def test_cli_managed_ddl_dispatch_and_result_mapping(tmp_path, monkeypatch):
    cfg, _unused = _cli_env(monkeypatch, tmp_path, strict=False)
    from backend.data_sources import ddl_step_service

    seen = []
    monkeypatch.setattr(
        ddl_step_service,
        "run_ddl_step",
        lambda path, source_id, job_id=None, max_workers=None: (
            seen.append((path, source_id, job_id, max_workers))
            or {
                "success": True,
                "execution_time": 0,
                "message": "受管 DDL 采集完成（3 个对象）",
                "job_id": str(JOB_ID),
            }
        ),
    )
    result = CliRunner().invoke(
        metadata_command,
        [
            "-c",
            str(cfg),
            "--step",
            "ddl",
            "--source-id",
            str(SOURCE_ID).upper(),
            "--max-workers",
            "7",
        ],
    )
    assert result.exit_code == 0, result.output
    (path, source_id, job_id, max_workers), *_ = [
        (s[0], s[1], s[2], s[3]) for s in seen
    ]
    assert source_id == SOURCE_ID and job_id is None and max_workers == 7
    assert Path(path) == cfg  # resolve_project_path：绝对路径原样透传
    assert str(JOB_ID) in result.output and "3 个对象" in result.output

    reused = CliRunner().invoke(
        metadata_command,
        [
            "-c",
            str(cfg),
            "--step",
            "ddl",
            "--source-id",
            str(SOURCE_ID),
            "--job-id",
            str(JOB_ID).upper(),
        ],
    )
    assert reused.exit_code == 0, reused.output
    assert seen[-1][1] == SOURCE_ID and seen[-1][2] == JOB_ID

    monkeypatch.setattr(
        ddl_step_service,
        "run_ddl_step",
        lambda *_a, **_kw: {
            "success": False,
            "execution_time": 0,
            "message": "选中范围内没有可采集对象",
            "job_id": str(JOB_ID),
        },
    )
    failed = CliRunner().invoke(
        metadata_command,
        ["-c", str(cfg), "--step", "ddl", "--source-id", str(SOURCE_ID)],
    )
    assert failed.exit_code == 1
    assert "没有可采集对象" in failed.output
    assert str(JOB_ID) in failed.output

    def bad_config(*_a, **_kw):
        raise DbStepConfigError("未找到指定 source_id 的数据源")

    monkeypatch.setattr(ddl_step_service, "run_ddl_step", bad_config)
    invalid = CliRunner().invoke(
        metadata_command,
        ["-c", str(cfg), "--step", "ddl", "--source-id", str(SOURCE_ID)],
    )
    assert invalid.exit_code == 2
    assert "未找到指定 source_id" in invalid.output


def test_cli_unmanaged_ddl_keeps_generic_path(tmp_path, monkeypatch):
    """未带 --source-id 的 ddl 继续走原通用路径，不进受管分支（验收 4）。"""
    out_dir = (
        Path.cwd().resolve() / "tests" / ".tmp" / f"ddl_{tmp_path.name}"
    ).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        yaml.safe_dump(
            {
                "database": {"database": "test_db", "schemas": ["public"]},
                "output": {
                    "output_dir": str(out_dir.relative_to(Path.cwd().resolve())),
                },
                "ddl_generation": {"comments": {"llm_enabled": False}},
                "json_generation": {
                    "comments": {"llm_enabled": False},
                    "table_classification": {"llm_enabled": False},
                },
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    from backend.data_sources import ddl_step_service

    run_ddl_step = Mock()
    monkeypatch.setattr(ddl_step_service, "run_ddl_step", run_ddl_step)

    class _DummyGenerator:
        def __init__(self, config_path, config=None):
            pass

        def generate(self, **_kwargs):
            # execute_ddl_json_md 会读取 GenerationResult 的全部统计字段，
            # 直接复用真实 dataclass（全字段有默认值）
            return GenerationResult(
                success=True,
                processed_tables=1,
                output_files=[str(out_dir / "ddl" / "test_db.public.t.sql")],
            )

    monkeypatch.setattr(
        "metaweave.core.metadata.generator.MetadataGenerator", _DummyGenerator
    )
    try:
        result = CliRunner().invoke(metadata_command, ["-c", str(cfg), "--step", "ddl"])
        assert result.exit_code == 0, result.output
        run_ddl_step.assert_not_called()
        assert "受管" not in result.output
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


# --- doc 18.1 §5.2：受管 DDL 快照覆盖登记源 sslmode（含 None 显式覆盖）------


def test_snapshot_copies_registered_sslmode(monkeypatch, tmp_path):
    """登记源 sslmode=require 时，快照把它交给执行层（经 DatabaseConnector）。"""
    rec = _install(monkeypatch, tmp_path, _source(sslmode="require"))
    assert service.run_ddl_step("cfg.yaml", SOURCE_ID, job_id=JOB_ID)["success"]
    (snapshot,) = rec.generator_configs
    assert snapshot["database"]["sslmode"] == "require"


def test_snapshot_none_sslmode_overrides_legacy_config_value(monkeypatch, tmp_path):
    """登记值为 None 时显式覆盖主配置遗留的 database.sslmode，不继承。"""
    monkeypatch.setattr("metaweave.utils.file_utils.get_project_root", lambda: tmp_path)
    main_config = copy.deepcopy(MAIN_CONFIG)
    main_config["database"]["sslmode"] = "prefer"
    snapshot = service.build_managed_ddl_config(
        main_config, _source(sslmode=None), "secret", JOB_ID
    )
    assert snapshot["database"]["sslmode"] is None
    # 主配置副本不被改动
    assert main_config["database"]["sslmode"] == "prefer"


def test_managed_snapshot_reaches_connector_params(monkeypatch, tmp_path):
    """连接器测试替身核对最终参数（doc 18.1 §5.2）。

    登记源 require 覆盖主配置其他值；登记源 None 而主配置有值时省略
    该参数——两条都断言快照经 DatabaseConnector 后的 conninfo。
    """
    from psycopg.conninfo import conninfo_to_dict

    from metaweave.core.metadata import connector as connector_module
    from metaweave.core.metadata.connector import DatabaseConnector

    captured = {}

    def fake_pool(conninfo, **_kwargs):
        captured["conninfo"] = conninfo
        return object()

    monkeypatch.setattr("metaweave.utils.file_utils.get_project_root", lambda: tmp_path)
    monkeypatch.setattr(connector_module, "ConnectionPool", fake_pool)

    main_config = copy.deepcopy(MAIN_CONFIG)
    main_config["database"]["sslmode"] = "prefer"

    require_snapshot = service.build_managed_ddl_config(
        main_config, _source(sslmode=" require "), "secret", JOB_ID
    )
    DatabaseConnector(require_snapshot["database"])
    assert conninfo_to_dict(captured["conninfo"])["sslmode"] == "require"

    none_snapshot = service.build_managed_ddl_config(
        main_config, _source(sslmode=None), "secret", JOB_ID
    )
    DatabaseConnector(none_snapshot["database"])
    assert "sslmode" not in conninfo_to_dict(captured["conninfo"])


# --- doc 19.1 §3：无凭据公共工作区快照 ---------------------------------------


def test_build_managed_output_snapshot_is_credential_free(monkeypatch, tmp_path):
    """公共快照：输出六键指向同一工作区，连接字段显式置 None，不接收
    密文或解密密码（doc 19.1 §3；cql_directory 为 doc 23.2 §5.1 第 6 键）。"""
    monkeypatch.setattr("metaweave.utils.file_utils.get_project_root", lambda: tmp_path)
    job_uuid = JOB_ID
    snapshot = service.build_managed_output_snapshot(
        MAIN_CONFIG, SOURCE_ID, job_uuid, "orders", ["public", "sales"]
    )
    workspace = tmp_path / "output" / str(SOURCE_ID) / str(job_uuid)
    assert snapshot["output"] == {
        "output_dir": str(workspace),
        "ddl_directory": str(workspace / "ddl"),
        "json_directory": str(workspace / "json"),
        "markdown_directory": str(workspace / "md"),
        "rel_directory": str(workspace / "rel"),
        "cql_directory": str(workspace / "cql"),
    }
    assert snapshot["database"]["database"] == "orders"
    assert snapshot["database"]["schemas"] == ["public", "sales"]
    for key in ("host", "port", "user", "password", "sslmode"):
        assert snapshot["database"][key] is None
    # 主配置副本不被改动
    assert MAIN_CONFIG["database"]["host"] == "cfg-host"


@pytest.mark.parametrize(
    "node",
    [
        {"database": 5},
        {"database": ["ab"]},
        {"output": 5},
        {"output": ["ab"]},
    ],
)
def test_snapshot_rejects_non_object_sections(monkeypatch, tmp_path, node):
    """database/output 非对象（含 dict() 静默改写的列表与 TypeError 的整数）
    在快照构造即拒绝（doc 19.1 §8 验收 2）。"""
    monkeypatch.setattr("metaweave.utils.file_utils.get_project_root", lambda: tmp_path)
    bad_config = copy.deepcopy(MAIN_CONFIG)
    bad_config.update(node)
    with pytest.raises(ValueError, match="必须是对象"):
        service.build_managed_output_snapshot(
            bad_config, SOURCE_ID, JOB_ID, "demo", ["public"]
        )


def test_snapshot_accepts_missing_or_null_sections(monkeypatch, tmp_path):
    """缺省与 null 视为空对象（doc 19.1 §4.5），不收紧空值行为。"""
    monkeypatch.setattr("metaweave.utils.file_utils.get_project_root", lambda: tmp_path)
    for updates in ({"database": None, "output": None}, {}):
        blank = copy.deepcopy(MAIN_CONFIG)
        blank.update(updates)
        snapshot = service.build_managed_output_snapshot(
            blank, SOURCE_ID, JOB_ID, "demo", ["public"]
        )
        assert snapshot["database"]["database"] == "demo"
        assert snapshot["output"]["ddl_directory"].endswith("/ddl")


def test_build_managed_ddl_config_still_adds_credentials(monkeypatch, tmp_path):
    """原函数在公共快照之上追加登记源连接字段，DDL/JSON 行为不变。"""
    monkeypatch.setattr("metaweave.utils.file_utils.get_project_root", lambda: tmp_path)
    snapshot = service.build_managed_ddl_config(
        MAIN_CONFIG, _source(), "secret", JOB_ID
    )
    assert snapshot["database"]["host"] == "localhost"
    assert snapshot["database"]["user"] == "reader"
    assert snapshot["database"]["password"] == "secret"
    assert snapshot["database"]["database"] == "demo"


def test_prep_rejects_non_list_exclude_tables_before_clean(monkeypatch, tmp_path):
    """三链口径一致（doc 16 侧小改）：DDL 准备链同样在清理前拒绝非法
    exclude_tables，不再等到生成期以 TypeError 失败。"""
    import pytest as _pytest

    from backend.data_sources.db_step_service import DbStepConfigError

    rec = _install(monkeypatch, tmp_path, _source())
    config = copy.deepcopy(MAIN_CONFIG)
    config["database"]["exclude_tables"] = None  # YAML 空节点
    monkeypatch.setattr(service, "load_main_config_snapshot", lambda _p: config)

    with _pytest.raises(DbStepConfigError, match="exclude_tables 必须是列表"):
        service.run_ddl_step("cfg.yaml", str(SOURCE_ID), job_id=JOB_ID)
    assert rec.started == []
