"""--clean 目录清理语义测试（doc 25 定案：删除顶层所有文件、不递归、保留子目录）。

覆盖未受管 CLI 的 rel / cql 步骤。受管工作区步骤的重跑语义已改为整目录
备份（doc 6 §6，`backup_directory_before_update`），由
`tests/unit/test_workspace_files.py` 覆盖；本文件只锁定仍在使用的
`clean_top_level_files` 路径。cql 生成必带短 ID 来源（doc 4 §4.2、doc 6 §1）。
"""

import shutil
from pathlib import Path

import yaml
from click.testing import CliRunner

from metaweave.cli import metadata_cli
from metaweave.core.cql_generator.models import CQLGenerationResult
from metaweave.core.relationships.models import RelationshipDiscoveryResult

SOURCE_ID = "db_src_1"


def _write_config(tmp_path: Path, step_dir_key: str) -> Path:
    # --clean 校验输出目录必须位于项目根目录内，因此使用项目内路径
    out_dir = (
        Path.cwd().resolve() / "tests" / ".tmp" / f"out_{tmp_path.name}"
    ).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    cfg = {
        "database": {"database": "test_db", "schemas": ["public"]},
        "output": {
            "output_dir": str(out_dir.relative_to(Path.cwd().resolve())),
            step_dir_key: str(
                (out_dir / step_dir_key).relative_to(Path.cwd().resolve())
            ),
        },
        "ddl_generation": {"comments": {"llm_enabled": False}},
        "json_generation": {
            "comments": {"llm_enabled": False},
            "table_classification": {"llm_enabled": False},
        },
    }
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
    return path


class _DummyRelPipeline:
    def __init__(
        self,
        _config_path,
        domain_filter=None,
        cross_domain=False,
        domain_resolver=None,
        config=None,
    ):
        pass

    def discover(self) -> RelationshipDiscoveryResult:
        return RelationshipDiscoveryResult(success=True, total_relations=1)


class _DummyCQLGenerator:
    def __init__(self, config_path, domain_resolver=None, config=None, provenance=None):
        # metadata_cli cql 分支以 generator.config 调 _clean_step_output_dir，
        # 必须与真实 CQLGenerator 一致从配置文件加载（空 dict 会让 clean 目标
        # 回退到默认 output/cql，误清真实产物目录）
        self.config = (
            yaml.safe_load(Path(config_path).read_text(encoding="utf-8")) or {}
        )

    def generate(self, step_name: str = "cql") -> CQLGenerationResult:
        return CQLGenerationResult(
            success=True, output_files=[f"import_all.{step_name}.cypher"]
        )


def _preset_dir(out_dir: Path, name: str) -> Path:
    """预置：顶层一个旧文件（应删）+ 子目录内一个文件（应保留）。"""
    d = out_dir / name
    d.mkdir(parents=True, exist_ok=True)
    (d / f"old.{name}").write_text("old", encoding="utf-8")
    sub = d / "subdir"
    sub.mkdir(exist_ok=True)
    (sub / "keep.txt").write_text("keep", encoding="utf-8")
    return d


def _test_step_clean_keeps_subdirs(tmp_path: Path, monkeypatch, step: str):
    cfg = _write_config(tmp_path, "rel" if step == "rel" else "cql")
    loaded = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
    out_dir = Path.cwd().resolve() / loaded["output"]["output_dir"]
    target = _preset_dir(out_dir, "rel" if step == "rel" else "cql")

    if step == "rel":
        monkeypatch.setattr(
            "metaweave.core.relationships.pipeline.RelationshipDiscoveryPipeline",
            _DummyRelPipeline,
            raising=False,
        )
    else:
        monkeypatch.setattr(
            "metaweave.core.cql_generator.generator.CQLGenerator",
            _DummyCQLGenerator,
            raising=False,
        )

    runner = CliRunner()
    try:
        argv = ["--config", str(cfg), "--step", step, "--clean"]
        if step == "cql":
            # doc 4 §4.2 + doc 6 §1：cql 生成必带来源（短 ID，不再接受 UUID）
            argv += ["--source-id", SOURCE_ID]
        result = runner.invoke(metadata_cli.metadata_command, argv)
        assert result.exit_code == 0, result.output

        # 顶层所有文件被删（不限扩展名）
        assert not (target / f"old.{target.name}").exists()
        # 子目录及其内容保留（不递归）
        assert (target / "subdir" / "keep.txt").exists()
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def test_step_rel_clean_keeps_subdirs(tmp_path: Path, monkeypatch):
    _test_step_clean_keeps_subdirs(tmp_path, monkeypatch, "rel")


def test_step_cql_clean_keeps_subdirs(tmp_path: Path, monkeypatch):
    _test_step_clean_keeps_subdirs(tmp_path, monkeypatch, "cql")


def test_step_cql_rejects_legacy_uuid_source_id(tmp_path: Path, monkeypatch):
    """断代改造后 source_id 只接受短 ID；旧 UUID 值在入口拒绝（doc 6 §1）。"""
    monkeypatch.setattr(
        "metaweave.core.cql_generator.generator.CQLGenerator",
        _DummyCQLGenerator,
        raising=False,
    )
    cfg = _write_config(tmp_path, "cql")
    loaded = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
    out_dir = Path.cwd().resolve() / loaded["output"]["output_dir"]
    try:
        result = CliRunner().invoke(
            metadata_cli.metadata_command,
            [
                "--config",
                str(cfg),
                "--step",
                "cql",
                "--source-id",
                "0f0e8d64-9d3a-4d0f-8d5f-2a3b4c5d6e7f",
            ],
        )
        assert result.exit_code == 2
        target = out_dir / "cql"
        if target.exists():
            assert list(target.iterdir()) == []  # 未执行任何清理
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)
