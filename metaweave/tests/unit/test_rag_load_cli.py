"""rag_load 命令测试（doc 24 重构；doc 20.1 移除 --type all / --continue-on-error）

mock LoaderFactory.create，不触碰真实 Milvus / Neo4j / PostgreSQL。
"""

from pathlib import Path
from unittest import mock

import pytest
import yaml
from click.testing import CliRunner

from metaweave.cli import loader_cli

SUMMARY_HEADING = "RAG 加载汇总"
SOURCE = "b9a4943d-44d6-4f35-b553-d25207f9d8c1"
JOB = "550e8400-e29b-41d4-a716-446655440000"


def _ok_loader(message="加载完成", execution_time=0.5):
    loader = mock.MagicMock()
    loader.validate.return_value = True
    loader.load.return_value = {
        "success": True,
        "message": message,
        "execution_time": execution_time,
    }
    return loader


def _fail_loader(message="boom"):
    loader = mock.MagicMock()
    loader.validate.return_value = True
    loader.load.return_value = {"success": False, "message": message}
    return loader


def _factory(loaders_by_type):
    """构造按类型分派的 LoaderFactory.create 替身；值为异常实例时抛出。"""

    def _create(load_type, config, metadata_config=None):
        value = loaders_by_type[load_type]
        if isinstance(value, Exception):
            raise value
        return value

    return _create


def _write_config(tmp_path, cfg=None):
    cfg = cfg or {}
    cfg.setdefault("database", {"database": "orders"})
    # clean=true 时会经共享 resolver 解析目标（用户审核第三轮第 3 条）：
    # collection_name / Neo4j uri / Milvus host-port 均须合法
    cfg.setdefault(
        "loaders",
        {
            "sql_loader": {"collection_name": "sql_c"},
            "table_schema_loader": {"collection_name": "ts_c"},
            "dim_loader": {"collection_name": "dim_c"},
            "cql_loader": {"neo4j": {"uri": "bolt://localhost:7687"}},
        },
    )
    cfg.setdefault(
        "vector_database",
        {
            "active": "milvus",
            "providers": {"milvus": {"host": "localhost", "port": 19530}},
        },
    )
    p = tmp_path / "metadata_config.yaml"
    p.write_text(yaml.dump(cfg), encoding="utf-8")
    return str(p)


class TestCommandContract:
    """命令契约：显式单类型 --type（doc 27 §2；doc 20.1 §10.7）"""

    def test_type_cql_single_load(self, tmp_path):
        runner = CliRunner()
        config = _write_config(tmp_path)
        loader = _ok_loader()

        with mock.patch.object(
            loader_cli.LoaderFactory, "create", side_effect=_factory({"cql": loader})
        ):
            result = runner.invoke(
                loader_cli.rag_load_command, ["--type", "cql", "-c", config]
            )

        assert result.exit_code == 0
        loader.load.assert_called_once_with(clean=False)

    @pytest.mark.parametrize("load_type", ["cql"])
    def test_clean_keeps_original_meaning_for_other_types(self, tmp_path, load_type):
        runner = CliRunner()
        config = _write_config(tmp_path)
        loader = _ok_loader()

        with mock.patch.object(
            loader_cli.LoaderFactory,
            "create",
            side_effect=_factory({load_type: loader}),
        ):
            result = runner.invoke(
                loader_cli.rag_load_command,
                ["--type", load_type, "-c", config, "--clean"],
            )

        assert result.exit_code == 0
        loader.load.assert_called_once_with(clean=True)

    def test_invalid_type_rejected_by_choice(self, tmp_path):
        runner = CliRunner()
        config = _write_config(tmp_path)

        for bad in ("dim", "dim_value", "content_md", "all", "sql"):
            result = runner.invoke(
                loader_cli.rag_load_command, ["--type", bad, "-c", config]
            )
            assert result.exit_code == 2
            assert "Invalid value" in result.output

    def test_continue_on_error_option_removed(self, tmp_path):
        config = _write_config(tmp_path)
        result = CliRunner().invoke(
            loader_cli.rag_load_command,
            ["--type", "sql", "-c", config, "--continue-on-error"],
        )
        assert result.exit_code == 2 and "No such option" in result.output

    def test_help_has_no_all_or_continue_on_error(self):
        result = CliRunner().invoke(loader_cli.rag_load_command, ["--help"])
        assert "all" not in result.output.split("加载类型")[1].split("示例")[0].replace(
            "table_schema", ""
        )
        assert "--continue-on-error" not in result.output
        assert "--type all" not in result.output


