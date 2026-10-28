"""单任务唯一配置快照合同测试（doc 26 §6.2；用户审核第 5 条）。

锁定：每个已开始任务的主配置读盘恰好一次——
- 共享执行函数内部（md 前置检查与主体执行复用同一快照）；
- 独立 CLI 端到端（此前 CLI 包装层一次 + 共享层一到两次，最多 3 次）。
"""

from unittest import mock

import pytest
import yaml
from click.testing import CliRunner

from metaweave.cli import metadata_cli
from metaweave.core.metadata.execution import (
    MetadataExecutionRequest,
    execute_ddl_json_md,
)


def _cfg_dict(output_dir="output"):
    return {
        "database": {"database": "demo", "schemas": ["public"]},
        "output": {"output_dir": output_dir},
        "ddl_generation": {"comments": {"llm_enabled": False}},
        "json_generation": {
            "comments": {"llm_enabled": False},
            "table_classification": {"llm_enabled": False},
        },
    }


@pytest.fixture
def spy_load_config(monkeypatch):
    """计数主配置读盘次数（共享层经 services.config_loader.load_config）。"""
    calls = []
    cfg = _cfg_dict()

    def _fake(path):
        calls.append(str(path))
        return cfg

    monkeypatch.setattr("services.config_loader.load_config", _fake)
    # LLM 预检与废弃节点校验为纯配置检查，mock 掉避免依赖完整 llm 段
    monkeypatch.setattr(
        "metaweave.services.llm_config_resolver._validate_declared_module_llm_paths",
        lambda _c: None,
    )
    monkeypatch.setattr(
        "metaweave.services.llm_config_resolver._validate_nonstandard_llm_paths",
        lambda _c: None,
    )
    return calls


class _DummyGenerator:
    instances = []

    def __init__(self, config_path, config=None):
        self.config = config or {}
        type(self).instances.append(self)

    def generate(self, *, step=None, **kwargs):
        from metaweave.core.metadata.models import GenerationResult

        return GenerationResult(
            success=True,
            processed_tables=1,
            failed_tables=0,
            output_files=[],
            errors=[],
        )


class TestSharedFunctionSnapshot:
    def test_md_task_reads_config_once(self, tmp_path, spy_load_config, monkeypatch):
        """execute_ddl_json_md(md) 内部只加载一次（前置检查与执行共用快照）。"""
        (tmp_path / "output" / "json").mkdir(parents=True)
        (tmp_path / "output" / "json" / "demo.public.t.json").write_text(
            "{}", encoding="utf-8"
        )
        cfg_path = tmp_path / "metadata_config.yaml"
        cfg_path.write_text("database:\n  database: demo\n", encoding="utf-8")

        monkeypatch.setattr(
            "metaweave.core.metadata.generator.MetadataGenerator", _DummyGenerator
        )
        monkeypatch.setattr(
            "metaweave.utils.file_utils.get_project_root", lambda: tmp_path
        )
        with mock.patch("click.echo"):
            result = execute_ddl_json_md(
                MetadataExecutionRequest(
                    config_path=cfg_path, operation="md", clean=False
                )
            )
        assert result["success"] is True
        assert len(spy_load_config) == 1

    @pytest.mark.parametrize("step", ["ddl", "json"])
    def test_ddl_json_reads_config_once(
        self, step, tmp_path, spy_load_config, monkeypatch
    ):
        cfg_path = tmp_path / "metadata_config.yaml"
        cfg_path.write_text("database:\n  database: demo\n", encoding="utf-8")
        monkeypatch.setattr(
            "metaweave.core.metadata.generator.MetadataGenerator", _DummyGenerator
        )
        monkeypatch.setattr(
            "metaweave.utils.file_utils.get_project_root", lambda: tmp_path
        )
        with mock.patch("click.echo"):
            result = execute_ddl_json_md(
                MetadataExecutionRequest(
                    config_path=cfg_path, operation=step, clean=True
                )
            )
        assert result["success"] is True
        assert len(spy_load_config) == 1


