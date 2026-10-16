"""metadata --generate-dim-config CLI 测试（原顶层命令 dim_config --generate 迁移而来）"""

from pathlib import Path

from click.testing import CliRunner

from metaweave.cli import metadata_cli


class _DummyDimTableConfigGenerator:
    init_kwargs = {}

    def __init__(self, json_dir, output_path):
        type(self).init_kwargs = {
            "json_dir": str(json_dir),
            "output_path": str(output_path),
        }

    def generate(self):
        output_path = Path(type(self).init_kwargs["output_path"])
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text("databases: {}\n", encoding="utf-8")
        return {
            "databases": {
                "demo": {"tables": {"public.dim_a": {"embedding_col": None}}},
            }
        }


def _invoke(runner, cfg, extra_args, monkeypatch, tmp_path, loaded_config):
    monkeypatch.setattr(
        "metaweave.core.dim_value.config_generator.DimTableConfigGenerator",
        _DummyDimTableConfigGenerator,
    )
    monkeypatch.setattr(
        "services.config_loader.load_config",
        lambda _path: loaded_config,
    )
    monkeypatch.setattr("metaweave.utils.file_utils.get_project_root", lambda: tmp_path)
    return runner.invoke(
        metadata_cli.metadata_command,
        ["--config", str(cfg), "--generate-dim-config", *extra_args],
    )


def test_generate_dim_config_resolves_path_from_loaders_config(tmp_path, monkeypatch):
    """不传 --dim-config 时，dim_tables.yaml 写入 loaders.dim_loader.config_file 声明的路径"""
    cfg = tmp_path / "metadata_config.yaml"
    cfg.write_text("database:\n  database: demo\n", encoding="utf-8")

    runner = CliRunner()
    loaded_config = {
        "output": {"json_directory": "output/json"},
        "loaders": {"dim_loader": {"config_file": "out2/dim_config/dim_tables.yaml"}},
    }
    result = _invoke(runner, cfg, [], monkeypatch, tmp_path, loaded_config)

    assert result.exit_code == 0, result.output
    assert _DummyDimTableConfigGenerator.init_kwargs["output_path"] == str(
        tmp_path / "out2" / "dim_config" / "dim_tables.yaml"
    )
    assert _DummyDimTableConfigGenerator.init_kwargs["json_dir"] == str(
        tmp_path / "output" / "json"
    )
    assert (tmp_path / "out2" / "dim_config" / "dim_tables.yaml").exists()
    assert "已生成" in result.output


def test_generate_dim_config_fallback_default_path(tmp_path, monkeypatch):
    """loaders.dim_loader.config_file 缺省时，回退 output/dim_config/dim_tables.yaml"""
    cfg = tmp_path / "metadata_config.yaml"
    cfg.write_text("database:\n  database: demo\n", encoding="utf-8")

    runner = CliRunner()
    loaded_config = {"output": {"json_directory": "output/json"}}
    result = _invoke(runner, cfg, [], monkeypatch, tmp_path, loaded_config)

    assert result.exit_code == 0, result.output
    assert _DummyDimTableConfigGenerator.init_kwargs["output_path"] == str(
        tmp_path / "output" / "dim_config" / "dim_tables.yaml"
    )


def test_generate_dim_config_cli_overrides_config(tmp_path, monkeypatch):
    """显式传入 --dim-config 时，覆盖配置解析出的路径（相对路径基于项目根）"""
    cfg = tmp_path / "metadata_config.yaml"
    cfg.write_text("database:\n  database: demo\n", encoding="utf-8")

    runner = CliRunner()
    loaded_config = {
        "output": {"json_directory": "output/json"},
        "loaders": {"dim_loader": {"config_file": "out2/dim_config/dim_tables.yaml"}},
    }
    result = _invoke(
        runner, cfg, ["--dim-config", "custom/my_dim_tables.yaml"],
        monkeypatch, tmp_path, loaded_config,
    )

    assert result.exit_code == 0, result.output
    assert _DummyDimTableConfigGenerator.init_kwargs["output_path"] == str(
        tmp_path / "custom" / "my_dim_tables.yaml"
    )


