"""CLI 无编排验收测试（doc 25：pipeline 与 standard 编排移除）

- 裸 metadata 必须指定 step 或 generate 模式
- 三个 --generate-* 独立模式无需 --step
- --step standard / --step json_llm 被 Choice 拒绝
- metaweave pipeline 经顶层命令组拒绝（doc 20.1 §10.8 移除）
- metadata --step json 仍为唯一 JSON 入口
"""

from pathlib import Path
from unittest import mock

import yaml
from click.testing import CliRunner

from metaweave.cli import metadata_cli
from metaweave.cli.main import cli
from metaweave.core.metadata.models import GenerationResult


def _write_min_config(tmp_path: Path) -> Path:
    """最小可加载配置（json 步骤 from_config / view 检查可通过）"""
    cfg = tmp_path / "metadata_config.yaml"
    cfg.write_text(
        yaml.safe_dump(
            {
                "database": {"database": "demo", "schemas": ["public"]},
                "output": {"output_dir": "output"},
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
    return cfg


# ---------------------------------------------------------------------------
# 1. 裸调用契约
# ---------------------------------------------------------------------------


def test_bare_metadata_requires_step_or_generate_mode(tmp_path):
    """无 --step 且无任何 --generate-* 模式 → UsageError（doc 25 §2.2）"""
    cfg = _write_min_config(tmp_path)
    runner = CliRunner()
    result = runner.invoke(metadata_cli.metadata_command, ["--config", str(cfg)])

    assert result.exit_code == 2
    assert "必须指定 step 或 generate 模式" in result.output


# ---------------------------------------------------------------------------
# 2. 三个 --generate-* 独立模式无需 --step
# ---------------------------------------------------------------------------


def test_generate_sql_rag_mode_works_without_step(tmp_path):
    """--generate-sql-rag 不传 --step 正常派发（mock 编排函数）"""
    cfg = _write_min_config(tmp_path)
    runner = CliRunner()
    # 结构化返回契约：None=成功（doc 26 §6.2）
    with mock.patch.object(
        metadata_cli, "_run_generate_sql_rag", return_value=None
    ) as m:
        result = runner.invoke(
            metadata_cli.metadata_command,
            ["--config", str(cfg), "--generate-sql-rag"],
        )

    assert result.exit_code == 0, result.output
    m.assert_called_once()


def test_generate_domains_mode_works_without_step(tmp_path, monkeypatch):
    """--generate-domains 不传 --step 正常执行"""
    cfg = _write_min_config(tmp_path)
    md_dir = tmp_path / "output" / "md"
    md_dir.mkdir(parents=True)

    class _DummyDomainGenerator:
        def __init__(self, **kwargs):
            pass

        def generate_from_context(self, user_description=None):
            return {"database": {"name": "demo"}, "domains": [{"name": "d1"}]}

        def write_to_yaml(self, _generated):
            return [{"name": "_未分类_"}, {"name": "d1"}]

    monkeypatch.setattr(
        "metaweave.core.metadata.domain_generator.DomainGenerator",
        _DummyDomainGenerator,
    )
    monkeypatch.setattr("services.config_loader.load_config", lambda _p: {"output": {}})
    monkeypatch.setattr("metaweave.utils.file_utils.get_project_root", lambda: tmp_path)

    runner = CliRunner()
    result = runner.invoke(
        metadata_cli.metadata_command, ["--config", str(cfg), "--generate-domains"]
    )

    assert result.exit_code == 0, result.output


def test_generate_dim_config_mode_works_without_step(tmp_path, monkeypatch):
    """--generate-dim-config 不传 --step 正常执行"""
    cfg = _write_min_config(tmp_path)

    class _DummyDimTableConfigGenerator:
        def __init__(self, json_dir, output_path):
            pass

        def generate(self):
            return {"databases": {}}

    monkeypatch.setattr(
        "metaweave.core.dim_value.config_generator.DimTableConfigGenerator",
        _DummyDimTableConfigGenerator,
    )
    monkeypatch.setattr(
        "services.config_loader.load_config",
        lambda _p: {"output": {"json_directory": "output/json"}},
    )
    monkeypatch.setattr("metaweave.utils.file_utils.get_project_root", lambda: tmp_path)

    runner = CliRunner()
    result = runner.invoke(
        metadata_cli.metadata_command, ["--config", str(cfg), "--generate-dim-config"]
    )

    assert result.exit_code == 0, result.output


# ---------------------------------------------------------------------------
# 3/4. Choice 拒绝（standard 编排与 json_llm 均不保留）
# ---------------------------------------------------------------------------


def test_step_standard_rejected_by_choice(tmp_path):
    cfg = _write_min_config(tmp_path)
    runner = CliRunner()
    result = runner.invoke(
        metadata_cli.metadata_command, ["--config", str(cfg), "--step", "standard"]
    )
    assert result.exit_code == 2
    assert "Invalid value for '--step'" in result.output


def test_step_json_llm_rejected_by_choice(tmp_path):
    cfg = _write_min_config(tmp_path)
    runner = CliRunner()
    result = runner.invoke(
        metadata_cli.metadata_command, ["--config", str(cfg), "--step", "json_llm"]
    )
    assert result.exit_code == 2
    assert "Invalid value for '--step'" in result.output


# ---------------------------------------------------------------------------
# 5. pipeline 命令组消亡（经顶层 cli group 验证）
# ---------------------------------------------------------------------------


def test_pipeline_command_is_unknown_and_not_listed(tmp_path, monkeypatch):
    """doc 20.1 §10.8：metaweave pipeline 整体移除，被顶层命令组当作未知命令拒绝。

    monkeypatch setup_metaweave_logging 隔离根回调的文件 handler 副作用
    （避免跨测试 "Logging error" 污染后续 CliRunner 输出）。
    """
    cfg = _write_min_config(tmp_path)
    monkeypatch.setattr(
        "metaweave.cli.main.setup_metaweave_logging", lambda _p=None: None
    )
    runner = CliRunner()

    for args in (["pipeline"], ["pipeline", "metadata"], ["pipeline", "-c", str(cfg)]):
        result = runner.invoke(cli, args)
        assert result.exit_code == 2
        combined = result.output + getattr(result, "stderr", "")
        assert "No such command 'pipeline'" in combined

    help_result = runner.invoke(cli, ["--help"])
    assert help_result.exit_code == 0
    assert "pipeline" not in help_result.output
    # 单步入口仍然可用
    assert "metadata" in help_result.output and "rag_load" in help_result.output


# ---------------------------------------------------------------------------
# 6. --step json 仍为唯一 JSON 入口（派发回归）
# ---------------------------------------------------------------------------


class _DummyMetadataGenerator:
    def __init__(self, _config_path, **kwargs):
        pass

    def generate(self, *, step=None, **kwargs):
        return GenerationResult(
            success=True,
            processed_tables=1,
            failed_tables=0,
            output_files=[],
            errors=[],
        )


def test_step_json_dispatches_generator(tmp_path, monkeypatch):
    """--step json → MetadataGenerator.generate(step="json") 派发保留"""
    cfg = _write_min_config(tmp_path)
    calls = []

    class _RecordingGenerator(_DummyMetadataGenerator):
        def generate(self, *, step=None, **kwargs):
            calls.append(step)
            return super().generate(step=step, **kwargs)

    monkeypatch.setattr(
        "metaweave.core.metadata.generator.MetadataGenerator", _RecordingGenerator
    )
    runner = CliRunner()
    result = runner.invoke(
        metadata_cli.metadata_command, ["--config", str(cfg), "--step", "json"]
    )

    assert result.exit_code == 0, result.output
    assert calls == ["json"]


def test_step_duplicate_rejected(tmp_path, monkeypatch):
    """--step 重复传入 → exit 2 + 中文报错；单次对照正常（doc 27 §3.1）。"""
    cfg = _write_min_config(tmp_path)
    calls = []

    class _RecordingGenerator(_DummyMetadataGenerator):
        def generate(self, *, step=None, **kwargs):
            calls.append(step)
            return super().generate(step=step, **kwargs)

    monkeypatch.setattr(
        "metaweave.core.metadata.generator.MetadataGenerator", _RecordingGenerator
    )
    runner = CliRunner()

    dup = runner.invoke(
        metadata_cli.metadata_command,
        ["--config", str(cfg), "--step", "ddl", "--step", "json"],
    )
    single = runner.invoke(
        metadata_cli.metadata_command,
        ["--config", str(cfg), "--step", "ddl"],
    )

    assert dup.exit_code == 2
    assert "仅允许传入一次" in dup.output
    assert single.exit_code == 0, single.output
    assert calls == ["ddl"]  # 单次对照：仅执行所传步骤


def test_incremental_option_removed(tmp_path):
    """--incremental 已删除（从未实现，留档 docs/update/0_plan.md）。

    参数在共享层重构后被静默忽略（值未进入请求）；按"删未实现功能"原则
    移除，未来重加见 0_plan.md §1。
    """
    cfg = _write_min_config(tmp_path)
    result = CliRunner().invoke(
        metadata_cli.metadata_command,
        ["--config", str(cfg), "--step", "ddl", "--incremental"],
    )
    assert result.exit_code == 2
    assert "No such option" in result.output

    short = CliRunner().invoke(
        metadata_cli.metadata_command,
        ["--config", str(cfg), "--step", "ddl", "-i"],
    )
    assert short.exit_code == 2
    assert "No such option" in short.output


# ---------------------------------------------------------------------------
# 失败出口：click.Abort 穿透，不再多打一条空错误
# ---------------------------------------------------------------------------


def test_step_failure_reports_single_error(tmp_path, monkeypatch):
    """--step 失败只输出一条业务错误 + Aborted!（click.Abort 穿透）。

    click.Abort 是 RuntimeError 子类、``str(Abort())`` 为空串：若被末尾的
    ``except Exception`` 接住，除正确消息外还会多打一条空的 "❌ 错误: "
    与一条空日志。此用例锁定单条错误输出与退出码 1。
    """
    monkeypatch.setattr("metaweave.utils.file_utils.get_project_root", lambda: tmp_path)
    cfg = _write_min_config(tmp_path)  # output_dir=output → json 目录不存在

    result = CliRunner().invoke(
        metadata_cli.metadata_command, ["--config", str(cfg), "--step", "md"]
    )

    assert result.exit_code == 1
    assert result.stderr.count("❌ 错误:") == 1
    assert "--step md 依赖 JSON 产物" in result.stderr
    assert "Aborted!" in result.stderr
    assert "元数据生成失败" not in result.stderr
