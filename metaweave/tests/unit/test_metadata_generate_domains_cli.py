from pathlib import Path

from click.testing import CliRunner

from metaweave.cli import metadata_cli


class _DummyDomainGenerator:
    init_kwargs = {}
    user_description = None

    def __init__(
        self,
        config,
        yaml_path,
        md_context_dir=None,
        md_context_mode="name_comment",
        md_context_limit=100,
    ):
        type(self).init_kwargs = {
            "config": config,
            "yaml_path": yaml_path,
            "md_context_dir": md_context_dir,
            "md_context_mode": md_context_mode,
            "md_context_limit": md_context_limit,
        }

    def generate_from_context(self, user_description=None):
        type(self).user_description = user_description
        return {
            "database": {"name": "test_db", "description": "test desc"},
            "domains": [{"name": "订单", "description": "订单领域"}],
        }

    def write_to_yaml(self, _generated):
        yaml_path = Path(type(self).init_kwargs["yaml_path"])
        yaml_path.parent.mkdir(parents=True, exist_ok=True)
        yaml_path.write_text("database:\n  name: test_db\n", encoding="utf-8")
        return [
            {"name": "_未分类_", "description": "无法归入其他业务主题的表"},
            {"name": "订单", "description": "订单领域"},
        ]


def test_generate_domains_uses_config_markdown_directory_by_default(tmp_path, monkeypatch):
    cfg = tmp_path / "metadata_config.yaml"
    cfg.write_text("database:\n  database: demo\n", encoding="utf-8")

    monkeypatch.setattr(
        "metaweave.core.metadata.domain_generator.DomainGenerator",
        _DummyDomainGenerator,
    )
    monkeypatch.setattr(
        "services.config_loader.load_config",
        lambda _path: {"output": {"markdown_directory": "configured/md"}},
    )
    monkeypatch.setattr("metaweave.utils.file_utils.get_project_root", lambda: tmp_path)

    runner = CliRunner()
    result = runner.invoke(
        metadata_cli.metadata_command,
        [
            "--config",
            str(cfg),
            "--generate-domains",
            "--description",
            "测试数据库说明",
        ],
    )

    assert result.exit_code == 0, result.output
    assert _DummyDomainGenerator.user_description == "测试数据库说明"
    expected_md_dir = str((tmp_path / "configured" / "md").resolve())
    assert _DummyDomainGenerator.init_kwargs["md_context_dir"] == expected_md_dir


def test_generate_domains_writes_to_configured_domains_directory(tmp_path, monkeypatch):
    """不传 --domains-config 时，db_domains.yaml 写入配置解析的 domains 目录下。

    覆盖两种解析分支：
    1) 显式 output.domains_directory
    2) 缺省 domains_directory 时回退 output.output_dir/domains
    """
    cfg = tmp_path / "metadata_config.yaml"
    cfg.write_text("database:\n  database: demo\n", encoding="utf-8")

    monkeypatch.setattr(
        "metaweave.core.metadata.domain_generator.DomainGenerator",
        _DummyDomainGenerator,
    )
    monkeypatch.setattr("metaweave.utils.file_utils.get_project_root", lambda: tmp_path)

    runner = CliRunner()

    # 分支 1: 显式 output.domains_directory
    monkeypatch.setattr(
        "services.config_loader.load_config",
        lambda _path: {"output": {"domains_directory": "output/domains"}},
    )
    result = runner.invoke(
        metadata_cli.metadata_command,
        ["--config", str(cfg), "--generate-domains"],
    )
    assert result.exit_code == 0, result.output
    assert _DummyDomainGenerator.init_kwargs["yaml_path"] == str(
        tmp_path / "output" / "domains" / "db_domains.yaml"
    )
    assert (tmp_path / "output" / "domains" / "db_domains.yaml").exists()

    # 分支 2: 缺省 domains_directory，回退 output_dir/domains
    monkeypatch.setattr(
        "services.config_loader.load_config",
        lambda _path: {"output": {"output_dir": "custom_out"}},
    )
    result = runner.invoke(
        metadata_cli.metadata_command,
        ["--config", str(cfg), "--generate-domains"],
    )
    assert result.exit_code == 0, result.output
    assert _DummyDomainGenerator.init_kwargs["yaml_path"] == str(
        tmp_path / "custom_out" / "domains" / "db_domains.yaml"
    )


def test_generate_domains_cli_domains_config_overrides_config(tmp_path, monkeypatch):
    """显式传入 --domains-config 时，覆盖配置解析出的路径（相对路径基于项目根）"""
    cfg = tmp_path / "metadata_config.yaml"
    cfg.write_text("database:\n  database: demo\n", encoding="utf-8")

    monkeypatch.setattr(
        "metaweave.core.metadata.domain_generator.DomainGenerator",
        _DummyDomainGenerator,
    )
    monkeypatch.setattr(
        "services.config_loader.load_config",
        lambda _path: {"output": {"domains_directory": "output/domains"}},
    )
    monkeypatch.setattr("metaweave.utils.file_utils.get_project_root", lambda: tmp_path)

    runner = CliRunner()
    result = runner.invoke(
        metadata_cli.metadata_command,
        [
            "--config",
            str(cfg),
            "--generate-domains",
            "--domains-config",
            "custom/my_domains.yaml",
        ],
    )

    assert result.exit_code == 0, result.output
    assert _DummyDomainGenerator.init_kwargs["yaml_path"] == str(
        tmp_path / "custom" / "my_domains.yaml"
    )