def test_generate_dim_config_clean_removes_files_keeps_subdirs(tmp_path, monkeypatch):
    """--clean 清理 dim_config 目录顶层文件，保留子目录及其内容"""
    cfg = tmp_path / "metadata_config.yaml"
    cfg.write_text("database:\n  database: demo\n", encoding="utf-8")

    dim_dir = tmp_path / "output" / "dim_config"
    dim_dir.mkdir(parents=True)
    (dim_dir / "dim_tables.yaml").write_text("old: 1\n", encoding="utf-8")
    (dim_dir / "dim_tables.yaml.bak_20260101_000000").write_text("bak\n", encoding="utf-8")
    (dim_dir / "notes.txt").write_text("note\n", encoding="utf-8")
    subdir = dim_dir / "manual"
    subdir.mkdir()
    (subdir / "keep.yaml").write_text("keep\n", encoding="utf-8")

    runner = CliRunner()
    loaded_config = {"output": {"json_directory": "output/json"}}
    result = _invoke(runner, cfg, ["--clean"], monkeypatch, tmp_path, loaded_config)

    assert result.exit_code == 0, result.output
    assert "已清理" in result.output
    # 顶层旧文件被清除，新生成的 dim_tables.yaml 存在
    assert not (dim_dir / "dim_tables.yaml.bak_20260101_000000").exists()
    assert not (dim_dir / "notes.txt").exists()
    assert (dim_dir / "dim_tables.yaml").exists()
    # 子目录及其内容保留
    assert (subdir / "keep.yaml").exists()


def test_generate_dim_config_without_clean_keeps_existing_files(tmp_path, monkeypatch):
    """不带 --clean 时，目录中已有的无关文件不被删除"""
    cfg = tmp_path / "metadata_config.yaml"
    cfg.write_text("database:\n  database: demo\n", encoding="utf-8")

    dim_dir = tmp_path / "output" / "dim_config"
    dim_dir.mkdir(parents=True)
    (dim_dir / "notes.txt").write_text("note\n", encoding="utf-8")

    runner = CliRunner()
    loaded_config = {"output": {"json_directory": "output/json"}}
    result = _invoke(runner, cfg, [], monkeypatch, tmp_path, loaded_config)

    assert result.exit_code == 0, result.output
    assert (dim_dir / "notes.txt").exists()
    assert (dim_dir / "dim_tables.yaml").exists()


def test_generate_dim_config_conflicts_with_generate_domains(tmp_path):
    cfg = tmp_path / "metadata_config.yaml"
    cfg.write_text("database:\n  database: demo\n", encoding="utf-8")

    runner = CliRunner()
    result = runner.invoke(
        metadata_cli.metadata_command,
        [
            "--config", str(cfg),
            "--generate-dim-config",
            "--generate-domains",
        ],
    )

    assert result.exit_code != 0
    assert "互斥" in result.output


def test_generate_dim_config_conflicts_with_domain(tmp_path):
    cfg = tmp_path / "metadata_config.yaml"
    cfg.write_text("database:\n  database: demo\n", encoding="utf-8")

    runner = CliRunner()
    result = runner.invoke(
        metadata_cli.metadata_command,
        [
            "--config", str(cfg),
            "--generate-dim-config",
            "--domain",
        ],
    )

    assert result.exit_code != 0
    assert "互斥" in result.output


def test_generate_dim_config_warns_out_of_scope_params(tmp_path, monkeypatch):
    """generate-dim-config 模式下显式传入无效参数 → 仅告警不拦截（exit 0）"""
    cfg = tmp_path / "metadata_config.yaml"
    cfg.write_text("database:\n  database: demo\n", encoding="utf-8")

    runner = CliRunner()
    loaded_config = {"output": {"json_directory": "output/json"}}
    result = _invoke(
        runner, cfg,
        ["--description", "某描述", "--md-limit", "5", "--tables", "a,b"],
        monkeypatch, tmp_path, loaded_config,
    )

    # 不拦截执行：生成仍完成
    assert result.exit_code == 0, result.output
    assert "已生成" in result.output
    # 三个无效参数各自告警
    assert "--description" in result.output and "不生效" in result.output
    assert "--md-limit" in result.output
    assert "--tables/-t" in result.output


