"""clean 期望目标运行期复核测试（doc 26 §6.2/测试 45）。

覆盖：
- 目标一致：清理与业务执行使用同一快照；
- 目标不一致 / 无法解析：不调用任何清理方法、任务 failed；
- 独立 CLI（expected=None）：不执行比较；
- 身份规范化：port 整数/字符串/前导零、host 大小写与空白 → 同一身份；
  database/collection 大小写变化 → 判定不同目标；URI 展示脱敏。
"""

from unittest import mock

import pytest
import yaml

from metaweave.core.clean_targets import normalize_neo4j_uri, resolve_clean_target
from metaweave.core.metadata.execution import (
    MetadataExecutionRequest,
    execute_cql,
    verify_clean_target,
)


def _main_config(port=19530, host="localhost", db="nl2sql"):
    return {
        "database": {"database": "demo"},
        "output": {"output_dir": "output"},
        "loaders": {
            "sql_loader": {"collection_name": "sql_coll"},
            "cql_loader": {
                "input_file": "output/cql/import_all.*.cypher",
                "neo4j": {
                    "uri": "bolt://localhost:7687",
                    "user": "neo4j",
                    "password": "pw",
                    "database": "neo4j",
                },
            },
        },
        "vector_database": {
            "active": "milvus",
            # 与真实配置一致：host/port/database 均在 providers.milvus 下
            "providers": {"milvus": {"host": host, "port": port, "database": db}},
        },
    }


class TestIdentityNormalization:
    # rag_load sql 的 Milvus identity 用例已随 doc 25.2 CLI sql 移除而删除；
    # cql 侧（Neo4j 预警脱敏）保留。

    def test_neo4j_uri_sanitized(self):
        t = resolve_clean_target("rag_load", "cql", _main_config())
        assert "neo4j:pw" not in t["display"]
        assert "neo4j:pw" not in str(t["identity"])
        # 带 userinfo 的 URI 直接拒绝（驱动不支持；第四轮第 1 条）
        with pytest.raises(ValueError):
            normalize_neo4j_uri("bolt://neo4j:pw@localhost:7687")


class TestVerifyHelper:
    def _request(self, expected):
        return MetadataExecutionRequest(
            config_path="x.yaml",
            operation="cql",
            clean=True,
            expected_clean_target=expected,
        )

    def test_no_expected_no_comparison(self):
        assert (
            verify_clean_target(self._request(None), "metadata", _main_config()) is None
        )

    def test_matching_identity_passes(self):
        expected = resolve_clean_target("metadata", "cql", _main_config())
        assert (
            verify_clean_target(self._request(expected), "metadata", _main_config())
            is None
        )

    def test_changed_target_fails_without_leaking_secrets(self):
        # expected 是 ddl 目录；当前 operation=cql → 身份必不同
        expected = resolve_clean_target("metadata", "ddl", _main_config())
        msg = verify_clean_target(self._request(expected), "metadata", _main_config())
        assert msg is not None
        assert "未执行任何清理" in msg
        assert "pw" not in msg


class TestExecuteCqlVerification:
    def _run(self, expected, main_config, monkeypatch, tmp_path):
        cfg_path = tmp_path / "metadata_config.yaml"
        cfg_path.write_text("database:\n  database: demo\n", encoding="utf-8")

        cleaned = []
        monkeypatch.setattr(
            "metaweave.core.metadata.execution._clean_step_dir",
            lambda step, cfg: cleaned.append(step),
        )
        monkeypatch.setattr(
            "services.config_loader.load_config", lambda _p: main_config
        )

        from metaweave.core.cql_generator.models import CQLGenerationResult

        class _Dummy:
            def __init__(
                self, config_path, domain_resolver=None, config=None, provenance=None
            ):
                self.config = config or {}

            def generate(self, step_name="cql"):
                return CQLGenerationResult(success=True, output_files=[])

        monkeypatch.setattr(
            "metaweave.core.cql_generator.generator.CQLGenerator", _Dummy
        )
        req = MetadataExecutionRequest(
            config_path=cfg_path,
            operation="cql",
            clean=True,
            cql_source_id="db_src_1",
            expected_clean_target=expected,
        )
        result = execute_cql(req)
        return result, cleaned

    def test_matching_target_cleans_and_succeeds(self, tmp_path, monkeypatch):
        cfg = _main_config()
        expected = resolve_clean_target("metadata", "cql", cfg)
        with mock.patch("click.echo"):
            result, cleaned = self._run(expected, cfg, monkeypatch, tmp_path)
        assert result["success"] is True
        assert cleaned == ["cql"]  # 清理被调用

    def test_changed_target_does_not_clean_and_fails(self, tmp_path, monkeypatch):
        cfg = _main_config()
        expected = resolve_clean_target("metadata", "cql", cfg)
        # 任务开始后配置被修改：cql 目录变化
        changed = _main_config()
        changed["output"] = {"output_dir": "output", "cql_directory": "other/cql"}
        with mock.patch("click.echo"):
            result, cleaned = self._run(expected, changed, monkeypatch, tmp_path)
        assert result["success"] is False
        assert "未执行任何清理" in result["message"]
        assert cleaned == []  # 没有任何清理副作用


class TestProjectRootRejectedAsCleanTarget:
    """项目根目录本身不能作为 clean 目标（否则删除根目录顶层文件）。"""

    @pytest.mark.parametrize("value", [".", "", "./", "output/..", ".//.", "foo/../"])
    def test_ensure_rejects_project_root_itself(self, tmp_path, monkeypatch, value):
        from metaweave.utils.file_utils import (
            ensure_path_in_project_root,
            resolve_project_path,
        )

        monkeypatch.setattr(
            "metaweave.utils.file_utils.get_project_root", lambda: tmp_path
        )
        with pytest.raises(ValueError) as ei:
            ensure_path_in_project_root(resolve_project_path(value))
        assert "不能是项目根目录本身" in str(ei.value)

    def test_ensure_rejects_explicit_root_path(self, tmp_path, monkeypatch):
        from metaweave.utils.file_utils import ensure_path_in_project_root

        monkeypatch.setattr(
            "metaweave.utils.file_utils.get_project_root", lambda: tmp_path
        )
        with pytest.raises(ValueError):
            ensure_path_in_project_root(tmp_path)

    def test_ensure_still_allows_strict_subdir(self, tmp_path, monkeypatch):
        from metaweave.utils.file_utils import (
            ensure_path_in_project_root,
            resolve_project_path,
        )

        monkeypatch.setattr(
            "metaweave.utils.file_utils.get_project_root", lambda: tmp_path
        )
        assert (
            ensure_path_in_project_root(resolve_project_path("output/ddl"))
            == (tmp_path / "output" / "ddl").resolve()
        )

    def test_resolve_clean_target_rejects_root_values(self, tmp_path, monkeypatch):
        """配置层："." / 空 output_dir 等解析为项目根 → 拒绝。"""
        monkeypatch.setattr(
            "metaweave.utils.file_utils.get_project_root", lambda: tmp_path
        )
        # ddl_directory: "."
        with pytest.raises(ValueError):
            resolve_clean_target(
                "metadata",
                "ddl",
                {"output": {"output_dir": "output", "ddl_directory": "."}},
            )
        # generate-sql-rag output_dir: ""
        with pytest.raises(ValueError):
            resolve_clean_target(
                "metadata",
                "generate-sql-rag",
                {"sql_rag": {"generation": {"output_dir": ""}}},
            )


