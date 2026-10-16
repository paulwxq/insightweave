"""metadata --generate-sql-rag CLI 测试（原 sql-rag generate/validate/run-all 合并而来）"""

from pathlib import Path
from unittest.mock import MagicMock

import yaml
from click.testing import CliRunner

from metaweave.cli import metadata_cli


class _DummyQuestionSQLGenerator:
    instances = []
    gen_success = True
    gen_total = 3
    gen_output_file = ""

    def __init__(self, llm_service, generation_config):
        self.llm_service = llm_service
        self.generation_config = generation_config
        self.clean_output = MagicMock()
        self.generate_calls = []
        type(self).instances.append(self)

    def generate(self, domains_config_path, md_dir, rel_dir=None):
        self.generate_calls.append(
            {
                "domains_config_path": str(domains_config_path),
                "md_dir": str(md_dir),
                "rel_dir": str(rel_dir) if rel_dir else None,
            }
        )
        from metaweave.core.sql_rag.models import GenerationResult

        return GenerationResult(
            success=type(self).gen_success,
            pairs=[],
            domain_stats={"域A": type(self).gen_total},
            total_generated=type(self).gen_total,
            output_file=type(self).gen_output_file,
        )


class _DummySQLValidator:
    instances = []
    stats = {}

    def __init__(self, connector, config, llm_service, md_dir, rel_dir):
        self.init_kwargs = {
            "config": config,
            "llm_service": llm_service,
            "md_dir": str(md_dir),
            "rel_dir": str(rel_dir),
        }
        self.validate_input = None
        type(self).instances.append(self)

    def validate_file(self, input_file, enable_repair=False):
        self.validate_input = {
            "input_file": str(input_file),
            "enable_repair": enable_repair,
        }
        return type(self).stats


def _base_config():
    return {
        "database": {"database": "testdb", "host": "localhost"},
        "llm": {"active": "qwen", "providers": {"qwen": {"model": "m", "api_key": "k"}}},
        "output": {"markdown_directory": "output/md", "rel_directory": "output/rel"},
        "sql_rag": {
            "generation": {"output_dir": "output/sql-rag"},
            "validation": {"enabled": True, "enable_sql_repair": False},
        },
    }


def _setup_env(tmp_path, db_name="testdb"):
    """写 metadata_config.yaml + db_domains.yaml + md 目录，返回配置文件路径"""
    cfg = tmp_path / "metadata_config.yaml"
    cfg.write_text("database:\n  database: testdb\n", encoding="utf-8")
    domains_dir = tmp_path / "output" / "domains"
    domains_dir.mkdir(parents=True)
    (domains_dir / "db_domains.yaml").write_text(
        yaml.safe_dump({"database": {"name": db_name}, "domains": []}, allow_unicode=True),
        encoding="utf-8",
    )
    md_dir = tmp_path / "output" / "md"
    md_dir.mkdir(parents=True)
    (md_dir / "testdb.public.t.md").write_text("# t\n", encoding="utf-8")
    return cfg


def _default_stats():
    return {
        "total": 3,
        "valid": 3,
        "invalid": 0,
        "success_rate": 100.0,
        "total_time": 0.5,
        "repair_stats": {"attempted": 0, "successful": 0, "failed": 0},
        "repair_apply_stats": {"modified": 0, "deleted": 0, "failed": 0},
    }


def _invoke(
    runner,
    cfg,
    extra_args,
    monkeypatch,
    tmp_path,
    loaded_config,
    *,
    gen_success=True,
    gen_total=3,
    stats=None,
):
    _DummyQuestionSQLGenerator.instances = []
    _DummyQuestionSQLGenerator.gen_success = gen_success
    _DummyQuestionSQLGenerator.gen_total = gen_total
    _DummyQuestionSQLGenerator.gen_output_file = str(
        tmp_path / "output" / "sql-rag" / "qs_testdb_pair.json"
    )
    _DummySQLValidator.instances = []
    _DummySQLValidator.stats = stats or _default_stats()

    llm_sentinel = object()
    connector_mock = MagicMock()
    connector_factory = MagicMock(return_value=connector_mock)
    precheck_declared = MagicMock()
    precheck_nonstandard = MagicMock()

    monkeypatch.setattr("services.config_loader.load_config", lambda _p: loaded_config)
    monkeypatch.setattr("metaweave.utils.file_utils.get_project_root", lambda: tmp_path)
    monkeypatch.setattr(
        "metaweave.services.llm_service.LLMService", lambda _c: llm_sentinel
    )
    monkeypatch.setattr(
        "metaweave.core.sql_rag.generator.QuestionSQLGenerator",
        _DummyQuestionSQLGenerator,
    )
    monkeypatch.setattr(
        "metaweave.core.metadata.connector.DatabaseConnector", connector_factory
    )
    monkeypatch.setattr(
        "metaweave.core.sql_rag.validator.SQLValidator", _DummySQLValidator
    )
    monkeypatch.setattr(
        "metaweave.services.llm_config_resolver._validate_declared_module_llm_paths",
        precheck_declared,
    )
    monkeypatch.setattr(
        "metaweave.services.llm_config_resolver._validate_nonstandard_llm_paths",
        precheck_nonstandard,
    )

    result = runner.invoke(
        metadata_cli.metadata_command,
        ["--config", str(cfg), "--generate-sql-rag", *extra_args],
    )
    spies = {
        "llm": llm_sentinel,
        "connector": connector_mock,
        "connector_factory": connector_factory,
        "precheck_declared": precheck_declared,
        "precheck_nonstandard": precheck_nonstandard,
    }
    return result, spies


