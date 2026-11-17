"""部署冒烟（doc 13 §8.1、§9.1；审核 R3/R5 修复）。

- ``--scope infra``：容器内执行的基础设施只读核验。PG 检查两个目标库
  及必要对象（MetaWeave 结构契约 + NL2SQL 持久化表，复用 deploy/contract.json
  集中契约）；Neo4j 检查连接与方案要求的 APOC 能力（只读函数）；Milvus
  检查服务与目标库（绝不建库）。
- ``--scope apps --mode public|maintenance``：经前端入口
  ``http://localhost:18080``。public 明确不携带控制令牌；maintenance 从
  ``DEPLOY_CONTROL_TOKEN`` 环境读取令牌（不写命令行参数或日志）。按现有
  端点做最小响应契约检查（审核 R5）：业务读取必须是正确的业务 JSON，
  不接受 health 响应或任意 JSON 冒充。

本脚本的无 Origin HTTP 冒烟成功不等价于浏览器 CORS 已验证（§9.2 的
Origin 验收与 Windows 浏览器验收另行执行）。
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable
from typing import Any

import httpx

CONTROL_TOKEN_HEADER = "x-insightweave-deploy-token"

# (名称, 路径, 契约键)：SPA 深层路由依赖 try_files 回退 index.html
APPS_CHECKS: tuple[tuple[str, str, str], ...] = (
    ("静态首页", "/", "html"),
    ("SPA 深层路由", "/query", "html"),
    ("MetaWeave 平台读取", "/api/data-sources", "metaweave_sources"),
    ("NL2SQL health", "/api/v2/health", "nl2sql_health"),
    ("NL2SQL 业务读取", "/api/v2/sessions?limit=1", "nl2sql_sessions"),
)


def _payload_problem(name: str, path: str, kind: str, payload: Any) -> str | None:
    """按现有端点的最小响应契约（审核 R5）；不复制完整业务模型。"""
    if kind == "html":
        return None
    if not isinstance(payload, dict):
        return f"{name}({path})：期望 JSON 对象，得到 {type(payload).__name__}"
    if kind == "metaweave_sources":
        items = payload.get("items")
        if not isinstance(items, list):
            return f"{name}({path})：期望 items 列表（数据源列表契约），得到 {items!r}"
        return None
    if kind == "nl2sql_health":
        if payload.get("code") != 200:
            return (
                f"{name}({path})：envelope code 应为 200，得到 {payload.get('code')!r}"
            )
        data = payload.get("data")
        if not isinstance(data, dict) or data.get("status") != "ok":
            return f"{name}({path})：health data.status 应为 ok，得到 {data!r}"
        return None
    if kind == "nl2sql_sessions":
        if payload.get("code") != 200:
            return (
                f"{name}({path})：envelope code 应为 200，得到 {payload.get('code')!r}"
            )
        data = payload.get("data")
        if not isinstance(data, dict) or not isinstance(data.get("items"), list):
            return f"{name}({path})：会话列表 data.items 应为列表，得到 {data!r}"
        if "total_all" not in data:
            return f"{name}({path})：会话列表缺少分页结构（total_all）"
        return None
    return None


def _validate(name: str, path: str, kind: str, response: httpx.Response) -> str | None:
    if response.status_code != 200:
        return f"{name}({path}): HTTP {response.status_code}"
    content_type = response.headers.get("content-type", "")
    if kind == "html":
        if "text/html" not in content_type or not response.text.strip():
            return f"{name}({path}): 期望非空 HTML（SPA 回退），得到 {content_type}"
        return None
    if "application/json" not in content_type:
        return f"{name}({path}): 期望 JSON 业务响应，得到 {content_type or '无 content-type'}"
    try:
        payload = response.json()
    except ValueError as exc:
        return f"{name}({path}): JSON 解析失败 {exc}"
    return _payload_problem(name, path, kind, payload)


def run_apps_smoke(
    base_url: str,
    *,
    mode: str,
    token: str | None,
    transport: httpx.BaseTransport | None = None,
) -> list[str]:
    """执行前端入口冒烟，返回失败清单；全部通过返回空表。"""
    if mode not in ("public", "maintenance"):
        raise ValueError(f"未知 apps 冒烟模式：{mode}")
    headers = {}
    if mode == "maintenance":
        token = token if token is not None else os.environ.get("DEPLOY_CONTROL_TOKEN")
        if not token:
            raise RuntimeError(
                "maintenance 冒烟缺少 DEPLOY_CONTROL_TOKEN（从受限配置读取）"
            )
        headers[CONTROL_TOKEN_HEADER] = token
    failures: list[str] = []
    with httpx.Client(
        base_url=base_url, headers=headers, timeout=10.0, transport=transport
    ) as client:
        for name, path, kind in APPS_CHECKS:
            try:
                response = client.get(path)
            except httpx.HTTPError as exc:
                failures.append(f"{name}({path}): 请求失败 {exc}")
                continue
            problem = _validate(name, path, kind, response)
            if problem:
                failures.append(problem)
    return failures


def run_infra_smoke(
    checkers: dict[str, Callable[[], None]],
) -> list[str]:
    """执行基础设施检查器；每个检查器正常返回或抛异常，失败收集为清单。"""
    failures: list[str] = []
    for name, check in checkers.items():
        try:
            check()
        except Exception as exc:  # noqa: BLE001 —— 冒烟收集全部失败项
            failures.append(f"{name}: {exc}")
    return failures


def build_default_infra_checkers(
    *,
    psycopg: Any = None,
    neo4j: Any = None,
    milvus_client_factory: Any = None,
    contract: dict[str, Any] | None = None,
) -> dict[str, Callable[[], None]]:
    """默认 infra 检查器（doc 13 §8.1/§9.1），在后端容器内执行。

    依赖模块/工厂/契约仅用于测试注入；生产路径按需惰性导入真实驱动并读取
    deploy/contract.json。全部只读：不建库、不建集合、不写业务图、不 setup。
    """
    from deploy.scripts.init_platform import (
        load_deploy_contract,
        verify_metaweave_objects,
    )

    def _module(given: Any, name: str) -> Any:
        if given is not None:
            return given
        import importlib

        return importlib.import_module(name)

    def check_postgres() -> None:
        module = _module(psycopg, "psycopg")
        spec = contract if contract is not None else load_deploy_contract()
        # 与 compose 的 :- 默认一致（doc 13 §3.2 固定命名；两个后端镜像各自只有
        # 自己 env 文件，检查器运行侧不要求两个变量同时存在）
        platform_db = os.environ.get("PLATFORM_DB_NAME", "metaweave")
        persist_db = os.environ.get("LANGGRAPH_DB_NAME", "lg_persist")

        def base_kwargs() -> dict[str, Any]:
            return {
                "host": os.environ["PLATFORM_DB_HOST"],
                "port": int(os.environ["PLATFORM_DB_PORT"]),
                "user": os.environ["PLATFORM_DB_USER"],
                "password": os.environ.get("PLATFORM_DB_PASSWORD", ""),
            }

        with module.connect(dbname="postgres", **base_kwargs()) as conn:
            rows = conn.execute(
                "SELECT datname FROM pg_database WHERE datname = ANY(%s)",
                ([platform_db, persist_db],),
            ).fetchall()
        present = {row[0] for row in rows}
        missing = sorted({platform_db, persist_db} - present)
        if missing:
            raise RuntimeError(f"PG 缺少目标库：{missing}")
        with module.connect(dbname=platform_db, **base_kwargs()) as conn:
            problems = verify_metaweave_objects(conn, spec["metaweave_schema"])
        if problems:
            raise RuntimeError("MetaWeave 结构契约不通过：" + "；".join(problems))
        with module.connect(dbname=persist_db, **base_kwargs()) as conn:
            rows = conn.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'db_deepagent'"
            ).fetchall()
        got_tables = {row[0] for row in rows}
        missing_tables = sorted(set(spec["nl2sql_tables"]) - got_tables)
        if missing_tables:
            raise RuntimeError(f"NL2SQL 持久化缺表：{missing_tables}")

    def check_neo4j() -> None:
        module = _module(neo4j, "neo4j")
        driver = module.GraphDatabase.driver(
            os.environ["NEO4J_URI"],
            auth=(os.environ["NEO4J_USER"], os.environ.get("NEO4J_PASSWORD", "")),
        )
        try:
            with driver.session(
                database=os.environ.get("NEO4J_DATABASE", "neo4j")
            ) as session:
                if session.run("RETURN 1 AS ok").single() is None:
                    raise RuntimeError("Neo4j RETURN 1 未返回记录")
                record = session.run("RETURN apoc.version() AS v").single()
                version = record.get("v") if record is not None else None
                if not version:
                    raise RuntimeError(
                        "APOC 不可用（apoc.version() 无结果）：检查插件卷与不受限配置"
                    )
        finally:
            driver.close()

    def check_milvus() -> None:
        if milvus_client_factory is None:
            from pymilvus import MilvusClient as _MilvusClient

            factory = _MilvusClient
        else:
            factory = milvus_client_factory
        uri = f"http://{os.environ['MILVUS_HOST']}:{os.environ.get('MILVUS_PORT', '19530')}"
        # 只读可用性：连接目标库并列集合；缺库清晰失败，不建库
        client = factory(uri=uri, db_name=os.environ["MILVUS_DATABASE"])
        client.list_collections()

    return {"postgres": check_postgres, "neo4j": check_neo4j, "milvus": check_milvus}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="部署冒烟（doc 13 §8.1）")
    parser.add_argument("--scope", choices=["infra", "apps"], required=True)
    parser.add_argument("--mode", choices=["maintenance", "public"], default="public")
    parser.add_argument("--base-url", default="http://localhost:18080")
    args = parser.parse_args(argv)
    if args.scope == "infra":
        failures = run_infra_smoke(build_default_infra_checkers())
    else:
        failures = run_apps_smoke(args.base_url, mode=args.mode, token=None)
    if failures:
        for line in failures:
            print(line, file=sys.stderr)
        return 1
    print(f"{args.scope} 冒烟通过（mode={args.mode}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
