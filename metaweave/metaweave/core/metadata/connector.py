"""数据库连接器

负责连接 PostgreSQL 数据库并执行查询。
"""

import logging
from typing import List, Optional, Dict, Any, Tuple
import pandas as pd
import psycopg
from psycopg.rows import dict_row
from psycopg.conninfo import make_conninfo
from psycopg_pool import ConnectionPool
from contextlib import contextmanager

from metaweave.utils.sql_templates import (
    CHECK_DATABASE_OBJECT_EXISTS_SQL,
    GET_DATABASE_OBJECTS_SQL,
    GET_SCHEMAS_SQL,
    GET_TABLES_SQL,
    SAMPLE_DATA_SQL,
    CHECK_TABLE_EXISTS_SQL,
)
from metaweave.core.metadata.models import DatabaseObjectRef
from metaweave.db.postgres_options import build_connect_kwargs

logger = logging.getLogger("metaweave.connector")

OBJECT_TYPE_TO_RELKINDS = {
    "table": ["r", "p"],
    "view": ["v"],
    "materialized_view": ["m"],
}


class DatabaseConnector:
    """PostgreSQL 数据库连接器

    使用 psycopg3 和连接池管理数据库连接。
    """

    def __init__(self, config: Dict[str, Any]):
        """初始化数据库连接器

        Args:
            config: 数据库配置字典，包含 host, port, database, user, password
        """
        self.config = config
        self.host = config.get("host", "localhost")
        self.port = config.get("port", 5432)
        self.database = config.get("database")
        self.user = config.get("user")
        self.password = config.get("password")

        # 打印实际配置值用于调试
        logger.info("数据库配置:")
        logger.info(f"  Host: {self.host}")
        logger.info(f"  Port: {self.port}")
        logger.info(f"  Database: {self.database}")
        logger.info(f"  User: {self.user}")

        # 验证必需参数
        if not self.database:
            raise ValueError("数据库名称（database）未配置")
        if not self.user:
            raise ValueError("数据库用户（user）未配置")
        if not self.password:
            raise ValueError("数据库密码（password）未配置")

        # 构建连接参数（doc 18.1 §4.1：经公共模块构造，不自行拼装）。
        # sslmode 从配置读取并规范化校验，非法值在建连前失败；DDL/JSON/REL
        # 既有路径不传超时，参数中不出现 connect_timeout/options。
        connect_kwargs = build_connect_kwargs(
            host=self.host,
            port=self.port,
            dbname=self.database,
            user=self.user,
            password=self.password,
            sslmode=config.get("sslmode"),
        )
        self.conninfo = make_conninfo(**connect_kwargs)

        # 连接池配置
        min_size = config.get("pool_min_size", 1)
        max_size = config.get("pool_max_size", 5)

        # 初始化连接池
        self.pool: Optional[ConnectionPool] = None
        try:
            self.pool = ConnectionPool(
                self.conninfo,
                min_size=min_size,
                max_size=max_size,
                open=True,
            )
            logger.info(f"数据库连接池已创建: {self.database}@{self.host}:{self.port}")
        except Exception:
            logger.error("创建数据库连接池失败（连接异常；凭据已隐藏）")
            raise

    @contextmanager
    def get_connection(self):
        """获取数据库连接（上下文管理器）

        Yields:
            psycopg.Connection: 数据库连接对象
        """
        if self.pool is None:
            raise RuntimeError("连接池未初始化")

        conn = None
        try:
            conn = self.pool.getconn()

            # 使用 autocommit 模式以避免长事务占用连接
            # 当前所有对 PostgreSQL 的操作均为只读查询，使用 autocommit 更加安全、简单
            if not conn.autocommit:
                conn.autocommit = True

            yield conn
        except Exception as e:
            logger.error("获取数据库连接失败（%s；凭据已隐藏）", type(e).__name__)
            raise
        finally:
            if conn:
                self.pool.putconn(conn)

    def execute_query(
        self, sql: str, params: Optional[Tuple] = None, fetch_one: bool = False
    ) -> List[Dict[str, Any]]:
        """执行查询并返回结果

        Args:
            sql: SQL 查询语句
            params: 查询参数
            fetch_one: 是否只返回一行结果

        Returns:
            查询结果列表（字典格式）
        """
        with self.get_connection() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                try:
                    cur.execute(sql, params)
                    if fetch_one:
                        result = cur.fetchone()
                        return [result] if result else []
                    else:
                        return cur.fetchall()
                except Exception as e:
                    # 语句与参数不进日志：SQL 原文与参数值受 doc 16 §4 约束，
                    # 定位靠调用方日志里的对象名与这里的错误类别。
                    logger.error(
                        "执行查询失败（%s；语句与参数已隐藏）", type(e).__name__
                    )
                    raise

    def get_schemas(self, exclude_system: bool = True) -> List[str]:
        """获取所有 schema

        Args:
            exclude_system: 是否排除系统 schema

        Returns:
            schema 名称列表
        """
        try:
            results = self.execute_query(GET_SCHEMAS_SQL)
            schemas = [row["schema_name"] for row in results]
            logger.info(f"获取到 {len(schemas)} 个 schema: {schemas}")
            return schemas
        except Exception as e:
            logger.error("获取 schema 列表失败（%s）", type(e).__name__)
            return []

    def get_tables(self, schema: str) -> List[str]:
        """获取指定 schema 下的所有表

        Args:
            schema: schema 名称

        Returns:
            表名列表
        """
        try:
            results = self.execute_query(GET_TABLES_SQL, (schema,))
            tables = [row["tablename"] for row in results]
            logger.info(f"Schema '{schema}' 包含 {len(tables)} 张表")
            return tables
        except Exception as e:
            # 吞异常返回空列表仅保留给未改造步骤；受管 ddl/json 的对象
            # 枚举走 get_database_objects，失败必须上抛（doc 16 §4）。
            logger.error("获取表列表失败 (schema=%s): %s", schema, type(e).__name__)
            return []

    def get_database_objects(
        self,
        schema: str,
        include_object_types: List[str],
    ) -> List[DatabaseObjectRef]:
        """获取指定 schema 下选中类型的数据库对象。"""
        relkinds = [
            relkind
            for object_type in include_object_types
            for relkind in OBJECT_TYPE_TO_RELKINDS[object_type]
        ]
        try:
            results = self.execute_query(GET_DATABASE_OBJECTS_SQL, (schema, relkinds))
            objects = [
                DatabaseObjectRef(
                    schema_name=row["schema_name"],
                    object_name=row["object_name"],
                    object_type=row["object_type"],
                )
                for row in results
            ]
            logger.info(
                "Schema '%s' 包含 %d 个选中数据库对象（类型: %s）",
                schema,
                len(objects),
                ", ".join(include_object_types),
            )
            return objects
        except Exception as e:
            # 查询失败必须上抛：返回空列表会被当作"schema 无对象"，多
            # schema 采集时现存 schema 的产物会掩盖缺失整个 schema 的
            # 不完整结果（doc 16 §4：空集合只表示查询成功且无对象）。
            logger.error("获取数据库对象失败 (schema=%s): %s", schema, type(e).__name__)
            raise

    def check_database_object_exists(
        self,
        schema: str,
        object_name: str,
        object_type: str,
    ) -> bool:
        """检查指定类型的数据库对象是否存在。"""
        relkinds = OBJECT_TYPE_TO_RELKINDS[object_type]
        try:
            results = self.execute_query(
                CHECK_DATABASE_OBJECT_EXISTS_SQL,
                (schema, object_name, relkinds),
                fetch_one=True,
            )
            return results[0]["exists"] if results else False
        except Exception as e:
            # 查询失败不能误报成"对象不存在"（那会被当作并发删除静默
            # 跳过），上抛让该对象按提取失败计数（doc 16 §4）。
            logger.error(
                "检查数据库对象失败 (%s.%s, type=%s): %s",
                schema,
                object_name,
                object_type,
                type(e).__name__,
            )
            raise

    def check_table_exists(self, schema: str, table: str) -> bool:
        """检查表是否存在

        Args:
            schema: schema 名称
            table: 表名

        Returns:
            表是否存在
        """
        try:
            results = self.execute_query(
                CHECK_TABLE_EXISTS_SQL, (schema, table), fetch_one=True
            )
            return results[0]["exists"] if results else False
        except Exception as e:
            logger.error(
                "检查表是否存在失败 (schema=%s, table=%s): %s",
                schema,
                table,
                type(e).__name__,
            )
            return False

    def sample_data(self, schema: str, table: str, limit: int = 1000) -> pd.DataFrame:
        """采样表数据

        Args:
            schema: schema 名称
            table: 表名
            limit: 采样行数

        Returns:
            样本数据 DataFrame
        """
        try:
            # 使用标识符引用来处理特殊字符
            sql = SAMPLE_DATA_SQL.format(
                schema=psycopg.sql.Identifier(schema).as_string(None),
                table=psycopg.sql.Identifier(table).as_string(None),
            )

            with self.get_connection() as conn:
                df = pd.read_sql_query(sql, conn, params=(limit,))
                logger.info(f"采样 {schema}.{table} 获取 {len(df)} 行数据")
                return df
        except Exception as e:
            logger.error(
                "采样数据失败 (schema=%s, table=%s): %s",
                schema,
                table,
                type(e).__name__,
            )
            return pd.DataFrame()

    def sample_data_preserving_types(
        self,
        schema: str,
        table: str,
        limit: int = 1000,
    ) -> pd.DataFrame:
        """采样数据并保留 psycopg 返回的 Python 标量类型。

        DDL 样例需要区分整数、浮点数和 Decimal。这里绕过 pandas SQL
        读取时的数值强制转换，并以 object dtype 构造 DataFrame。
        """
        try:
            sql = SAMPLE_DATA_SQL.format(
                schema=psycopg.sql.Identifier(schema).as_string(None),
                table=psycopg.sql.Identifier(table).as_string(None),
            )
            rows = self.execute_query(sql, (limit,))
            df = pd.DataFrame(rows, dtype=object)
            logger.info(
                "保真采样 %s.%s 获取 %d 行数据",
                schema,
                table,
                len(df),
            )
            return df
        except Exception as e:
            logger.error("保真采样失败 (%s.%s): %s", schema, table, type(e).__name__)
            return pd.DataFrame()

    def test_connection(self) -> bool:
        """测试数据库连接

        Returns:
            连接是否成功
        """
        try:
            with self.get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT version();")
                    version = cur.fetchone()
                    logger.info(f"数据库连接测试成功: {version[0]}")
                    return True
        except Exception as e:
            logger.error("数据库连接测试失败（%s）", type(e).__name__)
            return False

    def close(self):
        """关闭连接池"""
        if self.pool:
            self.pool.close()
            logger.info("数据库连接池已关闭")

    def __enter__(self):
        """上下文管理器入口"""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """上下文管理器出口"""
        self.close()
