"""元数据生成器

协调整个元数据生成流程的主控制器。
"""

import json
import logging
from threading import Lock
from typing import List, Optional, Dict, Any
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
import psycopg
from tqdm import tqdm


from metaweave.core.metadata.connector import DatabaseConnector
from metaweave.core.metadata.ddl_loader import DDLLoader, DDLLoaderError
from metaweave.core.metadata.extractor import MetadataExtractor
from metaweave.core.metadata.comment_generator import CommentGenerator
from metaweave.core.metadata.json_loader import JsonLoader, JsonLoaderError
from metaweave.core.metadata.logical_key_detector import LogicalKeyDetector
from metaweave.core.metadata.formatter import OutputFormatter
from metaweave.core.metadata.generation_config import MetadataGenerationConfig
from metaweave.core.metadata.models import (
    DatabaseObjectRef,
    GenerationResult,
    TableMetadata,
    normalize_database_object_types,
)
from metaweave.core.metadata.profiler import MetadataProfiler
from metaweave.services.llm_service import LLMService
from services.logging_manager import submit_with_context
from metaweave.utils.file_utils import get_project_root
from metaweave.utils.data_utils import (
    dataframe_to_sample_dict,
    get_column_statistics,
)
from services.config_loader import ConfigLoader

logger = logging.getLogger("metaweave.generator")
SUPPORTED_STEPS = {"ddl", "json", "cql", "md"}


def validate_exclude_tables(database: dict) -> None:
    """``database.exclude_tables`` 列表类型检查（doc 19.1 §4，无副作用）。

    入口收 ``database`` 节点以区分两种"取值为 None"：**键缺省**沿用空
    列表语义；**键存在且非列表**（含 YAML 空节点 ``null``）一律受控拒绝
    ——生成器遍历 ``null`` 会抛 ``TypeError``、遍历字符串会逐字符解释，
    都不能当合法配置。受管 JSON 与 MD 准备链共用。
    """
    if "exclude_tables" not in database:
        return
    if not isinstance(database["exclude_tables"], list):
        raise ValueError("database.exclude_tables 必须是列表")


def table_matches_exclusion(schema_name: str, table: str, patterns) -> bool:
    """共享排除匹配语义（doc 19.1 §4）：与生成器对象枚举同一规则。

    - 纯表名：跨 schema 的精确/尾 ``*`` 前缀匹配
    - ``schema.table`` / ``schema.table*`` / ``schema.*``：限定 schema
    - 空白模式跳过；包含两个及以上点的多段式沿用"记 warning 后忽略"

    供受管 MD 范围预检与生成器 ``_is_table_excluded()`` 共用，不另写一套。
    """
    for raw_pattern in patterns:
        pattern = str(raw_pattern).strip()
        if not pattern:
            continue
        dot_count = pattern.count(".")
        if dot_count == 0:
            if MetadataGenerator._match_prefix_or_exact(table, pattern):
                return True
            continue
        if dot_count == 1:
            pattern_schema, pattern_table = pattern.split(".", 1)
            if schema_name == pattern_schema and (
                MetadataGenerator._match_prefix_or_exact(table, pattern_table)
            ):
                return True
            continue
        logger.warning("exclude_tables 暂不支持三段式或多段模式，已忽略: %s", pattern)
    return False


def validate_json_sampling_method(sampling_config: dict) -> None:
    """``--step json`` 仅支持 LIMIT 采样（doc 18 §4，无副作用）。

    生成链与受管 JSON 准备链共用的唯一实现。非对象节点在入口受控拒绝
    （doc 19.1 §4：不让 ``AttributeError`` 冒充平台库故障）。
    """
    if not isinstance(sampling_config, dict):
        raise ValueError("sampling 必须是对象")
    sample_method = str(sampling_config.get("sample_method", "limit")).strip().lower()
    if sample_method != "limit":
        raise ValueError(
            "--step json 当前仅支持 sampling.sample_method=limit；"
            f"收到: {sample_method!r}"
        )