class TestCliEndToEndProtection:
    def test_metadata_clean_rejects_root_and_keeps_files(self, tmp_path, monkeypatch):
        """--step ddl --clean + ddl_directory '.' → 失败且根目录文件保留。"""
        from click.testing import CliRunner

        from metaweave.cli import metadata_cli

        marker = tmp_path / "pyproject.toml"
        marker.write_text("[project]\n", encoding="utf-8")
        (tmp_path / ".env").write_text("SECRET=1\n", encoding="utf-8")

        cfg_path = tmp_path / "metadata_config.yaml"
        cfg_path.write_text("database:\n  database: demo\n", encoding="utf-8")
        loaded = {
            "database": {"database": "demo"},
            "output": {"output_dir": "output", "ddl_directory": "."},
            "ddl_generation": {"comments": {"llm_enabled": False}},
        }
        monkeypatch.setattr("services.config_loader.load_config", lambda _p: loaded)
        monkeypatch.setattr(
            "metaweave.utils.file_utils.get_project_root", lambda: tmp_path
        )

        result = CliRunner().invoke(
            metadata_cli.metadata_command,
            ["--config", str(cfg_path), "--step", "ddl", "--clean"],
        )
        assert result.exit_code != 0
        # 关键断言：根目录顶层文件未被删除
        assert marker.exists()
        assert (tmp_path / ".env").exists()


class TestNeo4jUriValidation:
    """Neo4j URI 严格解析（用户审核第 2 条）：协议白名单/host/port，且不泄露凭据。"""

    def _resolve(self, uri):
        cfg = {"loaders": {"cql_loader": {"neo4j": {"uri": uri, "database": "neo4j"}}}}
        return resolve_clean_target("rag_load", "cql", cfg)

    @pytest.mark.parametrize(
        "uri",
        [
            "",
            "   ",
            "garbage",
            "http://h:1",
            "https://h:7687",
            "bolt://",
            "bolt://:7687",
            "neo4j:secret@localhost:7687",  # 无 //，原文含凭据——必须拒绝
            "bolt://h:0",
            "bolt://h:65536",
            "bolt://h:abc",
        ],
    )
    def test_invalid_uri_rejected_without_echoing_original(self, uri):
        with pytest.raises(ValueError) as ei:
            self._resolve(uri)
        msg = str(ei.value)
        # 错误消息不得回显原始 URI（可能含凭据）
        if uri.strip():
            assert uri.strip() not in msg
        assert "secret" not in msg

    @pytest.mark.parametrize(
        "uri,expected",
        [
            ("bolt://localhost:7687", "bolt://localhost:7687"),
            ("bolt://LOCALHOST:7687", "bolt://localhost:7687"),
            ("neo4j://h", "neo4j://h"),
            ("neo4j+s://h:7687", "neo4j+s://h:7687"),
            ("bolt+ssc://h:7688", "bolt+ssc://h:7688"),
            ("  bolt://h:7687  ", "bolt://h:7687"),
            ("bolt://h:7687/", "bolt://h:7687"),
            ("bolt+ssc://[::1]:7688", "bolt+ssc://[::1]:7688"),
        ],
    )
    def test_valid_uri_normalized(self, uri, expected):
        assert normalize_neo4j_uri(uri) == expected
        t = self._resolve(uri)
        assert t["display"].startswith(expected)

    def test_display_and_identity_use_normalized_only(self):
        t1 = self._resolve("bolt://HOST:7687")
        t2 = self._resolve("  bolt://host:7687  ")
        # 仅大小写/空白差异 → 同一身份（规范形式比较）
        assert t1["identity"] == t2["identity"]
        assert t1["display"] == t2["display"]


class TestConfigNodeTypeValidation:
    """clean 解析链上的配置节点类型错误 → 带路径的 ValueError（用户审核第 3 条）。

    不用 AttributeError 逃逸（那会以 exit 1 + traceback 结束，且可能掩盖代码错误）。
    """

    @pytest.mark.parametrize(
        "section,step,cfg,path",
        [
            ("rag_load", "cql", {"loaders": "bad"}, "loaders"),
            (
                "rag_load",
                "cql",
                {"loaders": {"cql_loader": "bad"}},
                "loaders.cql_loader",
            ),
            (
                "rag_load",
                "cql",
                {"loaders": {"cql_loader": {"neo4j": "bad"}}},
                "loaders.cql_loader.neo4j",
            ),
            ("metadata", "ddl", {"output": "bad"}, "output"),
            ("metadata", "generate-domains", {"output": "bad"}, "output"),
            ("metadata", "generate-sql-rag", {"sql_rag": "bad"}, "sql_rag"),
            (
                "metadata",
                "generate-sql-rag",
                {"sql_rag": {"generation": "bad"}},
                "sql_rag.generation",
            ),
        ],
    )
    def test_type_error_raises_pathful_value_error(self, section, step, cfg, path):
        with pytest.raises(ValueError) as ei:
            resolve_clean_target(section, step, cfg)
        msg = str(ei.value)
        assert path in msg
        assert "必须是对象" in msg


class TestFalsyValuesNotSilentlyDefaulted:
    """假值绕过修复（用户审核第三轮第 1 条）。

    键存在但值为 []/""/false/0/null 时不得被 `or {}`/falsy 判断静默当作
    "未配置"——尤其 output: [] + ddl clean 会清理默认目录。
    """

    @pytest.mark.parametrize(
        "section,step,cfg,frag",
        [
            ("metadata", "ddl", {"output": []}, "output 必须是对象"),
            (
                "metadata",
                "ddl",
                {"output": {"ddl_directory": []}},
                "output.ddl_directory",
            ),
            ("metadata", "ddl", {"output": {"output_dir": False}}, "output.output_dir"),
            ("metadata", "ddl", {"output": {"ddl_directory": ""}}, "不能为空字符串"),
            ("rag_load", "cql", {"loaders": []}, "loaders 必须是对象"),
            ("rag_load", "cql", {"loaders": False}, "loaders 必须是对象"),
            ("metadata", "generate-sql-rag", {"sql_rag": []}, "sql_rag 必须是对象"),
        ],
    )
    def test_falsy_bypass_rejected(self, section, step, cfg, frag):
        with pytest.raises(ValueError) as ei:
            resolve_clean_target(section, step, cfg)
        assert frag in str(ei.value)

    def test_key_missing_still_uses_default(self):
        """键真正缺失时才用缺省（不受本次收紧影响）。"""
        t = resolve_clean_target("metadata", "ddl", {})
        assert t["display"].endswith("/output/ddl")
        t2 = resolve_clean_target(
            "rag_load",
            "cql",
            {"loaders": {"cql_loader": {"neo4j": {"uri": "bolt://h"}}}},
        )
        assert "bolt://h" in t2["display"]

    def test_null_node_is_type_error(self):
        """显式 null 与键缺失区分：null 视为类型错误（节点层）。"""
        with pytest.raises(ValueError):
            resolve_clean_target("metadata", "ddl", {"output": None})