def test_generate_sql_rag_basic_with_validation(tmp_path, monkeypatch):
    """默认（validation.enabled=true）：生成后执行校验，路径解析与调用参数正确"""
    cfg = _setup_env(tmp_path)
    result, spies = _invoke(
        CliRunner(), cfg, [], monkeypatch, tmp_path, _base_config()
    )

    assert result.exit_code == 0, result.output
    gen = _DummyQuestionSQLGenerator.instances[0]
    assert gen.generate_calls[0]["domains_config_path"] == str(
        tmp_path / "output" / "domains" / "db_domains.yaml"
    )
    assert gen.generate_calls[0]["md_dir"] == str(tmp_path / "output" / "md")
    assert gen.generate_calls[0]["rel_dir"] == str(tmp_path / "output" / "rel")

    validator = _DummySQLValidator.instances[0]
    assert validator.validate_input["input_file"] == str(
        tmp_path / "output" / "sql-rag" / "qs_testdb_pair.json"
    )
    assert validator.validate_input["enable_repair"] is False
    # enable_sql_repair=false 时不向 validator 传 llm_service
    assert validator.init_kwargs["llm_service"] is None
    spies["connector"].close.assert_called_once()
    assert "生成完成" in result.output
    assert "校验完成" in result.output


def test_skips_validation_when_enabled_false(tmp_path, monkeypatch):
    """validation.enabled=false：跳过校验，且不创建数据库连接"""
    cfg = _setup_env(tmp_path)
    loaded_config = _base_config()
    loaded_config["sql_rag"]["validation"]["enabled"] = False
    result, spies = _invoke(
        CliRunner(), cfg, [], monkeypatch, tmp_path, loaded_config
    )

    assert result.exit_code == 0, result.output
    assert "跳过 SQL 校验" in result.output
    assert not _DummySQLValidator.instances
    spies["connector_factory"].assert_not_called()


def test_clean_removes_all_top_level_files_keeps_subdirs(tmp_path, monkeypatch):
    """--clean 清空输出目录顶层所有文件（统一语义，不限扩展名），保留子目录"""
    cfg = _setup_env(tmp_path)
    sql_dir = tmp_path / "output" / "sql-rag"
    sql_dir.mkdir(parents=True)
    (sql_dir / "qs_old_pair.json").write_text("old\n", encoding="utf-8")
    (sql_dir / "unrelated.txt").write_text("old\n", encoding="utf-8")
    sub = sql_dir / "keep"
    sub.mkdir()
    (sub / "note.txt").write_text("keep\n", encoding="utf-8")

    result, spies = _invoke(
        CliRunner(), cfg, ["--clean"], monkeypatch, tmp_path, _base_config()
    )

    assert result.exit_code == 0, result.output
    # 顶层所有文件被清，子目录保留
    assert not (sql_dir / "qs_old_pair.json").exists()
    assert not (sql_dir / "unrelated.txt").exists()
    assert (sql_dir / "keep" / "note.txt").exists()
    # 不再走 generator.clean_output 单文件删除
    gen = _DummyQuestionSQLGenerator.instances[0]
    gen.clean_output.assert_not_called()


def test_missing_domains_file_friendly_error(tmp_path, monkeypatch):
    """db_domains.yaml 缺失 → 友好报错并提示先执行 --generate-domains"""
    cfg = tmp_path / "metadata_config.yaml"
    cfg.write_text("database:\n  database: testdb\n", encoding="utf-8")
    (tmp_path / "output" / "md").mkdir(parents=True)
    # 故意不写 db_domains.yaml

    result, spies = _invoke(
        CliRunner(), cfg, [], monkeypatch, tmp_path, _base_config()
    )

    assert result.exit_code != 0
    assert "未找到业务域配置" in result.output
    assert "metaweave metadata --generate-domains" in result.output
    assert not _DummyQuestionSQLGenerator.instances