class TestTableSchemaDebugCli:
    """doc 20.1 §10.7：table_schema 调试加载"""

    def test_requires_both_uuids(self, tmp_path):
        config = _write_config(tmp_path)
        for args in (
            ["--type", "table_schema"],
            ["--type", "table_schema", "--source_id", SOURCE],
            ["--type", "table_schema", "--job_id", JOB],
            ["--type", "table_schema", "--source_id", "bad", "--job_id", JOB],
        ):
            result = CliRunner().invoke(
                loader_cli.rag_load_command, args + ["-c", config]
            )
            assert result.exit_code == 2, args

    def test_clean_never_resolves_clean_target_or_warns_about_wiping(
        self, tmp_path, monkeypatch
    ):
        config = _write_config(tmp_path)
        loader = _ok_loader()
        loader.publish_id = "11111111-2222-4333-8444-555555555555"
        loader.close = mock.MagicMock()
        seen = []
        import metaweave.core.clean_targets as clean_targets

        monkeypatch.setattr(
            clean_targets, "resolve_clean_target", lambda *a, **k: seen.append(a) or {}
        )
        monkeypatch.setattr(loader_cli.LoaderFactory, "create", lambda *a, **k: loader)
        for flag in ([], ["--clean"]):
            loader.load.reset_mock()
            result = CliRunner().invoke(
                loader_cli.rag_load_command,
                [
                    "--type",
                    "table_schema",
                    "--source_id",
                    SOURCE,
                    "--job_id",
                    JOB,
                    "-c",
                    config,
                ]
                + flag,
            )
            assert result.exit_code == 0, result.output
            # 不论是否传 --clean，都按 source_id 替换，不要求加载器清空集合
            loader.load.assert_called_once_with(clean=False)
            assert "清空目标数据库" not in result.output
            assert "调试批次 UUID" in result.output
        assert seen == []
        assert loader.close.call_count == 2

    @pytest.mark.parametrize(
        "loaders, expected",
        [
            ([], "loaders"),
            ({"table_schema_loader": []}, "loaders.table_schema_loader"),
        ],
    )
    def test_non_mapping_loaders_gets_shape_error_not_attribute_error(
        self, tmp_path, monkeypatch, loaders, expected
    ):
        """传 --source_id 时，节点形态校验先于受管配置派生（不得抛 AttributeError）。"""
        config = _write_config(tmp_path, {"loaders": loaders})
        create = mock.MagicMock()
        monkeypatch.setattr(loader_cli.LoaderFactory, "create", create)
        result = CliRunner().invoke(
            loader_cli.rag_load_command,
            [
                "--type",
                "table_schema",
                "--source_id",
                SOURCE,
                "--job_id",
                JOB,
                "-c",
                config,
            ],
        )
        assert result.exit_code == 1
        assert "配置错误" in result.output and expected in result.output
        assert (
            "AttributeError" not in result.output and "setdefault" not in result.output
        )
        create.assert_not_called()

    def test_missing_loaders_node_still_configured_from_source_id(
        self, tmp_path, monkeypatch
    ):
        """loaders 节点缺失时，受管派生仍会把输入路径与 source_id 注入加载器配置。"""
        cfg = {
            "vector_database": {
                "active": "milvus",
                "providers": {"milvus": {"host": "h", "port": 1}},
            }
        }
        path = tmp_path / "metadata_config.yaml"
        path.write_text(yaml.dump(cfg), encoding="utf-8")
        monkeypatch.setattr(loader_cli.LoaderFactory, "create", mock.MagicMock())
        result = CliRunner().invoke(
            loader_cli.rag_load_command,
            [
                "--type",
                "table_schema",
                "--source_id",
                SOURCE,
                "--job_id",
                JOB,
                "-c",
                str(path),
            ],
        )
        # collection_name 未配置 → 受管派生给出明确错误，而非形态异常
        assert result.exit_code == 1
        assert "collection_name" in result.output
        assert "AttributeError" not in result.output

    def test_loader_closed_even_when_load_fails(self, tmp_path, monkeypatch):
        config = _write_config(tmp_path)
        loader = _fail_loader("Milvus 超时")
        loader.publish_id = "x"
        monkeypatch.setattr(loader_cli.LoaderFactory, "create", lambda *a, **k: loader)
        result = CliRunner().invoke(
            loader_cli.rag_load_command,
            [
                "--type",
                "table_schema",
                "--source_id",
                SOURCE,
                "--job_id",
                JOB,
                "-c",
                config,
            ],
        )
        assert result.exit_code == 1
        loader.close.assert_called_once_with()

    def test_import_chain_needs_no_platform_dependencies(self):
        """M09：调试 CLI 不依赖平台库（backend / sqlalchemy）。"""
        import subprocess
        import sys

        code = (
            "import sys\n"
            "import metaweave.cli.loader_cli\n"
            "import metaweave.core.loaders.table_schema_loader\n"
            "import metaweave.core.table_schema.managed_input\n"
            "bad = [m for m in ('backend', 'sqlalchemy') if m in sys.modules]\n"
            "assert not bad, bad\n"
        )
        done = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True
        )
        assert done.returncode == 0, done.stderr