class TestFileUtilsResolversFalsyGuard:
    """file_utils 两个路径 resolver 的同类加固（clean 解析链共用）。"""

    def test_domains_resolver_rejects_falsy_output(self, tmp_path, monkeypatch):
        from metaweave.utils.file_utils import resolve_domains_yaml_path

        monkeypatch.setattr(
            "metaweave.utils.file_utils.get_project_root", lambda: tmp_path
        )
        with pytest.raises(ValueError):
            resolve_domains_yaml_path({"output": []}, None)
        with pytest.raises(ValueError):
            resolve_domains_yaml_path({"output": {"domains_directory": ""}}, None)

    def test_dim_resolver_rejects_falsy_loaders(self, tmp_path, monkeypatch):
        from metaweave.utils.file_utils import resolve_dim_tables_yaml_path

        monkeypatch.setattr(
            "metaweave.utils.file_utils.get_project_root", lambda: tmp_path
        )
        with pytest.raises(ValueError):
            resolve_dim_tables_yaml_path({"loaders": []}, None)
        with pytest.raises(ValueError):
            resolve_dim_tables_yaml_path({"loaders": {"dim_loader": []}}, None)
        with pytest.raises(ValueError):
            resolve_dim_tables_yaml_path(
                {"loaders": {"dim_loader": {"config_file": False}}}, None
            )

    def test_missing_keys_fall_back(self, tmp_path, monkeypatch):
        from metaweave.utils.file_utils import (
            resolve_dim_tables_yaml_path,
            resolve_domains_yaml_path,
        )

        monkeypatch.setattr(
            "metaweave.utils.file_utils.get_project_root", lambda: tmp_path
        )
        assert resolve_domains_yaml_path({}, None) == (
            tmp_path / "output" / "domains" / "db_domains.yaml"
        )
        assert resolve_dim_tables_yaml_path({}, None) == (
            tmp_path / "output" / "dim_config" / "dim_tables.yaml"
        )


class TestStandaloneCliSharesCleanResolver:
    """独立 CLI 的 clean 与 pipeline 共用同一套安全边界（用户审核第三轮第 3 条）。

    clean=false → 不解析；clean=true → 始终解析 current target
    （expected=None 时解析成功即通过，不比较 identity）。
    """

    @staticmethod
    def _env(tmp_path, cfg_dict):
        cfg = tmp_path / "metadata_config.yaml"
        cfg.write_text(yaml.safe_dump(cfg_dict), encoding="utf-8")
        return cfg

    def test_metadata_clean_rejects_falsy_output(self, tmp_path, monkeypatch):
        """独立 metadata --step ddl --clean + output: [] → 失败且不清理默认目录。"""
        from click.testing import CliRunner

        from metaweave.cli import metadata_cli

        (tmp_path / "output" / "ddl").mkdir(parents=True)
        old = tmp_path / "output" / "ddl" / "old.sql"
        old.write_text("-- old", encoding="utf-8")
        cfg = self._env(
            tmp_path,
            {
                "database": {"database": "demo"},
                "output": [],
                "ddl_generation": {"comments": {"llm_enabled": False}},
            },
        )
        monkeypatch.setattr(
            "metaweave.utils.file_utils.get_project_root", lambda: tmp_path
        )
        result = CliRunner().invoke(
            metadata_cli.metadata_command,
            ["--config", str(cfg), "--step", "ddl", "--clean"],
        )
        assert result.exit_code == 1
        assert "clean 当前目标无法解析" in result.output
        assert old.exists(), "解析失败时不得清理默认目录"

    @pytest.mark.parametrize(
        "load_type,loader_body,frag",
        [
            (
                "cql",
                {"cql_loader": {"neo4j": {"uri": "neo4j:secret@h:7687"}}},
                "Neo4j URI",
            ),
        ],
    )
    def test_rag_load_clean_rejects_invalid_target(
        self, load_type, loader_body, frag, tmp_path, monkeypatch
    ):
        from click.testing import CliRunner

        from metaweave.cli import loader_cli

        cfg = self._env(
            tmp_path,
            {
                "database": {"database": "demo"},
                "vector_database": {
                    "active": "milvus",
                    "providers": {"milvus": {"host": "h", "port": 19530}},
                },
                "loaders": loader_body,
            },
        )
        monkeypatch.setattr(
            "metaweave.utils.file_utils.get_project_root", lambda: tmp_path
        )
        result = CliRunner().invoke(
            loader_cli.rag_load_command,
            ["--type", load_type, "-c", str(cfg), "--clean"],
        )
        assert result.exit_code == 1
        # doc 4 §4.4：cql 不再解析 clean 目标（按源替换，无整库清空）；
        # 非法目标改由 loader 配置校验拒绝，仍不泄露凭据
        assert frag in result.output
        assert "secret" not in result.output  # 不泄露凭据

    def test_standalone_valid_config_still_cleans(self, tmp_path, monkeypatch):
        """合法配置的独立 CLI clean 不受影响（解析成功即通过）。"""
        from click.testing import CliRunner

        from metaweave.cli import metadata_cli

        (tmp_path / "out" / "ddl").mkdir(parents=True)
        old = tmp_path / "out" / "ddl" / "old.sql"
        old.write_text("-- old", encoding="utf-8")
        cfg = self._env(
            tmp_path,
            {
                "database": {"database": "demo"},
                "output": {"output_dir": "out", "ddl_directory": "out/ddl"},
                "ddl_generation": {"comments": {"llm_enabled": False}},
            },
        )
        monkeypatch.setattr(
            "metaweave.utils.file_utils.get_project_root", lambda: tmp_path
        )
        monkeypatch.setattr(
            "metaweave.core.metadata.generator.MetadataGenerator",
            __import__(
                "tests.unit.test_single_config_snapshot", fromlist=["_DummyGenerator"]
            )._DummyGenerator,
        )
        result = CliRunner().invoke(
            metadata_cli.metadata_command,
            ["--config", str(cfg), "--step", "ddl", "--clean"],
        )
        assert result.exit_code == 0, result.output
        assert not old.exists(), "合法路径下 clean 正常执行"

    def test_clean_false_skips_resolution(self, tmp_path, monkeypatch):
        """clean=false 时不触碰 resolver：非法目标配置在无 clean 时不影响生成。"""
        from click.testing import CliRunner

        from metaweave.cli import metadata_cli

        cfg = self._env(
            tmp_path,
            {
                "database": {"database": "demo"},
                "output": [],
                "ddl_generation": {"comments": {"llm_enabled": False}},
            },
        )
        monkeypatch.setattr(
            "metaweave.utils.file_utils.get_project_root", lambda: tmp_path
        )
        monkeypatch.setattr(
            "metaweave.core.metadata.generator.MetadataGenerator",
            __import__(
                "tests.unit.test_single_config_snapshot", fromlist=["_DummyGenerator"]
            )._DummyGenerator,
        )
        result = CliRunner().invoke(
            metadata_cli.metadata_command,
            ["--config", str(cfg), "--step", "ddl"],
        )
        # 无 clean：resolver 不参与（生成侧对 output 假值另行处理，这里仅断言未走 clean 校验路径）
        assert "clean 当前目标无法解析" not in result.output

    def test_domains_config_override_used_for_resolution(self, tmp_path, monkeypatch):
        """显式 --domains-config：resolver 用 override 解析（与实际 clean 目标一致）。"""
        from click.testing import CliRunner

        from metaweave.cli import metadata_cli

        custom_dir = tmp_path / "custom_domains"
        custom_dir.mkdir()
        (custom_dir / "old.yaml").write_text("x: 1", encoding="utf-8")
        cfg_path = tmp_path / "metadata_config.yaml"
        cfg_path.write_text(
            yaml.safe_dump({"database": {"database": "demo"}}), encoding="utf-8"
        )
        monkeypatch.setattr(
            "metaweave.utils.file_utils.get_project_root", lambda: tmp_path
        )
        monkeypatch.setattr(
            "services.config_loader.load_config", lambda _p: {"output": {}}
        )

        class _DummyDomainGen:
            def __init__(self, **kw):
                pass

            def generate_from_context(self, user_description=None):
                return {"database": {"name": "demo"}, "domains": []}

            def write_to_yaml(self, _g):
                return [{"name": "_未分类_"}]

        monkeypatch.setattr(
            "metaweave.core.metadata.domain_generator.DomainGenerator", _DummyDomainGen
        )
        result = CliRunner().invoke(
            metadata_cli.metadata_command,
            [
                "--config",
                str(cfg_path),
                "--generate-domains",
                "--clean",
                "--domains-config",
                str(custom_dir / "db_domains.yaml"),
            ],
        )
        assert result.exit_code == 0, result.output
        assert not (custom_dir / "old.yaml").exists(), "override 目录被清理（解析一致）"