class MetadataGenerator:
    """元数据生成器

    协调整个元数据生成流程，包括：
    1. 连接数据库
    2. 提取元数据
    3. 数据采样
    4. LLM 生成注释
    5. 识别逻辑主键
    6. 格式化输出
    """

    def __init__(self, config_path: str | Path, config: dict | None = None):
        """初始化元数据生成器

        Args:
            config_path: 配置文件路径（config 已注入时仅作来源标识，不触发读盘）
            config: 已完成 ConfigLoader 展开的完整主配置快照（doc 26 §6.2）；
                缺失时从 config_path 读取一次（兼容直接类调用）
        """
        self.config_path = Path(config_path)
        self.config = config if config is not None else self._load_config()
        self._result_lock = Lock()

        # 初始化各个组件
        self._init_components()

        logger.info("元数据生成器已初始化")

    def _load_config(self) -> Dict[str, Any]:
        """加载配置文件（使用 ConfigLoader 处理环境变量替换）"""
        try:
            config_loader = ConfigLoader(str(self.config_path))
            config = config_loader.load()
            if not config:
                raise ValueError(f"配置文件加载失败: {self.config_path}")
            from metaweave.services.llm_config_resolver import (
                _validate_declared_module_llm_paths,
                _validate_nonstandard_llm_paths,
            )

            _validate_declared_module_llm_paths(config)
            _validate_nonstandard_llm_paths(config)
            logger.info(f"配置文件加载成功: {self.config_path}")
            return config
        except Exception as e:
            logger.error(f"加载配置文件失败: {e}")
            raise

    def _init_components(self):
        """初始化所有组件"""
        # 数据库连接器（延迟初始化，md 步骤不需要）
        db_config = self.config.get("database", {})
        self.database_name = db_config.get("database")
        self.connector = None  # 延迟初始化，仅在需要时创建

        # 元数据提取器（延迟初始化）
        self.extractor = None

        # 仅解析配置；LLM 服务在具体 ddl/json 步骤确实需要时再初始化。
        self.generation_config = MetadataGenerationConfig.from_config(self.config)
        self.comment_enabled = False
        self.llm_service = None
        self.comment_generator = None
        self._pending_json_documents: List[tuple[Dict[str, Any], Path, str]] = []

        # 逻辑主键检测器
        # single_column_exclude_roles / composite_exclude_roles 直接从
        # logical_key_detection 节点读取（doc 15 2.8：已从 single_column /
        # composite 节点搬迁至此，不再由 generator 注入）
        logical_key_config = self.config.get("logical_key_detection", {})
        self.logical_key_enabled = logical_key_config.get("enabled", True)
        if self.logical_key_enabled:
            logger.info(
                "逻辑主键检测排除角色配置: single=%s, composite=%s",
                logical_key_config.get("single_column_exclude_roles"),
                logical_key_config.get("composite_exclude_roles"),
            )
            self.logical_key_detector = LogicalKeyDetector(logical_key_config)

        # 输出格式化器
        output_config = self.config.get("output", {})
        self.formatter = OutputFormatter(
            output_config, database_name=self.database_name
        )
        self.active_step = "ddl"
        self.active_formats = self.formatter.formats
        self.ddl_loader: Optional[DDLLoader] = None
        self._json_loader: Optional[JsonLoader] = None
        self.profiler = MetadataProfiler(self.config)

        # 采样配置
        self.sampling_config = self.config.get("sampling", {})
        self.sampling_enabled = self.sampling_config.get("enabled", True)
        self.sample_size = self.sampling_config.get("sample_size", 1000)
        self.ddl_sample_count = self.formatter.sample_record_options["count"]

        # 列统计配置
        column_stats_config = self.sampling_config.get("column_statistics", {})
        self.column_stats_enabled = column_stats_config.get("enabled", True)
        self.value_dist_threshold = column_stats_config.get(
            "value_distribution_threshold", 10
        )
        logger.info(
            f"列统计配置: enabled={self.column_stats_enabled}, threshold={self.value_dist_threshold}"
        )

    def _validate_ddl_sampling_config(self) -> None:
        """仅在 DDL 步骤校验 DDL 样例行数，避免影响 JSON 独立执行。"""
        ddl_sampling_needed = bool(
            self.generation_config.ddl_comments.llm_enabled
        ) or bool(self.formatter.sample_record_options["enabled"])
        if (
            self.sampling_enabled
            and ddl_sampling_needed
            and self.ddl_sample_count > self.sample_size
        ):
            raise ValueError(
                "output.ddl_options.sample_records.count 不能大于 "
                "sampling.sample_size"
            )

    def _configure_step_llm(self) -> None:
        """按当前步骤配置 LLM，避免构造生成器时初始化无关服务。"""
        self.comment_enabled = False
        self.llm_service = None
        self.comment_generator = None
        if self.active_step != "ddl":
            return
        if not self.generation_config.ddl_comments.llm_enabled:
            return

        from metaweave.services.llm_config_resolver import resolve_module_llm_config

        try:
            llm_config = resolve_module_llm_config(self.config, "ddl_generation.llm")
            self.llm_service = LLMService(llm_config)
            self.comment_generator = CommentGenerator(self.llm_service)
            self.comment_enabled = True
        except Exception as exc:
            logger.warning("DDL LLM 服务初始化失败，注释生成将被禁用: %s", exc)

    def _ensure_connector(self):
        """延迟初始化数据库连接器（仅在需要时）"""
        if self.connector is None:
            db_config = self.config.get("database", {})
            self.connector = DatabaseConnector(db_config)
            self.extractor = MetadataExtractor(self.connector)
            logger.info("数据库连接器已延迟初始化")

    def generate(
        self,
        schemas: Optional[List[str]] = None,
        tables: Optional[List[str]] = None,
        incremental: bool = False,
        max_workers: int = 4,
        step: str = "ddl",
    ) -> GenerationResult:
        """生成元数据

        Args:
            schemas: 指定要处理的 schema 列表，None 表示处理配置文件中的所有 schema
            tables: 指定要处理的表列表，None 表示处理所有表
            incremental: 是否增量更新模式（暂未实现）
            max_workers: 最大并发数

        Returns:
            生成结果
        """
        result = GenerationResult(success=True)
        self.active_step = self._normalize_step(step)
        self.active_formats = self._resolve_formats_for_step(self.active_step)
        if self.active_step == "ddl":
            self._validate_ddl_sampling_config()
        self._configure_step_llm()
        if self.active_step == "json":
            self._pending_json_documents = []
            self._validate_json_sampling_method()
            self.formatter.validate_json_options()
        elif self.active_step == "md":
            self._json_loader = None
        logger.info(f"执行步骤: {self.active_step}")

        try:
            # 测试数据库连接（md 步骤跳过）
            if self.active_step != "md":
                self._ensure_connector()  # 延迟初始化
                if not self.connector.test_connection():
                    result.success = False
                    result.add_error("数据库连接失败")
                    return result
            else:
                logger.info("md 步骤跳过数据库连接（从 JSON 文件读取）")

            # 获取要处理的 schema 列表
            if schemas is None:
                schemas = self.config.get("database", {}).get("schemas", [])

            if not schemas:
                if self.active_step == "md":
                    # md 步骤：从 JSON 目录推断 schemas（doc 17 §3.4）
                    schemas = self._get_json_loader().infer_schemas()
                    if not schemas:
                        result.success = False
                        result.add_error(
                            "md 步骤无法推断 schemas：JSON 目录为空且配置文件未指定 database.schemas\n"
                            "请在配置文件中设置 database.schemas 或先执行 --step json"
                        )
                        return result
                else:
                    self._ensure_connector()
                    schemas = self.connector.get_schemas()

            logger.info(f"将处理以下 schema: {schemas}")

            # 获取所有要处理的数据库对象
            all_tables = self._get_tables_to_process(schemas, tables)

            if not all_tables:
                logger.warning("没有找到需要处理的数据库对象")
                return result

            logger.info(f"共找到 {len(all_tables)} 个数据库对象待处理")

            # 并发处理表
            if max_workers > 1:
                result = self._process_tables_parallel(all_tables, max_workers, result)
            else:
                result = self._process_tables_sequential(all_tables, result)

            if self.active_step == "json":
                self._finalize_json_documents(result)

            logger.info(
                "元数据生成完成: 成功 %d，失败 %d",
                result.processed_tables,
                result.failed_tables,
            )

        except Exception as e:
            # 异常原文不进日志（doc 16 §4）：文本可能带出 SQL 或数据上下文，
            # 定位靠步骤日志里的对象名与这里的错误类别。
            logger.error("元数据生成过程出错: %s", type(e).__name__)
            result.success = False
            result.add_error(str(e))

        finally:
            # 关闭数据库连接（防空检查）
            if self.connector is not None:
                self.connector.close()

        return result

    def _get_tables_to_process(
        self, schemas: List[str], tables: Optional[List[str]]
    ) -> List[tuple[str, str, str]]:
        """获取要处理的数据库对象列表。

        Args:
            schemas: schema 列表
            tables: 表名列表（可选）

        Returns:
            (schema, object_name, object_type) 元组列表
        """
        all_objects = []
        exclude_patterns = self.config.get("database", {}).get("exclude_tables", [])

        for schema in schemas:
            # md 步骤：从 JSON 目录枚举对象，并按配置选择对象类型（doc 17 §3.4）。
            if self.active_step == "md":
                allowed_types = set(self._resolve_database_object_types())
                schema_objects = [
                    database_object
                    for database_object in self._get_json_loader().list_objects(schema)
                    if database_object.object_type in allowed_types
                ]
            elif self.active_step in {"ddl", "json"}:
                schema_objects = self.connector.get_database_objects(
                    schema,
                    self._resolve_database_object_types(),
                )
            else:
                # 下游步骤将在各自改造阶段增加对象类型选择；当前保持只处理普通表。
                schema_objects = [
                    DatabaseObjectRef(schema, table, "table")
                    for table in self.connector.get_tables(schema)
                ]

            for database_object in schema_objects:
                object_name = database_object.object_name
                # 如果指定了表名列表，只处理列表中的表
                if tables and object_name not in tables:
                    continue

                if not self._is_table_excluded(schema, object_name, exclude_patterns):
                    all_objects.append(
                        (
                            database_object.schema_name,
                            object_name,
                            database_object.object_type,
                        )
                    )

        return all_objects

    def _resolve_database_object_types(self) -> List[str]:
        """读取并校验 DDL/JSON 阶段允许处理的数据库对象类型。"""
        configured = self.config.get("database", {}).get(
            "include_object_types",
            ["table"],
        )
        return normalize_database_object_types(configured)

    def _resolve_ddl_object_types(self) -> List[str]:
        """保留既有内部接口，DDL 与 JSON 现共用同一对象类型配置。"""
        return self._resolve_database_object_types()

    def _validate_json_sampling_method(self) -> None:
        """本轮 JSON 画像只允许记录已真实实现的 LIMIT 采样。"""
        validate_json_sampling_method(self.sampling_config)

    @staticmethod
    def _match_prefix_or_exact(value: str, pattern: str) -> bool:
        """仅支持精确匹配或以后缀 * 结尾的前缀匹配。"""
        if pattern.endswith("*"):
            return value.startswith(pattern[:-1])
        return value == pattern

    def _is_table_excluded(
        self,
        schema: str,
        table: str,
        exclude_patterns: List[str],
    ) -> bool:
        """判断表是否命中 exclude_tables。

        支持：
        - orders：跨 schema 精确匹配表名
        - ord_*：跨 schema 表名前缀匹配
        - public.orders：指定 schema 精确匹配
        - public.ord_*：指定 schema 表名前缀匹配
        - public.*：指定 schema 下全部表

        暂不支持：
        - db.schema.table
        - *orders / *mid* / ord_*_bak 等任意位置通配
        """
        return table_matches_exclusion(schema, table, exclude_patterns)

    def _process_tables_sequential(
        self, tables: List[tuple[str, str, str]], result: GenerationResult
    ) -> GenerationResult:
        """顺序处理表"""
        progress_desc = "处理对象" if self.active_step in {"ddl", "md"} else "处理表"
        with tqdm(total=len(tables), desc=progress_desc) as pbar:
            for schema, table, object_type in tables:
                try:
                    self._process_table(schema, table, object_type, result)
                    result.processed_tables += 1
                except Exception as e:
                    entity = "对象" if self.active_step == "md" else "表"
                    logger.error(
                        f"处理{entity}失败 ({schema}.{table}): {type(e).__name__}"
                    )
                    if self.active_step == "md":
                        result.success = False
                    result.failed_tables += 1
                    result.add_error(f"{schema}.{table}: {str(e)}")
                finally:
                    pbar.update(1)

        return result

    def _process_tables_parallel(
        self,
        tables: List[tuple[str, str, str]],
        max_workers: int,
        result: GenerationResult,
    ) -> GenerationResult:
        """并行处理表"""
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            # 提交所有任务；表级任务经 submit_with_context 复制提交者上下文，
            # 使 ContextVar 化的步骤日志标签（doc 10.1 §4.4）在线程池内可继承
            future_to_table = {
                submit_with_context(
                    executor,
                    self._process_table,
                    schema,
                    table,
                    object_type,
                    result,
                ): (schema, table)
                for schema, table, object_type in tables
            }

            # 使用 tqdm 显示进度
            progress_desc = (
                "处理对象" if self.active_step in {"ddl", "md"} else "处理表"
            )
            with tqdm(total=len(tables), desc=progress_desc) as pbar:
                for future in as_completed(future_to_table):
                    schema, table = future_to_table[future]
                    try:
                        future.result()
                        result.processed_tables += 1
                    except Exception as e:
                        entity = "对象" if self.active_step == "md" else "表"
                        logger.error(
                            f"处理{entity}失败 ({schema}.{table}): {type(e).__name__}"
                        )
                        if self.active_step == "md":
                            result.success = False
                        result.failed_tables += 1
                        result.add_error(f"{schema}.{table}: {str(e)}")
                    finally:
                        pbar.update(1)

        return result

    def _process_table(
        self, schema: str, table: str, object_type: str, result: GenerationResult
    ):
        if self.active_step == "json":
            # json 步骤：从 DDL 读取，但执行 COUNT/采样/画像（需要数据库）
            self._process_table_from_ddl(schema, table, object_type, result)
        elif self.active_step == "md":
            # md 步骤：完全 file-only，不访问数据库（doc 17 §3.4）
            self._process_table_from_json_for_md(
                schema,
                table,
                object_type,
                result,
            )
        else:
            # ddl/rel 等其他步骤：直接查库
            self._process_table_from_db(schema, table, object_type, result)

    def _process_table_from_db(
        self, schema: str, table: str, object_type: str, result: GenerationResult
    ):
        """处理单张表

        Args:
            schema: schema 名称
            table: 表名
            result: 生成结果对象
        """
        logger.info("开始处理数据库对象: %s.%s (%s)", schema, table, object_type)

        try:
            # 1. 提取元数据
            metadata = self.extractor.extract_all(schema, table, object_type)
            if not metadata:
                raise ValueError(f"提取元数据失败: {schema}.{table}")

            # 设置数据库名称
            metadata.database = self.connector.database

            # 2. 数据采样
            sample_data = None
            ddl_only_mode = self.active_step == "ddl" and self.active_formats == ["ddl"]
            if self.sampling_enabled:
                if ddl_only_mode:
                    needs_sample_data = bool(self.comment_enabled) or bool(
                        getattr(self.formatter, "sample_record_options", {}).get(
                            "enabled", True
                        )
                    )
                    if needs_sample_data:
                        sample_data = self._sample_data_for_ddl(schema, table)
                else:
                    sample_data = self.connector.sample_data(
                        schema, table, self.sample_size
                    )
                    self._apply_column_statistics(metadata, sample_data)

            # 3. 生成注释（如果启用）
            if self.comment_enabled:
                comment_result = self.comment_generator.enrich_metadata_with_comments(
                    metadata,
                    sample_data,
                    overwrite=self.generation_config.ddl_comments.overwrite,
                )
                with self._result_lock:
                    result.generated_comments += comment_result.generated_count
                    result.llm_request_count += comment_result.request_count
                    result.llm_object_comment_success_count += (
                        comment_result.object_success_count
                    )
                    result.llm_object_comment_failure_count += (
                        comment_result.object_failure_count
                    )
                    result.llm_column_comment_success_count += (
                        comment_result.column_success_count
                    )
                    result.llm_column_comment_failure_count += (
                        comment_result.column_failure_count
                    )
                    if comment_result.has_failures:
                        result.success = False
                        for failure in comment_result.failures:
                            result.add_error(f"{failure.target}: {failure.reason}")

            if not ddl_only_mode:
                # 4. 生成列画像
                column_profiles = self.profiler._profile_columns(metadata, sample_data)
                metadata.column_profiles = column_profiles

                # 5. 生成逻辑主键（依赖列画像；采样关闭时跳过检测，
                # 保持无候选语义——detect() 不接受 None，doc 18 §2）
                if (
                    self.logical_key_enabled
                    and sample_data is not None
                    and metadata.column_profiles
                ):
                    logical_keys = self.logical_key_detector.detect(
                        metadata, sample_data
                    )
                    metadata.candidate_logical_primary_keys = logical_keys
                    if logical_keys:
                        result.logical_keys_found += len(logical_keys)

                # 6. 生成表画像（依赖列画像+逻辑主键）
                table_profile = self.profiler._profile_table(metadata, column_profiles)
                metadata.table_profile = table_profile

            # 7. 格式化输出
            output_files = self.formatter.format_and_save(
                metadata, sample_data, formats_override=self.active_formats
            )
            for file_path in output_files.values():
                result.add_output_file(file_path)

            if self.active_step == "ddl":
                self._accumulate_ddl_statistics(metadata, result)

            logger.info("数据库对象处理完成: %s.%s (%s)", schema, table, object_type)

        except Exception as e:
            # 异常原文与 traceback 不进日志（doc 16 §4）：异常文本可能
            # 带出 SQL 片段或数据上下文，定位靠对象名与错误类别。
            logger.error("处理表失败 (%s.%s): %s", schema, table, type(e).__name__)
            raise

    def _accumulate_ddl_statistics(
        self,
        metadata: TableMetadata,
        result: GenerationResult,
    ) -> None:
        """累计一个数据库对象的类型、物理约束和索引数量。

        元数据中的每个对象代表一个完整约束，因此复合约束按一个计数。
        """
        with self._result_lock:
            result.processed_object_counts[metadata.object_type] = (
                result.processed_object_counts.get(metadata.object_type, 0) + 1
            )
            result.physical_primary_key_constraints_found += len(metadata.primary_keys)
            result.physical_foreign_key_constraints_found += len(metadata.foreign_keys)
            result.unique_constraints_found += len(metadata.unique_constraints)
            result.indexes_found += len(metadata.indexes)
            result.regular_indexes_found += sum(
                1 for index in metadata.indexes if not index.is_unique
            )
            result.unique_indexes_found += sum(
                1 for index in metadata.indexes if index.is_unique
            )

    def _sample_data_for_ddl(self, schema: str, table: str):
        """按 DDL 样例配置读取数据，并在内存中过滤全空行和重复行。

        数据库读取数量与 sample_records.count 相同；过滤后不再补采。
        """
        try:
            target_rows = self.formatter.sample_record_options["count"]
            if target_rows == 0:
                return None

            df = self.connector.sample_data_preserving_types(
                schema,
                table,
                target_rows,
            )
            if df is None or df.empty:
                return df

            candidate_count = len(df)
            serialized_rows = dataframe_to_sample_dict(df, max_rows=candidate_count)
            seen_rows = set()
            retained_positions = []
            all_null_count = 0
            duplicate_count = 0

            for position, row in enumerate(serialized_rows):
                if all(value is None for value in row.values()):
                    all_null_count += 1
                    continue

                row_key = json.dumps(
                    row,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                if row_key in seen_rows:
                    duplicate_count += 1
                    continue

                seen_rows.add(row_key)
                retained_positions.append(position)

            if not retained_positions:
                logger.info(
                    "DDL 样例处理 %s.%s：候选 %d 行，过滤全空行 %d 行，"
                    "去重 %d 行，最终保留 0 行",
                    schema,
                    table,
                    candidate_count,
                    all_null_count,
                    duplicate_count,
                )
                return df.iloc[0:0]

            result = df.iloc[retained_positions].head(target_rows).copy()
            logger.info(
                "DDL 样例处理 %s.%s：候选 %d 行，过滤全空行 %d 行，"
                "去重 %d 行，最终保留 %d 行",
                schema,
                table,
                candidate_count,
                all_null_count,
                duplicate_count,
                len(result),
            )
            return result
        except Exception as e:
            logger.warning(f"DDL 采样失败 ({schema}.{table}): {e}")
            return None

    def _process_table_from_ddl(
        self, schema: str, table: str, object_type: str, result: GenerationResult
    ):
        logger.info(f"开始处理表 (DDL): {schema}.{table}")
        try:
            parsed = self._get_ddl_loader().load_table(schema, table)
            metadata = parsed.metadata
            if metadata.object_type != object_type:
                raise DDLLoaderError(
                    f"DDL 对象类型为 {metadata.object_type}，数据库对象类型为 "
                    f"{object_type}"
                )

            # 设置数据库名称（从 DDL loader 获取）
            metadata.database = self._get_ddl_loader().database_name
            if object_type in {"view", "materialized_view"}:
                ddl_columns = {
                    column.column_name: column for column in metadata.columns
                }
                catalog_columns = self.extractor.extract_columns(
                    schema,
                    table,
                    object_type,
                )
                if not catalog_columns:
                    raise DDLLoaderError(
                        f"无法从数据库读取 {object_type} 字段: {schema}.{table}"
                    )
                # 类型、可空性和默认值来自 PostgreSQL 目录；DDL 中经人工或
                # LLM 补全的注释继续作为 JSON 注释来源。
                for column in catalog_columns:
                    ddl_column = ddl_columns.get(column.column_name)
                    if ddl_column:
                        column.comment = ddl_column.comment
                        column.comment_source = ddl_column.comment_source
                metadata.columns = catalog_columns
            # JSON 使用数据库目录补齐完整索引事实（含约束支撑索引、表达式键
            # 和 INCLUDE 列）；DDL/MD 的行为不受此刷新影响。
            metadata.indexes = self.extractor.extract_json_indexes(schema, table)
        except DDLLoaderError as exc:
            logger.error(f"DDL 解析失败 ({schema}.{table}): {exc}")
            result.failed_tables += 1
            result.add_error(f"{schema}.{table}: {exc}")
            return

        # 从数据库查询真实的行数（使用 COUNT(*) 获取精确值）。标识符以
        # Identifier 引用（对齐 sample_data 的既有写法）；查询失败计为
        # 对象失败——查询失败≠空表，不得把 row_count=0 当作事实落盘
        # （doc 18 §4 定调：采样失败仍维持空样本降级，不在此收紧）
        try:
            count_sql = (
                "SELECT COUNT(*) as row_count FROM "
                f"{psycopg.sql.Identifier(schema).as_string(None)}."
                f"{psycopg.sql.Identifier(table).as_string(None)}"
            )
            query_result = self.connector.execute_query(count_sql, fetch_one=True)
            if query_result and len(query_result) > 0:
                metadata.row_count = query_result[0].get("row_count", 0)
                logger.info(f"✅ 更新行数: {schema}.{table} = {metadata.row_count}")
            else:
                logger.warning(f"⚠️ 未获取到行数: {schema}.{table}, row_count 保持为 0")
        except Exception as e:
            # 异常原文可能带 SQL 片段，只记错误类别（doc 18 §4）
            logger.warning(f"❌ 查询行数失败 ({schema}.{table}): {type(e).__name__}")
            result.failed_tables += 1
            result.add_error(f"{schema}.{table}: 行数查询失败（{type(e).__name__}）")
            return

        sample_data = None
        if self.sampling_enabled:
            sample_data = self.connector.sample_data(schema, table, self.sample_size)
            self._apply_column_statistics(metadata, sample_data)

        # 步骤1: 生成列画像
        column_profiles = self.profiler._profile_columns(metadata, sample_data)
        metadata.column_profiles = column_profiles

        # 步骤2: 生成逻辑主键（依赖列画像；采样关闭时跳过检测，
        # 保持无候选语义——detect() 不接受 None，doc 18 §2）
        if (
            self.logical_key_enabled
            and sample_data is not None
            and metadata.column_profiles
        ):
            logical_keys = self.logical_key_detector.detect(metadata, sample_data)
            metadata.candidate_logical_primary_keys = logical_keys
            if logical_keys:
                result.logical_keys_found += len(logical_keys)

        # 步骤3: 生成表画像（依赖列画像+逻辑主键）
        table_profile = self.profiler._profile_table(metadata, column_profiles)
        metadata.table_profile = table_profile

        document = self.formatter.build_json_document(metadata, sample_data)
        output_path = self.formatter.json_output_path(metadata)
        with self._result_lock:
            self._pending_json_documents.append(
                (document, output_path, metadata.full_name)
            )
            result.processed_object_counts[metadata.object_type] = (
                result.processed_object_counts.get(metadata.object_type, 0) + 1
            )

        logger.info(f"表规则画像完成 (JSON): {schema}.{table}")

    def _finalize_json_documents(self, result: GenerationResult) -> None:
        """在规则画像完成后统一执行可选 LLM 增强并原子保存。"""
        from metaweave.core.metadata.json_llm_enhancer import JsonLlmEnhancer

        enhancer = JsonLlmEnhancer(
            self.config,
            runtime_override={"langchain_config": {"use_async": False}},
        )
        pending = sorted(self._pending_json_documents, key=lambda item: str(item[1]))
        for document, output_path, object_name in pending:
            outcome = enhancer.enhance_document(document)
            result.llm_request_count += outcome.request_count
            if outcome.llm_called and outcome.success:
                result.llm_success_count += 1
            elif not outcome.success:
                result.llm_failure_count += 1
            if outcome.comment_task_attempted:
                if outcome.comment_task_succeeded:
                    result.llm_comment_success_count += 1
                else:
                    result.llm_comment_failure_count += 1
            result.llm_object_comment_success_count += (
                outcome.object_comment_success_count
            )
            result.llm_object_comment_failure_count += (
                outcome.object_comment_failure_count
            )
            result.llm_column_comment_success_count += (
                outcome.column_comment_success_count
            )
            result.llm_column_comment_failure_count += (
                outcome.column_comment_failure_count
            )
            if outcome.classification_task_attempted:
                if outcome.classification_task_succeeded:
                    result.llm_classification_success_count += 1
                else:
                    result.llm_classification_failure_count += 1
            result.generated_comments += outcome.generated_comments

            try:
                saved_path = self.formatter.save_json_document(
                    outcome.document, output_path
                )
                result.add_output_file(str(saved_path))
                category = (
                    str(
                        outcome.document.get("table_profile", {}).get(
                            "table_category", "unknown"
                        )
                        or "unknown"
                    )
                    .strip()
                    .lower()
                )
                result.table_category_counts[category] = (
                    result.table_category_counts.get(category, 0) + 1
                )
            except Exception as exc:
                result.success = False
                result.failed_tables += 1
                result.add_error(f"{object_name}: JSON 保存失败: {exc}")
                continue

            if not outcome.success:
                result.success = False
                result.add_error(
                    f"{object_name}: LLM 增强存在失败项，已保存可用 JSON: "
                    f"{outcome.error}"
                )

    def _process_table_from_json_for_md(
        self, schema: str, object_name: str, object_type: str, result: GenerationResult
    ):
        """md 专用：从 JSON 产物生成 Markdown（file-only，不访问数据库）

        doc 17 §3.4/§3.5：md 只读 output/json/*.json，不回退 DDL。
        - 不执行 COUNT 查询（md 不展示行数）
        - 不执行列画像/逻辑主键/表画像（md 不需要）
        - 不采样数据库（使用 JSON 的 sample_records）
        - 完全 file-only，零数据库访问
        """
        logger.info(
            "开始处理数据库对象 (Markdown): %s.%s (%s)",
            schema,
            object_name,
            object_type,
        )

        # 1. 从 JSON 文件还原元数据（映射错误按单对象失败处理）
        metadata = self._get_json_loader().load_object(schema, object_name)
        if metadata.object_type != object_type:
            raise JsonLoaderError(
                f"JSON 对象类型为 {metadata.object_type}，枚举对象类型为 {object_type}"
            )
        metadata.database = self.database_name

        # 2. 将 JSON sample_records 转换为 DataFrame（仅用于渲染示例值）。
        # JSON 数值已类型化，转回字符串后可能丢失 DDL 原有小数尾零
        # （18.00 → 18.0），属 doc 17 §5 验收项 1 接受的差异。
        sample_data = None
        if metadata.sample_records:
            import pandas as pd

            sample_data = pd.DataFrame(metadata.sample_records)
            logger.info(
                "使用 JSON 样例数据: %s.%s, %d 行",
                schema,
                object_name,
                len(sample_data),
            )
        else:
            logger.warning("JSON 无样例数据: %s.%s", schema, object_name)

        # 3. 跳过列统计、画像、逻辑主键（md 不需要）
        # 注意：
        # - md 输出不展示列统计信息
        # - md 输出不展示画像/逻辑主键
        # - 保持字段为空值，避免序列化错误
        metadata.column_profiles = {}
        metadata.candidate_logical_primary_keys = []
        metadata.table_profile = None

        # 4. 格式化输出（仅生成 markdown）
        output_files = self.formatter.format_and_save(
            metadata,
            sample_data,  # 用于提取示例值
            formats_override=["markdown"],  # 仅输出 md 格式
        )
        markdown_path = output_files.get("markdown")
        if not markdown_path:
            raise RuntimeError(f"Markdown 保存失败: {schema}.{object_name}")

        with self._result_lock:
            result.add_output_file(markdown_path)
            result.processed_object_counts[object_type] = (
                result.processed_object_counts.get(object_type, 0) + 1
            )

        # 注意：不需要 result.processed_tables += 1
        # 外层框架（_process_tables_sequential/parallel）已统计
        logger.info(
            "Markdown 生成完成: %s.%s (%s)",
            schema,
            object_name,
            object_type,
        )

    def _generate_summary(self, result: GenerationResult):
        """生成汇总报告"""
        summary_lines = []
        summary_lines.append("=" * 60)
        summary_lines.append("元数据生成汇总报告")
        summary_lines.append("=" * 60)
        unit = "个对象" if self.active_step in {"ddl", "json", "md"} else "张表"
        summary_lines.append(f"成功处理: {result.processed_tables} {unit}")
        summary_lines.append(f"处理失败: {result.failed_tables} {unit}")
        summary_lines.append(f"生成注释: {result.generated_comments} 个")
        if self.active_step == "ddl":
            summary_lines.append(f"LLM 请求: {result.llm_request_count} 次")
            summary_lines.append(
                "  - 对象注释: "
                f"成功 {result.llm_object_comment_success_count}，"
                f"失败 {result.llm_object_comment_failure_count}"
            )
            summary_lines.append(
                "  - 字段注释: "
                f"成功 {result.llm_column_comment_success_count}，"
                f"失败 {result.llm_column_comment_failure_count}"
            )
            summary_lines.append(
                f"Table: {result.processed_object_counts.get('table', 0)} 个"
            )
            summary_lines.append(
                f"View: {result.processed_object_counts.get('view', 0)} 个"
            )
            summary_lines.append(
                "Materialized View: "
                f"{result.processed_object_counts.get('materialized_view', 0)} 个"
            )
            summary_lines.append(
                f"物理主键约束: {result.physical_primary_key_constraints_found} 个"
            )
            summary_lines.append(
                f"物理外键约束: {result.physical_foreign_key_constraints_found} 个"
            )
            summary_lines.append(f"唯一约束: {result.unique_constraints_found} 个")
            summary_lines.append(f"索引总数: {result.indexes_found} 个")
            summary_lines.append(f"  - 普通索引: {result.regular_indexes_found} 个")
            summary_lines.append(f"  - 唯一索引: {result.unique_indexes_found} 个")
            summary_lines.append("逻辑主键识别: 未执行")
        elif self.active_step == "json":
            summary_lines.append(
                f"Table: {result.processed_object_counts.get('table', 0)} 个"
            )
            summary_lines.append(
                f"View: {result.processed_object_counts.get('view', 0)} 个"
            )
            summary_lines.append(
                "Materialized View: "
                f"{result.processed_object_counts.get('materialized_view', 0)} 个"
            )
            summary_lines.append(f"识别逻辑主键: {result.logical_keys_found} 个")
            summary_lines.append(f"LLM 请求: {result.llm_request_count} 次")
            summary_lines.append(f"LLM 增强成功: {result.llm_success_count} 个对象")
            summary_lines.append(f"LLM 增强失败: {result.llm_failure_count} 个对象")
            summary_lines.append(
                "  - 注释任务: "
                f"成功 {result.llm_comment_success_count}，"
                f"失败 {result.llm_comment_failure_count}"
            )
            summary_lines.append(
                "    - 对象注释项: "
                f"成功 {result.llm_object_comment_success_count}，"
                f"失败 {result.llm_object_comment_failure_count}"
            )
            summary_lines.append(
                "    - 字段注释项: "
                f"成功 {result.llm_column_comment_success_count}，"
                f"失败 {result.llm_column_comment_failure_count}"
            )
            summary_lines.append(
                "  - 分类任务: "
                f"成功 {result.llm_classification_success_count}，"
                f"失败 {result.llm_classification_failure_count}"
            )
            summary_lines.append(
                "表分类: "
                f"fact={result.table_category_counts.get('fact', 0)}，"
                f"dim={result.table_category_counts.get('dim', 0)}，"
                f"bridge={result.table_category_counts.get('bridge', 0)}，"
                f"unknown={result.table_category_counts.get('unknown', 0)}"
            )
        elif self.active_step == "md":
            summary_lines.append(
                f"Table: {result.processed_object_counts.get('table', 0)} 个"
            )
            summary_lines.append(
                f"View: {result.processed_object_counts.get('view', 0)} 个"
            )
            summary_lines.append(
                "Materialized View: "
                f"{result.processed_object_counts.get('materialized_view', 0)} 个"
            )
        summary_lines.append(f"输出文件: {len(result.output_files)} 个")

        if result.errors:
            summary_lines.append("\n错误列表:")
            for error in result.errors[:10]:  # 最多显示 10 个错误
                summary_lines.append(f"  - {error}")
            if len(result.errors) > 10:
                summary_lines.append(f"  ... 还有 {len(result.errors) - 10} 个错误")

        summary_lines.append("=" * 60)

        summary_text = "\n".join(summary_lines)
        logger.info(f"\n{summary_text}")

        # 保存汇总报告到文件
        try:
            from metaweave.utils.file_utils import save_text, ensure_dir
            from datetime import datetime

            output_dir = self.config.get("output", {}).get("output_dir", "output")
            output_dir = get_project_root() / output_dir
            ensure_dir(output_dir)

            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            summary_file = output_dir / f"summary_{timestamp}.txt"
            save_text(summary_text, summary_file)
            logger.info(f"汇总报告已保存: {summary_file}")
        except Exception as e:
            logger.error(f"保存汇总报告失败: {e}")

    def _get_ddl_loader(self) -> DDLLoader:
        if self.ddl_loader is None:
            ddl_dir = self.formatter.ddl_dir
            self.ddl_loader = DDLLoader(ddl_dir, database_name=self.database_name)
        return self.ddl_loader

    @property
    def managed_json_loader(self) -> JsonLoader:
        """本轮生成器实际使用的 JsonLoader 只读入口（doc 19.1 §5）。

        受管 MD 核验用它拿到同一加载器（同一份枚举缓存），避免新建
        加载器或回退读盘造成版本错位。
        """
        return self._get_json_loader()

    def _get_json_loader(self) -> JsonLoader:
        """md 步骤的 JSON 产物加载器（惰性构造，doc 17 §3.4）。"""
        if self._json_loader is None:
            self._json_loader = JsonLoader(
                self.formatter.json_dir, database_name=self.database_name
            )
        return self._json_loader

    def _apply_column_statistics(self, metadata: TableMetadata, sample_data):
        if not self.column_stats_enabled or sample_data is None or sample_data.empty:
            return
        for col in metadata.columns:
            if col.column_name in sample_data.columns:
                col.statistics = get_column_statistics(
                    sample_data,
                    col.column_name,
                    value_distribution_threshold=self.value_dist_threshold,
                )
        logger.info(
            f"已计算 {len(metadata.columns)} 个字段的统计信息: {metadata.full_name}"
        )

    def _normalize_step(self, step: str) -> str:
        """标准化步骤参数"""
        normalized = (step or "all").lower()
        if normalized not in SUPPORTED_STEPS:
            raise ValueError(f"不支持的步骤: {step}")
        return normalized

    def _resolve_formats_for_step(self, step: str) -> List[str]:
        """根据步骤确定需要输出的文件格式"""
        mapping = {
            "ddl": ["ddl"],
            "json": ["json"],
            "md": ["markdown"],
        }
        if step == "cql":
            logger.warning("CQL 生成尚未实现，暂不输出文件")
            return []
        return mapping.get(step, [])