def test_resolves_domains_from_config(tmp_path, monkeypatch):
    """不传 --domains-config 时，按 output.domains_directory 解析路径"""
    cfg = _setup_env(tmp_path)
    custom_dir = tmp_path / "custom_domains"
    custom_dir.mkdir()
    (custom_dir / "db_domains.yaml").write_text(
        yaml.safe_dump({"database": {"name": "testdb"}, "domains": []}),
        encoding="utf-8",
    )
    # 删除默认位置，确保走的是配置目录
    (tmp_path / "output" / "domains" / "db_domains.yaml").unlink()

    loaded_config = _base_config()
    loaded_config["output"]["domains_directory"] = "custom_domains"
    result, spies = _invoke(
        CliRunner(), cfg, [], monkeypatch, tmp_path, loaded_config
    )

    assert result.exit_code == 0, result.output
    gen = _DummyQuestionSQLGenerator.instances[0]
    assert gen.generate_calls[0]["domains_config_path"] == str(
        custom_dir / "db_domains.yaml"
    )


def test_md_context_dir_cli_explicit_wins(tmp_path, monkeypatch):
    """显式 --md-context-dir 覆盖配置，生成与校验共用同一目录"""
    cfg = _setup_env(tmp_path)
    custom_md = tmp_path / "my_md"
    custom_md.mkdir()

    result, spies = _invoke(
        CliRunner(), cfg, ["--md-context-dir", str(custom_md)],
        monkeypatch, tmp_path, _base_config(),
    )

    assert result.exit_code == 0, result.output
    gen = _DummyQuestionSQLGenerator.instances[0]
    assert gen.generate_calls[0]["md_dir"] == str(custom_md)
    validator = _DummySQLValidator.instances[0]
    assert validator.init_kwargs["md_dir"] == str(custom_md)


def test_md_dir_from_output_markdown_directory(tmp_path, monkeypatch):
    """不显式传入时，md 目录取 output.markdown_directory（统一解析语义）"""
    cfg = _setup_env(tmp_path)
    cfg_md = tmp_path / "cfg_md"
    cfg_md.mkdir()

    loaded_config = _base_config()
    loaded_config["output"]["markdown_directory"] = "cfg_md"
    result, spies = _invoke(
        CliRunner(), cfg, [], monkeypatch, tmp_path, loaded_config
    )

    assert result.exit_code == 0, result.output
    gen = _DummyQuestionSQLGenerator.instances[0]
    assert gen.generate_calls[0]["md_dir"] == str(cfg_md)


def test_quality_gate_blocks_when_repair_off_and_invalid(tmp_path, monkeypatch):
    """enable_sql_repair=false 且存在无效 SQL → 非 0 退出（对齐 pipeline 步骤 9）"""
    cfg = _setup_env(tmp_path)
    stats = {
        "total": 10,
        "valid": 8,
        "invalid": 2,
        "success_rate": 80.0,
        "total_time": 1.0,
        "repair_stats": {"attempted": 0, "successful": 0, "failed": 0},
        "repair_apply_stats": {"modified": 0, "deleted": 0, "failed": 0},
    }
    result, spies = _invoke(
        CliRunner(), cfg, [], monkeypatch, tmp_path, _base_config(), stats=stats
    )

    assert result.exit_code != 0
    assert "无效 SQL" in result.output
    assert "enable_sql_repair" in result.output
    # finally 保证连接关闭
    spies["connector"].close.assert_called_once()


def test_quality_gate_passes_all_valid_repair_off(tmp_path, monkeypatch):
    """全部有效（repair off）→ 正常退出"""
    cfg = _setup_env(tmp_path)
    stats = {
        "total": 10,
        "valid": 10,
        "invalid": 0,
        "success_rate": 100.0,
        "total_time": 1.0,
        "repair_stats": {"attempted": 0, "successful": 0, "failed": 0},
        "repair_apply_stats": {"modified": 0, "deleted": 0, "failed": 0},
    }
    result, spies = _invoke(
        CliRunner(), cfg, [], monkeypatch, tmp_path, _base_config(), stats=stats
    )

    assert result.exit_code == 0, result.output


