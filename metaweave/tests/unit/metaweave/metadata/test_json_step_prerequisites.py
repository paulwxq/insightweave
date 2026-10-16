"""doc 18 前置改造的用例：采样关闭时的逻辑主键边界、输出配置校验提取、
JsonLlmEnhancer 日志脱敏（doc 18 §2/§4/§6）。
"""

import logging
from threading import Lock
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from metaweave.core.metadata.generator import MetadataGenerator
from metaweave.core.metadata.logical_key_detector import LogicalKeyDetector
from metaweave.core.metadata.models import (
    ColumnInfo,
    GenerationResult,
    TableMetadata,
)
from metaweave.core.metadata.profiler import MetadataProfiler


def _table_without_pk(name: str = "events") -> TableMetadata:
    metadata = TableMetadata(
        schema_name="public",
        table_name=name,
        columns=[
            ColumnInfo("event_id", 1, "integer"),
            ColumnInfo("event_code", 2, "varchar", is_nullable=True),
        ],
    )
    assert not metadata.primary_keys
    return metadata


def _generator_for(active_step: str, sampling_enabled: bool) -> MetadataGenerator:
    """直调处理方法所需的最小生成器状态（不建目录、不连库）。"""
    generator = MetadataGenerator.__new__(MetadataGenerator)
    generator.active_step = active_step
    generator.active_formats = ["json"]
    generator.sampling_enabled = sampling_enabled
    generator.sample_size = 2
    generator.column_stats_enabled = False
    generator.comment_enabled = False
    generator.logical_key_enabled = True
    generator.logical_key_detector = LogicalKeyDetector({})
    generator.profiler = MetadataProfiler()
    generator.connector = MagicMock()
    generator.connector.database = "demo"
    generator.connector.execute_query.return_value = [{"row_count": 0}]
    generator.extractor = MagicMock()
    generator.extractor.extract_json_indexes.return_value = []
    generator.extractor.extract_all.return_value = _table_without_pk()
    generator.formatter = MagicMock()
    generator.formatter.build_json_document.return_value = {}
    generator.formatter.json_output_path.return_value = "unused.json"
    generator._result_lock = Lock()
    generator._pending_json_documents = []
    return generator


class TestLogicalKeyWithoutSampling:
    def test_from_ddl_skips_detection_and_keeps_no_candidates(self):
        """doc 18 §2：采样关闭 + 逻辑主键开启 + 无物理主键——不抛
        AttributeError，未采样保持无候选语义。"""
        metadata = _table_without_pk()
        generator = _generator_for("json", sampling_enabled=False)
        loader = MagicMock()
        loader.load_table.return_value = SimpleNamespace(metadata=metadata)
        loader.database_name = "demo"
        generator._get_ddl_loader = MagicMock(return_value=loader)
        result = GenerationResult(success=True)

        generator._process_table_from_ddl("public", "events", "table", result)

        assert result.failed_tables == 0
        assert metadata.candidate_logical_primary_keys == []

    def test_from_db_skips_detection_and_keeps_no_candidates(self):
        """同类防御（doc 18 §2/§6）：直调 `_process_table_from_db()`。"""
        generator = _generator_for("json", sampling_enabled=False)
        result = GenerationResult(success=True)

        generator._process_table_from_db("public", "events", "table", result)

        assert result.failed_tables == 0

    def test_from_ddl_still_detects_when_sampled(self):
        """有采样数据时逻辑主键检测仍执行（防过度修复）。"""
        import pandas as pd

        metadata = _table_without_pk()
        generator = _generator_for("json", sampling_enabled=True)
        generator.connector.sample_data.return_value = pd.DataFrame(
            [{"event_id": 1, "event_code": "a"}, {"event_id": 2, "event_code": "b"}]
        )
        detector = MagicMock()
        detector.detect.return_value = []
        generator.logical_key_detector = detector
        loader = MagicMock()
        loader.load_table.return_value = SimpleNamespace(metadata=metadata)
        loader.database_name = "demo"
        generator._get_ddl_loader = MagicMock(return_value=loader)
        result = GenerationResult(success=True)

        generator._process_table_from_ddl("public", "events", "table", result)

        detector.detect.assert_called_once()
        assert result.failed_tables == 0


