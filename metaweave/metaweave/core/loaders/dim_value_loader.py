"""维度值加载器：将维表文本列向量化后写入 Milvus。

doc 22.2 改造：集合 schema 换为 col_id 显式主键（四段拼接）+ source_id/publish_id，
字段改名 col_value_embedding / updated_at；写入走 upsert + 按源整份替换；
发布链路以构造参数注入拦截时归一好的 DimTablesConfig（不重读文件）。
CLI 入口已随 doc 22.2 移除，本加载器仅服务 Web 发布链路与测试。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple, Type

from psycopg import sql

from metaweave.core.dim_value.models import DimTablesConfig, LoaderOptions
from metaweave.core.loaders.base import BaseLoader
from metaweave.services.embedding_service import EmbeddingService
from metaweave.services.vector_db.milvus_client import MilvusClient, _lazy_import_milvus
from metaweave.utils.file_utils import load_yaml, resolve_project_path
from metaweave.utils.logger import get_metaweave_logger
from services.db.pg_connection import PGConnectionManager
from services.config_loader import ConfigLoader

logger = get_metaweave_logger("dim_value.loader")

# doc 22.2 §4.1：col_value 的 UTF-8 字节硬边界（Milvus max_length 计字节数）
COL_VALUE_MAX_BYTES = 1024


class ConfigurationError(Exception):
    """配置错误。"""


class DataValidationError(Exception):
    """数据验证错误。"""


class EmbeddingError(Exception):
    """向量化失败（如 API 限流或超时）。"""


class DimValueLoader(BaseLoader):
    """维度值加载器。

    从 PostgreSQL 读取维表数据，调用 Embedding 模型向量化后，
    加载到向量数据库（当前仅支持 Milvus）的 dim_value_embeddings Collection。
    """

    def __init__(
        self,
        config: Dict[str, Any],
        milvus_client_cls: Type[MilvusClient] = MilvusClient,
        pg_manager_cls: Type[PGConnectionManager] = PGConnectionManager,
        embedding_service_cls: Type[EmbeddingService] = EmbeddingService,
        metadata_config: Optional[Dict[str, Any]] = None,
        source_id: Optional[str] = None,
        publish_id: Optional[str] = None,
        dim_tables: Optional[DimTablesConfig] = None,
    ):
        super().__init__(config)
        # doc 26 §6.2：完整主配置快照注入优先；缺失时懒加载一次
        self._injected_metadata_config = metadata_config
        self.milvus_client_cls = milvus_client_cls
        self.pg_manager_cls = pg_manager_cls
        self.embedding_service_cls = embedding_service_cls

        if not source_id or not publish_id:
            # doc 22.2 §13.2：CLI 已移除，发布链路必传——显式要求注入
            raise ConfigurationError(
                "DimValueLoader 需要 source_id 与 publish_id（Web 发布链路注入）"
            )
        self.source_id = str(source_id)
        self.publish_id = str(publish_id)
        # doc 22.2 审核修订 19：注入拦截时归一好的配置，加载器不再重读文件
        self._injected_dim_tables = dim_tables

        loader_cfg = self.config.get("dim_loader", {})

        self.dim_tables_path = self._resolve_path(
            loader_cfg.get("config_file", "output/dim_config/dim_tables.yaml")
        )
        self.collection_name = str(loader_cfg.get("collection_name", "")).strip()
        self.metadata_config_path = self._resolve_path(
            self.config.get("metadata_config_file", "configs/metadata_config.yaml")
        )

        self.options = LoaderOptions.from_dict(loader_cfg.get("options", {}))

        self._metadata_config: Dict[str, Any] = {}
        self._vector_db_config: Dict[str, Any] = {}
        self._milvus_client: MilvusClient | None = None
        self._embedding_service: EmbeddingService | None = None
        self._pg_manager: PGConnectionManager | None = None
        self._active_database_name: str | None = None
        # doc 22.2 审核修订 16：首个 upsert 调用之前置 True；delete 前确保已 True
        self._replacement_started = False

    # ---- lifecycle ----

    def close(self) -> None:
        """释放 Milvus 连接；客户端未创建时直接返回，允许重复调用。

        关闭异常只记告警，不覆盖原结果或原异常（TableSchemaLoader.close 同款）。
        """

        client = self._milvus_client
        if client is None:
            return
        self._milvus_client = None
        try:
            client.close()
        except Exception:  # noqa: BLE001
            logger.warning("关闭 Milvus 连接失败", exc_info=True)

    def _retry(
        self,
        func,
        retries: int = 3,
        delay: int = 2,
        desc: str | None = None,
    ) -> Any:
        """简单重试封装，用于连接类操作。"""

        last_exc: Exception | None = None
        for attempt in range(1, retries + 1):
            try:
                return func()
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                if attempt < retries:
                    logger.warning(
                        "%s失败（尝试 %d/%d），%d 秒后重试: %s",
                        desc or func.__name__,
                        attempt,
                        retries,
                        delay,
                        exc,
                    )
                    time.sleep(delay)
                else:
                    logger.error(
                        "%s失败（已重试 %d 次）: %s",
                        desc or func.__name__,
                        retries,
                        exc,
                    )
                    raise
        if last_exc:
            raise last_exc

    # ---- helpers ----
    @staticmethod
    def _resolve_path(path_str: str) -> Path:
        # doc 26 §6.2：统一走共享项目路径 resolver
        return resolve_project_path(path_str)

    @staticmethod
    def _truncate_utf8(text: str, limit: int) -> str:
        """按 UTF-8 字节截断且不撕裂多字节字符（doc 22.2 审核修订 17/23）。"""

        encoded = text.encode("utf-8")
        if len(encoded) <= limit:
            return text
        truncated = encoded[:limit]
        while truncated:
            try:
                return truncated.decode("utf-8")
            except UnicodeDecodeError:
                truncated = truncated[:-1]
        return ""

    def _load_metadata_config(self) -> Dict[str, Any]:
        """加载 metadata_config（doc 26 §6.2：注入快照优先，缺失才读盘）"""
        if self._injected_metadata_config is not None:
            return self._injected_metadata_config
        try:
            # 使用 ConfigLoader 以支持环境变量替换（如 ${MILVUS_HOST:localhost}）
            config_loader = ConfigLoader(str(self.metadata_config_path))
            metadata_config = config_loader.load()
            if not metadata_config:
                raise ConfigurationError(
                    f"无法加载 metadata_config.yaml: {self.metadata_config_path}"
                )
            return metadata_config
        except Exception as exc:
            raise ConfigurationError(
                f"加载 metadata_config.yaml 失败: {self.metadata_config_path}\n"
                f"原因: {exc}"
            ) from exc

    def _get_vector_db_config(self) -> Dict[str, Any]:
        metadata_config = self._metadata_config or self._load_metadata_config()
        if "vector_database" not in metadata_config:
            raise ConfigurationError(
                "metadata_config.yaml 缺少 'vector_database' 配置段"
            )

        vector_database = metadata_config["vector_database"]
        required_fields = ["active", "providers"]
        for field in required_fields:
            if field not in vector_database:
                raise ConfigurationError(f"vector_database 缺少必填字段: {field}")

        active = vector_database.get("active")
        if active != "milvus":
            raise ConfigurationError(
                f"当前版本仅支持 Milvus，但配置为: {active}；请设置 vector_database.active = 'milvus'"
            )

        providers = vector_database.get("providers", {})
        milvus_cfg = providers.get("milvus")
        if not milvus_cfg:
            raise ConfigurationError(
                "未找到 Milvus 的配置: vector_database.providers.milvus"
            )
        return milvus_cfg

    def _get_active_database_name(self) -> str:
        metadata_config = self._metadata_config or self._load_metadata_config()
        db_cfg = metadata_config.get("database") or {}
        database_name = str(db_cfg.get("database", "")).strip()
        if not database_name:
            raise ConfigurationError("metadata_config.yaml 缺少 database.database 配置")
        return database_name

    def _get_dim_tables(self) -> DimTablesConfig:
        """注入优先（审核修订 19：一次发布同一份配置内容）；缺省回退读文件仅供测试。"""
        if self._injected_dim_tables is not None:
            return self._injected_dim_tables
        return self._load_dim_tables(self._get_active_database_name())

    def validate(self) -> bool:
        """验证配置与依赖服务。

        注入 dim_tables 时跳过产物存在性与读盘（审核修订 38：文件在拦截后被删/改坏
        不得导致 validate 失败）——exists() 与 _load_dim_tables 只属未注入的回退路径。
        """

        if self._injected_dim_tables is None and not self.dim_tables_path.exists():
            logger.error("dim_tables.yaml 不存在: %s", self.dim_tables_path)
            return False

        if not self.collection_name:
            logger.error("dim_loader.collection_name 未配置")
            return False

        try:
            self._metadata_config = self._load_metadata_config()
            self._vector_db_config = self._get_vector_db_config()
            self._active_database_name = self._get_active_database_name()
            if self._injected_dim_tables is None:
                self._load_dim_tables(self._active_database_name)
        except Exception as exc:  # noqa: BLE001
            logger.error("配置校验失败: %s", exc)
            return False

        try:
            self._embedding_service = self.embedding_service_cls(
                self._metadata_config.get("embedding", {})
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("Embedding 服务初始化失败: %s", exc)
            return False

        try:
            self._milvus_client = self.milvus_client_cls(self._vector_db_config)
            if not self._retry(
                self._milvus_client.test_connection, desc="Milvus 连接测试"
            ):
                return False
        except Exception as exc:  # noqa: BLE001
            logger.error("Milvus 连接测试失败: %s", exc)
            return False

        try:
            self._pg_manager = self.pg_manager_cls(
                self._metadata_config.get("database")
            )
            if not self._retry(
                self._pg_manager.test_connection, desc="PostgreSQL 连接测试"
            ):
                return False
        except Exception as exc:  # noqa: BLE001
            logger.error("PostgreSQL 连接测试失败: %s", exc)
            return False

        return True

    def load(self, clean: bool = False) -> Dict[str, Any]:
        """执行加载操作（结果规则见 doc 22.2 §7.3）。

        - 任一批向量化失败/单条缺向量/写入未确认 → success=False、不删旧记录；
        - 源库查询失败 → error_kind="source"、终止后续表；
        - 集合结构不符 → error_kind="schema_mismatch"；
        - 注入配置含空 embedding_col → error_kind="input"（防御性双保险）；
        - 全部应处理批次成功（含有表零值）→ 按源删除旧记录并成功。
        """

        start_ts = time.time()
        self._replacement_started = False
        result: Dict[str, Any] = {
            "success": True,
            "message": "加载成功",
            "replacement_started": False,
            "error_kind": None,
            "failed_table": None,
            "error_summary": None,
            "tables_total": 0,
            "tables_processed": 0,
            "tables_skipped": 0,
            "records_loaded": 0,
            "records_failed": 0,
            "records_unconfirmed": 0,
            "records_skipped": 0,
        }
        total_tables = 0

        try:
            if not self.collection_name:
                raise ConfigurationError("dim_loader.collection_name 未配置")

            logger.info("开始加载维度值到 Milvus (collection=%s)", self.collection_name)
            if not self._metadata_config:
                self._metadata_config = self._load_metadata_config()
            if not self._vector_db_config:
                self._vector_db_config = self._get_vector_db_config()
            if not self._active_database_name:
                self._active_database_name = self._get_active_database_name()
            if not self._embedding_service:
                self._embedding_service = self.embedding_service_cls(
                    self._metadata_config.get("embedding", {})
                )
            if not self._milvus_client:
                self._milvus_client = self.milvus_client_cls(self._vector_db_config)
            if not self._pg_manager:
                self._pg_manager = self.pg_manager_cls(
                    self._metadata_config.get("database")
                )

            logger.info("初始化依赖服务: PostgreSQL / Milvus / Embedding")
            self._retry(self._milvus_client.connect, desc="Milvus 连接")
            if hasattr(self._pg_manager, "initialize"):
                try:
                    self._retry(self._pg_manager.initialize, desc="PostgreSQL 初始化")
                except Exception:  # noqa: BLE001
                    logger.warning(
                        "PostgreSQL 初始化失败，将在首次访问时重试", exc_info=True
                    )

            dim_tables_config = self._get_dim_tables()
            total_tables = len(dim_tables_config.tables)
            result["tables_total"] = total_tables
            logger.info(
                "读取配置: %s (database=%s, %d 个维表)",
                (
                    self.dim_tables_path
                    if self._injected_dim_tables is None
                    else "<注入配置>"
                ),
                self._active_database_name,
                total_tables,
            )

            # 注入内容防御性校验（doc 22.2 审核修订 30：双保险）
            empty_cols = [
                cfg.full_table_name
                for cfg in dim_tables_config.tables.values()
                if not cfg.embedding_cols_list
            ]
            if empty_cols:
                result.update(
                    success=False,
                    error_kind="input",
                    message=f"dim_tables 配置存在未指定 embedding_col 的表: {', '.join(sorted(empty_cols))}",
                )
                return result

            # 集合结构核验（doc 22.2 §4.3：结构不符 → schema_mismatch，不触碰数据）
            try:
                self._ensure_collection(clean=clean)
            except ValueError as exc:
                result.update(
                    success=False, error_kind="schema_mismatch", message=str(exc)
                )
                return result

            source_failed = False
            for full_name, dim_cfg in dim_tables_config.tables.items():
                embedding_cols = dim_cfg.embedding_cols_list
                if not embedding_cols:
                    # 前置校验已拦截；此处兜底跳过
                    result["tables_skipped"] += 1
                    continue

                logger.info(
                    "加载表: %s.%s（向量化列: %s）",
                    dim_cfg.schema,
                    dim_cfg.table,
                    ", ".join(embedding_cols),
                )

                for embedding_col in embedding_cols:
                    try:
                        cleaned, skipped = self._load_table(
                            dim_cfg.schema, dim_cfg.table, embedding_col
                        )
                    except Exception as exc:  # noqa: BLE001
                        # doc 22.2 审核修订 44：源库查询失败终止后续表
                        logger.error(
                            "源库查询失败，终止后续表: %s.%s 原因: %s",
                            dim_cfg.schema,
                            dim_cfg.table,
                            exc,
                        )
                        result.update(
                            success=False,
                            error_kind="source",
                            failed_table=full_name,
                            error_summary=str(exc),
                            message=f"源库查询失败（{full_name}: {exc}）",
                        )
                        source_failed = True
                        break
                    result["records_skipped"] += skipped

                    if cleaned:
                        stats = self._batch_embed_and_insert(cleaned)
                        result["records_loaded"] += stats["loaded"]
                        result["records_failed"] += stats["failed"]
                        result["records_unconfirmed"] += stats["unconfirmed"]
                    logger.info(
                        "  - 表 %s 清洗后 %d 条，跳过 %d 条",
                        full_name,
                        len(cleaned),
                        skipped,
                    )
                if source_failed:
                    break
                result["tables_processed"] += 1

            if source_failed:
                result["replacement_started"] = self._replacement_started
                return result

            if result["records_failed"] or result["records_unconfirmed"]:
                result["success"] = False
                if result["error_kind"] is None:
                    result["error_kind"] = "embedding"
                result["replacement_started"] = self._replacement_started
                result["message"] = self._compose_failure_message(result)
                return result

            # 全部应处理批次成功（含有表零值，审核修订 43）→ 按源整份替换
            self._replacement_started = True  # 删除调用前确保已置位（审核修订 16）
            self._milvus_client.delete_by_expr(
                self.collection_name,
                f'source_id == "{self.source_id}" and publish_id != "{self.publish_id}"',
            )
            result["replacement_started"] = True

        except Exception as exc:  # noqa: BLE001
            logger.error("加载失败: %s", exc)
            result["success"] = False
            result["message"] = str(exc)
            if result["error_kind"] is None:
                result["error_kind"] = "input"
            result["replacement_started"] = self._replacement_started
        finally:
            result["execution_time"] = round(time.time() - start_ts, 2)
            summary_logger = logger.info if result.get("success") else logger.error
            summary_logger(
                "维度值加载完成: 处理 %d/%d 张表，加载 %d 条，失败 %d 条，未确认 %d 条，跳过 %d 条，耗时 %.2fs",
                result["tables_processed"],
                total_tables or (result["tables_processed"] + result["tables_skipped"]),
                result["records_loaded"],
                result["records_failed"],
                result["records_unconfirmed"],
                result["records_skipped"],
                result["execution_time"],
            )

        return result

    @staticmethod
    def _compose_failure_message(result: Dict[str, Any]) -> str:
        loaded = result["records_loaded"]
        failed = result["records_failed"]
        unconfirmed = result["records_unconfirmed"]
        if unconfirmed:
            return (
                f"维度值未全部发布到 Milvus（成功 {loaded} 条 / 失败 {failed} 条，"
                f"另有 {unconfirmed} 条写入结果未确认），请重新发布"
            )
        return f"维度值未全部发布到 Milvus（成功 {loaded} 条 / 失败 {failed} 条），请重新发布"

    # ---- internal helpers ----
    def _load_dim_tables(self, active_database: str) -> DimTablesConfig:
        dim_tables_yaml = load_yaml(self.dim_tables_path)
        try:
            from metaweave.core.dim_value.models import DimTablesConfig as _Cfg

            return _Cfg.from_yaml(dim_tables_yaml, database=active_database)
        except ValueError as exc:
            raise ConfigurationError(str(exc)) from exc

    def _ensure_collection(self, clean: bool = False) -> None:
        _, _, FieldSchema, CollectionSchema, _, DataType, _ = _lazy_import_milvus()

        fields = [
            FieldSchema(
                name="col_id", dtype=DataType.VARCHAR, max_length=2048, is_primary=True
            ),
            FieldSchema(name="source_id", dtype=DataType.VARCHAR, max_length=36),
            FieldSchema(name="publish_id", dtype=DataType.VARCHAR, max_length=36),
            FieldSchema(name="table_name", dtype=DataType.VARCHAR, max_length=256),
            FieldSchema(name="col_name", dtype=DataType.VARCHAR, max_length=128),
            FieldSchema(name="col_value", dtype=DataType.VARCHAR, max_length=1024),
            FieldSchema(
                name="col_value_embedding", dtype=DataType.FLOAT_VECTOR, dim=1024
            ),
            FieldSchema(name="updated_at", dtype=DataType.INT64),
        ]
        schema = CollectionSchema(
            fields=fields, description="Embedding index for dimension value text fields"
        )
        index_params = {
            "index_type": "HNSW",
            "metric_type": "COSINE",
            "params": {"M": 16, "efConstruction": 200},
        }

        assert self._milvus_client is not None
        self._milvus_client.ensure_collection(
            collection_name=self.collection_name,
            schema=schema,
            index_params=index_params,
            clean=clean,
            validate_schema=True,
            index_field_name="col_value_embedding",
        )

    def _load_table(
        self, schema: str, table: str, embedding_col: str
    ) -> Tuple[List[Dict[str, Any]], int]:
        """读取并清洗单表单列，返回 (待向量化记录, 数据类跳过数)。

        清洗规则（doc 22.2 §7.3）：strip / 跳空 / UTF-8 字节硬边界截断（记 warning）/
        列内去重 / 每表上限；截断后的值照常参与写入（不是跳过）。
        """

        table_start = time.time()
        raw_rows = self._fetch_table_data(schema, table, embedding_col)
        table_name = f"{self._active_database_name}.{schema}.{table}"

        # doc 22.2 审核修订 23：硬边界恒生效，配置只能再收紧
        hard_limit = min(
            self.options.max_text_length or COL_VALUE_MAX_BYTES, COL_VALUE_MAX_BYTES
        )

        cleaned: List[Dict[str, Any]] = []
        skipped = 0
        seen_values = set()

        for row in raw_rows:
            value = None
            if isinstance(row, dict):
                value = row.get("col_value") or row.get(embedding_col)
            elif isinstance(row, (list, tuple)) and row:
                value = row[0]

            if value is None:
                skipped += 1
                continue

            text = str(value).strip()
            if self.options.skip_empty_values and not text:
                skipped += 1
                continue

            byte_len = len(text.encode("utf-8"))
            if byte_len > hard_limit:
                original_len = byte_len
                text = self._truncate_utf8(text, hard_limit)
                logger.warning(
                    "维表值超过 %d 字节上限，已安全截断: 表=%s 列=%s 原始=%d 字节 截断后=%d 字节",
                    hard_limit,
                    table_name,
                    embedding_col,
                    original_len,
                    len(text.encode("utf-8")),
                )

            if text in seen_values:
                skipped += 1
                continue
            seen_values.add(text)

            cleaned.append(
                {
                    "table_name": table_name,
                    "col_name": embedding_col,
                    "col_value": text,
                }
            )

            if (
                self.options.max_records_per_table
                and len(cleaned) >= self.options.max_records_per_table
            ):
                break

        logger.info(
            "  - 读取 %d 条，清洗后可用 %d 条，跳过 %d 条 (耗时 %.2fs)",
            len(raw_rows),
            len(cleaned),
            skipped,
            time.time() - table_start,
        )
        return cleaned, skipped

    def _fetch_table_data(
        self, schema: str, table: str, column: str
    ) -> List[Dict[str, Any]]:
        """从 PostgreSQL 获取维表数据。"""

        assert self._pg_manager is not None
        limit_clause = (
            sql.SQL(" LIMIT %s") if self.options.max_records_per_table else sql.SQL("")
        )

        query = (
            sql.SQL(
                "SELECT DISTINCT CAST({col} AS TEXT) AS col_value "
                "FROM {schema}.{table} "
                "WHERE {col} IS NOT NULL AND LENGTH(BTRIM(CAST({col} AS TEXT))) > 0"
            ).format(
                col=sql.Identifier(column),
                schema=sql.Identifier(schema),
                table=sql.Identifier(table),
            )
            + limit_clause
        )

        params: Tuple[Any, ...] | List[Any] = []
        if self.options.max_records_per_table:
            params = [self.options.max_records_per_table]

        with self._pg_manager.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(query, params or None)
                rows = cur.fetchall()

        return rows

    def _batch_embed_and_insert(self, records: List[Dict[str, Any]]) -> Dict[str, int]:
        """逐批向量化并 upsert（doc 22.2 §7.3 结果规则）。

        - 批 Embedding 重试耗尽（3 次尝试）→ 该批计入 failed，跳批继续；
        - 单条缺向量 → failed +1；
        - upsert 调用前置 replacement_started（审核修订 16）；
        - upsert 抛错 → 整批计入 unconfirmed、跳批继续；
        - 返回数不足 → loaded += 确认数、unconfirmed += 差额（审核修订 29）。
        """

        loaded_total = 0
        failed_total = 0
        unconfirmed_total = 0

        assert self._embedding_service is not None
        assert self._milvus_client is not None

        max_retries = 2
        for batch in self._iter_batches(records, self.options.batch_size):
            texts = [r["col_value"] for r in batch]
            embeddings = None
            for attempt in range(max_retries + 1):
                try:
                    embeddings = self._embedding_service.get_embeddings(texts)
                    break
                except Exception as exc:  # noqa: BLE001
                    if attempt < max_retries:
                        logger.warning(
                            "  - 向量化失败（尝试 %d/%d），重试中: %s",
                            attempt + 1,
                            max_retries + 1,
                            exc,
                        )
                    else:
                        logger.error(
                            "  - 向量化失败（已重试 %d 次），跳过该批次 %d 条记录: %s",
                            max_retries,
                            len(batch),
                            exc,
                        )
            if embeddings is None:
                failed_total += len(batch)
                continue

            enriched: List[Dict[str, Any]] = []
            ts = int(time.time())
            for rec in batch:
                vec = embeddings.get(rec["col_value"])
                if vec is None:
                    failed_total += 1
                    continue

                enriched.append(
                    {
                        **rec,
                        "col_id": (
                            f"{self.source_id}:{rec['table_name']}:"
                            f"{rec['col_name']}:{rec['col_value']}"
                        ),
                        "source_id": self.source_id,
                        "publish_id": self.publish_id,
                        "col_value_embedding": (
                            vec.tolist() if hasattr(vec, "tolist") else vec
                        ),
                        "updated_at": ts,
                    }
                )

            if not enriched:
                continue

            # upsert 调用前置位：调用报错也不能假定服务端完全没有写入
            self._replacement_started = True
            try:
                written = self._milvus_client.upsert_batch(
                    self.collection_name, enriched
                )
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "  - Milvus upsert 抛错，该批 %d 条计入未确认，跳过继续: %s",
                    len(enriched),
                    exc,
                )
                unconfirmed_total += len(enriched)
                continue

            loaded_total += written
            if written < len(enriched):
                shortfall = len(enriched) - written
                logger.error(
                    "  - Milvus 写入数量不足: 期望 %d，确认 %d，差额 %d 条计入未确认",
                    len(enriched),
                    written,
                    shortfall,
                )
                unconfirmed_total += shortfall

        return {
            "loaded": loaded_total,
            "failed": failed_total,
            "unconfirmed": unconfirmed_total,
        }

    @staticmethod
    def _iter_batches(
        items: List[Dict[str, Any]], batch_size: int
    ) -> Iterable[List[Dict[str, Any]]]:
        for i in range(0, len(items), batch_size):
            yield items[i : i + batch_size]


__all__ = [
    "DimValueLoader",
    "ConfigurationError",
    "DataValidationError",
    "EmbeddingError",
]