class TestNeo4jUriContractMatchesLoader:
    """resolver 的 uri 契约与 CQLLoader 一致（用户审核第三轮第 4 条）。

    CQLLoader._get_neo4j_config 直接取 neo4j_section["uri"]（必填），
    resolver 不得虚构默认值——否则计划阶段"合法"、任务阶段才失败。
    """

    @pytest.mark.parametrize(
        "neo_cfg",
        [
            {},  # uri 缺失
            {"uri": ""},  # 空串
            {"database": "neo4j"},  # 缺失但其他键存在
        ],
    )
    def test_missing_or_blank_uri_is_config_error(self, neo_cfg):
        cfg = {"loaders": {"cql_loader": {"neo4j": neo_cfg}}}
        with pytest.raises(ValueError) as ei:
            resolve_clean_target("rag_load", "cql", cfg)
        assert "neo4j.uri" in str(ei.value)


class TestNeo4jUriNotLeakedInLogs:
    """连接日志不泄露敏感信息——执行真实生产路径（用户审核第四轮第 4 条）：
    CQLLoader.validate() 与 Neo4jConnectionManager.initialize() 本体，
    而非复刻逻辑或源码字符串检查。
    """

    def test_cql_loader_construct_rejects_illegal_uri(self, tmp_path):
        """构造期强制校验（第五轮第 1 条）：非法 uri 在任何入口一致拒绝。"""
        from metaweave.core.loaders.cql_loader import CQLLoader

        cql_file = tmp_path / "import_all.demo.cypher"
        cql_file.write_text("// x", encoding="utf-8")
        for bad in (
            "bolt://h:7687?token=secret_token_xyz",
            "neo4j://h:7687?policy=blue",
            "bolt://u:pw@h:7687",
        ):
            cfg = {
                "cql_loader": {
                    "input_file": str(cql_file),
                    "neo4j": {
                        "uri": bad,
                        "user": "neo4j",
                        "password": "secret_pw_abc",
                        "database": "neo4j",
                    },
                },
                "metadata_config_file": str(tmp_path / "c.yaml"),
            }
            with pytest.raises(ValueError) as ei:
                CQLLoader(cfg)
            assert "neo4j.uri 非法" in str(ei.value)
            assert "secret_token_xyz" not in str(ei.value)
            assert "pw@" not in str(ei.value)

    def test_cql_loader_failure_log_redacted(self, tmp_path, caplog, monkeypatch):
        """真实调用 validate()：连接异常中的 URI 与密码均被脱敏。"""
        import logging

        from metaweave.core.loaders.cql_loader import CQLLoader

        cql_file = tmp_path / "import_all.demo.cypher"
        cql_file.write_text("// x", encoding="utf-8")
        cfg = {
            "cql_loader": {
                "input_file": str(cql_file),
                "neo4j": {
                    "uri": "bolt://h:7687",
                    "user": "neo4j",
                    "password": "secret_pw_abc",
                    "database": "neo4j",
                },
            },
            "metadata_config_file": str(tmp_path / "c.yaml"),
        }

        class _BoomManager:
            def __init__(self, _cfg):
                pass

            def initialize(self):
                raise RuntimeError(
                    "Given URI: 'bolt://h:7687?token=TOP_SECRET' "
                    "(auth secret_pw_abc)"
                )

            def test_connection(self):
                return True

        monkeypatch.setattr(
            "metaweave.core.loaders.cql_loader.Neo4jConnectionManager",
            _BoomManager,
        )
        loader = CQLLoader(cfg)
        with caplog.at_level(logging.ERROR):
            assert loader.validate() is False

        assert "TOP_SECRET" not in caplog.text
        assert "secret_pw_abc" not in caplog.text
        assert "<redacted-uri>" in caplog.text
        assert "***" in caplog.text

    def test_neo4j_connection_initialize_real_path_no_leak(self, capsys, monkeypatch):
        """真实调用 Neo4jConnectionManager.initialize()：stdout 不含 URI/凭据。

        mock GraphDatabase.driver 以避免真连接；断言 driver 仍收到原始
        uri（功能不受影响），但终端输出零泄露。
        """
        from services.db import neo4j_connection

        received = {}

        def _fake_driver(uri, **kwargs):
            received["uri"] = uri
            received["auth"] = kwargs.get("auth")
            return mock.MagicMock()

        monkeypatch.setattr(
            "services.db.neo4j_connection.GraphDatabase.driver", _fake_driver
        )

        mgr = neo4j_connection.Neo4jConnectionManager(
            {
                "uri": "bolt://h:7687?token=secret_token_xyz",
                "user": "neo4j",
                "password": "secret_pw_abc",
                "database": "neo4j",
            }
        )
        mgr.initialize()

        out = capsys.readouterr().out
        assert "secret_token_xyz" not in out
        assert "secret_pw_abc" not in out
        assert "bolt://h" not in out
        assert "驱动已初始化" in out
        # 驱动仍收到原始 uri 与凭据（功能不受影响）
        assert received["uri"] == "bolt://h:7687?token=secret_token_xyz"
        assert received["auth"] == ("neo4j", "secret_pw_abc")