class TestExtractedOptionValidators:
    """doc 18 §4：三项输出配置 + 采样方式校验提取为无副作用函数。"""

    def test_json_options_rules(self):
        from metaweave.core.metadata.formatter import validate_json_output_options

        validate_json_output_options({})
        validate_json_output_options({"include_generation_timestamps": False})
        with pytest.raises(ValueError, match="output.json_options 必须是对象"):
            validate_json_output_options("nope")
        with pytest.raises(
            ValueError, match="include_generation_timestamps 必须是布尔值"
        ):
            validate_json_output_options({"include_generation_timestamps": "yes"})

    def test_output_options_three_rules(self):
        from metaweave.core.metadata.formatter import validate_output_options

        validate_output_options({})
        with pytest.raises(ValueError, match="sample_records.count 必须是非负整数"):
            validate_output_options({"ddl_options": {"sample_records": {"count": -1}}})
        with pytest.raises(ValueError, match="sample_records.count 必须是非负整数"):
            validate_output_options({"ddl_options": {"sample_records": {"count": "3"}}})
        with pytest.raises(ValueError, match="sample_value_count"):
            validate_output_options({"markdown_options": {"sample_value_count": "abc"}})
        # YAML `.inf` 会让 int() 抛 OverflowError（非 ValueError 子类），
        # 同样按配置错误受控拒绝（doc 19.1 §4：不冒充平台库故障）
        with pytest.raises(ValueError, match="sample_value_count"):
            validate_output_options(
                {"markdown_options": {"sample_value_count": float("inf")}}
            )
        with pytest.raises(ValueError, match="output.json_options 必须是对象"):
            validate_output_options({"json_options": 42})

    def test_output_options_keeps_loose_input_semantics(self):
        """锚定（doc 18 §4）：空值经 or {} 视为未配置，不额外收紧。"""
        from metaweave.core.metadata.formatter import validate_output_options

        validate_output_options({"json_options": None})
        validate_output_options({"ddl_options": None, "markdown_options": None})
        validate_output_options({"markdown_options": {"sample_value_count": "3"}})

    def test_sampling_method_rule(self):
        from metaweave.core.metadata.generator import validate_json_sampling_method

        validate_json_sampling_method({})
        validate_json_sampling_method({"sample_method": "LIMIT"})
        with pytest.raises(ValueError, match="仅支持 sampling.sample_method=limit"):
            validate_json_sampling_method({"sample_method": "random"})


SECRET_MARKER = "UNIQUE-MARKER-9f3a-sample"


