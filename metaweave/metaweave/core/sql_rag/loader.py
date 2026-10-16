"""SQL Example 向量化加载器（Milvus，受管口径 doc 25.2）

读取受管 `output/<source_id>/<job_id>/sql-rag/` 顶层全部非忽略 `*.json`
（目录即输入），对 question 向量化后按源整份替换写入 Milvus
`sql_example_embeddings`；`example_id` 前缀为注入的 `source_id`
（不再读取 `database.database`）。
"""

import hashlib
import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Type
from uuid import UUID

from metaweave.utils.file_utils import resolve_project_path
from metaweave.core.loaders.base import BaseLoader
from services.config_loader import ConfigLoader
from metaweave.services.embedding_service import EmbeddingService
from metaweave.services.vector_db.milvus_client import MilvusClient

logger = logging.getLogger(__name__)

# 顶层忽略前缀（崩溃残留的临时文件不算输入，与解冻条件同口径）
TMP_IGNORE_PREFIX = ".tmp"
# Milvus VARCHAR max_length 计 UTF-8 字节数（doc 25.2 §7.3 字节口径）
QUESTION_SQL_MAX_BYTES = 16384
DOMAIN_MAX_BYTES = 256
EMBEDDING_ATTEMPTS = 3


def collect_sql_example_input_files(input_dir: Path) -> List[Path]:
    """枚举输入目录顶层合格 `*.json` 普通文件，按文件名排序。

    预检（backend `check_sql_output_ready`）与 loader 的 `validate()`/`load()`
    三方共用的唯一枚举实现（doc 25.2 §7.2 第 2 条）：忽略 `.tmp*` 前缀，
    仅取 `*.json` 后缀的普通文件（symlink/子目录/其他后缀不消费）。
    """
    files = (
        [
            path
            for path in input_dir.iterdir()
            if not path.name.startswith(TMP_IGNORE_PREFIX)
            and path.suffix == ".json"
            and path.is_file()
            and not path.is_symlink()
        ]
        if input_dir.is_dir()
        else []
    )
    return sorted(files, key=lambda p: p.name)


def _normalize_uuid_value(value: Any, field: str) -> str:
    """UUID 对象或合法 UUID 字符串统一规范化为字符串（doc 25.2 §7.2 第 1 条）。"""
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, str):
        try:
            return str(UUID(value))
        except ValueError as exc:
            raise ValueError(f"{field} 不是合法 UUID: {value!r}") from exc
    raise ValueError(
        f"{field} 必须是 UUID 对象或 UUID 字符串，实际为 {type(value).__name__}"
    )


def _utf8_len(text: str) -> int:
    return len(text.encode("utf-8"))


def _lazy_import_milvus():
    """延迟导入 pymilvus"""
    from pymilvus import CollectionSchema, DataType, FieldSchema

    return FieldSchema, CollectionSchema, DataType