class TestCliEndToEndSnapshot:
    @staticmethod
    def _write_cfg(tmp_path):
        cfg = tmp_path / "metadata_config.yaml"
        cfg.write_text(
            yaml.safe_dump(_cfg_dict(), allow_unicode=True), encoding="utf-8"
        )
        return cfg

    @pytest.mark.parametrize("step", ["ddl", "json", "md"])
    def test_cli_step_reads_config_once(
        self, step, tmp_path, spy_load_config, monkeypatch
    ):
        """独立 CLI 单步端到端只读盘一次（此前 CLI 层 + 共享层最多 3 次）。"""
        (tmp_path / "output" / "json").mkdir(parents=True)
        (tmp_path / "output" / "json" / "demo.public.t.json").write_text(
            "{}", encoding="utf-8"
        )
        cfg = self._write_cfg(tmp_path)
        monkeypatch.setattr(
            "metaweave.core.metadata.generator.MetadataGenerator", _DummyGenerator
        )
        monkeypatch.setattr(
            "metaweave.utils.file_utils.get_project_root", lambda: tmp_path
        )

        result = CliRunner().invoke(
            metadata_cli.metadata_command,
            ["--config", str(cfg), "--step", step],
        )
        assert result.exit_code == 0, result.output
        assert len(spy_load_config) == 1, f"step={step} 读盘 {len(spy_load_config)} 次"