def test_generate_domains_conflicts_with_cross_domain(tmp_path):
    cfg = tmp_path / "metadata_config.yaml"
    cfg.write_text("database:\n  database: demo\n", encoding="utf-8")

    runner = CliRunner()
    result = runner.invoke(
        metadata_cli.metadata_command,
        [
            "--config",
            str(cfg),
            "--generate-domains",
            "--cross-domain",
        ],
    )

    assert result.exit_code != 0
    assert "互斥" in result.output


def test_generate_domains_conflicts_with_no_cross_domain(tmp_path):
    """--no-cross-domain 同样应与 --generate-domains 互斥"""
    cfg = tmp_path / "metadata_config.yaml"
    cfg.write_text("database:\n  database: demo\n", encoding="utf-8")

    runner = CliRunner()
    result = runner.invoke(
        metadata_cli.metadata_command,
        [
            "--config",
            str(cfg),
            "--generate-domains",
            "--no-cross-domain",
        ],
    )

    assert result.exit_code != 0
    assert "互斥" in result.output


def test_generate_domains_succeeds_despite_yaml_domain_all(tmp_path, monkeypatch):
    """yaml 中配置了 relationships.domain: all，但 db_domains.yaml 不存在时，
    --generate-domains 仍应成功（不受 domain 合并逻辑影响）。
    """
    cfg = tmp_path / "metadata_config.yaml"
    cfg.write_text("database:\n  database: demo\n", encoding="utf-8")

    monkeypatch.setattr(
        "metaweave.core.metadata.domain_generator.DomainGenerator",
        _DummyDomainGenerator,
    )
    monkeypatch.setattr(
        "services.config_loader.load_config",
        lambda _path: {
            "relationships": {"domain": "all", "cross_domain": True},
            "output": {"markdown_directory": "output/md"},
        },
    )
    monkeypatch.setattr("metaweave.utils.file_utils.get_project_root", lambda: tmp_path)

    runner = CliRunner()
    result = runner.invoke(
        metadata_cli.metadata_command,
        ["--config", str(cfg), "--generate-domains"],
    )

    assert result.exit_code == 0, result.output
    assert "已生成" in result.output


def test_generate_domains_clean_removes_top_level_files_keeps_subdirs(tmp_path, monkeypatch):
    """--generate-domains --clean：清 domains 目录顶层所有文件，保留子目录（统一语义）。"""
    cfg = tmp_path / "metadata_config.yaml"
    cfg.write_text("database:\n  database: demo\n", encoding="utf-8")

    domains_dir = tmp_path / "domains_out"
    domains_dir.mkdir(parents=True)
    (domains_dir / "db_domains_old.yaml").write_text("old", encoding="utf-8")
    sub = domains_dir / "keep"
    sub.mkdir()
    (sub / "note.txt").write_text("keep", encoding="utf-8")

    monkeypatch.setattr(
        "metaweave.core.metadata.domain_generator.DomainGenerator",
        _DummyDomainGenerator,
    )
    monkeypatch.setattr(
        "services.config_loader.load_config",
        lambda _path: {},
    )
    monkeypatch.setattr("metaweave.utils.file_utils.get_project_root", lambda: tmp_path)

    runner = CliRunner()
    result = runner.invoke(
        metadata_cli.metadata_command,
        [
            "--config", str(cfg),
            "--generate-domains",
            "--clean",
            "--domains-config", str(domains_dir / "db_domains.yaml"),
        ],
    )

    assert result.exit_code == 0, result.output
    # 顶层旧文件被清理，子目录保留，新文件由生成器写入
    assert not (domains_dir / "db_domains_old.yaml").exists()
    assert (domains_dir / "keep" / "note.txt").exists()
    assert (domains_dir / "db_domains.yaml").exists()
    assert "🧹" in result.output


def test_generate_domains_rejects_non_positive_md_limit(tmp_path, monkeypatch):
    """显式 --md-limit 0/负数 → UsageError exit 2（doc 26 测试 46，废除静默钳制）。"""
    cfg = tmp_path / "metadata_config.yaml"
    cfg.write_text("database:\n  database: demo\n", encoding="utf-8")
    monkeypatch.setattr("metaweave.utils.file_utils.get_project_root", lambda: tmp_path)
    runner = CliRunner()

    for bad in ("0", "-3"):
        result = runner.invoke(
            metadata_cli.metadata_command,
            ["--config", str(cfg), "--generate-domains", "--md-limit", bad],
        )
        assert result.exit_code == 2, result.output
        assert "md_limit" in result.output

    # 单次正整数对照正常
    monkeypatch.setattr(
        "metaweave.core.metadata.domain_generator.DomainGenerator",
        _DummyDomainGenerator,
    )
    monkeypatch.setattr(
        "services.config_loader.load_config", lambda _p: {"output": {}}
    )
    md_dir = tmp_path / "output" / "md"
    md_dir.mkdir(parents=True, exist_ok=True)
    ok = runner.invoke(
        metadata_cli.metadata_command,
        ["--config", str(cfg), "--generate-domains", "--md-limit", "5"],
    )
    assert ok.exit_code == 0, ok.output
