"""断代改造的一次性旧数据清理（doc 6 §8、6.2 §12 第 4–7 项）。

清理对象（全部为本项目管理数据与派生产物，不触及源业务库）：
1. Neo4j：本项目旧子图（旧 UUID source_id 或无 source_id 的节点及关系）；
2. Milvus：受管集合中旧 UUID/无来源的发布记录；
3. output/：旧 UUID 数据源目录与旧 job 目录（不触碰新 db_src_N/ws_N 结构）；
4. NL2SQL：db_deepagent schema 的 checkpoints/checkpoint_blobs/
   checkpoint_writes/store/result_snapshots 及本地结果文件。

安全约定：
- 默认 --dry-run 只打印将执行的动作，--yes 才实际执行；
- 不删除 PostgreSQL 改名留存的旧表（那由 rebuild 脚本负责且永不删除）；
- 不加入启动或发布流程；执行前应停止会写旧模型的服务；
- 连接配置取各项目 .env（NEO4J_*、MILVUS_*、PLATFORM_DB_*），缺失的
  目标跳过并提示。

用法：
    uv run python scripts/cleanup_legacy_data.py --dry-run
    uv run python scripts/cleanup_legacy_data.py --yes
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.persistence.platform_db import (  # noqa: E402
    load_platform_settings_from_env,
    load_project_env,
)

UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)

NL2SQL_CLEAN_TABLES = (
    "checkpoints",
    "checkpoint_blobs",
    "checkpoint_writes",
    "store",
    "result_snapshots",
)


def _plan_header(title: str) -> None:
    print(f"\n== {title} ==")


def cleanup_neo4j(execute: bool) -> None:
    _plan_header("Neo4j 旧子图")
    uri = _env("NEO4J_URI")
    if not uri:
        print("跳过：缺少 NEO4J_URI")
        return
    try:
        from neo4j import GraphDatabase
    except ImportError:
        print("跳过：未安装 neo4j 驱动")
        return
    user = _env("NEO4J_USER", "neo4j")
    password = _env("NEO4J_PASSWORD", "")
    database = _env("NEO4J_DATABASE", "neo4j")
    if not password:
        print("跳过：缺少 NEO4J_PASSWORD")
        return
    driver = GraphDatabase.driver(uri, auth=(user, password))
    try:
        with driver.session(database=database) as session:
            # 本项目旧子图：无 source_id 或 source_id 为旧 UUID 的节点（任意
            # label，与发布侧事务内清理同一判定）；新短 ID 节点不动
            count = session.run(
                "MATCH (n) WHERE n.source_id IS NULL OR "
                "(n.source_id =~ $uuid_pat) "
                "RETURN count(n) AS c",
                uuid_pat="(?i)^[0-9a-f-]{36}$",
            ).single()["c"]
            rel_count = session.run(
                "MATCH ()-[r]->() WHERE r.source_id IS NULL OR "
                "(r.source_id =~ $uuid_pat) "
                "RETURN count(r) AS c",
                uuid_pat="(?i)^[0-9a-f-]{36}$",
            ).single()["c"]
            print(f"将删除节点 {count} 个、关系 {rel_count} 条（无/旧 UUID 来源）")
            if execute:
                session.run(
                    "MATCH (n) WHERE n.source_id IS NULL OR "
                    "(n.source_id =~ $uuid_pat) "
                    "DETACH DELETE n",
                    uuid_pat="(?i)^[0-9a-f-]{36}$",
                )
                print("已删除")
            else:
                print("dry-run：未执行")
    finally:
        driver.close()


def _env(name: str, default: str | None = None) -> str | None:
    import os

    return os.getenv(name) or default


def cleanup_milvus(execute: bool) -> None:
    _plan_header("Milvus 旧发布记录")
    try:
        from pymilvus import MilvusClient
    except ImportError:
        print("跳过：未安装 pymilvus")
        return
    host = _env("MILVUS_HOST", "localhost")
    port = _env("MILVUS_PORT", "19530")
    database = _env("MILVUS_DATABASE", "default")
    # 受管集合名与 metaweave configs/metadata_config.yaml 一致
    collections = (
        "table_schema_embeddings",
        "dim_value_embeddings",
        "sql_example_embeddings",
    )
    client = MilvusClient(uri=f"http://{host}:{port}", db_name=database)
    try:
        for collection in collections:
            if not client.has_collection(collection):
                print(f"{collection}: 不存在，跳过")
                continue
            expr = 'source_id == "" or source_id like "%-%-%-%-%"'
            try:
                count = client.query(
                    collection, filter=expr, output_fields=["count(*)"]
                )
                total = count[0]["count(*)"] if count else 0
            except Exception:  # noqa: BLE001 —— 结构差异按 0 处理并提示
                total = None
            print(f"{collection}: 旧/无来源记录 {total}")
            if execute and total:
                client.delete(collection, filter=expr)
                print(f"{collection}: 已删除")
            elif not execute:
                print(f"{collection}: dry-run 未执行")
    finally:
        client.close()


def cleanup_output(execute: bool) -> None:
    _plan_header("output/ 旧目录")
    from metaweave.utils.file_utils import get_project_root

    output = get_project_root() / "output"
    if not output.is_dir():
        print("output/ 不存在，跳过")
        return
    for child in sorted(output.iterdir()):
        if not child.is_dir():
            continue
        name = child.name
        if name.startswith("."):
            continue
        if UUID_RE.match(name):
            print(f"删除旧 UUID 数据源目录: {name}")
            if execute:
                shutil.rmtree(child, ignore_errors=True)
            continue
        # 旧 CLI 步骤目录（顶层 dim_config/、sql-rag/ 为 CLI 断代前默认）
        if name in (
            "dim_config",
            "sql-rag",
            "domains",
            "ddl",
            "json",
            "md",
            "rel",
            "cql",
            "db",
        ):
            print(f"顶层 CLI 目录（人工确认是否删除）: {name}")
    if not execute:
        print("dry-run：未执行删除")


def cleanup_nl2sql(execute: bool) -> None:
    _plan_header("NL2SQL 旧会话与快照")
    import psycopg

    settings = None
    try:
        settings = load_platform_settings_from_env()
    except Exception:  # noqa: BLE001
        pass
    if settings is None:
        print("跳过：缺少 PLATFORM_DB_* 环境变量")
        return
    schema = _env("NL2SQL_SCHEMA", "db_deepagent")
    dsn = (
        f"host={settings.host} port={settings.port} dbname={settings.name} "
        f"user={settings.user} password={settings.password}"
    )
    with psycopg.connect(dsn) as conn:
        for table in NL2SQL_CLEAN_TABLES:
            exists = conn.execute(
                "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = %s AND table_name = %s)",
                (schema, table),
            ).fetchone()[0]
            if not exists:
                print(f"{schema}.{table}: 不存在，跳过")
                continue
            count = conn.execute(
                f'SELECT count(*) FROM "{schema}"."{table}"'  # noqa: S608 固定白名单表
            ).fetchone()[0]
            print(f"{schema}.{table}: {count} 行")
            if execute:
                conn.execute(f'DELETE FROM "{schema}"."{table}"')
                print(f"{schema}.{table}: 已清空")
        if execute:
            conn.commit()
        else:
            print("dry-run：未执行")
    # 本地结果文件根目录（nl2sql 侧 results 配置，人工确认路径）
    print("本地结果文件（results 根目录）请按 nl2sql 配置人工核对删除")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--dry-run", action="store_true", help="只打印动作，不执行")
    group.add_argument("--yes", action="store_true", help="实际执行清理")
    args = parser.parse_args()
    execute = args.yes

    load_project_env()
    print("断代改造一次性旧数据清理（doc 6 §8）")
    print("执行前请停止会写旧模型的服务；本脚本不删除 PostgreSQL 留存旧表。")
    cleanup_neo4j(execute)
    cleanup_milvus(execute)
    cleanup_output(execute)
    cleanup_nl2sql(execute)
    print("\n完成。请核验无旧 UUID 数据残留后，再按 rebuild 脚本切换平台库。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