class TestSqlRagStructuredFailure:
    """generate-sql-rag 失败原因非空（用户审核第 6 条：Abort 空消息问题）。"""

    def test_unified_entry_used_by_cli(self):
        """CLI 的 generate-sql-rag 分支经共享入口（不再直连内部函数）。"""
        import inspect

        src = inspect.getsource(metadata_cli.metadata_command.callback)
        assert "execute_generate_sql_rag(" in src
        # 直连调用（非 import 行）不应出现在命令体
        assert "_run_generate_sql_rag(" not in src

    def test_cli_entry_failure_reason_not_empty(self, tmp_path, monkeypatch):
        """端到端：domains 文件缺失 → CLI 报错含具体原因（非空消息）。"""
        # md 目录存在、domains 缺失
        (tmp_path / "output" / "md").mkdir(parents=True)
        (tmp_path / "output" / "md" / "demo.public.t.md").write_text(
            "# t\n", encoding="utf-8"
        )
        cfg = tmp_path / "metadata_config.yaml"
        cfg.write_text(
            yaml.safe_dump(
                {
                    "database": {"database": "demo"},
                    "output": {"markdown_directory": "output/md"},
                    "sql_rag": {"generation": {"output_dir": "output/sql-rag"}},
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr(
            "services.config_loader.load_config",
            lambda _p: _cfg_dict()
            | {
                "output": {"markdown_directory": "output/md"},
                "sql_rag": {"generation": {"output_dir": "output/sql-rag"}},
            },
        )
        monkeypatch.setattr(
            "metaweave.services.llm_config_resolver._validate_declared_module_llm_paths",
            lambda _c: None,
        )
        monkeypatch.setattr(
            "metaweave.services.llm_config_resolver._validate_nonstandard_llm_paths",
            lambda _c: None,
        )
        monkeypatch.setattr(
            "metaweave.utils.file_utils.get_project_root", lambda: tmp_path
        )

        result = CliRunner().invoke(
            metadata_cli.metadata_command,
            ["--config", str(cfg), "--generate-sql-rag"],
        )
        assert result.exit_code == 1
        assert "未找到业务域配置" in result.output


class TestMaxWorkersValidation:
    """--max-workers 0/负数处理（用户审核第 5 条）：入口拒绝 + 执行层防御。"""

    @pytest.mark.parametrize("bad", ["0", "-1"])
    def test_cli_rejects_non_positive(self, bad, tmp_path, monkeypatch):
        """CLI 层 IntRange(min=1)：0/负数 → 参数错误 exit 2。"""
        cfg = tmp_path / "metadata_config.yaml"
        cfg.write_text("database:\n  database: demo\n", encoding="utf-8")
        result = CliRunner().invoke(
            metadata_cli.metadata_command,
            ["--config", str(cfg), "--step", "ddl", "--max-workers", bad],
        )
        assert result.exit_code == 2
        assert "Invalid value" in result.output

    def test_cli_accepts_positive(self, tmp_path, spy_load_config, monkeypatch):
        cfg = tmp_path / "metadata_config.yaml"
        cfg.write_text("database:\n  database: demo\n", encoding="utf-8")
        monkeypatch.setattr(
            "metaweave.core.metadata.generator.MetadataGenerator", _DummyGenerator
        )
        monkeypatch.setattr(
            "metaweave.utils.file_utils.get_project_root", lambda: tmp_path
        )
        result = CliRunner().invoke(
            metadata_cli.metadata_command,
            ["--config", str(cfg), "--step", "ddl", "--max-workers", "1"],
        )
        assert result.exit_code == 0, result.output

    @pytest.mark.parametrize("bad", [0, -1, True])
    def test_execution_layer_rejects_non_positive(
        self, bad, tmp_path, spy_load_config, monkeypatch
    ):
        """共享层防御：0/负数/布尔 → 结构化失败（不再被 `or 4` 假值重置）。"""
        cfg_path = tmp_path / "metadata_config.yaml"
        cfg_path.write_text("database:\n  database: demo\n", encoding="utf-8")
        monkeypatch.setattr(
            "metaweave.core.metadata.generator.MetadataGenerator", _DummyGenerator
        )
        monkeypatch.setattr(
            "metaweave.utils.file_utils.get_project_root", lambda: tmp_path
        )
        with mock.patch("click.echo"):
            result = execute_ddl_json_md(
                MetadataExecutionRequest(
                    config_path=cfg_path, operation="ddl", max_workers_override=bad
                )
            )
        assert result["success"] is False
        assert "max_workers 必须为正整数" in result["message"]

    def test_execution_layer_none_uses_default_4(
        self, tmp_path, spy_load_config, monkeypatch
    ):
        """未覆盖 → 4（默认不变）。"""
        cfg_path = tmp_path / "metadata_config.yaml"
        cfg_path.write_text("database:\n  database: demo\n", encoding="utf-8")
        captured = {}

        class _Recorder(_DummyGenerator):
            def generate(self, *, step=None, **kwargs):
                captured.update(kwargs)
                return super().generate(step=step, **kwargs)

        monkeypatch.setattr(
            "metaweave.core.metadata.generator.MetadataGenerator", _Recorder
        )
        monkeypatch.setattr(
            "metaweave.utils.file_utils.get_project_root", lambda: tmp_path
        )
        with mock.patch("click.echo"):
            result = execute_ddl_json_md(
                MetadataExecutionRequest(config_path=cfg_path, operation="ddl")
            )
        assert result["success"] is True
        assert captured["max_workers"] == 4


class TestParamGuardBeforeCleanOrder:
    """参数防御先于 clean 副作用（用户审核第三轮第 2 条）。

    共享执行层支持直接 Python 调用（CLI 的 IntRange 拦不到），防御性校验
    必须在任何依赖检查与 clean 之前——失败时不得删除旧产物。
    """

    def _env(self, tmp_path):
        (tmp_path / "out" / "ddl").mkdir(parents=True)
        old = tmp_path / "out" / "ddl" / "old.sql"
        old.write_text("-- old", encoding="utf-8")
        cfg = tmp_path / "c.yaml"
        cfg.write_text("database:\n  database: demo\n", encoding="utf-8")
        cfg_dict = {
            "database": {"database": "demo"},
            "output": {"output_dir": "out", "ddl_directory": "out/ddl"},
        }
        return cfg, cfg_dict, old

    @pytest.mark.parametrize("bad", [0, -1, True])
    def test_invalid_max_workers_keeps_old_artifacts(self, bad, tmp_path, monkeypatch):
        from metaweave.core.metadata.execution import execute_ddl_json_md

        cfg, cfg_dict, old = self._env(tmp_path)
        monkeypatch.setattr(
            "metaweave.utils.file_utils.get_project_root", lambda: tmp_path
        )
        with mock.patch(
            "metaweave.core.metadata.execution.load_main_config_snapshot",
            return_value=cfg_dict,
        ), mock.patch("click.echo"):
            result = execute_ddl_json_md(
                MetadataExecutionRequest(
                    config_path=cfg,
                    operation="ddl",
                    clean=True,
                    max_workers_override=bad,
                )
            )
        assert result["success"] is False
        assert "max_workers 必须为正整数" in result["message"]
        assert old.exists(), "参数校验失败时不得执行 clean"

    def test_valid_max_workers_still_cleans(self, tmp_path, monkeypatch):
        from metaweave.core.metadata.execution import execute_ddl_json_md

        cfg, cfg_dict, old = self._env(tmp_path)
        monkeypatch.setattr(
            "metaweave.utils.file_utils.get_project_root", lambda: tmp_path
        )
        monkeypatch.setattr(
            "metaweave.core.metadata.generator.MetadataGenerator", _DummyGenerator
        )
        with mock.patch(
            "metaweave.core.metadata.execution.load_main_config_snapshot",
            return_value=cfg_dict,
        ), mock.patch("click.echo"):
            result = execute_ddl_json_md(
                MetadataExecutionRequest(
                    config_path=cfg,
                    operation="ddl",
                    clean=True,
                    max_workers_override=1,
                )
            )
        assert result["success"] is True
        assert not old.exists(), "合法路径下 clean 应正常执行"
