"""路径统一测试 42a（doc 26 §7.3）：相对路径一律基于项目根目录解析。

阶段一（路径统一提交）门槛：从项目根与任意其他 cwd 运行独立
metadata / rag_load CLI，同一主配置的相对路径必须解析到同一绝对目标。
本组测试不导入、注册或调用任何 pipeline 入口。
"""

from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from metaweave.cli import metadata_cli
from metaweave.core.metadata.formatter import OutputFormatter
from metaweave.core.relationships.pipeline import RelationshipDiscoveryPipeline
from metaweave.core.relationships.writer import RelationshipWriter
from metaweave.core.sql_rag.generator import QuestionSQLGenerator
from metaweave.core.sql_rag.loader import SQLExampleLoader
from metaweave.utils.file_utils import resolve_project_path


@pytest.fixture
def project_root(tmp_path, monkeypatch):
    """显式项目根 fixture（doc 26 §6.2）：单点拦截 file_utils.get_project_root。"""
    (tmp_path / "output").mkdir()
    monkeypatch.setattr("metaweave.utils.file_utils.get_project_root", lambda: tmp_path)
    # 反证：cwd 指向与项目根不同的目录，解析结果仍须落在项目根下
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    return tmp_path


# ---------------------------------------------------------------------------
# 共享 resolver 本体
# ---------------------------------------------------------------------------


class TestResolveProjectPath:
    def test_relative_resolves_to_project_root(self, project_root):
        assert resolve_project_path("output/md") == project_root / "output" / "md"

    def test_absolute_unchanged(self, project_root):
        assert resolve_project_path("/abs/x") == Path("/abs/x")

    def test_independent_of_cwd(self, project_root):
        # cwd 为 elsewhere，结果仍基于项目根
        assert resolve_project_path("a/b").is_absolute()
        assert resolve_project_path("a/b") == project_root / "a" / "b"


# ---------------------------------------------------------------------------
# 生成输出：formatter（ddl/json/md 显式目录 + output_dir 回退）
# ---------------------------------------------------------------------------


class TestFormatterPaths:
    def _fmt(self, config):
        return OutputFormatter(config, database_name="db")

    def test_explicit_dirs_project_root_based(self, project_root):
        f = self._fmt(
            {
                "output_dir": "out",
                "ddl_directory": "my/ddl",
                "json_directory": "my/json",
                "markdown_directory": "my/md",
            }
        )
        assert f.ddl_dir == project_root / "my" / "ddl"
        assert f.json_dir == project_root / "my" / "json"
        assert f.markdown_dir == project_root / "my" / "md"

    def test_output_dir_fallback_chain(self, project_root):
        """output_dir 相对时，ddl/md/json 回退目录同样落项目根。"""
        f = self._fmt({"output_dir": "out"})
        assert f.ddl_dir == project_root / "out" / "ddl"
        assert f.markdown_dir == project_root / "out" / "md"
        assert f.json_dir == project_root / "out" / "json"

    def test_default_output_dir(self, project_root):
        f = self._fmt({})
        assert f.json_dir == project_root / "output" / "json"

    def test_absolute_dirs_unchanged(self, project_root):
        abs_out = project_root / "abs_out"  # 真实存在的绝对路径
        abs_ddl = project_root / "abs_ddl"
        abs_out.mkdir()
        abs_ddl.mkdir()
        f = self._fmt(
            {
                "output_dir": str(abs_out),
                "ddl_directory": str(abs_ddl),
            }
        )
        assert f.output_dir == abs_out
        assert f.ddl_dir == abs_ddl


# ---------------------------------------------------------------------------
# 依赖输入：关系发现读 JSON 与 rel 写出同基准（读写分裂修复的直接断言）
# ---------------------------------------------------------------------------


class TestRelationshipPaths:
    def _cfg_path(self, project_root, output_cfg):
        cfg_path = project_root / "rel_cfg.yaml"
        cfg_path.write_text(
            yaml.safe_dump(
                {
                    "output": output_cfg,
                    "database": {
                        "database": "db",
                        "schemas": ["public"],
                        "user": "u",
                        "password": "p",
                        "host": "h",
                    },
                    "llm": {
                        "active": "qwen",
                        "providers": {"qwen": {"model": "m", "api_key": "k"}},
                    },
                }
            ),
            encoding="utf-8",
        )
        return cfg_path

    def test_read_json_and_write_rel_same_base(self, project_root):
        (project_root / "o" / "json").mkdir(parents=True)
        cfg_path = self._cfg_path(
            project_root, {"json_directory": "o/json", "rel_directory": "o/rel"}
        )
        pipeline = RelationshipDiscoveryPipeline(cfg_path)
        writer = RelationshipWriter(
            yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
        )
        assert pipeline.json_dir == project_root / "o" / "json"
        assert writer.rel_dir == project_root / "o" / "rel"

    def test_fallback_derivation(self, project_root):
        (project_root / "prod" / "json").mkdir(parents=True)
        cfg_path = self._cfg_path(project_root, {"output_dir": "prod"})
        pipeline = RelationshipDiscoveryPipeline(cfg_path)
        writer = RelationshipWriter(
            yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
        )
        assert pipeline.json_dir == project_root / "prod" / "json"
        assert writer.rel_dir == project_root / "output" / "rel"


