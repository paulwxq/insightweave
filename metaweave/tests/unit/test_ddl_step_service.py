"""受管 DDL 步骤 worker（doc 16 §4、6.2 §5/§7.1）。

断代改造适配：worker 新签名 `run_ddl_step(ctx: StepContext) -> StepOutcome`；
清理语义由整目录备份（`backup_directory_before_update`）取代；快照输出键
增加 `dim_tables_directory`（dim_config/ 更名 dim/）。
"""

import copy
import json
import shutil
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest
import yaml
from click.testing import CliRunner

from backend.data_sources import ddl_step_service as service
from backend.data_sources.step_context import DbStepConfigError, StepContext
from backend.persistence.models import DbSourceInfo
from metaweave.cli.metadata_cli import metadata_command
from metaweave.core.clean_targets import resolve_ddl_output_dir

SOURCE_ID = "db_src_1"
WORKSPACE_ID = "ws_1"
RUN_ID = UUID("11111111-2222-4333-8444-555555555555")

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
        display_name="测试源",
        description=None,
        db_type="postgresql",
        host="localhost",
        port=5432,
        db_name="demo",
        username="reader",
        password_ciphertext="cipher",
        sslmode=None,
        db_version=None,
        selected_schemas=["public"],
        is_active=True,
    )
    values.update(overrides)
    return DbSourceInfo(**values)


def _install(monkeypatch, tmp_path, main_config=None, **overrides):
    """项目根指向 tmp_path；生成器换成记录型替身；备份走真实实现。"""
    from metaweave.utils import file_utils

    monkeypatch.setattr(file_utils, "get_project_root", lambda: tmp_path)
    import backend.security.source_password as crypto

    monkeypatch.setattr(crypto, "decrypt_password", lambda _cipher: "secret")

    rec = SimpleNamespace(
        generator_configs=[],
        generate_calls=[],
        gen_success=overrides.pop("gen_success", True),
        gen_failed=overrides.pop("gen_failed", 0),
        gen_processed=overrides.pop("gen_processed", 2),
        gen_files=overrides.pop(
            "gen_files", ["demo.public.a.sql", "demo.public.b.sql"]
        ),
        gen_missing=overrides.pop("gen_missing", ()),
        generate_error=overrides.pop("generate_error", None),
    )
    assert not overrides, overrides
    rec.main_config = (
        main_config if main_config is not None else copy.deepcopy(MAIN_CONFIG)
    )

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


def _ctx(tmp_path, rec, source=None):
    return StepContext(
        settings=SimpleNamespace(),
        source=source if source is not None else _source(),
        main_config=rec.main_config,
        config_path=Path("configs/metadata_config.yaml"),
        source_id=SOURCE_ID,
        workspace_id=WORKSPACE_ID,
        run_id=RUN_ID,
        step_name="ddl",
        step_params=None,
    )


def _workspace(tmp_path):
    return tmp_path / "output" / SOURCE_ID / WORKSPACE_ID


# ---------- 快照与成功路径 ----------