class TestEnhancerLogSanitization:
    """doc 18 §4：日志与错误摘要不携带 LLM 响应/prompt/样例值。"""

    def _enhancer(self):
        from metaweave.core.metadata.json_llm_enhancer import JsonLlmEnhancer

        config = {
            "llm": {
                "active": "qwen",
                "providers": {"qwen": {"model": "m", "api_key": "k"}},
            },
            "json_generation": {
                "comments": {"llm_enabled": True, "overwrite": False},
                "table_classification": {"llm_enabled": False},
            },
        }
        return JsonLlmEnhancer(config)

    def test_parse_failure_logs_no_response_at_info_and_debug(self, caplog):
        enhancer = self._enhancer()
        for level in (logging.INFO, logging.DEBUG):
            caplog.clear()
            with caplog.at_level(level, logger="metaweave.json_llm"):
                enhancer._parse_llm_response(f"垃圾{SECRET_MARKER}不是JSON", "t1")
            assert SECRET_MARKER not in caplog.text

    def test_no_json_start_logs_no_response_at_info_and_debug(self, caplog):
        enhancer = self._enhancer()
        for level in (logging.INFO, logging.DEBUG):
            caplog.clear()
            with caplog.at_level(level, logger="metaweave.json_llm"):
                enhancer._parse_llm_response(f"plain text {SECRET_MARKER}", "t1")
            assert SECRET_MARKER not in caplog.text

    def test_request_exception_neither_logged_nor_summarized(self, caplog):
        """LLM 调用抛含标记异常：日志与 outcome.error 都只留错误类别。"""
        import json

        enhancer = self._enhancer()
        service = MagicMock(model="m")
        service.call_llm.side_effect = RuntimeError(f"boom {SECRET_MARKER}")
        enhancer.llm_service = service
        document = {
            "metadata_version": "3.0",
            "object_info": {
                "database": "d",
                "schema_name": "public",
                "object_name": "t",
                "object_type": "table",
                "comment": "",
                "comment_source": "",
                "total_rows": 0,
            },
            "column_profiles": {
                "c1": {
                    "ordinal_position": 1,
                    "data_type": "integer",
                    "is_nullable": True,
                    "comment": "",
                    "comment_source": "",
                }
            },
            "table_profile": None,
            "sample_records": {"sample_method": "none", "records": []},
        }

        with caplog.at_level(logging.DEBUG, logger="metaweave.json_llm"):
            outcome = enhancer.enhance_document(json.loads(json.dumps(document)))

        assert outcome.success is False
        assert SECRET_MARKER not in caplog.text
        assert SECRET_MARKER not in (outcome.error or "")
        assert "RuntimeError" in (outcome.error or "")


def test_llm_init_failure_error_summary_only_keeps_type_name(caplog):
    """初始化失败分支的 outcome.error 不带异常文本（doc 18 §4）。"""
    import json

    enhancer = TestEnhancerLogSanitization()._enhancer()

    def fail_init():
        raise RuntimeError(f"init boom {SECRET_MARKER}")

    enhancer._ensure_llm_service = fail_init
    document = {
        "metadata_version": "3.0",
        "object_info": {
            "database": "d",
            "schema_name": "public",
            "object_name": "t",
            "object_type": "table",
            "comment": "",
            "comment_source": "",
            "total_rows": 0,
        },
        "column_profiles": {
            "c1": {
                "ordinal_position": 1,
                "data_type": "integer",
                "is_nullable": True,
                "comment": "",
                "comment_source": "",
            }
        },
        "table_profile": None,
        "sample_records": {"sample_method": "none", "records": []},
    }

    with caplog.at_level(logging.DEBUG, logger="metaweave.json_llm"):
        outcome = enhancer.enhance_document(json.loads(json.dumps(document)))

    assert outcome.success is False
    assert SECRET_MARKER not in caplog.text
    assert SECRET_MARKER not in (outcome.error or "")
    assert "RuntimeError" in (outcome.error or "")


def test_formatter_accepts_null_option_sections_like_precheck(tmp_path):
    """预检放行 `ddl_options/markdown_options: null` 时构造也不得抛
    AttributeError——两边同一种"空值视为未配置"规范化（doc 18 §4）。"""
    from metaweave.core.metadata.formatter import (
        OutputFormatter,
        validate_output_options,
    )

    config = {
        "output_dir": str(tmp_path),
        "ddl_options": None,
        "markdown_options": None,
    }
    validate_output_options(config)  # 预检放行
    formatter = OutputFormatter(config, database_name="demo")  # 构造不得抛
    assert formatter.sample_record_options == {"enabled": True, "count": 3}
    assert formatter.markdown_sample_value_count == 2