def test_repair_enabled_warns_without_blocking(tmp_path, monkeypatch):
    """enable_sql_repair=true 且修复有失败 → 仅告警不阻断；llm_service 复用生成段实例"""
    cfg = _setup_env(tmp_path)
    loaded_config = _base_config()
    loaded_config["sql_rag"]["validation"]["enable_sql_repair"] = True
    stats = {
        "total": 10,
        "valid": 8,
        "invalid": 2,
        "success_rate": 80.0,
        "total_time": 1.0,
        "repair_stats": {"attempted": 2, "successful": 1, "failed": 1},
        "repair_apply_stats": {"modified": 1, "deleted": 1, "failed": 0},
    }
    result, spies = _invoke(
        CliRunner(), cfg, [], monkeypatch, tmp_path, loaded_config, stats=stats
    )

    assert result.exit_code == 0, result.output
    validator = _DummySQLValidator.instances[0]
    assert validator.init_kwargs["llm_service"] is spies["llm"]
    assert validator.validate_input["enable_repair"] is True
    assert "修复后有效" in result.output
    assert "修复回写" in result.output


def test_llm_prechecks_invoked(tmp_path, monkeypatch):
    """分支显式调用两个 LLM 配置预检（load_config 本身不含预检）"""
    cfg = _setup_env(tmp_path)
    result, spies = _invoke(
        CliRunner(), cfg, [], monkeypatch, tmp_path, _base_config()
    )

    assert result.exit_code == 0, result.output
    spies["precheck_declared"].assert_called_once()
    spies["precheck_nonstandard"].assert_called_once()


def test_generation_failure_aborts_before_validation(tmp_path, monkeypatch):
    """生成失败 → 直接终止，不进入校验段（不建连接、不建 validator）"""
    cfg = _setup_env(tmp_path)
    result, spies = _invoke(
        CliRunner(), cfg, [], monkeypatch, tmp_path, _base_config(),
        gen_success=False,
    )

    assert result.exit_code != 0
    assert not _DummySQLValidator.instances
    spies["connector_factory"].assert_not_called()


def test_conflicts_with_generate_domains(tmp_path):
    cfg = tmp_path / "metadata_config.yaml"
    cfg.write_text("database:\n  database: demo\n", encoding="utf-8")

    result = CliRunner().invoke(
        metadata_cli.metadata_command,
        ["--config", str(cfg), "--generate-sql-rag", "--generate-domains"],
    )

    assert result.exit_code != 0
    assert "互斥" in result.output


def test_conflicts_with_generate_dim_config(tmp_path):
    cfg = tmp_path / "metadata_config.yaml"
    cfg.write_text("database:\n  database: demo\n", encoding="utf-8")

    result = CliRunner().invoke(
        metadata_cli.metadata_command,
        ["--config", str(cfg), "--generate-sql-rag", "--generate-dim-config"],
    )

    assert result.exit_code != 0
    assert "互斥" in result.output


def test_conflicts_with_domain(tmp_path):
    cfg = tmp_path / "metadata_config.yaml"
    cfg.write_text("database:\n  database: demo\n", encoding="utf-8")

    result = CliRunner().invoke(
        metadata_cli.metadata_command,
        ["--config", str(cfg), "--generate-sql-rag", "--domain"],
    )

    assert result.exit_code != 0
    assert "互斥" in result.output


def test_warns_out_of_scope_params(tmp_path, monkeypatch):
    """generate-sql-rag 模式下显式传入无效参数 → 仅告警不拦截"""
    cfg = _setup_env(tmp_path)
    result, spies = _invoke(
        CliRunner(), cfg,
        ["--description", "某描述", "--tables", "a,b", "--md-limit", "5"],
        monkeypatch, tmp_path, _base_config(),
    )

    assert result.exit_code == 0, result.output
    assert "生成完成" in result.output
    assert "--description" in result.output and "不生效" in result.output
    assert "--tables/-t" in result.output
    assert "--md-limit" in result.output


def test_no_warning_for_valid_params(tmp_path, monkeypatch):
    """generate-sql-rag 模式下传入有效参数（--domains-config/--md-context-dir/--clean）不产生告警"""
    cfg = _setup_env(tmp_path)
    domains_path = tmp_path / "output" / "domains" / "db_domains.yaml"
    md_dir = tmp_path / "output" / "md"

    result, spies = _invoke(
        CliRunner(), cfg,
        ["--domains-config", str(domains_path), "--md-context-dir", str(md_dir), "--clean"],
        monkeypatch, tmp_path, _base_config(),
    )

    assert result.exit_code == 0, result.output
    assert "不生效" not in result.output
