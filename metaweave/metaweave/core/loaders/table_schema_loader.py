from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Type
from uuid import uuid4

from metaweave.core.loaders.base import BaseLoader
from metaweave.core.table_schema import (
    VARCHAR_LIMITS,
    LoaderOptions,
    MDParser,
    JSONExtractor,
    ObjectType,
    SchemaObject,
    check_record_lengths,
    is_empty_description,
)
from metaweave.utils.source_id import normalize_run_uuid, normalize_source_id
from metaweave.services.embedding_service import EmbeddingService
from metaweave.services.vector_db.milvus_client import MilvusClient, _lazy_import_milvus
from metaweave.utils.file_utils import resolve_project_path
from metaweave.utils.logger import get_metaweave_logger
from services.config_loader import ConfigLoader


logger = get_metaweave_logger("table_schema.loader")


class TableSchemaLoader(BaseLoader):
    """表结构加载器：读取 MD + JSON v3，向量化后写入 Milvus。"""

    def __init__(
        self,
        config: Dict[str, Any],
        milvus_client_cls: Type[MilvusClient] = MilvusClient,
        embedding_service_cls: Type[EmbeddingService] = EmbeddingService,
        metadata_config: Optional[Dict[str, Any]] = None,
        publish_run_id: Optional[str] = None,
    ):
        super().__init__(config)
        # doc 26 §6.2：完整主配置快照注入优先；缺失时经
        # metadata_config_file 懒加载一次（兼容直接类调用）
        self._injected_metadata_config = metadata_config
        loader_cfg = self.config.get("table_schema_loader", {})

        self.md_directory = self._resolve_path(
            loader_cfg.get("md_directory", "output/md")
        )
        self.json_llm_directory = self._resolve_path(
            loader_cfg.get("json_llm_directory", "output/json")
        )
        self.collection_name = str(loader_cfg.get("collection_name", "")).strip()
        self.metadata_config_path = self._resolve_path(
            self.config.get("metadata_config_file", "configs/metadata_config.yaml")
        )
        self.options = LoaderOptions.from_dict(loader_cfg.get("options", {}))
        self.managed = bool(loader_cfg.get("source_id"))
        self.source_id: Optional[str] = None
        self.job_id: Optional[str] = None
        self.publish_run_id: Optional[str] = None
        if self.managed:
            # doc 20.1 §10.1：同一批记录共用规范化的 source_id / publish_run_id。
            # 未传 publish_run_id（调试 CLI）时生成仅作 Milvus 批次标记的 UUID。
            self.source_id = str(normalize_source_id(loader_cfg["source_id"]))
            if loader_cfg.get("job_id"):
                self.job_id = str(loader_cfg["job_id"])
            self.publish_run_id = str(normalize_run_uuid(publish_run_id or uuid4()))
        self._prepared = None
        self._prepared_objects = None
        self._embedding_dim: Optional[int] = None

        self._metadata_config: Dict[str, Any] = {}
        self._vector_db_config: Dict[str, Any] = {}
        self._milvus_client: Optional[MilvusClient] = None
        self._embedding_service: Optional[EmbeddingService] = None

        self.milvus_client_cls = milvus_client_cls
        self.embedding_service_cls = embedding_service_cls

    # ---- helpers ----
    @staticmethod
    def _resolve_path(path_str: str) -> Path:
        # doc 26 §6.2：统一走共享项目路径 resolver
        return resolve_project_path(path_str)

    def _load_metadata_config(self) -> Dict[str, Any]:
        if self._injected_metadata_config is not None:
            return self._injected_metadata_config
        try:
            loader = ConfigLoader(str(self.metadata_config_path))
            metadata_config = loader.load()
            if not metadata_config:
                raise ValueError(
                    f"无法加载 metadata_config.yaml: {self.metadata_config_path}"
                )
            return metadata_config
        except Exception as exc:  # noqa: BLE001
            raise ValueError(f"加载 metadata_config.yaml 失败: {exc}") from exc

    def _get_vector_db_config(self) -> Dict[str, Any]:
        metadata_config = self._metadata_config or self._load_metadata_config()
        vector_db = metadata_config.get("vector_database") or {}
        active = vector_db.get("active")
        if active != "milvus":
            raise ValueError(
                "当前仅支持 Milvus，请设置 vector_database.active = 'milvus'"
            )
        milvus_cfg = (vector_db.get("providers") or {}).get("milvus")
        if not milvus_cfg:
            raise ValueError("缺少 Milvus 配置: vector_database.providers.milvus")
        return milvus_cfg

    def _get_embedding_config(self) -> Dict[str, Any]:
        metadata_config = self._metadata_config or self._load_metadata_config()
        embedding_cfg = metadata_config.get("embedding") or {}
        if not embedding_cfg:
            raise ValueError("metadata_config.yaml 缺少 embedding 配置")
        return embedding_cfg

    def _get_embedding_dimension(self) -> int:
        embedding_cfg = self._get_embedding_config()
        active = embedding_cfg.get("active")
        providers = embedding_cfg.get("providers") or {}
        active_cfg = providers.get(active, {})
        dim = active_cfg.get("dimensions")
        if not dim:
            raise ValueError(
                "未找到 embedding 维度配置 embedding.providers.{active}.dimensions"
            )
        return int(dim)

    # ---- lifecycle ----
    def prepare_input(self, expected_json_marker=None, database_name=None):
        """在外部服务初始化和清理前完成受管输入解析，保留内存快照。

        字段解析、空描述处理和长度预检全部由共用函数完成（doc 20.1 §1.2）。
        """
        from metaweave.core.table_schema.managed_input import (
            prepare_managed_table_schema_input,
        )

        if self.managed:
            if self.options.max_tables:
                raise ValueError("受管发布不允许 max_tables 截断对象范围")
            self._prepared = prepare_managed_table_schema_input(
                self.json_llm_directory,
                self.md_directory,
                self.source_id,
                self.options,
                expected_json_marker,
                database_name,
            )
            self._prepared_objects = self._prepared.groups
        return self._prepared_objects

    @property
    def empty_desc_columns(self) -> List[str]:
        return list(self._prepared.empty_desc_columns) if self._prepared else []

    def close(self) -> None:
        """释放 Milvus 连接；客户端未创建时直接返回，允许重复调用。

        关闭异常只记录告警，不覆盖原结果或原异常。
        """
        client = self._milvus_client
        if client is None:
            return
        self._milvus_client = None
        try:
            client.close()
        except Exception:  # noqa: BLE001
            logger.warning("Milvus 连接释放失败")

    def validate(self) -> bool:
        """验证配置和依赖服务。"""
        if not self.md_directory.exists():
            logger.error("md_directory 不存在: %s", self.md_directory)
            return False

        if not self.json_llm_directory.exists():
            logger.error("json_llm_directory 不存在: %s", self.json_llm_directory)
            return False

        if not self.collection_name:
            logger.error("table_schema_loader.collection_name 未配置")
            return False

        try:
            if self.managed and self._prepared_objects is None:
                self.prepare_input()
            self._metadata_config = self._load_metadata_config()
            self._vector_db_config = self._get_vector_db_config()
        except Exception as exc:  # noqa: BLE001
            logger.error("配置校验失败: %s", exc)
            return False

        try:
            self._embedding_service = self.embedding_service_cls(
                self._get_embedding_config()
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("Embedding 服务初始化失败: %s", exc)
            return False

        try:
            self._milvus_client = self.milvus_client_cls(self._vector_db_config)
            if not self._milvus_client.test_connection(create_database=True):
                return False
        except Exception as exc:  # noqa: BLE001
            logger.error("Milvus 连接测试失败: %s", exc)
            return False

        return True

    def load(self, clean: bool = False) -> Dict[str, Any]:
        """加载表结构。

        受管输入（带 source_id）：忽略 ``clean``，固定为“向量化全部成功 → 分批 upsert
        → 全部成功后按源清理旧批次”；结果中的 ``replacement_started`` 表示是否已调用
        首批 upsert。非受管内部调试路径沿用旧的 clean/insert、upsert 语义。
        """
        start_ts = time.time()
        result: Dict[str, Any] = {
            "success": True,
            "message": "加载成功",
            "tables_processed": 0,
            "tables_skipped": 0,
            "objects_loaded": 0,
            "objects_skipped": 0,
            "empty_desc_columns": [],
            "replacement_started": False,
        }

        try:
            if self.managed and self._prepared_objects is None:
                self.prepare_input()
            if not self.collection_name:
                raise ValueError("table_schema_loader.collection_name 未配置")

            groups = self._collect_groups(result)
            if self.managed:
                result["empty_desc_columns"] = self.empty_desc_columns
                self._log_empty_desc_columns(result["empty_desc_columns"])
                # 构造最终记录后、Embedding 之前再核对一次全部 VARCHAR（含实际 publish_run_id）
                for objects in groups:
                    for obj in objects:
                        check_record_lengths(self._build_record(obj, None))

            if not self._metadata_config:
                self._metadata_config = self._load_metadata_config()
            if not self._vector_db_config:
                self._vector_db_config = self._get_vector_db_config()
            if not self._embedding_service:
                self._embedding_service = self.embedding_service_cls(
                    self._get_embedding_config()
                )
            if not self._milvus_client:
                self._milvus_client = self.milvus_client_cls(self._vector_db_config)

            logger.info("初始化依赖服务: Milvus / Embedding")
            self._milvus_client.connect(create_database=True)

            # 受管：不 drop 集合，先校验/创建再 Embedding；非受管沿用 clean 语义
            self._ensure_collection(clean=False if self.managed else clean)
            logger.info("确保 Collection 存在: %s", self.collection_name)

            all_objects = [obj for objects in groups for obj in objects]
            vectors, embed_skipped = self._embed_all(all_objects)
            result["objects_skipped"] += embed_skipped
            if self.managed and embed_skipped:
                raise ValueError("表结构向量化未全部成功，请重新发布")
            self._check_vector_dimensions(vectors)

            written_total, write_skipped = self._write_batches(
                all_objects, vectors, clean, result
            )
            result["objects_loaded"] += written_total
            result["objects_skipped"] += write_skipped
            result["tables_processed"] = sum(1 for objects in groups if objects)

            if self.managed:
                if (
                    result["tables_skipped"]
                    or result["objects_skipped"]
                    or result["objects_loaded"] != len(all_objects)
                    or not all_objects
                ):
                    raise ValueError("表结构未全部加载成功，请重新发布")
                self._cleanup_old_batches()

        except Exception as exc:  # noqa: BLE001
            logger.error("加载失败: %s", exc, exc_info=True)
            result["success"] = False
            result["message"] = str(exc)
        finally:
            result["execution_time"] = round(time.time() - start_ts, 2)

        return result

    # ---- core steps ----
    def _collect_groups(self, result: Dict[str, Any]) -> List[List[SchemaObject]]:
        """受管：直接使用输入快照；非受管：逐表读取，单表错误按表跳过。"""
        if self.managed:
            return list(self._prepared_objects)

        md_files = sorted(self.md_directory.glob("*.md"))
        max_tables = (
            self.options.max_tables if self.options.max_tables else len(md_files)
        )
        groups: List[List[SchemaObject]] = []
        for idx, md_file in enumerate(md_files[:max_tables], start=1):
            logger.info("[%d/%d] 处理表 Markdown", idx, min(max_tables, len(md_files)))
            try:
                objects = self._load_table_objects(md_file)
            except Exception as exc:  # noqa: BLE001
                logger.warning("跳过表 %s，原因: %s", md_file.name, exc)
                result["tables_skipped"] += 1
                continue
            if not objects:
                logger.info("  - 未生成对象，跳过")
                result["tables_skipped"] += 1
                continue
            groups.append(objects)
        return groups

    def _log_empty_desc_columns(self, columns: List[str]) -> None:
        for object_id in columns:
            logger.info(
                "跳过空描述字段 source_id=%s job_id=%s object_id=%s reason=empty_description",
                self.source_id,
                self.job_id,
                object_id,
            )
        if columns:
            logger.info("空描述跳过字段共 %d 个: %s", len(columns), ", ".join(columns))

    def _build_record(self, obj: SchemaObject, embedding: Any) -> Dict[str, Any]:
        return obj.to_milvus_dict(
            embedding=embedding,
            source_id=self.source_id,
            publish_run_id=self.publish_run_id,
        )

    def _ensure_collection(self, clean: bool = False) -> None:
        _, _, FieldSchema, CollectionSchema, _, DataType, _ = _lazy_import_milvus()

        dim = self._get_embedding_dimension()
        self._embedding_dim = dim
        # doc 20.1 §10.6.2：11 个字段，唯一主键 entity_id；VARCHAR 上限统一取自 VARCHAR_LIMITS（与预检同源）
        fields = [
            FieldSchema(
                name="entity_id",
                dtype=DataType.VARCHAR,
                max_length=VARCHAR_LIMITS["entity_id"],
                is_primary=True,
                auto_id=False,
            ),
            FieldSchema(
                name="source_id",
                dtype=DataType.VARCHAR,
                max_length=VARCHAR_LIMITS["source_id"],
            ),
            FieldSchema(
                name="publish_run_id",
                dtype=DataType.VARCHAR,
                max_length=VARCHAR_LIMITS["publish_run_id"],
            ),
            FieldSchema(
                name="object_id",
                dtype=DataType.VARCHAR,
                max_length=VARCHAR_LIMITS["object_id"],
            ),
            FieldSchema(
                name="object_type",
                dtype=DataType.VARCHAR,
                max_length=VARCHAR_LIMITS["object_type"],
            ),
            FieldSchema(
                name="parent_id",
                dtype=DataType.VARCHAR,
                max_length=VARCHAR_LIMITS["parent_id"],
            ),
            FieldSchema(
                name="object_desc",
                dtype=DataType.VARCHAR,
                max_length=VARCHAR_LIMITS["object_desc"],
            ),
            FieldSchema(name="embedding", dtype=DataType.FLOAT_VECTOR, dim=dim),
            FieldSchema(
                name="time_col_hint",
                dtype=DataType.VARCHAR,
                max_length=VARCHAR_LIMITS["time_col_hint"],
            ),
            FieldSchema(
                name="table_category",
                dtype=DataType.VARCHAR,
                max_length=VARCHAR_LIMITS["table_category"],
            ),
            FieldSchema(name="updated_at", dtype=DataType.INT64),
        ]
        schema = CollectionSchema(
            fields=fields,
            description="Table and column schema embeddings for NL2SQL",
            enable_dynamic_field=False,
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
        )

    def _load_table_objects(self, md_file: Path) -> List[SchemaObject]:
        """非受管内部调试路径：不与 JSON 字段全集对账，空描述按旧口径跳过。"""
        md_parser = MDParser(md_file)
        # JSON 与 MD 采用同名文件（仅扩展名不同），直接使用文件名对齐。
        table_name = md_file.stem

        json_file = self.json_llm_directory / f"{table_name}.json"
        json_extractor = JSONExtractor(json_file)

        desc = md_parser.get_table_description().strip()
        if not desc:
            logger.warning("MD 内容为空，跳过表: %s", table_name)
            return []
        now = int(time.time())
        objects: List[SchemaObject] = [
            SchemaObject(
                object_type=ObjectType.TABLE,
                object_id=table_name,
                parent_id=table_name,
                object_desc=desc,
                time_col_hint=json_extractor.format_time_col_hint(),
                table_category=json_extractor.get_table_category(),
                updated_at=now,
            )
        ]
        if self.options.include_columns:
            for col_name, col_desc in md_parser.get_column_descriptions().items():
                if is_empty_description(col_desc):
                    if self.options.skip_empty_desc:
                        continue
                    raise ValueError(f"字段没有有效描述: {table_name}.{col_name}")
                objects.append(
                    SchemaObject(
                        object_type=ObjectType.COLUMN,
                        object_id=f"{table_name}.{col_name}",
                        parent_id=table_name,
                        object_desc=col_desc.strip(),
                        updated_at=now,
                    )
                )
        return objects

    def _embed_all(self, objects: List[SchemaObject]) -> "tuple[Dict[str, Any], int]":
        """为全部对象完成向量化；返回 {描述文本: 向量} 与失败/缺失的对象数。

        向量保留 Embedding 服务返回的原始形式（NumPy 数组），不在此阶段转换。
        """
        assert self._embedding_service is not None
        vectors: Dict[str, Any] = {}
        skipped = 0
        max_retries = 2

        for batch in self._iter_batches(objects, self.options.batch_size):
            texts = [obj.object_desc for obj in batch]
            embeddings = None
            for attempt in range(max_retries + 1):
                try:
                    embeddings = self._embedding_service.get_embeddings(texts)
                    break
                except Exception as exc:  # noqa: BLE001
                    if attempt < max_retries:
                        logger.warning(
                            "向量化失败（尝试 %d/%d），重试中: %s",
                            attempt + 1,
                            max_retries + 1,
                            exc,
                        )
                    else:
                        logger.error(
                            "向量化失败（已重试 %d 次），该批次 %d 条记录无向量: %s",
                            max_retries,
                            len(batch),
                            exc,
                        )
            if embeddings is None:
                skipped += len(batch)
                continue
            for obj in batch:
                vec = embeddings.get(obj.object_desc)
                if vec is None:
                    skipped += 1
                else:
                    vectors[obj.object_desc] = vec
        return vectors, skipped

    def _check_vector_dimensions(self, vectors: Dict[str, Any]) -> None:
        """首次 upsert 前核对实际向量维度与集合维度一致。"""
        expected = self._embedding_dim
        if expected is None:
            return
        for vec in vectors.values():
            actual = int(vec.shape[-1]) if hasattr(vec, "shape") else len(vec)
            if actual != expected:
                raise ValueError(
                    f"向量维度与集合不一致: 实际 {actual}，集合 {expected}"
                )

    def _write_batches(
        self,
        objects: List[SchemaObject],
        vectors: Dict[str, Any],
        clean: bool,
        result: Dict[str, Any],
    ) -> "tuple[int, int]":
        """按 batch_size 分批写入；仅在构造当前批次时把该批向量转为 Python 列表。"""
        assert self._milvus_client is not None
        loaded = 0
        skipped = 0
        use_insert = clean and not self.managed

        for batch in self._iter_batches(objects, self.options.batch_size):
            records: List[Dict[str, Any]] = []
            for obj in batch:
                vec = vectors.get(obj.object_desc)
                if vec is None:
                    skipped += 1
                    continue
                records.append(
                    self._build_record(
                        obj, vec.tolist() if hasattr(vec, "tolist") else vec
                    )
                )
            if not records:
                continue

            # 即使本次调用报错也不假定它完全没有写入
            result["replacement_started"] = True
            if use_insert:
                written = self._milvus_client.insert_batch(
                    self.collection_name, records
                )
            else:
                written = self._milvus_client.upsert_batch(
                    self.collection_name, records
                )
            loaded += written
            if self.managed and written != len(records):
                raise ValueError(
                    f"Milvus 写入数量不足: 期望 {len(records)}，实际 {written}"
                )
            skipped += max(0, len(records) - written)
            records = None  # 释放该批临时列表

        return loaded, skipped

    def _cleanup_old_batches(self) -> None:
        """全部 upsert 成功后，仅删除本源中不属于本次批次的记录。"""
        assert self._milvus_client is not None
        expr = f'source_id == "{self.source_id}" and publish_run_id != "{self.publish_run_id}"'
        deleted = self._milvus_client.delete_by_expr(self.collection_name, expr)
        logger.info("已清理本源旧批次记录: %s 条", deleted)

    @staticmethod
    def _iter_batches(
        items: List[SchemaObject], batch_size: int
    ) -> List[List[SchemaObject]]:
        for i in range(0, len(items), batch_size):
            yield items[i : i + batch_size]


__all__ = ["TableSchemaLoader"]
