"""Milvus 向量数据库客户端封装（公共层）。

本模块从 MetaWeave 下沉到公共服务层，供 NL2SQL 和 MetaWeave 共同使用。
不继承任何基类，仅提供连接和基础操作能力。
"""

import logging
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


def _lazy_import_milvus() -> Tuple[Any, ...]:
    """延迟导入 pymilvus，避免在未安装时立刻失败。"""

    try:
        from pymilvus import (
            Collection,
            CollectionSchema,
            DataType,
            FieldSchema,
            connections,
            db,
            utility,
        )
    except ImportError as exc:  # pragma: no cover - 在未安装 pymilvus 时用于友好提示
        raise ImportError("pymilvus 未安装，请安装后再使用 MilvusClient") from exc

    return connections, db, FieldSchema, CollectionSchema, Collection, DataType, utility


class MilvusClient:
    """Milvus 客户端封装（公共层）。

    不继承 BaseVectorClient，避免公共组件反向依赖 MetaWeave。
    """

    def __init__(self, config: Dict[str, Any]):
        # 配置必须是 mapping（用户审核第七轮第 1 条）：`dict(config or {})`
        # 会把 []/None/false 静默变成 {}，使加载器以默认 host/port/database
        # "验证通过"，而非 clean 路径的 resolver 却会拒绝同一配置。
        if not isinstance(config, dict):
            raise ValueError(
                f"Milvus 配置必须是对象（mapping），实际为 {type(config).__name__}"
            )
        # 与 clean 预警的规范化保持一致（用户审核第六轮第 1 条）：host 去首尾
        # 空白并小写、database 去首尾空白——否则预警 identity（已规范化）与
        # 实际连接参数（原值）可能不同，运行期比较无法发现。
        config = dict(config)
        if isinstance(config.get("host"), str):
            config["host"] = config["host"].strip().lower()
        # database 统一规则（用户审核第六轮第 3 条）：只有键缺失才不切换
        # 数据库；键存在时必须是非空字符串（[]/false/空白此前被 falsy 判断
        # 静默跳过、落到默认库）
        if "database" in config:
            raw_db = config["database"]
            if not isinstance(raw_db, str) or not raw_db.strip():
                raise ValueError(
                    "Milvus database 必须是非空字符串或省略该键"
                    f"（实际为 {type(raw_db).__name__}；省略时不切换数据库）"
                )
            config["database"] = raw_db.strip()
        self.config = config
        self.alias = config.get("alias", "default")
        self.connected = False

    def connect(self, create_database: bool = False) -> None:
        """建立连接并切换到配置的 database。

        默认 database 不存在时清晰失败（NL2SQL 查询侧）；``create_database=True``
        时与 ``ensure_collection()`` 一致，缺失则创建（Loader 首次初始化侧）。
        """
        connections, db, *_, utility = _lazy_import_milvus()
        if self.connected:
            return

        # 建立连接
        connections.connect(
            alias=self.alias,
            host=self.config.get("host", "localhost"),
            port=str(self.config.get("port", "19530")),
            user=self.config.get("user"),
            password=self.config.get("password"),
            timeout=self.config.get("timeout", 30),
        )

        # ⚠️ 在设置 connected=True 之前先验证 database（避免状态污染）
        db_name = self.config.get("database")
        if db_name:
            # 检查 database 是否存在（不自动创建，遵循"清晰失败"原则）
            # ⚠️ 传递 using=self.alias 以操作正确的连接
            existing_databases = db.list_database(using=self.alias)
            if db_name not in existing_databases and create_database:
                db.create_database(db_name, using=self.alias)
            elif db_name not in existing_databases:
                raise ValueError(
                    f"Milvus database '{db_name}' 不存在。\n"
                    f"可用的 databases: {existing_databases}\n"
                    f"请先运行 MetaWeave Loader 创建 database 和 Collection，然后再启动 NL2SQL 模块。"
                )
            # ⚠️ 传递 using=self.alias 以操作正确的连接
            db.using_database(db_name, using=self.alias)

        # ✅ 所有验证通过后才设置 connected=True（避免状态污染）
        self.connected = True
        logger.info(
            "✅ 已连接 Milvus: %s:%s/%s",
            self.config.get("host"),
            self.config.get("port"),
            db_name,
        )

    def test_connection(self, create_database: bool = False) -> bool:
        try:
            self.connect(create_database=create_database)
            *_, utility = _lazy_import_milvus()
            # 使用 utility.list_collections() 来测试连接
            utility.list_collections(using=self.alias)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.error("Milvus 连接测试失败: %s", exc)
            return False

    @staticmethod
    def _field_signature(field: Any) -> Dict[str, Any]:
        """提取字段定义中参与结构核对的属性（类型、主键、长度、维度）。"""
        params = getattr(field, "params", None) or {}

        def _as_int(value: Any) -> Optional[int]:
            if value is None:
                return None
            try:
                return int(value)
            except (TypeError, ValueError):
                return None

        return {
            "dtype": getattr(field, "dtype", None),
            "is_primary": bool(getattr(field, "is_primary", False)),
            "auto_id": bool(getattr(field, "auto_id", False)),
            "max_length": _as_int(params.get("max_length")),
            "dim": _as_int(params.get("dim")),
        }

    @classmethod
    def validate_collection_schema(
        cls, existing_schema: Any, expected_schema: Any
    ) -> None:
        """按字段名集合核对已有集合结构；字段声明顺序不参与比较。

        字段缺失、多出、类型/主键/auto_id/VARCHAR 上限/向量维度不符均抛 ValueError。
        """
        existing = {f.name: f for f in getattr(existing_schema, "fields", [])}
        expected = {f.name: f for f in getattr(expected_schema, "fields", [])}

        problems: List[str] = []
        missing = sorted(set(expected) - set(existing))
        extra = sorted(set(existing) - set(expected))
        if missing:
            problems.append(f"缺少字段 {missing}")
        if extra:
            problems.append(f"多出字段 {extra}")
        for name in sorted(set(expected) & set(existing)):
            want = cls._field_signature(expected[name])
            have = cls._field_signature(existing[name])
            for key in ("dtype", "is_primary", "auto_id", "max_length", "dim"):
                if want[key] != have[key]:
                    problems.append(
                        f"字段 {name} 的 {key} 不符（期望 {want[key]}，实际 {have[key]}）"
                    )
        if problems:
            raise ValueError("已有集合结构与期望不一致: " + "；".join(problems))

    def ensure_collection(
        self,
        collection_name: str,
        schema: Any,
        index_params: Dict[str, Any],
        clean: bool = False,
        validate_schema: bool = False,
        index_field_name: str = "embedding",
    ) -> Any:
        """确保 Collection 存在（用于 MetaWeave Loader）。

        注意：此方法会自动创建 database（如果不存在），与 connect() 的"清晰失败"策略不同。
        - NL2SQL 使用 connect() → database 不存在时报错
        - MetaWeave Loader 使用 ensure_collection() → 自动创建 database

        validate_schema 默认关闭，保持 sql/dim_value 等加载器的既有行为；
        仅 table_schema 传 True（doc 20.1 §10.3/§10.6.3）：集合已存在时，在创建索引、
        加载集合之前按字段名集合核对结构，不符则抛 ValueError，不修改任何数据。
        """
        connections, db, FieldSchema, CollectionSchema, Collection, _, utility = (
            _lazy_import_milvus()
        )

        # ⚠️ 不调用 self.connect()，自己管理连接（避免 database 不存在时报错）
        if not self.connected:
            connections.connect(
                alias=self.alias,
                host=self.config.get("host", "localhost"),
                port=str(self.config.get("port", "19530")),
                user=self.config.get("user"),
                password=self.config.get("password"),
                timeout=self.config.get("timeout", 30),
            )
            self.connected = True

        # 自动创建 database（如果不存在）
        db_name = self.config.get("database")
        if db_name:
            # ⚠️ 传递 using=self.alias 以操作正确的连接
            if db_name not in db.list_database(using=self.alias):
                db.create_database(db_name, using=self.alias)
            db.using_database(db_name, using=self.alias)

        # 使用 utility.list_collections() 替代 db.list_collections()
        existing_collections = utility.list_collections(using=self.alias)

        if clean and collection_name in existing_collections:
            Collection(collection_name, using=self.alias).drop()

        if collection_name not in utility.list_collections(using=self.alias):
            collection = Collection(
                name=collection_name,
                schema=schema,
                shards_num=self.config.get("shards_num", 2),
                using=self.alias,  # 明确指定使用的连接别名
            )
        else:
            collection = Collection(collection_name, using=self.alias)
            if validate_schema:
                self.validate_collection_schema(collection.schema, schema)

        # 创建向量索引（若不存在）。index_field_name 缺省 "embedding" 保持
        # table_schema / sql 现行为；dim_value 传 "col_value_embedding"（doc 22.2 审核修订 1）。
        if not collection.indexes:
            collection.create_index(
                field_name=index_field_name,
                index_params=index_params,
            )

        collection.load()
        return collection

    def insert_batch(
        self,
        collection_name: str,
        data: List[Dict[str, Any]],
    ) -> int:
        *_, Collection, _, _ = _lazy_import_milvus()
        if not data:
            return 0

        collection = Collection(collection_name, using=self.alias)
        # 插入时跳过 auto_id 主键字段（Milvus 会自动生成）
        field_names = self._get_field_names_for_insert(collection)
        columns: Dict[str, List[Any]] = {f: [] for f in field_names}
        for row in data:
            for f in field_names:
                columns[f].append(row.get(f))

        entities = [columns[f] for f in field_names]
        mr = collection.insert(entities)
        collection.flush()
        return (
            len(mr.primary_keys)
            if mr and getattr(mr, "primary_keys", None)
            else len(data)
        )

    def upsert_batch(
        self,
        collection_name: str,
        data: List[Dict[str, Any]],
    ) -> int:
        """批量整行 Upsert 数据到 Milvus Collection。

        适用于显式主键（auto_id=False，如 object_id、entity_id）的场景；每行须带全部
        schema 字段。返回值是服务端确认写入的条数：响应缺少主键列表时返回 0，
        不再用输入条数冒充成功数（doc 20.1 §10.3）。
        """
        *_, Collection, _, _ = _lazy_import_milvus()
        if not data:
            return 0

        collection = Collection(collection_name, using=self.alias)

        # 按 Collection 的 schema 顺序构建列式数据，避免字段顺序不一致
        field_names = [f.name for f in collection.schema.fields]
        columns: Dict[str, List[Any]] = {f: [] for f in field_names}

        for row in data:
            for f in field_names:
                columns[f].append(row.get(f))

        entities = [columns[f] for f in field_names]
        mr = collection.upsert(entities)
        collection.flush()
        primary_keys = getattr(mr, "primary_keys", None) if mr else None
        return len(primary_keys) if primary_keys else 0

    def delete_by_expr(self, collection_name: str, expr: str) -> int:
        """按标量过滤表达式删除记录，返回服务端报告的删除数（可能为 0）。

        集合须已 load（ensure_collection 会 load）。不强制 flush，也不轮询可见性：
        读取可见性取决于查询一致性级别（doc 20.1 §10.3 第 6 步）。
        """
        *_, Collection, _, _ = _lazy_import_milvus()
        if not expr or not expr.strip():
            raise ValueError("删除表达式不能为空，拒绝无过滤的全量删除")
        collection = Collection(collection_name, using=self.alias)
        result = collection.delete(expr)
        return int(getattr(result, "delete_count", 0) or 0)

    @staticmethod
    def _get_field_names_for_insert(collection: Any) -> List[str]:
        field_names: List[str] = []
        for field in collection.schema.fields:
            is_primary = bool(getattr(field, "is_primary", False))
            auto_id = bool(getattr(field, "auto_id", False))
            if is_primary and auto_id:
                continue
            field_names.append(field.name)
        return field_names

    def get_collection_stats(self, collection_name: str) -> Dict[str, Any]:
        *_, Collection, _, _ = _lazy_import_milvus()
        collection = Collection(collection_name, using=self.alias)
        return collection.describe()

    def close(self) -> None:
        connections, *_ = _lazy_import_milvus()
        try:
            connections.disconnect(alias=self.alias)
        except Exception:
            pass
        self.connected = False