class TestNeo4jUriStrictBoundary:
    """URI 边界收紧（用户审核第四轮第 1 条）。

    - userinfo：驱动拒绝（Username is not supported in the URI）；
    - query：neo4j:// 的 query 是 routing context（驱动 parse_routing_context
      解析），具业务语义——剥离会让不同 routing context 产生相同 identity；
    - path / fragment：驱动不使用。
    以上 v1 一律拒绝，凭据只经 user/password 配置。
    """

    @pytest.mark.parametrize(
        "uri",
        [
            "bolt://u:pw@h:7687",  # userinfo（密码）
            "bolt://u@h:7687",  # userinfo（仅用户名）
            "neo4j://h:7687?policy=blue",  # routing context 查询参数
            "bolt://h:7687?x=1",  # 任意查询参数
            "bolt://h:7687/db",  # path
            "bolt://h:7687#frag",  # fragment
        ],
    )
    def test_boundary_values_rejected(self, uri):
        with pytest.raises(ValueError):
            resolve_clean_target(
                "rag_load",
                "cql",
                {"loaders": {"cql_loader": {"neo4j": {"uri": uri}}}},
            )

    def test_different_routing_contexts_not_confused(self):
        """policy=blue 与 policy=green 均被拒绝（不再产生相同 identity）。"""
        for policy in ("blue", "green"):
            with pytest.raises(ValueError):
                resolve_clean_target(
                    "rag_load",
                    "cql",
                    {
                        "loaders": {
                            "cql_loader": {
                                "neo4j": {"uri": f"neo4j://h:7687?policy={policy}"}
                            }
                        }
                    },
                )

    def test_credentials_from_separate_config(self):
        """凭据经 user/password 配置时 URI 合法（driver 认证走 auth 参数）。"""
        t = resolve_clean_target(
            "rag_load",
            "cql",
            {
                "loaders": {
                    "cql_loader": {
                        "neo4j": {"uri": "bolt://h:7687", "user": "u", "password": "p"}
                    }
                }
            },
        )
        assert t["display"] == "bolt://h:7687 / database=neo4j"
        assert "p" not in str(t["identity"]).replace("bolt://h:7687", "")

    def test_trailing_slash_tolerated(self):
        assert normalize_neo4j_uri("bolt://h:7687/") == "bolt://h:7687"


class TestNullValuesRejected:
    """键存在且值为 null → 拒绝（用户审核第四轮第 2 条两处缺口）。

    - get_optional_str 曾把"键缺失"与"显式 null"都返回 None：
      `ddl_directory: null` 回退并清理默认目录；`neo4j.database: null`
      使预警显示 database=neo4j，而驱动实际使用服务端默认库；
    - _normalize_host 曾 str() 强转：host: []/false/0/字典被规范化为
      "[]"/"false"/"0"/字典字符串。
    """

    @pytest.mark.parametrize(
        "section,step,cfg,frag",
        [
            ("metadata", "ddl", {"output": {"ddl_directory": None}}, "ddl_directory"),
            ("metadata", "ddl", {"output": {"output_dir": None}}, "output_dir"),
            (
                "metadata",
                "generate-sql-rag",
                {"sql_rag": {"generation": {"output_dir": None}}},
                "output_dir",
            ),
            (
                "rag_load",
                "cql",
                {
                    "loaders": {
                        "cql_loader": {
                            "neo4j": {"uri": "bolt://h:7687", "database": None}
                        }
                    }
                },
                "database",
            ),
        ],
    )
    def test_null_path_values_rejected(self, section, step, cfg, frag):
        with pytest.raises(ValueError) as ei:
            resolve_clean_target(section, step, cfg)
        assert frag in str(ei.value)
        assert "null" in str(ei.value).lower() or "不能为空" in str(ei.value)

    def test_key_missing_still_uses_default(self):
        """键缺失（非 null）仍走缺省——区分语义不受影响。"""
        t = resolve_clean_target("metadata", "ddl", {"output": {}})
        assert t["display"].endswith("/output/ddl")

    def test_neo4j_database_matches_loader_semantics(self):
        """合法 database 时预警值与 CQLLoader.get 语义一致（同值）。"""
        t = resolve_clean_target(
            "rag_load",
            "cql",
            {
                "loaders": {
                    "cql_loader": {
                        "neo4j": {"uri": "bolt://h:7687", "database": "mydb"}
                    }
                }
            },
        )
        assert "database=mydb" in t["display"]


class TestRagLoadResolverBeforeLoaderDerivation:
    """resolver 先于 loaders 派生/操作（用户审核第四轮第 3 条）。

    clean=true 时须先经共享 resolver——loaders 形态非法（[]）由其给出带
    完整配置路径的友好错误，而非 deepcopy 后的原始 TypeError；
    clean=false 路径同样补形态校验（友好失败）。
    """

    @staticmethod
    def _cfg(tmp_path, loaders):
        cfg = tmp_path / "c.yaml"
        cfg.write_text(
            yaml.safe_dump(
                {
                    "database": {"database": "demo"},
                    "loaders": loaders,
                }
            ),
            encoding="utf-8",
        )
        return cfg

    @pytest.mark.parametrize(
        "loaders,frag",
        [
            ([], "loaders 必须是对象"),
            ({"cql_loader": []}, "loaders.cql_loader 必须是对象"),
        ],
    )
    def test_clean_true_uses_resolver_error(self, tmp_path, monkeypatch, loaders, frag):
        from click.testing import CliRunner

        from metaweave.cli import loader_cli

        cfg = self._cfg(tmp_path, loaders)
        monkeypatch.setattr(
            "metaweave.utils.file_utils.get_project_root", lambda: tmp_path
        )
        result = CliRunner().invoke(
            loader_cli.rag_load_command, ["--type", "cql", "-c", str(cfg), "--clean"]
        )
        assert result.exit_code == 1
        # doc 4 §4.4：cql 不再走 clean 目标解析；形态错误由配置校验友好报出
        assert "配置错误" in result.output and frag in result.output
        # 不得出现原始 TypeError/AttributeError 文本
        assert "list indices must be integers" not in result.output
        assert "object has no attribute 'get'" not in result.output

    @pytest.mark.parametrize(
        "loaders,frag",
        [
            ([], "loaders 必须是对象"),
            ({"cql_loader": []}, "loaders.cql_loader 必须是对象"),
        ],
    )
    def test_clean_false_also_friendly(self, tmp_path, monkeypatch, loaders, frag):
        """无 clean 路径同样友好失败（形态校验在 deepcopy 之前）。"""
        from click.testing import CliRunner

        from metaweave.cli import loader_cli

        cfg = self._cfg(tmp_path, loaders)
        monkeypatch.setattr(
            "metaweave.utils.file_utils.get_project_root", lambda: tmp_path
        )
        result = CliRunner().invoke(
            loader_cli.rag_load_command, ["--type", "cql", "-c", str(cfg)]
        )
        assert result.exit_code == 1
        assert "配置错误" in result.output and frag in result.output

    def test_valid_loaders_still_work(self, tmp_path, monkeypatch):
        """合法 loaders 回归：clean=true 正常执行。"""
        from click.testing import CliRunner

        from metaweave.cli import loader_cli

        cfg = self._cfg(
            tmp_path,
            {
                "cql_loader": {"neo4j": {"uri": "bolt://localhost:7687"}},
            },
        )
        monkeypatch.setattr(
            "metaweave.utils.file_utils.get_project_root", lambda: tmp_path
        )

        class _OkLoader:
            def __init__(self, _c, metadata_config=None):
                pass

            def validate(self):
                return True

            def load(self, clean=False):
                return {"success": True, "message": "ok", "execution_time": 0.1}

        monkeypatch.setattr(
            "metaweave.core.loaders.factory.LoaderFactory.create",
            lambda t, c, metadata_config=None: _OkLoader(c),
        )
        result = CliRunner().invoke(
            loader_cli.rag_load_command, ["--type", "cql", "-c", str(cfg), "--clean"]
        )
        assert result.exit_code == 0, result.output