class TestFailurePaths:
    """失败路径：单类型失败直接退出码非 0"""

    def test_load_returns_failure_exit_code(self, tmp_path):
        runner = CliRunner()
        config = _write_config(tmp_path)

        with mock.patch.object(
            loader_cli.LoaderFactory,
            "create",
            side_effect=_factory({"cql": _fail_loader("embedding 401")}),
        ):
            result = runner.invoke(
                loader_cli.rag_load_command, ["--type", "cql", "-c", config]
            )

        assert result.exit_code == 1
        assert "embedding 401" in result.output

    @pytest.mark.parametrize(
        "exc",
        [
            FileNotFoundError("qs.json 不存在"),
            ValueError("配置错误: 缺少段"),
            RuntimeError("连接超时"),
        ],
    )
    def test_helper_exception_contract(self, tmp_path, exc):
        """helper 异常契约：create 抛三类异常 → 失败、不泄露堆栈。"""
        runner = CliRunner()
        config = _write_config(tmp_path)

        with mock.patch.object(
            loader_cli.LoaderFactory, "create", side_effect=_factory({"cql": exc})
        ):
            result = runner.invoke(
                loader_cli.rag_load_command, ["--type", "cql", "-c", config]
            )

        assert result.exit_code == 1
        assert str(exc) in result.output
        assert "Traceback" not in result.output


class TestExplicitTypeContract:
    """裸调用拒绝、重复传入防护"""

    def test_bare_invocation_requires_type(self, tmp_path):
        runner = CliRunner()
        config = _write_config(tmp_path)
        result = runner.invoke(loader_cli.rag_load_command, ["-c", config])

        assert result.exit_code == 2
        assert "必须指定 --type" in result.output

    def test_duplicate_type_rejected(self, tmp_path):
        runner = CliRunner()
        config = _write_config(tmp_path)
        loader = _ok_loader()

        with mock.patch.object(
            loader_cli.LoaderFactory, "create", side_effect=_factory({"cql": loader})
        ):
            dup = runner.invoke(
                loader_cli.rag_load_command,
                ["--type", "cql", "--type", "table_schema", "-c", config],
            )
            single = runner.invoke(
                loader_cli.rag_load_command, ["--type", "cql", "-c", config]
            )

        assert dup.exit_code == 2
        assert "仅允许传入一次" in dup.output
        assert single.exit_code == 0  # 单次对照


class TestOldCommandDeath:
    """旧命令必须经顶层 cli group 验证消亡"""

    def test_load_command_no_longer_exists(self, tmp_path):
        from metaweave.cli.main import cli

        runner = CliRunner()
        config = _write_config(tmp_path)
        result = runner.invoke(cli, ["load", "--type", "sql", "-c", config])

        assert result.exit_code == 2
        combined = result.output + getattr(result, "stderr", "")
        assert "No such command 'load'" in combined

    def test_rag_load_registered_on_top_level(self, tmp_path):
        from metaweave.cli.main import cli

        runner = CliRunner()
        config = _write_config(tmp_path)

        with mock.patch.object(
            loader_cli.LoaderFactory,
            "create",
            side_effect=_factory({"cql": _ok_loader()}),
        ):
            result = runner.invoke(cli, ["rag_load", "--type", "cql", "-c", config])

        assert result.exit_code == 0


class TestContractLocks:
    """契约锁定：单类型失败无汇总、统一计时"""

    def test_single_type_failure_no_summary_abort_by_command(self, tmp_path):
        runner = CliRunner()
        config = _write_config(tmp_path)
        loader = _fail_loader("连接被拒绝")

        with mock.patch.object(
            loader_cli.LoaderFactory, "create", side_effect=_factory({"cql": loader})
        ):
            result = runner.invoke(
                loader_cli.rag_load_command, ["--type", "cql", "-c", config]
            )

        assert result.exit_code == 1
        assert SUMMARY_HEADING not in result.output
        assert "连接被拒绝" in result.output

    def test_execution_time_from_helper_perf_counter(self, tmp_path):
        """耗时来自 helper 统一 perf_counter，而非 loader 自报。"""
        config = _write_config(tmp_path)
        with (
            mock.patch.object(
                loader_cli.LoaderFactory,
                "create",
                side_effect=_factory({"cql": _ok_loader(execution_time=0.5)}),
            ),
            mock.patch.object(loader_cli.time, "perf_counter", side_effect=[0.0, 1.0]),
        ):
            out = loader_cli._run_single_load("cql", Path(config), False, False)

        assert out["success"] is True
        assert out["execution_time"] == 1.0