def test_generate_dim_config_no_warning_for_valid_params(tmp_path, monkeypatch):
    """generate-dim-config 模式下传入有效参数（--dim-config/--clean）不产生告警"""
    cfg = tmp_path / "metadata_config.yaml"
    cfg.write_text("database:\n  database: demo\n", encoding="utf-8")

    runner = CliRunner()
    loaded_config = {"output": {"json_directory": "output/json"}}
    result = _invoke(
        runner, cfg,
        ["--dim-config", "out2/dim_tables.yaml", "--clean"],
        monkeypatch, tmp_path, loaded_config,
    )

    assert result.exit_code == 0, result.output
    assert "不生效" not in result.output


def test_mode_scoped_params_matrix():
    """作用域矩阵常量断言：各参数的合法模式与设计一致"""
    from metaweave.cli.metadata_cli import _MODE_SCOPED_PARAMS as m

    # --clean：step 与 generate-dim-config/generate-domains 有效
    assert "clean" in m["step"]
    assert "clean" in m["generate-dim-config"]
    assert "clean" in m["generate-domains"]

    # --description 及 md-context 三件套：仅 generate-domains
    for p in ("description", "md_context_dir", "md_context_mode", "md_limit"):
        assert p in m["generate-domains"]
        assert p not in m["step"]
        assert p not in m["generate-dim-config"]

    # --dim-config：仅 generate-dim-config
    assert "dim_config_path" in m["generate-dim-config"]
    assert "dim_config_path" not in m["generate-domains"]
    assert "dim_config_path" not in m["step"]

    # --schemas/--tables/--max-workers：仅 step
    for p in ("schemas", "tables", "max_workers"):
        assert p in m["step"]
        assert p not in m["generate-domains"]
        assert p not in m["generate-dim-config"]

    # --domains-config：跨分支（generate-domains 写 / step 的 rel·cql 读）
    assert "domains_config" in m["generate-domains"]
    assert "domains_config" in m["step"]

    # --generate-sql-rag：domains_config / md_context_dir / clean 三参数有效
    assert "domains_config" in m["generate-sql-rag"]
    assert "md_context_dir" in m["generate-sql-rag"]
    assert "clean" in m["generate-sql-rag"]
    for p in (
        "description", "md_context_mode", "md_limit", "dim_config_path",
        "schemas", "tables", "max_workers",
    ):
        assert p not in m["generate-sql-rag"]


class TestResolveDimTablesYamlPath:
    """resolve_dim_tables_yaml_path 的优先级解析"""

    def test_cli_explicit_wins(self, tmp_path):
        from metaweave.utils.file_utils import resolve_dim_tables_yaml_path

        p = resolve_dim_tables_yaml_path(
            {"loaders": {"dim_loader": {"config_file": "a/b.yaml"}}},
            "cli/x.yaml",
            tmp_path,
        )
        assert p == tmp_path / "cli" / "x.yaml"

    def test_loaders_config_file(self, tmp_path):
        from metaweave.utils.file_utils import resolve_dim_tables_yaml_path

        p = resolve_dim_tables_yaml_path(
            {"loaders": {"dim_loader": {"config_file": "out2/dim_tables.yaml"}}},
            None,
            tmp_path,
        )
        assert p == tmp_path / "out2" / "dim_tables.yaml"

    def test_fallback_default(self, tmp_path):
        from metaweave.utils.file_utils import resolve_dim_tables_yaml_path

        p = resolve_dim_tables_yaml_path({}, None, tmp_path)
        assert p == tmp_path / "output" / "dim_config" / "dim_tables.yaml"