def test_count_failure_fails_object_without_leaking_or_saving(caplog):
    """doc 18 §4 口径：COUNT 查询失败计为对象失败——查询失败≠空表，
    不得把 row_count=0 当作事实落盘；日志只留错误类别与对象定位。"""
    generator = _generator_for("json", sampling_enabled=False)
    generator.connector.execute_query.side_effect = RuntimeError(
        f"boom {SECRET_MARKER}"
    )
    metadata = _table_without_pk()
    loader = MagicMock()
    loader.load_table.return_value = SimpleNamespace(metadata=metadata)
    loader.database_name = "demo"
    generator._get_ddl_loader = MagicMock(return_value=loader)
    result = GenerationResult(success=True)

    with caplog.at_level(logging.DEBUG, logger="metaweave.metadata.generator"):
        generator._process_table_from_ddl("public", "events", "table", result)

    assert result.failed_tables == 1
    assert generator._pending_json_documents == []  # 不把错误事实排队落盘
    assert SECRET_MARKER not in caplog.text
    assert "查询行数失败" in caplog.text
    assert "RuntimeError" in caplog.text


def test_count_sql_quotes_identifiers():
    """COUNT 语句以 Identifier 引用 schema/表名（对齐 sample_data 的既有
    写法），需引用的标识符不再触发语法错误路径。"""
    generator = _generator_for("json", sampling_enabled=False)
    generator.connector.execute_query.return_value = [{"row_count": 7}]
    metadata = _table_without_pk()
    loader = MagicMock()
    loader.load_table.return_value = SimpleNamespace(metadata=metadata)
    loader.database_name = "demo"
    generator._get_ddl_loader = MagicMock(return_value=loader)

    generator._process_table_from_ddl(
        "public", "events", "table", GenerationResult(success=True)
    )

    count_sql = generator.connector.execute_query.call_args.args[0]
    assert count_sql == 'SELECT COUNT(*) as row_count FROM "public"."events"'
    assert metadata.row_count == 7


class TestValidatorNodeTypeChecks:
    """doc 19.1 §4：共享校验器入口的对象/列表类型检查——非法类型在
    start_step 与清理前受控拒绝，不冒充平台库故障。"""

    def test_sampling_node_must_be_object(self):
        from metaweave.core.metadata.generator import validate_json_sampling_method

        with pytest.raises(ValueError, match="sampling 必须是对象"):
            validate_json_sampling_method("abc")

    def test_output_numeric_nodes_must_be_objects(self):
        from metaweave.core.metadata.formatter import validate_output_numeric_options

        with pytest.raises(ValueError, match="ddl_options 必须是对象"):
            validate_output_numeric_options({"ddl_options": "abc"})
        with pytest.raises(ValueError, match="sample_records 必须是对象"):
            validate_output_numeric_options({"ddl_options": {"sample_records": "abc"}})
        with pytest.raises(ValueError, match="markdown_options 必须是对象"):
            validate_output_numeric_options({"markdown_options": "abc"})

    def test_exclude_tables_must_be_list_when_present(self):
        """入口收 database 节点：键缺省=空列表语义；键存在且非列表
        （含 YAML 空节点 null）一律拒绝（doc 19.1 §4）。"""
        from metaweave.core.metadata.generator import validate_exclude_tables

        validate_exclude_tables({})  # 键缺省
        validate_exclude_tables({"exclude_tables": []})
        validate_exclude_tables({"exclude_tables": ["t*", "public.orders"]})
        for bad in (None, "abc", 42, {"a": 1}):
            with pytest.raises(ValueError, match="exclude_tables 必须是列表"):
                validate_exclude_tables({"exclude_tables": bad})

    def test_shared_exclusion_matcher_semantics(self):
        """共享匹配语义与生成器一致：精确、尾 * 前缀、schema 前缀；
        多段式沿用 warning 后忽略。"""
        from metaweave.core.metadata.generator import table_matches_exclusion

        assert table_matches_exclusion("public", "orders", ["orders"])
        assert table_matches_exclusion("public", "orders_tmp", ["orders*"])
        assert table_matches_exclusion("public", "orders", ["public.orders"])
        assert not table_matches_exclusion("sales", "orders", ["public.orders"])
        assert not table_matches_exclusion("public", "orders", ["a.b.c"])  # 多段式忽略