class SQLExampleLoader(BaseLoader):
    """SQL Example 向量化加载器（受管口径：目录即输入 + 按源整份替换）"""

    def __init__(
        self,
        config: Dict[str, Any],
        metadata_config: Optional[Dict[str, Any]] = None,
        milvus_client_cls: Type[MilvusClient] = MilvusClient,
        embedding_service_cls: Type[EmbeddingService] = EmbeddingService,
        source_id: Any = None,
        publish_id: Any = None,
    ):
        super().__init__(config)
        self.loader_cfg = self.config.get("sql_loader", {})
        raw_input_dir = self.loader_cfg.get("input_dir", "")
        # 空值保持 "" sentinel（validate 按未配置报错），仅非空相对路径走共享 resolver
        self.input_dir = resolve_project_path(raw_input_dir) if raw_input_dir else ""
        self.collection_name = str(self.loader_cfg.get("collection_name", "")).strip()
        self.options = self.loader_cfg.get("options", {})
        self.batch_size = self.options.get("batch_size", 50)

        # source_id/publish_id：UUID 对象或合法 UUID 字符串，统一规范化为字符串；
        # 非法值记录标记，由 validate() 拒绝（构造不抛错）
        self._source_id: Optional[str] = None
        self._publish_id: Optional[str] = None
        self._uuid_error: Optional[str] = None
        try:
            if source_id is not None:
                self._source_id = _normalize_uuid_value(source_id, "source_id")
            if publish_id is not None:
                self._publish_id = _normalize_uuid_value(publish_id, "publish_id")
        except ValueError as exc:
            self._uuid_error = str(exc)

        # 读取 metadata_config 以获取 embedding 和 vector_database 配置
        self.metadata_config_path = self.config.get(
            "metadata_config_file", "configs/metadata_config.yaml"
        )
        # 完整主配置快照注入优先；缺失时 validate 懒加载一次
        self._injected_metadata_config = metadata_config

        self._milvus_client_cls = milvus_client_cls
        self._embedding_service_cls = embedding_service_cls
        self._milvus_client: Optional[MilvusClient] = None
        self._embedding_service: Optional[EmbeddingService] = None
        self._embedding_dim: int = 1024
        self._metadata_config: Dict[str, Any] = {}
        self._replacement_started = False

    # ------------------------------------------------------------------
    # validate（doc 25.2 §7.2）
    # ------------------------------------------------------------------

    def validate(self) -> bool:
        """验证配置和依赖服务（六项按序，任一失败返回 False）"""
        if self._uuid_error is not None:
            logger.error("source_id/publish_id 非法: %s", self._uuid_error)
            return False
        if self._source_id is None or self._publish_id is None:
            logger.error("未注入 source_id/publish_id（受管发布必填）")
            return False

        # 输入目录：存在且顶层至少一个非忽略 *.json 普通文件（共享枚举 helper）；
        # 枚举 OSError（不可读等）同样按校验失败返回 False（§7.2 受控口径）
        if not self.input_dir:
            logger.error("未配置 sql_loader.input_dir")
            return False
        input_path = Path(self.input_dir)
        if not input_path.is_dir():
            logger.error("输入目录不存在或没有可加载的 *.json 样例: %s", self.input_dir)
            return False
        try:
            ready_files = collect_sql_example_input_files(input_path)
        except OSError as exc:
            logger.error("输入目录不可读: %s (%s)", self.input_dir, exc)
            return False
        if not ready_files:
            logger.error("输入目录不存在或没有可加载的 *.json 样例: %s", self.input_dir)
            return False

        if not self.collection_name:
            logger.error("未配置 sql_loader.collection_name")
            return False

        if not _is_positive_int(self.batch_size):
            logger.error("options.batch_size 必须为正整数，实际为 %r", self.batch_size)
            return False

        # 加载 metadata_config（注入优先）
        if self._injected_metadata_config is not None:
            self._metadata_config = self._injected_metadata_config
        else:
            try:
                meta_path = Path(self.metadata_config_path)
                if not meta_path.exists():
                    logger.error(
                        "metadata_config 不存在: %s", self.metadata_config_path
                    )
                    return False
                self._metadata_config = ConfigLoader(str(meta_path)).load()
            except Exception:
                logger.exception("加载 metadata_config 失败")
                return False

        # 获取 embedding 维度
        embedding_config = self._metadata_config.get("embedding", {})
        active_provider = embedding_config.get("active", "qwen")
        provider_config = embedding_config.get("providers", {}).get(active_provider, {})
        self._embedding_dim = provider_config.get("dimensions", 1024)

        # 初始化 embedding service
        try:
            self._embedding_service = self._embedding_service_cls(embedding_config)
        except Exception:
            logger.exception("初始化 EmbeddingService 失败")
            return False

        # 初始化 Milvus client（与 table_schema 加载器同一约束）
        try:
            from metaweave.utils.file_utils import require_mapping

            vector_config = (
                require_mapping(
                    self._metadata_config["vector_database"], "vector_database"
                )
                if "vector_database" in self._metadata_config
                else {}
            )
            active = vector_config.get("active")
            if active != "milvus":
                raise ValueError(
                    "当前仅支持 Milvus，请设置 vector_database.active = "
                    f"'milvus'（实际为 {active!r}）"
                )
            providers = (
                require_mapping(vector_config["providers"], "vector_database.providers")
                if "providers" in vector_config
                else {}
            )
            if "milvus" not in providers:
                raise ValueError("缺少 Milvus 配置: vector_database.providers.milvus")
            milvus_config = require_mapping(
                providers["milvus"], "vector_database.providers.milvus"
            )
            if not milvus_config:
                raise ValueError(
                    "缺少 Milvus 配置: vector_database.providers.milvus（为空配置）"
                )
            self._milvus_client = self._milvus_client_cls(milvus_config)
            self._milvus_client.connect()
        except Exception as e:
            logger.exception("初始化 MilvusClient 失败")
            logger.error("配置校验失败: %s", e)
            return False

        logger.info("SQLExampleLoader 验证通过")
        return True

    # ------------------------------------------------------------------
    # load（doc 25.2 §7.3：目录即输入 + 按源整份替换）
    # ------------------------------------------------------------------

    def load(self) -> Dict[str, Any]:
        """执行加载：读取 → 长度预检 → 向量化 → upsert → 全部成功后删旧。

        内部全捕获、从不抛出；结果结构见 doc 25.2 §7.3 第 8 条。
        """
        start_time = time.time()
        self._replacement_started = False
        result: Dict[str, Any] = {
            "success": False,
            "replacement_started": False,
            "error_kind": None,
            "error_file": None,
            "error_summary": None,
            "total": 0,
            "records_loaded": 0,
            "records_failed": 0,
            "records_unconfirmed": 0,
            "records_skipped": 0,
            "execution_time": 0.0,
        }
        try:
            # 防御性复查（防不经 validate() 直调；正常 Web 路径已被先拦）——
            # 空 input_dir 不落 Path("") == 项目根 的环境漂移枚举（审核修订 27）
            if not _is_positive_int(self.batch_size):
                result["error_summary"] = (
                    f"options.batch_size 必须为正整数（实际为 {self.batch_size!r}）"
                )
                return self._finish(result, start_time)
            if not self.input_dir:
                result["error_summary"] = "未配置 sql_loader.input_dir"
                return self._finish(result, start_time)

            # 1. 输入读取：目录即输入（共享枚举 helper，按文件名排序合并）
            merged = self._read_input_pairs(result)
            if result["error_kind"] == "input":
                return self._finish(result, start_time)
            result["total"] = len(merged)

            # 2. 长度预检与数据类跳过（先于向量化，doc 25.2 修订 16）
            eligible: List[Dict[str, Any]] = []
            for pair in merged:
                question_sql = json.dumps(
                    {"question": pair["question"], "sql": pair["sql"]},
                    ensure_ascii=False,
                )
                q_bytes = _utf8_len(question_sql)
                d_bytes = _utf8_len(pair["domain"])
                if q_bytes > QUESTION_SQL_MAX_BYTES:
                    logger.warning(
                        "question_sql 超过 %d UTF-8 字节（%d），跳过: %s...",
                        QUESTION_SQL_MAX_BYTES,
                        q_bytes,
                        pair["question"][:50],
                    )
                    result["records_skipped"] += 1
                elif d_bytes > DOMAIN_MAX_BYTES:
                    logger.warning(
                        "domain 超过 %d UTF-8 字节（%d），跳过: %s...",
                        DOMAIN_MAX_BYTES,
                        d_bytes,
                        pair["question"][:50],
                    )
                    result["records_skipped"] += 1
                else:
                    eligible.append({**pair, "question_sql": question_sql})

            # 3. 集合核验（先于任何向量化）：仅结构校验 ValueError 归 schema_mismatch；
            # 连接/建库/索引/加载等普通故障冒至第 9 条 catch-all（error_kind=None），
            # 不误导用户删集合（审核修订 29）
            schema, index_params = self._build_schema()
            try:
                self._milvus_client.ensure_collection(
                    self.collection_name,
                    schema,
                    index_params,
                    clean=False,
                    validate_schema=True,
                )
            except ValueError as exc:
                logger.error("集合结构核验失败: %s", exc)
                result["error_kind"] = "schema_mismatch"
                result["error_summary"] = str(exc)
                return self._finish(result, start_time)

            # 4. 分批向量化（仅对未跳过样例）
            for batch_start in range(0, len(eligible), self.batch_size):
                batch = eligible[batch_start : batch_start + self.batch_size]
                self._process_batch(batch, result)

            # 5. 成功判定与删旧
            if result["records_failed"] == 0 and result["records_unconfirmed"] == 0:
                delete_expr = (
                    f'source_id == "{self._source_id}" '
                    f'and publish_id != "{self._publish_id}"'
                )
                # 删旧调用前置位（22.2 修订 16 同款）：全跳过零 upsert 场景下
                # 删旧超时也不得误报未开始替换、错误保留旧 current（审核修订 29）
                self._replacement_started = True
                try:
                    self._milvus_client.delete_by_expr(
                        self.collection_name, delete_expr
                    )
                except Exception as exc:
                    logger.error("按源删旧失败: %s", exc)
                    result["error_kind"] = "cleanup"
                    result["error_summary"] = str(exc)
                    return self._finish(result, start_time)
                result["success"] = True

            return self._finish(result, start_time)
        except Exception as exc:  # noqa: BLE001
            # catch-all：只填摘要与实际 replacement_started，不改写 error_kind
            logger.exception("SQL 样例加载未分类异常")
            result["error_summary"] = str(exc)
            return self._finish(result, start_time)

    def _read_input_pairs(self, result: Dict[str, Any]) -> List[Dict[str, Any]]:
        """枚举并解析输入目录全部合格 `*.json`，逐项校验结构。"""
        merged: List[Dict[str, Any]] = []
        input_path = Path(self.input_dir)
        try:
            files = collect_sql_example_input_files(input_path)
        except OSError as exc:
            # 目录不可读归 input（表②语义），不落 catch-all 表⑥（审核修订 27）
            result["error_kind"] = "input"
            result["error_file"] = None
            result["error_summary"] = str(exc)
            return []
        for file_path in files:
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    pairs = json.load(f)
                if not isinstance(pairs, list):
                    raise ValueError("顶层不是 JSON 数组")
                for pair in pairs:
                    if not isinstance(pair, dict):
                        raise ValueError("样例项不是对象")
                    question = pair.get("question")
                    sql = pair.get("sql")
                    if not isinstance(question, str) or not question:
                        raise ValueError("question 缺失或为空")
                    if not isinstance(sql, str) or not sql:
                        raise ValueError("sql 缺失或为空")
                    domain = pair.get("domain", "")
                    if not isinstance(domain, str):
                        domain = ""
                    merged.append({"question": question, "sql": sql, "domain": domain})
            except Exception as exc:
                result["error_kind"] = "input"
                result["error_file"] = file_path.name
                result["error_summary"] = f"{file_path.name}: {exc}"
                return []
        if not merged:
            result["error_kind"] = "input"
            result["error_file"] = None
            result["error_summary"] = "合并后没有任何可加载的 SQL 样例"
        return merged

    def _process_batch(
        self, batch: List[Dict[str, Any]], result: Dict[str, Any]
    ) -> None:
        """处理一个批次：向量化（外层 3 次）→ 构造行 → upsert（前置置位）。"""
        questions = [p["question"] for p in batch]
        embeddings = None
        for attempt in range(EMBEDDING_ATTEMPTS):
            try:
                embeddings = self._embedding_service.get_embeddings(questions)
                break
            except Exception:
                if attempt < EMBEDDING_ATTEMPTS - 1:
                    logger.warning(
                        "Embedding 服务调用失败，重试 %d/%d",
                        attempt + 1,
                        EMBEDDING_ATTEMPTS - 1,
                    )
                else:
                    logger.exception("Embedding 服务调用失败，批次计入 records_failed")
        if embeddings is None:
            result["records_failed"] += len(batch)
            return

        rows = []
        for pair in batch:
            embedding = embeddings.get(pair["question"])
            if embedding is None:
                logger.warning(
                    "question 未找到对应 embedding，计入失败: %s",
                    pair["question"][:50],
                )
                result["records_failed"] += 1
                continue
            rows.append(
                {
                    "example_id": self.compute_example_id(
                        self._source_id, pair["question"], pair["sql"]
                    ),
                    "source_id": self._source_id,
                    "publish_id": self._publish_id,
                    "question_sql": pair["question_sql"],
                    "domain": pair["domain"],
                    "embedding": (
                        embedding.tolist()
                        if hasattr(embedding, "tolist")
                        else list(embedding)
                    ),
                    "updated_at": int(time.time()),
                }
            )

        if not rows:
            return
        # 每个 upsert 调用之前置位（调用报错不能假定服务端未写入）
        self._replacement_started = True
        try:
            confirmed = self._milvus_client.upsert_batch(self.collection_name, rows)
            result["records_loaded"] += confirmed
            if confirmed < len(rows):
                result["records_unconfirmed"] += len(rows) - confirmed
        except Exception:
            logger.exception("Milvus upsert 失败，批次 %d 条计入未确认", len(rows))
            result["records_unconfirmed"] += len(rows)

    def _build_schema(self):
        """构建 Milvus Collection Schema（doc 25.2 §4.1：七字段）"""
        FieldSchema, CollectionSchema, DataType = _lazy_import_milvus()

        fields = [
            FieldSchema(
                name="example_id",
                dtype=DataType.VARCHAR,
                max_length=256,
                is_primary=True,
                auto_id=False,
            ),
            FieldSchema(name="source_id", dtype=DataType.VARCHAR, max_length=36),
            FieldSchema(name="publish_id", dtype=DataType.VARCHAR, max_length=36),
            FieldSchema(
                name="question_sql",
                dtype=DataType.VARCHAR,
                max_length=16384,
            ),
            FieldSchema(
                name="embedding",
                dtype=DataType.FLOAT_VECTOR,
                dim=self._embedding_dim,
            ),
            FieldSchema(
                name="domain",
                dtype=DataType.VARCHAR,
                max_length=256,
            ),
            FieldSchema(
                name="updated_at",
                dtype=DataType.INT64,
            ),
        ]
        schema = CollectionSchema(fields=fields, description="SQL Example Embeddings")

        index_params = {
            "index_type": "HNSW",
            "metric_type": "COSINE",
            "params": {"M": 16, "efConstruction": 200},
        }
        return schema, index_params

    def _finish(self, result: Dict[str, Any], start_time: float) -> Dict[str, Any]:
        result["replacement_started"] = self._replacement_started
        result["execution_time"] = round(time.time() - start_time, 2)
        level = logger.info if result.get("success") else logger.error
        level(
            "SQLExampleLoader 完成: success=%s total=%d loaded=%d failed=%d "
            "unconfirmed=%d skipped=%d kind=%s",
            result.get("success"),
            result.get("total", 0),
            result.get("records_loaded", 0),
            result.get("records_failed", 0),
            result.get("records_unconfirmed", 0),
            result.get("records_skipped", 0),
            result.get("error_kind"),
        )
        return result

    def close(self) -> None:
        """释放 Milvus 连接；客户端未创建时直接返回，允许重复调用。

        关闭异常仅记告警不抛出（照 `TableSchemaLoader.close()` 语义）。
        """
        if self._milvus_client is None:
            return
        try:
            self._milvus_client.close()
        except Exception:  # noqa: BLE001
            logger.warning("关闭 Milvus 连接失败（忽略）", exc_info=True)

    @staticmethod
    def compute_example_id(source_id: Any, question: str, sql: str) -> str:
        """计算 example_id：`source_id:sha256(question+sql)[:8]`（唯一拼接路径）"""
        prefix = (
            str(source_id)
            if isinstance(source_id, UUID)
            else _normalize_uuid_value(source_id, "source_id")
        )
        content = question + sql
        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()[:8]
        return f"{prefix}:{content_hash}"


def _is_positive_int(value: Any) -> bool:
    """正整数判定：显式排除 bool（isinstance(True, int) 为真）与 0/负数/非整数。"""
    return isinstance(value, int) and not isinstance(value, bool) and value > 0