# ---------------------------------------------------------------------------
# CQL 与 SQL-RAG
# ---------------------------------------------------------------------------


class TestCqlAndSqlRagPaths:
    def test_cql_generator_dir(self, project_root, monkeypatch):
        """CQL 输出目录基于项目根（clean 分叉修复）。"""
        from metaweave.core.cql_generator.generator import CQLGenerator

        cfg_path = project_root / "cfg.yaml"
        cfg_path.write_text("output:\n  cql_directory: gen/cql\n", encoding="utf-8")
        gen = CQLGenerator(cfg_path)
        assert gen.cql_dir == project_root / "gen" / "cql"

    def test_sql_rag_output_dir(self, project_root):
        g = QuestionSQLGenerator(None, {"output_dir": "rag/out"})
        assert g.output_dir == project_root / "rag" / "out"

    def test_sql_rag_output_dir_default(self, project_root):
        g = QuestionSQLGenerator(None, {})
        assert g.output_dir == project_root / "output" / "sql-rag"

    def test_sql_loader_input_dir(self, project_root):
        loader = SQLExampleLoader(
            {"sql_loader": {"input_dir": "rag/sql-rag", "collection_name": "c"}}
        )
        assert loader.input_dir == project_root / "rag" / "sql-rag"


# ---------------------------------------------------------------------------
# 日志：默认配置、显式配置与文件目标同样基于项目根（doc 15 §3.1）
# ---------------------------------------------------------------------------


class TestLoggingPaths:
    def test_default_config_path_resolves_to_project_root(self, project_root):
        """默认 configs/logging.yaml 定位到项目根（cwd 为 elsewhere）。"""
        from services.logging_manager import configure_logging

        with pytest.raises(FileNotFoundError) as excinfo:
            configure_logging("api")
        assert str(project_root / "configs" / "logging.yaml") in str(excinfo.value)

    def test_explicit_relative_config_resolves_to_project_root(self, project_root):
        from services.logging_manager import configure_logging

        with pytest.raises(FileNotFoundError) as excinfo:
            configure_logging("api", "custom/dir/logging.yaml")
        assert str(project_root / "custom" / "dir" / "logging.yaml") in str(
            excinfo.value
        )

    def test_absolute_config_unchanged(self, project_root, tmp_path):
        from services.logging_manager import configure_logging

        absolute = tmp_path / "abs-logging.yaml"
        with pytest.raises(FileNotFoundError) as excinfo:
            configure_logging("api", absolute)
        assert str(absolute) in str(excinfo.value)


# ---------------------------------------------------------------------------
# CLI 层：clean 目标与自动推断（子目录运行一致性）
# ---------------------------------------------------------------------------


class TestCliPathTargets:
    def _cfg_file(self, project_root, output_cfg):
        cfg = project_root / "metadata_config.yaml"
        cfg.write_text(
            yaml.safe_dump({"database": {"database": "db"}, "output": output_cfg}),
            encoding="utf-8",
        )
        return cfg

    def test_md_step_clean_target_project_root(
        self, project_root, monkeypatch, tmp_path
    ):
        """--step md --clean 的目标 = 项目根 markdown 目录（cwd 为 elsewhere）。"""
        cfg = self._cfg_file(project_root, {"markdown_directory": "o/md"})
        md_dir = project_root / "o" / "md"
        md_dir.mkdir(parents=True)
        (md_dir / "old.md").write_text("old", encoding="utf-8")
        # md 步骤前置：JSON 目录存在当前库的三段式文件（doc 17 §3.4）
        json_dir = project_root / "output" / "json"
        json_dir.mkdir(parents=True, exist_ok=True)
        (json_dir / "db.public.t.json").write_text("{}", encoding="utf-8")

        from metaweave.core.metadata.models import GenerationResult

        class _DummyGenerator:
            def __init__(self, _cfg, **kw):
                pass

            def generate(self, *, step=None, **kw):
                return GenerationResult(
                    success=True,
                    processed_tables=1,
                    failed_tables=0,
                    output_files=[],
                    errors=[],
                )

        monkeypatch.setattr(
            "metaweave.core.metadata.generator.MetadataGenerator", _DummyGenerator
        )
        result = CliRunner().invoke(
            metadata_cli.metadata_command,
            ["--config", str(cfg), "--step", "md", "--clean"],
        )
        assert result.exit_code == 0, result.output
        assert not (md_dir / "old.md").exists()

    def test_clean_outside_project_root_rejected(self, project_root, tmp_path):
        """clean 目标越出项目根 → 拒绝（绝对路径不改写，但 clean 越界仍拒）。"""
        from metaweave.utils.file_utils import ensure_path_in_project_root

        outside = tmp_path.parent / "outside_dir"
        with pytest.raises(ValueError):
            ensure_path_in_project_root(outside)