def test_success_builds_workspace_snapshot_and_reports(monkeypatch, tmp_path):
    rec = _install(monkeypatch, tmp_path)
    before = copy.deepcopy(MAIN_CONFIG)
    outcome = service.run_ddl_step(_ctx(tmp_path, rec))
    assert outcome.success is True
    assert "2 个对象" in outcome.message
    assert MAIN_CONFIG == before  # 快照只改内存副本，不动主配置
    workspace = _workspace(tmp_path)
    (snapshot,) = rec.generator_configs
    assert snapshot["database"]["host"] == "localhost"
    assert snapshot["database"]["port"] == 5432
    assert snapshot["database"]["database"] == "demo"
    assert snapshot["database"]["user"] == "reader"
    assert snapshot["database"]["password"] == "secret"
    assert snapshot["database"]["schemas"] == ["public"]
    # 七个输出键全部指进同一工作区（doc 16 §4 + doc 22.1 dim/ 更名 +
    # doc 23.2 cql_directory）；生成器是录制替身、不真落盘，"不碰全局
    # output/{ddl,md,json}"由本快照断言代理覆盖。
    assert snapshot["output"] == {
        "output_dir": str(workspace),
        "ddl_directory": str(workspace / "ddl"),
        "markdown_directory": str(workspace / "md"),
        "json_directory": str(workspace / "json"),
        "rel_directory": str(workspace / "rel"),
        "dim_tables_directory": str(workspace / "dim"),
        "cql_directory": str(workspace / "cql"),
    }
    assert rec.generate_calls == [("ddl", 4, None, None)]
    manifest = json.loads(
        (workspace / "ddl" / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest == {"files": ["demo.public.a.sql", "demo.public.b.sql"]}
    assert outcome.output_files == [
        "ddl/demo.public.a.sql",
        "ddl/demo.public.b.sql",
        "ddl/manifest.json",
    ]


def test_backup_moves_old_content_including_marker_before_update(monkeypatch, tmp_path):
    """重跑前整目录备份（doc 6 §6）：旧文件与旧 marker 移入 .backup/。"""
    from backend.workspace_files.markers import write_collection_marker

    rec = _install(monkeypatch, tmp_path)
    ddl_dir = _workspace(tmp_path) / "ddl"
    ddl_dir.mkdir(parents=True)
    (ddl_dir / "demo.public.stale.sql").write_text("old", encoding="utf-8")
    (ddl_dir / "subdir").mkdir()
    (ddl_dir / "subdir" / "keep.txt").write_text("keep", encoding="utf-8")
    write_collection_marker(
        ddl_dir, UUID("99999999-9999-4999-8999-999999999999"), "success"
    )
    sibling = tmp_path / "output" / SOURCE_ID / "ws_2" / "ddl"
    sibling.mkdir(parents=True)
    (sibling / "keep.sql").write_text("keep", encoding="utf-8")

    outcome = service.run_ddl_step(_ctx(tmp_path, rec))
    assert outcome.success is True
    backups = list((ddl_dir / ".backup").iterdir())
    assert len(backups) == 1
    backup = backups[0]
    assert backup.name.startswith("bak_")
    # 旧内容（含点号 marker 与子目录）整体移入备份
    assert (backup / "demo.public.stale.sql").read_text(encoding="utf-8") == "old"
    assert (backup / "subdir" / "keep.txt").read_text(encoding="utf-8") == "keep"
    assert list(backup.glob(".coll_run_id_*"))
    # 顶层只剩新产物 + 清单 + .backup
    assert {p.name for p in ddl_dir.iterdir()} == {
        ".backup",
        "demo.public.a.sql",
        "demo.public.b.sql",
        "manifest.json",
    }
    # 其他工作区不受影响
    assert (sibling / "keep.sql").exists()


def test_manifest_write_failure_fails_the_step(monkeypatch, tmp_path):
    """清单写不进去：本次产物不可发布，不能标 success。"""
    rec = _install(monkeypatch, tmp_path)

    def broken_manifest(*_args):
        raise OSError("disk full")

    monkeypatch.setattr(service, "_write_ddl_manifest", broken_manifest)
    outcome = service.run_ddl_step(_ctx(tmp_path, rec))
    assert outcome.success is False
    assert outcome.message == "DDL 采集清单写入失败"
    assert outcome.output_files == []


# ---------- 成功条件核对 ----------


def test_zero_objects_fails_with_explicit_reason(monkeypatch, tmp_path):
    """选中范围无对象：有意判 failed 且说明原因（doc 16 §4）。"""
    rec = _install(monkeypatch, tmp_path, gen_processed=0, gen_files=[])
    outcome = service.run_ddl_step(_ctx(tmp_path, rec))
    assert outcome.success is False
    assert outcome.message == "选中范围内没有可采集对象"


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
    rec = _install(monkeypatch, tmp_path, **overrides)
    outcome = service.run_ddl_step(_ctx(tmp_path, rec))
    assert outcome.success is False
    assert fragment in outcome.message


def test_claimed_but_missing_output_file_fails_verification(monkeypatch, tmp_path):
    """生成器声称输出但未落盘（如 save_text 失败被忽略）：核对以磁盘为准。"""
    rec = _install(monkeypatch, tmp_path, gen_missing=("demo.public.b.sql",))
    outcome = service.run_ddl_step(_ctx(tmp_path, rec))
    assert outcome.success is False
    assert "未实际落盘" in outcome.message


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


def test_verify_ignores_dot_entries_in_top_level_comparison(tmp_path):
    """`.backup/` 与点号 marker 不计入顶层产物比对（doc 6 §6）。"""
    ddl_dir = tmp_path / "ddl"
    ddl_dir.mkdir()
    listed = ddl_dir / "demo.public.a.sql"
    listed.write_text("-- x\n", encoding="utf-8")
    (
        ddl_dir
        / ".coll_run_id_99999999-9999-4999-8999-999999999999_20260101-000000000_success"
    ).touch()
    (ddl_dir / ".backup").mkdir()
    (ddl_dir / ".backup" / "old.sql").write_text("-- old\n", encoding="utf-8")
    result = SimpleNamespace(
        success=True,
        failed_tables=0,
        processed_tables=1,
        output_files=[str(listed)],
    )
    assert service.verify_ddl_collection(result, ddl_dir) == (True, "")


# ---------- 准备链拒绝（备份之前） ----------


def test_invalid_generation_config_fails_before_backup(monkeypatch, tmp_path):
    rec = _install(monkeypatch, tmp_path)

    class _BrokenConfig:
        @staticmethod
        def from_config(_config):
            raise ValueError("ddl_generation.comments 配置非法")

    monkeypatch.setattr(service, "MetadataGenerationConfig", _BrokenConfig)
    # 预置旧产物：构造期失败不触发备份，旧产物原地保留（R6 语义）。
    # 注：当前实现抛裸 ValueError（未包 DbStepConfigError，编排层按非受控
    # 失败计），断言两者皆可，控制点在"备份之前失败"。
    ddl_dir = _workspace(tmp_path) / "ddl"
    ddl_dir.mkdir(parents=True)
    (ddl_dir / "demo.public.old.sql").write_text("old", encoding="utf-8")
    with pytest.raises((ValueError, DbStepConfigError), match="配置非法"):
        service.run_ddl_step(_ctx(tmp_path, rec))
    assert rec.generate_calls == []
    assert (ddl_dir / "demo.public.old.sql").exists()
    assert not (ddl_dir / ".backup").exists()


def test_prep_rejects_non_list_exclude_tables_before_backup(monkeypatch, tmp_path):
    """非法 exclude_tables 在备份前拒绝（doc 16 侧口径）。"""
    rec = _install(monkeypatch, tmp_path)
    config = copy.deepcopy(MAIN_CONFIG)
    config["database"]["exclude_tables"] = None  # YAML 空节点
    rec.main_config = config
    ddl_dir = _workspace(tmp_path) / "ddl"
    ddl_dir.mkdir(parents=True)
    (ddl_dir / "demo.public.old.sql").write_text("old", encoding="utf-8")
    with pytest.raises(
        (ValueError, DbStepConfigError), match="exclude_tables 必须是列表"
    ):
        service.run_ddl_step(_ctx(tmp_path, rec))
    assert rec.generate_calls == []
    assert (ddl_dir / "demo.public.old.sql").exists()


def test_bad_max_workers_are_config_errors(monkeypatch, tmp_path):
    rec = _install(monkeypatch, tmp_path)
    for bad in (0, -1, True, "4"):
        with pytest.raises(DbStepConfigError, match="max_workers"):
            service.run_ddl_step(_ctx(tmp_path, rec), max_workers=bad)
    assert rec.generate_calls == []


def test_explicit_max_workers_passes_through(monkeypatch, tmp_path):
    rec = _install(monkeypatch, tmp_path)
    outcome = service.run_ddl_step(_ctx(tmp_path, rec), max_workers=6)
    assert outcome.success is True
    assert rec.generate_calls == [("ddl", 6, None, None)]


# ---------- 运行期失败 ----------


def test_generator_exception_marks_step_failed(monkeypatch, tmp_path):
    rec = _install(monkeypatch, tmp_path, generate_error=RuntimeError("boom"))
    ddl_dir = _workspace(tmp_path) / "ddl"
    ddl_dir.mkdir(parents=True)
    (ddl_dir / "demo.public.old.sql").write_text("old", encoding="utf-8")
    outcome = service.run_ddl_step(_ctx(tmp_path, rec))
    assert outcome.success is False
    assert "DDL 采集失败（RuntimeError）" in outcome.message
    assert "boom" not in outcome.message  # 不回显异常原文
    # 备份在生成之前完成：旧产物已移入 .backup（生成失败不恢复旧产物）
    backups = list((ddl_dir / ".backup").iterdir())
    assert len(backups) == 1
    assert (backups[0] / "demo.public.old.sql").exists()


# ---------- 目录解析边界 ----------


def test_resolve_ddl_output_dir_returns_guarded_path(monkeypatch, tmp_path):
    monkeypatch.setattr("metaweave.utils.file_utils.get_project_root", lambda: tmp_path)
    resolved = resolve_ddl_output_dir(SOURCE_ID, WORKSPACE_ID)
    expected = tmp_path / "output" / SOURCE_ID / WORKSPACE_ID / "ddl"
    assert resolved == expected
    assert resolved.is_absolute()
    # 旧 UUID 入口不再合法（doc 6 §1）
    with pytest.raises(ValueError):
        resolve_ddl_output_dir("b9a4943d-44d6-4f35-b553-d25207f9d8c1", WORKSPACE_ID)


@pytest.mark.parametrize("linked_level", ["output", "source", "workspace", "ddl"])
def test_resolver_rejects_in_project_symlink(monkeypatch, tmp_path, linked_level):
    monkeypatch.setattr("metaweave.utils.file_utils.get_project_root", lambda: tmp_path)
    expected = tmp_path / "output" / SOURCE_ID / WORKSPACE_ID / "ddl"
    other = tmp_path / "other" / "ddl"
    other.mkdir(parents=True)
    keep = other / "keep.txt"
    keep.write_text("keep", encoding="utf-8")
    if linked_level == "output":
        (tmp_path / "output").symlink_to(tmp_path / "other", target_is_directory=True)
    elif linked_level == "source":
        expected.parents[2].mkdir()
        expected.parents[1].symlink_to(tmp_path / "other", target_is_directory=True)
    elif linked_level == "workspace":
        expected.parents[1].mkdir(parents=True)
        expected.parent.symlink_to(tmp_path / "other", target_is_directory=True)
    else:
        expected.parent.mkdir(parents=True)
        expected.symlink_to(other, target_is_directory=True)
    with pytest.raises(ValueError, match="符号链接"):
        resolve_ddl_output_dir(SOURCE_ID, WORKSPACE_ID)
    assert keep.read_text(encoding="utf-8") == "keep"


# ---------- CLI 受管组合 ----------


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
    cfg = tmp_path / "config.yaml"
    cfg.write_text("database: {}\n", encoding="utf-8")
    from backend.data_sources import step_context

    dispatched = []
    monkeypatch.setattr(
        "backend.data_sources.ddl_step_service.run_ddl_step",
        lambda *_a, **_kw: dispatched.append("run") or pytest.fail("不应执行"),
    )
    result = CliRunner().invoke(
        metadata_command,
        [
            "-c",
            str(cfg),
            "--step",
            "ddl",
            "--source-id",
            SOURCE_ID,
            "--workspace-id",
            WORKSPACE_ID,
            *extra,
        ],
    )
    assert result.exit_code == 2, result.output
    assert "不接受" in result.output
    assert dispatched == []


@pytest.mark.parametrize(
    ("args", "fragment"),
    [
        (
            ["--step", "ddl", "--workspace-id", WORKSPACE_ID],
            "--workspace-id 仅在受管模式",
        ),
        (
            ["--step", "ddl", "--source-id", SOURCE_ID],
            "受管 --step ddl 必须同时指定 --workspace-id",
        ),
        (["--step", "json", "--source-id", SOURCE_ID], "--source-id 仅适用于"),
        (
            ["--step", "md", "--workspace-id", WORKSPACE_ID],
            "--workspace-id 仅适用于",
        ),
        (
            [
                "--step",
                "ddl",
                "--source-id",
                SOURCE_ID,
                "--workspace-id",
                WORKSPACE_ID,
                "--generate-domains",
            ],
            "不能与 --generate-*",
        ),
    ],
)
def test_cli_rejects_invalid_ddl_combinations(tmp_path, monkeypatch, args, fragment):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("database: {}\n", encoding="utf-8")
    result = CliRunner().invoke(metadata_command, ["-c", str(cfg), *args])
    assert result.exit_code == 2, result.output
    assert fragment in result.output


def test_cli_managed_ddl_dispatch_and_result_mapping(tmp_path, monkeypatch):
    """受管 ddl 派发：装配 StepContext（工作区定位）→ run_ddl_step → 结果映射。"""
    cfg = tmp_path / "config.yaml"
    cfg.write_text("database: {}\n", encoding="utf-8")

    from backend.data_sources import ddl_step_service, step_context
    from backend.persistence import platform_db

    monkeypatch.setattr(
        platform_db, "load_platform_settings_from_env", lambda: object()
    )
    seen = []

    def fake_ctx(_settings=None, **kwargs):
        return SimpleNamespace(**kwargs)

    monkeypatch.setattr(step_context, "load_step_context", fake_ctx)

    outcome_ok = SimpleNamespace(success=True, message="受管 DDL 采集完成（3 个对象）")
    monkeypatch.setattr(
        ddl_step_service,
        "run_ddl_step",
        lambda ctx, max_workers=None: seen.append((ctx, max_workers)) or outcome_ok,
    )
    result = CliRunner().invoke(
        metadata_command,
        [
            "-c",
            str(cfg),
            "--step",
            "ddl",
            "--source-id",
            SOURCE_ID,
            "--workspace-id",
            WORKSPACE_ID,
            "--max-workers",
            "7",
        ],
    )
    assert result.exit_code == 0, result.output
    ((ctx_args, workers),) = seen
    assert ctx_args.source_id == SOURCE_ID
    assert ctx_args.workspace_id == WORKSPACE_ID
    assert ctx_args.step_name == "ddl"
    assert workers == 7
    assert "3 个对象" in result.output
    assert WORKSPACE_ID in result.output

    monkeypatch.setattr(
        ddl_step_service,
        "run_ddl_step",
        lambda _ctx, max_workers=None: SimpleNamespace(
            success=False, message="选中范围内没有可采集对象"
        ),
    )
    failed = CliRunner().invoke(
        metadata_command,
        [
            "-c",
            str(cfg),
            "--step",
            "ddl",
            "--source-id",
            SOURCE_ID,
            "--workspace-id",
            WORKSPACE_ID,
        ],
    )
    assert failed.exit_code == 1
    assert "没有可采集对象" in failed.output

    def bad_config(_ctx, max_workers=None):
        raise DbStepConfigError("未找到指定 source_id 的数据源")

    monkeypatch.setattr(ddl_step_service, "run_ddl_step", bad_config)
    invalid = CliRunner().invoke(
        metadata_command,
        [
            "-c",
            str(cfg),
            "--step",
            "ddl",
            "--source-id",
            SOURCE_ID,
            "--workspace-id",
            WORKSPACE_ID,
        ],
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
    from metaweave.core.metadata.models import GenerationResult

    run_ddl_step = SimpleNamespace(called=0)
    monkeypatch.setattr(
        ddl_step_service,
        "run_ddl_step",
        lambda *_a, **_kw: pytest.fail("未受管 ddl 不应进入受管服务"),
    )

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
        assert "受管" not in result.output
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


# --- doc 18.1 §5.2：受管 DDL 快照覆盖登记源 sslmode（含 None 显式覆盖）------


def test_snapshot_copies_registered_sslmode(monkeypatch, tmp_path):
    """登记源 sslmode=require 时，快照把它交给执行层（经 DatabaseConnector）。"""
    rec = _install(monkeypatch, tmp_path)
    ctx = StepContext(
        settings=SimpleNamespace(),
        source=_source(sslmode="require"),
        main_config=rec.main_config,
        config_path=Path("cfg.yaml"),
        source_id=SOURCE_ID,
        workspace_id=WORKSPACE_ID,
        run_id=RUN_ID,
        step_name="ddl",
        step_params=None,
    )
    assert service.run_ddl_step(ctx).success
    (snapshot,) = rec.generator_configs
    assert snapshot["database"]["sslmode"] == "require"


def test_snapshot_none_sslmode_overrides_legacy_config_value(monkeypatch, tmp_path):
    """登记值为 None 时显式覆盖主配置遗留的 database.sslmode，不继承。"""
    monkeypatch.setattr("metaweave.utils.file_utils.get_project_root", lambda: tmp_path)
    main_config = copy.deepcopy(MAIN_CONFIG)
    main_config["database"]["sslmode"] = "prefer"
    snapshot = service.build_managed_ddl_config(
        main_config, _source(sslmode=None), "secret", WORKSPACE_ID
    )
    assert snapshot["database"]["sslmode"] is None
    # 主配置副本不被改动
    assert main_config["database"]["sslmode"] == "prefer"


def test_managed_snapshot_reaches_connector_params(monkeypatch, tmp_path):
    """连接器测试替身核对最终参数（doc 18.1 §5.2）。"""
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
        main_config, _source(sslmode=" require "), "secret", WORKSPACE_ID
    )
    DatabaseConnector(require_snapshot["database"])
    assert conninfo_to_dict(captured["conninfo"])["sslmode"] == "require"

    none_snapshot = service.build_managed_ddl_config(
        main_config, _source(sslmode=None), "secret", WORKSPACE_ID
    )
    DatabaseConnector(none_snapshot["database"])
    assert "sslmode" not in conninfo_to_dict(captured["conninfo"])


# --- doc 19.1 §3：无凭据公共工作区快照 ---------------------------------------


def test_build_managed_output_snapshot_is_credential_free(monkeypatch, tmp_path):
    """公共快照：输出七键指向同一工作区，连接字段显式置 None，不接收
    密文或解密密码（doc 19.1 §3；dim/ 为 doc 22.1 更名后的第 6 键）。"""
    monkeypatch.setattr("metaweave.utils.file_utils.get_project_root", lambda: tmp_path)
    snapshot = service.build_managed_output_snapshot(
        MAIN_CONFIG, SOURCE_ID, WORKSPACE_ID, "orders", ["public", "sales"]
    )
    workspace = tmp_path / "output" / SOURCE_ID / WORKSPACE_ID
    assert snapshot["output"] == {
        "output_dir": str(workspace),
        "ddl_directory": str(workspace / "ddl"),
        "json_directory": str(workspace / "json"),
        "markdown_directory": str(workspace / "md"),
        "rel_directory": str(workspace / "rel"),
        "dim_tables_directory": str(workspace / "dim"),
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
            bad_config, SOURCE_ID, WORKSPACE_ID, "demo", ["public"]
        )


def test_snapshot_accepts_missing_or_null_sections(monkeypatch, tmp_path):
    """缺省与 null 视为空对象（doc 19.1 §4.5），不收紧空值行为。"""
    monkeypatch.setattr("metaweave.utils.file_utils.get_project_root", lambda: tmp_path)
    for updates in ({"database": None, "output": None}, {}):
        blank = copy.deepcopy(MAIN_CONFIG)
        blank.update(updates)
        snapshot = service.build_managed_output_snapshot(
            blank, SOURCE_ID, WORKSPACE_ID, "demo", ["public"]
        )
        assert snapshot["database"]["database"] == "demo"
        assert snapshot["output"]["ddl_directory"].endswith("/ddl")


def test_build_managed_ddl_config_still_adds_credentials(monkeypatch, tmp_path):
    """原函数在公共快照之上追加登记源连接字段，DDL/JSON 行为不变。"""
    monkeypatch.setattr("metaweave.utils.file_utils.get_project_root", lambda: tmp_path)
    snapshot = service.build_managed_ddl_config(
        MAIN_CONFIG, _source(), "secret", WORKSPACE_ID
    )
    assert snapshot["database"]["host"] == "localhost"
    assert snapshot["database"]["user"] == "reader"
    assert snapshot["database"]["password"] == "secret"
    assert snapshot["database"]["database"] == "demo"
