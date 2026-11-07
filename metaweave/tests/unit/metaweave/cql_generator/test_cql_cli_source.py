"""CLI --step cql 来源参数传递测试（doc 4 §4.2：实体键身份必填）

- execute_cql 缺 cql_source_id：明确失败提示（不再落到 writer 深处报错）；
- 提供合法 source_id：provenance=(source_id, 随机 publish_run_id) 注入生成器；
- CLI --step cql 未带 --source-id 时以可读提示退出非零。
"""

from pathlib import Path

import yaml
from click.testing import CliRunner

from metaweave.cli import metadata_cli
from metaweave.core.metadata.execution import (
    MetadataExecutionRequest,
    execute_cql,
)
from metaweave.core.cql_generator.models import CQLGenerationResult

SOURCE_ID = "db_src_1"


def _cfg(tmp_path: Path) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "database": {"database": "demo"},
                "output": {"cql_directory": "output/cql"},
            }
        ),
        encoding="utf-8",
    )
    return path


class TestExecuteCqlSourceId:
    def test_missing_source_id_fails_with_clear_message(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "services.config_loader.load_config", lambda _p: {"output": {}}
        )
        req = MetadataExecutionRequest(
            config_path=_cfg(tmp_path), operation="cql", cql_source_id=None
        )
        result = execute_cql(req)
        assert result["success"] is False
        assert "--source-id" in result["message"]
        assert "实体键" in result["message"]

    def test_invalid_short_id_rejected(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "services.config_loader.load_config", lambda _p: {"output": {}}
        )
        req = MetadataExecutionRequest(
            config_path=_cfg(tmp_path),
            operation="cql",
            cql_source_id="not-a-short-id",
        )
        result = execute_cql(req)
        assert result["success"] is False
        assert "--source-id" in result["message"]

    def test_provenance_passed_to_generator(self, tmp_path, monkeypatch):
        captured = {}

        class _Dummy:
            def __init__(
                self, config_path, domain_resolver=None, config=None, provenance=None
            ):
                captured["provenance"] = provenance
                self.config = config or {}

            def generate(self, step_name="cql"):
                return CQLGenerationResult(success=True, output_files=[])

        monkeypatch.setattr(
            "metaweave.core.cql_generator.generator.CQLGenerator", _Dummy
        )
        monkeypatch.setattr(
            "services.config_loader.load_config", lambda _p: {"output": {}}
        )
        req = MetadataExecutionRequest(
            config_path=_cfg(tmp_path),
            operation="cql",
            cql_source_id=SOURCE_ID,
        )
        result = execute_cql(req)
        assert result["success"] is True
        provenance = captured["provenance"]
        assert provenance is not None
        assert provenance[0] == SOURCE_ID  # source_id 归一化传入
        assert provenance[1]  # 调试 publish_run_id（随机 UUID，不写平台库）


class TestCliStepCql:
    def test_cli_cql_without_source_id_exits_nonzero(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(
            metadata_cli.metadata_command,
            ["--config", str(_cfg(tmp_path)), "--step", "cql"],
        )
        assert result.exit_code != 0
        assert "--source-id" in result.output

    def test_cli_cql_accepts_source_id(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        captured = {}

        class _Dummy:
            def __init__(
                self, config_path, domain_resolver=None, config=None, provenance=None
            ):
                captured["provenance"] = provenance
                self.config = config or {}

            def generate(self, step_name="cql"):
                return CQLGenerationResult(success=True, output_files=[])

        monkeypatch.setattr(
            "metaweave.core.cql_generator.generator.CQLGenerator", _Dummy
        )
        result = CliRunner().invoke(
            metadata_cli.metadata_command,
            [
                "--config",
                str(_cfg(tmp_path)),
                "--step",
                "cql",
                "--source-id",
                SOURCE_ID,
            ],
        )
        assert result.exit_code == 0, result.output
        assert captured["provenance"][0] == SOURCE_ID
