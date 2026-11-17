"""加载器工厂类"""

from typing import Dict, Any

from metaweave.core.loaders.base import BaseLoader


class LoaderFactory:
    """加载器工厂类

    根据加载类型创建对应的加载器实例。
    """

    # 注册表：加载类型 -> 加载器类
    _loaders: Dict[str, type] = {}

    @classmethod
    def create(
        cls,
        load_type: str,
        config: Dict[str, Any],
        metadata_config: "Dict[str, Any] | None" = None,
    ) -> BaseLoader:
        """创建加载器实例

        Args:
            load_type: 加载类型（"cql"/"content_md"/"dim"/"sql"）
            config: loaders 段派生配置字典
            metadata_config: 完整主配置快照（doc 26 §6.2）；支持注入的
                loader 收到后不再经 metadata_config_file 读盘。None 表示
                未注入（兼容旧行为，loader 自行懒加载）

        Returns:
            BaseLoader: 加载器实例

        Raises:
            ValueError: 未知的加载类型
        """
        loader_class = cls._loaders.get(load_type)
        if not loader_class:
            # 延迟注册：避免循环导入的加载器在首次请求时注册
            cls._register_lazy(load_type)
            loader_class = cls._loaders.get(load_type)
        if not loader_class:
            raise ValueError(
                f"未知的加载类型: {load_type}，"
                f"支持的类型: {list(cls._loaders.keys())}"
            )

        # 支持注入的 loader（table_schema/dim_value/sql）接收完整快照；
        # 其余（cql 等）仅收派生配置副本（doc 26 §6.2：显式分派消除歧义）
        if metadata_config is not None and load_type in cls._INJECTABLE_METADATA_CONFIG:
            return loader_class(config, metadata_config=metadata_config)
        return loader_class(config)

    @classmethod
    def register(cls, load_type: str, loader_class: type):
        """注册新的加载器类型（用于扩展）

        Args:
            load_type: 加载类型标识
            loader_class: 加载器类

        Example:
            >>> LoaderFactory.register("cql", CQLLoader)
        """
        cls._loaders[load_type] = loader_class

    # 延迟注册映射：load_type -> (module_path, class_name)
    _lazy_loaders: Dict[str, tuple] = {
        "sql": ("metaweave.core.sql_rag.loader", "SQLExampleLoader"),
    }

    # 支持完整主配置快照注入的 loader 类型（doc 26 §6.2）
    _INJECTABLE_METADATA_CONFIG = {"table_schema", "dim_value", "sql"}

    @classmethod
    def _register_lazy(cls, load_type: str):
        """按需导入并注册延迟加载器（用于避免循环导入）"""
        lazy = cls._lazy_loaders.get(load_type)
        if lazy:
            import importlib

            module = importlib.import_module(lazy[0])
            loader_class = getattr(module, lazy[1])
            cls.register(load_type, loader_class)

    @classmethod
    def get_supported_types(cls) -> list:
        """获取支持的加载类型列表

        Returns:
            list: 支持的加载类型
        """
        all_types = set(cls._loaders.keys()) | set(cls._lazy_loaders.keys())
        return sorted(all_types)


# 注册内置加载器
def _register_builtin_loaders():
    """注册内置的加载器类型"""
    # 延迟导入，避免循环依赖
    from metaweave.core.loaders.cql_loader import CQLLoader
    from metaweave.core.loaders.dim_value_loader import DimValueLoader
    from metaweave.core.loaders.table_schema_loader import TableSchemaLoader

    LoaderFactory.register("cql", CQLLoader)
    # dim 与 dim_value 均指向维表值加载器
    LoaderFactory.register("dim", DimValueLoader)
    LoaderFactory.register("dim_value", DimValueLoader)
    LoaderFactory.register("table_schema", TableSchemaLoader)

    # sql loader 延迟注册，避免与 sql_rag.loader → loaders.base 的循环导入
    # 实际注册在 LoaderFactory.create() 首次请求 "sql" 时触发
    pass


# 自动注册内置加载器
_register_builtin_loaders()