class TestNeo4jBoundaryConsistentAcrossEntries:
    """严格边界对 clean/non-clean 一致的端到端验证（用户审核第五轮第 1 条）。"""

    @pytest.mark.parametrize("with_clean", [True, False])
    @pytest.mark.parametrize(
        "bad_uri",
        [
            "neo4j://host:7687?policy=blue",  # query
            "bolt://host:7687/db",  # path
            "bolt://u:pw@host:7687",  # userinfo
        ],
    )
    def test_cli_rejects_same_illegal_uri_regardless_of_clean(
        self, tmp_path, monkeypatch, with_clean, bad_uri
    ):
        """三类非法 uri（query/path/userinfo）：有/无 --clean 均拒绝。"""
        from click.testing import CliRunner

        from metaweave.cli import loader_cli

        cql_file = tmp_path / "import_all.demo.cypher"
        cql_file.write_text("// x", encoding="utf-8")
        cfg = tmp_path / "c.yaml"
        cfg.write_text(
            yaml.safe_dump(
                {
                    "database": {"database": "demo"},
                    "loaders": {
                        "cql_loader": {
                            "input_file": str(cql_file),
                            "neo4j": {
                                "uri": bad_uri,
                                "user": "u",
                                "password": "pw",
                                "database": "neo4j",
                            },
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr(
            "metaweave.utils.file_utils.get_project_root", lambda: tmp_path
        )
        args = ["--type", "cql", "-c", str(cfg)] + (["--clean"] if with_clean else [])
        result = CliRunner().invoke(loader_cli.rag_load_command, args)
        assert result.exit_code == 1
        # 拒绝来源随入口而异（clean 由 resolver 先拦、无 clean 由 CQLLoader
        # 构造期拦），但"拒绝同一配置"的边界一致；敏感值不回显
        assert "neo4j.uri 非法" in result.output or "Neo4j URI 不允许" in result.output
        assert "policy=blue" not in result.output
        assert "pw@host" not in result.output

    def test_valid_uri_passes_both_modes(self, tmp_path, monkeypatch):
        """合法 uri：有/无 --clean 均正常（功能不受影响）。"""
        from click.testing import CliRunner

        from metaweave.cli import loader_cli

        cql_file = tmp_path / "import_all.demo.cypher"
        cql_file.write_text("// x", encoding="utf-8")
        cfg = tmp_path / "c.yaml"
        cfg.write_text(
            yaml.safe_dump(
                {
                    "database": {"database": "demo"},
                    "loaders": {
                        "cql_loader": {
                            "input_file": str(cql_file),
                            "neo4j": {
                                "uri": "bolt://localhost:7687",
                                "user": "u",
                                "password": "pw",
                                "database": "neo4j",
                            },
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr(
            "metaweave.utils.file_utils.get_project_root", lambda: tmp_path
        )

        class _OkLoader:
            def __init__(self, _c, metadata_config=None):
                pass

            def validate(self):
                return True

            def load(self, clean=False):
                return {"success": True, "message": "ok", "execution_time": 0.1}

        monkeypatch.setattr(
            "metaweave.core.loaders.factory.LoaderFactory.create",
            lambda t, c, metadata_config=None: _OkLoader(c),
        )
        for extra in ([], ["--clean"]):
            result = CliRunner().invoke(
                loader_cli.rag_load_command, ["--type", "cql", "-c", str(cfg)] + extra
            )
            assert result.exit_code == 0, result.output


class TestRagLoadMainConfigShapeValidation:
    """主配置与依赖节点形态统一校验（用户审核第五轮第 2 条）。

    空文件（None）、顶层列表——此前均抛原始 TypeError/AttributeError。
    sql 专属的 database/sql_rag 节点校验用例已随 doc 25.2 CLI sql 移除而删除。
    """

    @pytest.mark.parametrize(
        "body,frag",
        [
            ("", "主配置必须是对象"),
            ("- x\n", "主配置必须是对象"),
        ],
    )
    def test_shape_errors_are_structured(self, tmp_path, monkeypatch, body, frag):
        from click.testing import CliRunner

        from metaweave.cli import loader_cli

        cfg = tmp_path / "c.yaml"
        cfg.write_text(body, encoding="utf-8")
        monkeypatch.setattr(
            "metaweave.utils.file_utils.get_project_root", lambda: tmp_path
        )
        result = CliRunner().invoke(
            loader_cli.rag_load_command, ["--type", "cql", "-c", str(cfg)]
        )
        assert result.exit_code == 1
        assert "配置错误" in result.output and frag in result.output
        # 不得出现原始 Python 异常文本
        assert "NoneType' is not iterable" not in result.output
        assert "object has no attribute 'get'" not in result.output

    def test_resolver_also_friendly_with_clean(self, tmp_path, monkeypatch):
        """clean=true 时同样结构化（主配置 None 由形态校验拦截，doc 4 后
        cql 不再走 clean 目标解析）。"""
        from click.testing import CliRunner

        from metaweave.cli import loader_cli

        cfg = tmp_path / "c.yaml"
        cfg.write_text("", encoding="utf-8")
        monkeypatch.setattr(
            "metaweave.utils.file_utils.get_project_root", lambda: tmp_path
        )
        result = CliRunner().invoke(
            loader_cli.rag_load_command, ["--type", "cql", "-c", str(cfg), "--clean"]
        )
        assert result.exit_code == 1
        assert "配置错误" in result.output
        assert "主配置" in result.output and "必须是对象" in result.output

    def test_valid_config_regression(self, tmp_path, monkeypatch):
        """合法配置不受影响。"""
        from click.testing import CliRunner

        from metaweave.cli import loader_cli

        cfg = tmp_path / "c.yaml"
        cfg.write_text(
            yaml.safe_dump(
                {
                    "database": {"database": "demo"},
                    "loaders": {
                        "cql_loader": {"neo4j": {"uri": "bolt://localhost:7687"}}
                    },
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr(
            "metaweave.utils.file_utils.get_project_root", lambda: tmp_path
        )

        class _OkLoader:
            def __init__(self, _c, metadata_config=None):
                pass

            def validate(self):
                return True

            def load(self, clean=False):
                return {"success": True, "message": "ok", "execution_time": 0.1}

        monkeypatch.setattr(
            "metaweave.core.loaders.factory.LoaderFactory.create",
            lambda t, c, metadata_config=None: _OkLoader(c),
        )
        result = CliRunner().invoke(
            loader_cli.rag_load_command, ["--type", "cql", "-c", str(cfg)]
        )
        assert result.exit_code == 0, result.output


class TestRealDriverFailurePathNoLeak:
    """真实驱动（不 mock 任何组件）的失败路径不泄露（用户审核第五轮第 3 条）。

    此前失败路径测试的异常是人工构造的（_BoomManager）；本组走真实
    CQLLoader + 真实 Neo4jConnectionManager/驱动。
    """

    @pytest.mark.parametrize(
        "bad_uri",
        [
            "bolt://h:7687?token=TOP_SECRET",
            "neo4j://h:7687?policy=TOP_SECRET",
            "bolt://u:TOP_SECRET@h:7687",
        ],
    )
    def test_real_construction_rejects_illegal_uri_without_leak(
        self, tmp_path, bad_uri
    ):
        """真实构造（无 mock）：非法 uri 在边界层被拒，异常与日志无敏感值。"""
        from metaweave.core.loaders.cql_loader import CQLLoader

        cql_file = tmp_path / "import_all.demo.cypher"
        cql_file.write_text("// x", encoding="utf-8")
        cfg = {
            "cql_loader": {
                "input_file": str(cql_file),
                "neo4j": {
                    "uri": bad_uri,
                    "user": "u",
                    "password": "REAL_PW_XYZ",
                    "database": "neo4j",
                },
            },
            "metadata_config_file": str(tmp_path / "c.yaml"),
        }
        with pytest.raises(ValueError) as ei:
            CQLLoader(cfg)
        assert "TOP_SECRET" not in str(ei.value)
        assert "REAL_PW_XYZ" not in str(ei.value)

    def test_real_driver_failure_log_no_leak(self, tmp_path, caplog, capsys):
        """真实驱动连接不可达端口：失败输出（logger + stdout）无敏感值与密码。

        真实驱动异常经 Neo4jConnectionManager 的 print（stdout）与
        CQLLoader 的 logger 两路输出，两路都要断言。
        """
        import logging

        from metaweave.core.loaders.cql_loader import CQLLoader

        cql_file = tmp_path / "import_all.demo.cypher"
        cql_file.write_text("// x", encoding="utf-8")
        cfg = {
            "cql_loader": {
                "input_file": str(cql_file),
                "neo4j": {
                    # 合法 URI（无可泄露内容），连不可达端口触发真实驱动异常
                    "uri": "bolt://127.0.0.1:1",
                    "user": "u",
                    "password": "REAL_PW_XYZ",
                    "database": "neo4j",
                },
            },
            "metadata_config_file": str(tmp_path / "c.yaml"),
        }
        loader = CQLLoader(cfg)
        with caplog.at_level(logging.ERROR):
            assert loader.validate() is False
        combined = caplog.text + capsys.readouterr().out
        assert "REAL_PW_XYZ" not in combined
        assert "token" not in combined
        # 真实驱动路径确实被执行（连接测试失败消息出现）
        assert "连接测试失败" in combined or "Neo4j 连接失败" in combined


class TestWarningIdentityMatchesLoaderParams:
    """预警 identity 与 loader 实际连接参数一致（用户审核第六轮第 1 条）。

    预警侧对 host/database 做 strip/lower，但 loader 曾使用原值——
    `database: " neo4j "` 等配置下预警与实际执行不同。修复后 loader 侧
    统一同一套规范化。
    """

    def test_milvus_client_normalizes_host_and_database(self):
        from services.vector_db.milvus_client import MilvusClient

        client = MilvusClient(
            {"host": " LOCALHOST ", "port": 19530, "database": " nl2sql "}
        )
        assert client.config["host"] == "localhost"
        assert client.config["database"] == "nl2sql"

    def test_milvus_client_does_not_mutate_caller_dict(self):
        """规范化在副本上进行，不污染调用方配置（各 loader 共享引用）。"""
        from services.vector_db.milvus_client import MilvusClient

        original = {"host": " H ", "database": " D "}
        MilvusClient(original)
        assert original == {"host": " H ", "database": " D "}

    def test_neo4j_database_normalized_in_loader(self, tmp_path):
        from metaweave.core.loaders.cql_loader import CQLLoader

        cql = tmp_path / "x.cypher"
        cql.write_text("// x", encoding="utf-8")
        cfg = {
            "cql_loader": {
                "input_file": str(cql),
                "neo4j": {
                    "uri": "bolt://h:7687",
                    "user": "u",
                    "password": "p",
                    "database": " neo4j ",
                },
            },
            "metadata_config_file": str(tmp_path / "c.yaml"),
        }
        loader = CQLLoader(cfg)
        assert loader.neo4j_config["database"] == "neo4j"

        ident = resolve_clean_target(
            "rag_load", "cql", {"loaders": {"cql_loader": cfg["cql_loader"]}}
        )["identity"]
        assert ident[1] == loader.neo4j_config["database"]

    def test_neo4j_default_database_uses_loader_semantics(self, tmp_path):
        """缺省/空白 database → 'neo4j'（loader 与预警同值）。"""
        from metaweave.core.loaders.cql_loader import CQLLoader

        cql = tmp_path / "x.cypher"
        cql.write_text("// x", encoding="utf-8")
        for db_value in (None,):
            cfg = {
                "cql_loader": {
                    "input_file": str(cql),
                    "neo4j": {"uri": "bolt://h:7687", "user": "u", "password": "p"},
                },
                "metadata_config_file": str(tmp_path / "c.yaml"),
            }
            loader = CQLLoader(cfg)
            assert loader.neo4j_config["database"] == "neo4j"


class TestPortErrorNoEcho:
    """非法端口的错误消息不得回显端口位置的原始文本（用户审核第六轮第 2 条）。"""

    def test_resolver_port_error_no_echo(self):
        with pytest.raises(ValueError) as ei:
            resolve_clean_target(
                "rag_load",
                "cql",
                {
                    "loaders": {
                        "cql_loader": {"neo4j": {"uri": "bolt://localhost:TOP_SECRET"}}
                    }
                },
            )
        msg = str(ei.value)
        assert "TOP_SECRET" not in msg
        assert "1–65535" in msg or "1..65535" in msg or "整数" in msg

    @pytest.mark.parametrize("with_clean", [True, False])
    def test_cli_port_error_no_echo(self, tmp_path, monkeypatch, with_clean):
        """命令级：有/无 --clean 的输出均不含端口位置的敏感文本。"""
        from click.testing import CliRunner

        from metaweave.cli import loader_cli

        cfg = tmp_path / "c.yaml"
        cfg.write_text(
            yaml.safe_dump(
                {
                    "database": {"database": "demo"},
                    "loaders": {
                        "cql_loader": {
                            "neo4j": {
                                "uri": "bolt://localhost:TOP_SECRET",
                                "user": "u",
                                "password": "p",
                                "database": "neo4j",
                            }
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr(
            "metaweave.utils.file_utils.get_project_root", lambda: tmp_path
        )
        args = ["--type", "cql", "-c", str(cfg)] + (["--clean"] if with_clean else [])
        result = CliRunner().invoke(loader_cli.rag_load_command, args)
        assert result.exit_code == 1
        assert "TOP_SECRET" not in result.output
        assert "端口非法" in result.output


_MISSING = object()


class TestInvalidDatabaseRejectedInLoader:
    """非法 database 值在 loader 侧报错，不再静默用默认库（用户审核第六轮第 3 条）。

    统一规则：只有**键缺失**才用默认库；键存在时必须为非空字符串。
    此前非 clean 加载会把 null/123/[]/false/空白静默改成默认库，
    可能向默认库写入数据。
    """

    @staticmethod
    def _cql_cfg(tmp_path, database_sentinel):
        cql = tmp_path / "x.cypher"
        cql.write_text("// x", encoding="utf-8")
        neo = {"uri": "bolt://h:7687", "user": "u", "password": "p"}
        if database_sentinel is not _MISSING:
            neo["database"] = database_sentinel
        return {
            "cql_loader": {"input_file": str(cql), "neo4j": neo},
            "metadata_config_file": str(tmp_path / "c.yaml"),
        }

    @pytest.mark.parametrize("bad", [None, 123, [], False, "   "])
    def test_cql_loader_rejects_invalid_database(self, tmp_path, bad):
        from metaweave.core.loaders.cql_loader import CQLLoader

        with pytest.raises(ValueError) as ei:
            CQLLoader(self._cql_cfg(tmp_path, bad))
        assert "database 必须是非空字符串或省略该键" in str(ei.value)

    def test_cql_loader_missing_key_uses_default(self, tmp_path):
        from metaweave.core.loaders.cql_loader import CQLLoader

        loader = CQLLoader(self._cql_cfg(tmp_path, _MISSING))
        assert loader.neo4j_config["database"] == "neo4j"

    @pytest.mark.parametrize("bad", [None, 123, [], False, "   "])
    def test_milvus_client_rejects_invalid_database(self, bad):
        from services.vector_db.milvus_client import MilvusClient

        with pytest.raises(ValueError) as ei:
            MilvusClient({"host": "h", "database": bad})
        assert "Milvus database 必须是非空字符串或省略该键" in str(ei.value)

    def test_milvus_missing_key_means_no_switch(self):
        from services.vector_db.milvus_client import MilvusClient

        assert MilvusClient({"host": "h"}).config.get("database") is None

    @pytest.mark.parametrize("with_clean", [True, False])
    @pytest.mark.parametrize(
        "load_type,loader_body,frag",
        [
            (
                "cql",
                {
                    "cql_loader": {
                        "neo4j": {
                            "uri": "bolt://h:7687",
                            "user": "u",
                            "password": "p",
                            "database": [],
                        }
                    }
                },
                "database 必须是非空字符串",
            ),
        ],
    )
    def test_cli_clean_and_non_clean_consistent(
        self, tmp_path, monkeypatch, with_clean, load_type, loader_body, frag
    ):
        """命令级对照：非法 database 有/无 --clean 均失败（不再 only-clean 拒绝）。"""
        from click.testing import CliRunner

        from metaweave.cli import loader_cli

        cfg_dict = {"database": {"database": "demo"}, "loaders": loader_body}
        cfg = tmp_path / "c.yaml"
        cfg.write_text(yaml.safe_dump(cfg_dict), encoding="utf-8")
        monkeypatch.setattr(
            "metaweave.utils.file_utils.get_project_root", lambda: tmp_path
        )
        args = ["--type", load_type, "-c", str(cfg)] + (
            ["--clean"] if with_clean else []
        )
        result = CliRunner().invoke(loader_cli.rag_load_command, args)
        assert result.exit_code == 1
        # 拒绝来源随入口/时机而异：clean 由 resolver 先拦；非 clean 时
        # CQLLoader 构造直接抛错、MilvusClient 在 loader.validate() 内抛错
        # （validate 捕获后返回 False → "验证失败"）
        assert (
            frag in result.output
            or "clean 当前目标无法解析" in result.output
            or "验证失败" in result.output
        )


class TestMilvusConfigShapeRejected:
    """Milvus 配置必须为 mapping（用户审核第七轮第 1 条）。

    `dict(config or {})` 曾把 []/None/false 静默变 {}，使加载器以默认
    host/port/database "验证通过"；resolver 却会拒绝同一配置。
    """

    @pytest.mark.parametrize("bad", [[], None, False, "x", 0, 1.5])
    def test_milvus_client_rejects_non_mapping(self, bad):
        from services.vector_db.milvus_client import MilvusClient

        with pytest.raises(ValueError) as ei:
            MilvusClient(bad)
        assert "Milvus 配置必须是对象（mapping）" in str(ei.value)

    def test_milvus_client_accepts_mapping(self):
        from services.vector_db.milvus_client import MilvusClient

        assert MilvusClient({"host": "h"}).config == {"host": "h"}

    @pytest.mark.parametrize("bad", [[], None, False])
    def test_sql_loader_validate_false_for_bad_providers(self, tmp_path, bad):
        """SQLExampleLoader：providers.milvus 非 mapping → validate False（受管口径）。"""
        from metaweave.core.sql_rag.loader import SQLExampleLoader

        sql_dir = tmp_path / "sql-rag"
        sql_dir.mkdir()
        (sql_dir / "qs_demo_pair.json").write_text(
            '[{"question": "Q", "sql": "SELECT 1;", "domain": ""}]',
            encoding="utf-8",
        )
        loader = SQLExampleLoader(
            {"sql_loader": {"input_dir": str(sql_dir), "collection_name": "c"}},
            metadata_config={
                "embedding": {
                    "active": "qwen",
                    "providers": {
                        "qwen": {"model": "m", "api_key": "k", "dimensions": 1024}
                    },
                },
                "vector_database": {"active": "milvus", "providers": {"milvus": bad}},
            },
            source_id="db_src_1",
            publish_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeffff",
        )
        assert loader.validate() is False


class TestMilvusActiveAndProviderRequired:
    """active 必须为 milvus 且 provider 明确存在（用户审核第八轮第 1 条）。

    SQL 加载器与 resolver 此前只校验类型：active: pgvector 仍走 Milvus、
    providers.milvus 缺失/空 {} 落到客户端默认连接——与 table_schema
    加载器的既有约束不一致，且存在清理非预期 Collection 的风险。
    rag_load sql 的 resolver/CLI 对照用例已随 doc 25.2 CLI sql 移除而删除，
    loader 侧约束由下方 validate 用例覆盖（受管口径）。
    """

    @pytest.mark.parametrize(
        "vd,frag",
        [
            ({"providers": {"milvus": {"host": "h"}}}, "active"),
            ({"active": "pgvector", "providers": {"milvus": {"host": "h"}}}, "active"),
            ({"active": "milvus", "providers": {}}, "providers.milvus"),
            ({"active": "milvus", "providers": {"milvus": {}}}, "providers.milvus"),
        ],
    )
    def test_sql_loader_validate_false(self, tmp_path, caplog, vd, frag):
        """经**注入参数**提供配置，断言具体错误原因且 Milvus 客户端未被创建。

        受管口径（doc 25.2）：input_dir 目录即输入、source_id/publish_id
        必须注入——vector_database 约束校验仍在其后按序生效。
        """
        import logging

        from metaweave.core.sql_rag.loader import SQLExampleLoader

        sql_dir = tmp_path / "sql-rag"
        sql_dir.mkdir()
        (sql_dir / "qs_demo_pair.json").write_text(
            '[{"question": "Q", "sql": "SELECT 1;", "domain": ""}]',
            encoding="utf-8",
        )

        created = []

        class _FakeMilvus:
            def __init__(self, cfg):
                created.append(cfg)

            def connect(self):  # pragma: no cover - 不应被调用
                pass

            def test_connection(self):
                return True

        class _FakeEmb:
            def __init__(self, _cfg):
                pass

        loader = SQLExampleLoader(
            {
                "sql_loader": {"input_dir": str(sql_dir), "collection_name": "c"},
            },
            metadata_config={
                "embedding": {
                    "active": "qwen",
                    "providers": {
                        "qwen": {"model": "m", "api_key": "k", "dimensions": 1024}
                    },
                },
                "vector_database": vd,
            },
            milvus_client_cls=_FakeMilvus,
            embedding_service_cls=_FakeEmb,
            source_id="db_src_1",
            publish_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeffff",
        )
        with caplog.at_level(logging.ERROR):
            assert loader.validate() is False
        # 具体错误原因出现在日志（而非在 embedding/文件阶段提前失败）
        assert frag in caplog.text
        # Milvus 客户端未被创建（校验在连接/写入前拦截）
        assert created == []
