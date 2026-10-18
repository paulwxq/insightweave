"""db_deepagent 命令行入口（docs/v4/5 §15）

步骤 1 交付 `doctor --stage env` 与 `maintenance init-platform`；query／chat／
acceptance 及 doctor 其余阶段随步骤 2–5 落地。doctor 不调用模型生成、不执行
业务查询（§15.3）。
"""

from __future__ import annotations

import argparse
import contextlib
import json
import importlib.metadata
import sys


from src.utils.logger import get_module_logger

logger = get_module_logger("nl2sql.db_deepagent.cli")

# 依赖版本核对清单（§2.1；sqlglot 为锁定版本，其余记录实际值）
_LOCKED_VERSIONS = {"sqlglot": "30.22.0"}
_VERSION_PACKAGES = (
    "deepagents",
    "langchain",
    "langgraph",
    "sqlglot",
    "psycopg",
    "pymilvus",
    "langgraph-checkpoint-postgres",
)

_DOCTOR_STAGES = ("env", "retrieval", "all")

# doctor 装配验证/维护路径使用的固定占位来源（doc 4 §4.7）：这些路径不执行
# 按源检索/寻路，仅装配；真实会话必须传登记 source_id
_PLACEHOLDER_SOURCE_ID = "00000000-0000-4000-8000-000000000000"


def _resolve_registration_and_apply(cfg, source_id: str, *, json_mode: bool = False):
    """读取平台登记源并注入运行配置（doc 4 §4.7 端到端来源一致）。

    业务库连接（host/port/user/password/sslmode）与 table_key_prefix 等
    三键全部取登记值——检索、图查询、业务库连接保持同一来源；登记缺失/
    禁用/密钥不可用明确失败，不回退为 YAML 固定连接。
    """
    from src.modules.db_deepagent.source_registry import (
        SourceResolutionError,
        apply_registration_to_config,
        resolve_source_registration,
    )

    try:
        registration = resolve_source_registration(source_id)
        return apply_registration_to_config(cfg, registration)
    except SourceResolutionError as exc:
        message = f"登记源解析失败: {exc}"
        if json_mode:
            _emit_json_error("invalid_argument", message, code=2)
        print(message, file=sys.stderr)
        raise SystemExit(2) from None


def _validate_source_id_arg(raw: str | None) -> str:
    """校验 --source-id 为合法 UUID（doc 4 §3.2：不自动选源、不回退）。"""
    import uuid as _uuid

    if not raw:
        print(
            "必须显式传入 --source-id（平台登记 UUID）；不自动选择唯一源，"
            "也不回退为无源查询",
            file=sys.stderr,
        )
        raise SystemExit(2)
    try:
        return str(_uuid.UUID(raw.strip()))
    except ValueError:
        print(f"--source-id 不是合法 UUID: {raw!r}", file=sys.stderr)
        raise SystemExit(2) from None


def _collect_dependency_report() -> list[tuple[str, bool, str]]:
    """SDK 及核心依赖版本核对；锁定版本不一致记为失败。"""
    report: list[tuple[str, bool, str]] = []
    for pkg in _VERSION_PACKAGES:
        try:
            version = importlib.metadata.version(pkg)
        except importlib.metadata.PackageNotFoundError:
            report.append((f"依赖 {pkg}", False, "未安装"))
            continue
        locked = _LOCKED_VERSIONS.get(pkg)
        ok = locked is None or version == locked
        detail = version if ok else f"{version}（要求 {locked}）"
        report.append((f"依赖 {pkg}", ok, detail))
    return report


def _check_business_db() -> tuple[bool, str]:
    """业务库连接、current_database()、只读设置与连接池回调（§15.3 env）。"""
    import psycopg.pq

    from src.modules.db_deepagent.config import load_db_agent_config
    from src.modules.db_deepagent.services.business_db import BusinessDbService

    cfg = load_db_agent_config()
    svc = BusinessDbService(cfg.business_db)
    try:
        svc.open()
        assert svc.datasource_id and svc.datasource_id.startswith("pg:")
        with svc._pool.connection() as conn:
            with conn.transaction():
                db = conn.execute("SELECT current_database()").fetchone()[0]
                readonly = conn.execute(
                    "SHOW default_transaction_read_only"
                ).fetchone()[0]
            status = conn.info.transaction_status
        if db != cfg.business_db.expected_database:
            return False, f"current_database()={db}"
        if readonly != "on":
            return False, f"default_transaction_read_only={readonly}"
        if status != psycopg.pq.TransactionStatus.IDLE:
            return False, f"连接池回调后连接状态 {status.name}（应为 IDLE）"
        return True, f"database={db} readonly=on IDLE {svc.datasource_id}"
    finally:
        svc.close()


def _check_platform_db() -> tuple[bool, str]:
    """平台库连接、current_schema() 与表位置（§15.3 env）。"""
    from src.modules.db_deepagent.config import load_db_agent_config
    from src.modules.db_deepagent.persistence.platform_pool import (
        build_platform_pool,
        check_platform_tables,
    )

    cfg = load_db_agent_config()
    pool = build_platform_pool(cfg.platform_db)
    try:
        pool.open()
        missing = check_platform_tables(pool, cfg.platform_db.schema_name)
        if missing:
            return (
                False,
                f"缺少表 {', '.join(missing)}（先运行 maintenance init-platform）",
            )
        return True, f"schema={cfg.platform_db.schema_name} 六表齐全"
    finally:
        pool.close()


def _check_model_and_harness() -> tuple[bool, str]:
    """模型构造、request_timeout 设置、HarnessProfile 匹配键与命中（§15.3 env；不调用模型）。"""
    from src.modules.db_deepagent.agent import (
        build_model,
        effective_request_timeout,
        register_and_verify_harness_profile,
    )
    from src.modules.db_deepagent.config import load_db_agent_config

    cfg = load_db_agent_config()
    model = build_model(cfg)
    timeout = effective_request_timeout(model)
    if timeout != cfg.model.request_timeout_seconds:
        return False, f"request_timeout 未生效（effective={timeout!r}）"
    key = register_and_verify_harness_profile(model, cfg)
    return True, f"{type(model).__name__} timeout={timeout} key={key}"


def _build_embedding_and_milvus(source_id: str | None = None):
    """构造 Embedding 客户端与 Milvus 检索适配器（连接与集合存在性在构造时验证）。"""
    from src.modules.db_deepagent.config import load_db_agent_config
    from src.modules.db_deepagent.services import _embedding_profile_and_key
    from src.modules.db_deepagent.services.embedding import DbEmbeddingClient
    from src.modules.db_deepagent.services.milvus_retrieval import DbMilvusRetrieval

    cfg = load_db_agent_config()
    profile, api_key = _embedding_profile_and_key(cfg)
    embedding = DbEmbeddingClient(
        profile,
        api_key=api_key,
        request_timeout_s=cfg.embedding.request_timeout_seconds,
    )
    return (
        cfg,
        profile,
        embedding,
        DbMilvusRetrieval(cfg, embedding, source_id=source_id),
    )


def _check_milvus() -> tuple[bool, str]:
    """Milvus 连接、数据库与三个集合存在（§15.3 retrieval；运维全库统计）。"""
    cfg, _profile, _embedding, milvus = _build_embedding_and_milvus(
        _PLACEHOLDER_SOURCE_ID
    )
    try:
        # 各集合实体计数（主键均为 VARCHAR，用非空下界作全量查询）——
        # 全库口径，仅作运维统计，不能判断指定源知识就绪（doc 4 §4.7）
        counts = []
        for kind, pk in (
            ("table_schema", "object_id"),
            ("dim_value", "col_id"),
            ("sql_example", "example_id"),
        ):
            name = getattr(cfg.milvus.collections, kind)
            rows = milvus._collection(kind).query(
                expr=f'{pk} >= ""', timeout=cfg.milvus.timeout
            )
            counts.append(f"{name}={len(rows)}")
        return (
            True,
            f"db={cfg.milvus.database} alias={milvus._alias} {' '.join(counts)}",
        )
    finally:
        milvus.close()


def _check_embedding_consistency() -> tuple[bool, str]:
    """发布记录与配置/Milvus 的模型、维度、向量字段一致性（§6.3）。"""
    from src.modules.db_deepagent.services.embedding_consistency import (
        check_embedding_consistency,
    )

    cfg, profile, embedding, milvus = _build_embedding_and_milvus(
        _PLACEHOLDER_SOURCE_ID
    )
    try:
        release = check_embedding_consistency(cfg, profile, embedding, milvus)
        return (
            True,
            f"release_batch={release['release_batch']} model={release['model']} "
            f"dim={release['dimensions']}",
        )
    finally:
        milvus.close()


def _check_neo4j_join_graph() -> tuple[bool, str]:
    """Neo4j 连接、Table 节点与 JOIN_ON 边规模、APOC 可用（运维全库统计）。"""
    from src.modules.db_deepagent.config import load_db_agent_config
    from src.modules.db_deepagent.services import _neo4j_manager

    cfg = load_db_agent_config()
    manager = _neo4j_manager(cfg)
    try:
        with manager.get_session() as session:
            tables = session.run("MATCH (t:Table) RETURN count(t) AS n").single()["n"]
            edges = session.run(
                "MATCH ()-[r:JOIN_ON]->() RETURN count(r) AS n"
            ).single()["n"]
            apoc = session.run("RETURN apoc.version() AS v").single()["v"]
        return True, f"{cfg.neo4j.database}: Table={tables} JOIN_ON={edges} APOC={apoc}"
    finally:
        manager.close()


def _check_source_scoped(source_id: str) -> tuple[bool, str]:
    """doctor --source-id：只读统计指定源的 Milvus 三集合与 Neo4j 四类计数。

    doc 4 §4.7 可选辅助交付：不生成 Embedding、不调用模型、不执行业务 SQL；
    依赖不可用明确失败；数量为零只说明未发布或空集合，不以“非零”替代
    完整就绪验收。来源登记核验（db_source_info 存在性）由 doc 2 装配层
    承接，此处仅做 UUID 格式校验（差异见交付说明）。
    """
    import uuid as _uuid

    sid = str(_uuid.UUID(source_id))
    parts: list[str] = []

    cfg, _profile, _embedding, milvus = _build_embedding_and_milvus(sid)
    try:
        for kind, pk in (
            ("table_schema", "object_id"),
            ("dim_value", "col_id"),
            ("sql_example", "example_id"),
        ):
            name = getattr(cfg.milvus.collections, kind)
            rows = milvus._collection(kind).query(
                expr=f'source_id == "{sid}"',
                output_fields=["count(*)"],
                timeout=cfg.milvus.timeout,
            )
            # 服务端计数：不把全部命中记录拉回客户端再 len()
            parts.append(f"milvus:{name}={int(rows[0]['count(*)']) if rows else 0}")
    finally:
        milvus.close()

    from src.modules.db_deepagent.config import load_db_agent_config
    from src.modules.db_deepagent.services import _neo4j_manager

    cfg = load_db_agent_config()
    manager = _neo4j_manager(cfg)
    try:
        with manager.get_session() as session:
            row = session.run(
                """
                MATCH (t:Table) WHERE t.source_id = $sid
                WITH count(t) AS tables
                OPTIONAL MATCH (c:Column) WHERE c.source_id = $sid
                WITH tables, count(c) AS columns
                OPTIONAL MATCH ()-[h:HAS_COLUMN]->() WHERE h.source_id = $sid
                WITH tables, columns, count(h) AS has_column
                OPTIONAL MATCH ()-[r:JOIN_ON]->() WHERE r.source_id = $sid
                RETURN tables, columns, has_column, count(r) AS join_on
                """,
                sid=sid,
            ).single()
            parts.append(
                f"neo4j:Table={row['tables']} Column={row['columns']} "
                f"HAS_COLUMN={row['has_column']} JOIN_ON={row['join_on']}"
            )
    finally:
        manager.close()

    zero = [p for p in parts if p.endswith("=0")]
    detail = " ".join(parts)
    if zero:
        detail += f"（零计数 {len(zero)}/7 项，可能未发布或空集合）"
    return True, detail


def _collect_doctor_results(
    stage: str, *, verbose: bool = True, source_id: str | None = None
) -> list:
    """执行各阶段检查并返回 [(名称, ok, 详情)]；过程打印仅在 verbose 时。"""
    results: list[tuple[str, bool, str]] = []

    if stage in ("env", "all"):
        if verbose:
            print("-- env --")
        results.extend(_collect_dependency_report())
        for name, check in (
            ("配置校验（§4.2）", _check_config),
            ("业务库（§6.1）", _check_business_db),
            ("平台库（§13.1）", _check_platform_db),
            ("模型与 HarnessProfile（§10.2/§11.1）", _check_model_and_harness),
        ):
            try:
                ok, detail = check()
            except Exception as exc:  # doctor 汇总失败原因，不让单项异常中断
                ok, detail = False, f"{type(exc).__name__}: {exc}"
            results.append((name, ok, detail))

    if stage in ("retrieval", "all"):
        if verbose:
            print("-- retrieval --")
        for name, check in (
            ("Milvus（§6.4）", _check_milvus),
            ("Embedding 一致性（§6.3）", _check_embedding_consistency),
            ("Neo4j JOIN 图（§6.6）", _check_neo4j_join_graph),
        ):
            try:
                ok, detail = check()
            except Exception as exc:  # doctor 汇总失败原因，不让单项异常中断
                ok, detail = False, f"{type(exc).__name__}: {exc}"
            results.append((name, ok, detail))

    if stage == "all":
        if verbose:
            print("-- assembly --")
        try:
            ok, detail = _check_assembly()
        except Exception as exc:  # noqa: BLE001
            ok, detail = False, f"{type(exc).__name__}: {exc}"
        results.append(("装配后验证（§10.6）", ok, detail))

    if source_id is not None:
        if verbose:
            print("-- source-scoped --")
        try:
            ok, detail = _check_source_scoped(source_id)
        except Exception as exc:  # noqa: BLE001
            ok, detail = False, f"{type(exc).__name__}: {exc}"
        results.append((f"按源统计（source_id={source_id}）", ok, detail))
    return results


def run_doctor(
    stage: str = "all", *, json_output: bool = False, source_id: str | None = None
) -> int:
    """执行检查并输出；json_output 时 stdout 仅单个 JSON 文档（§15.2）。"""
    if not json_output:
        print(f"== doctor / stage={stage} ==")
    if json_output:
        with contextlib.redirect_stdout(sys.stderr):
            results = _collect_doctor_results(stage, verbose=False, source_id=source_id)
    else:
        results = _collect_doctor_results(stage, verbose=True, source_id=source_id)
    passed = sum(1 for _, ok, _ in results if ok)
    if json_output:
        print(
            json.dumps(
                {
                    "stage": stage,
                    "source_id": source_id,
                    "passed": passed,
                    "total": len(results),
                    "items": [
                        {"name": name, "ok": ok, "detail": detail}
                        for name, ok, detail in results
                    ],
                },
                ensure_ascii=False,
            )
        )
        return 0 if passed == len(results) else 1
    for name, ok, detail in results:
        mark = "PASS" if ok else "FAIL"
        print(f"  [{mark}] {name}: {detail}")
    print(f"== {passed}/{len(results)} 项通过 ==")
    return 0 if passed == len(results) else 1


def _check_config() -> tuple[bool, str]:
    from src.modules.db_deepagent.config import (
        compute_validation_fingerprint,
        load_db_agent_config,
    )

    cfg = load_db_agent_config()
    fp = compute_validation_fingerprint(cfg)
    return True, f"§4.2 检查通过 validation_fingerprint={fp[:12]}…"


# ============================================================================
# 步骤 4：query／chat 与 JSON 输出隔离（§15.1、§15.2）
# ============================================================================


def _prescan_json_mode(argv: list[str]) -> bool:
    """轻量预扫描：判断 --json 是否出现（尊重 -- 分隔符，其后为位置参数）。"""
    for token in argv:
        if token == "--":
            break
        if token == "--json":
            return True
    return False


class _JsonAwareParser(argparse.ArgumentParser):
    """--json 模式下参数错误进入统一 JSON 错误出口（§15.2）。

    exit(0)（--help／--version）属正常帮助退出，不进入错误出口。
    """

    json_mode = False

    def error(self, message: str) -> None:
        if self.json_mode:
            _emit_json_error("invalid_argument", message, code=2)
        super().error(message)

    def exit(self, status: int = 0, message: str | None = None) -> None:
        if status != 0 and self.json_mode:
            _emit_json_error(
                "invalid_argument", (message or "参数错误").strip(), code=2
            )
        super().exit(status, message)


def _emit_json_error(error_type: str, message: str, *, code: int) -> None:
    """JSON 错误文档：唯一允许出现在 stdout 的内容（§15.2 错误协议）。"""
    print(
        json.dumps(
            {
                "status": "error",
                "error": {
                    "error_type": error_type,
                    "message": message,
                    "retryable": False,
                },
            },
            ensure_ascii=False,
        )
    )
    raise SystemExit(code)


def _render_result(result) -> None:
    """Rich／纯文本共用结构的纯文本渲染（步骤 4 先交付纯文本骨架）。"""
    print(f"状态: {result.status}    耗时: {result.stats.elapsed_ms}ms")
    print(f"会话: {result.session_id}  线程: {result.thread_id}")
    print(f"\n{result.answer or '（无答案）'}")
    if result.findings:
        print("\n发现:")
        for finding in result.findings:
            print(f"  - {finding.text}  [证据: {', '.join(finding.evidence_ids)}]")
    if result.evidence:
        print("\n证据:")
        for item in result.evidence:
            reused = ""
            if item.reused_from is not None:
                reused = (
                    f"（沿用：原运行 {item.reused_from.get('run_id')}，"
                    f"生成于 {item.result_generated_at}）"
                )
            print(
                f"  - {item.evidence_id} {item.kind} 行数={item.row_count} "
                f"截断={item.source_truncated} ref={item.result_ref}{reused}"
            )
    if result.limitations:
        print("\n限制:")
        for limitation in result.limitations:
            print(f"  - {limitation}")
    if result.open_questions:
        print("\n待澄清:")
        for question in result.open_questions:
            print(f"  - {question}")
    stats = result.stats
    token_info = (
        f"{stats.input_tokens}/{stats.output_tokens}"
        if stats.input_tokens is not None
        else "未知/不完整"
    )
    print(
        f"\n统计: 模型步骤={stats.model_calls} 摘要尝试={stats.summary_model_calls} "
        f"总尝试={stats.total_model_calls} 工具={stats.tool_calls} Token={token_info}"
    )
    for warning in result.warnings:
        print(f"警告: {warning}")


class _Session:
    """一次 CLI 会话的装配：services + persistence + agent + runner。

    source_id 为平台登记 UUID（doc 4 §4.7）：CLI 会话在构造服务前显式接收
    并验证，服务绑定来源不可变；同一 CLI 会话内复用（chat 允许），退出释放。
    """

    def __init__(
        self,
        cfg,
        *,
        session_id: str,
        source_id: str,
        memory_persistence: bool = False,
    ):
        from src.modules.db_deepagent.agent import build_db_agent
        from src.modules.db_deepagent.persistence.archive import QaArchive
        from src.modules.db_deepagent.persistence.checkpoint import build_persistence
        from src.modules.db_deepagent.services import build_services

        self.cfg = cfg
        self.session_id = session_id
        self.source_id = _validate_source_id_arg(source_id)
        self.services = build_services(cfg, source_id=self.source_id)
        # 命令行覆盖后统一以最终 cfg.persistence.mode 决定装配：
        # memory 模式不建平台池，Runner 也不取会话锁。
        # services 建成后的任一后续构造失败，都按 close() 相同顺序回收
        # （persistence/平台池 → services），不留半装配会话
        memory = memory_persistence or cfg.persistence.mode == "memory"
        self.persistence = None
        platform_pool = None
        try:
            if not memory:
                platform_pool = self._open_platform_pool(cfg)
            self.persistence = build_persistence(cfg, platform_pool=platform_pool)
            self.handle = build_db_agent(
                self.services,
                session_id=session_id,
                checkpointer=self.persistence.checkpointer,
                store=self.persistence.store,
            )
            self.archive = QaArchive(self.persistence.store)
            from src.modules.db_deepagent.runner import DbAgentRunner

            # 会话级互斥只在 postgres 持久化下生效；memory 模式无平台库，跳过
            self.runner = DbAgentRunner(
                services=self.services,
                handle=self.handle,
                archive=self.archive,
                persistence=None if memory else self.persistence,
            )
        except BaseException:
            if self.persistence is not None:
                try:
                    self.persistence.close()
                except Exception:  # noqa: BLE001（回收失败不覆盖原始错误）
                    pass
            elif platform_pool is not None:
                # build_persistence 未接管即失败：自行关闭平台池
                platform_pool.close()
            self.services.close()
            raise

    @staticmethod
    def _open_platform_pool(cfg):
        from src.modules.db_deepagent.persistence.platform_pool import (
            build_platform_pool,
            check_platform_tables,
        )

        pool = build_platform_pool(cfg.platform_db)
        try:
            try:
                pool.open(wait=False)
                pool.wait()
            except Exception as exc:
                raise RuntimeError(f"平台库连接失败: {exc}") from exc
            missing = check_platform_tables(pool, cfg.platform_db.schema_name)
            if missing:
                raise RuntimeError(
                    f"平台库缺表 {', '.join(missing)}；请先运行 maintenance init-platform"
                )
        except BaseException:
            # 打开/等待/表检查任一步失败：函数不返回，池由这里关闭，
            # 不留无主的已打开连接池（调用方拿不到 pool 无法负责）
            pool.close()
            raise
        return pool

    def close(self) -> None:
        try:
            self.persistence.close()
        finally:
            self.services.close()


def _run_acceptance(args) -> int:
    """§18.4：业务 F×N 轮 + 回归/协议每轮；汇总写 workspace/acceptance/。"""
    import subprocess
    from datetime import datetime as _dt
    from pathlib import Path

    from src.modules.db_deepagent.config import load_db_agent_config

    cfg = load_db_agent_config()
    out_dir = (
        Path(cfg.workspace.root) / "acceptance" / _dt.now().strftime("%Y%m%d-%H%M%S")
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    business = "src/tests/db_deepagent/integration/test_business_cases.py"
    regression = " ".join(_acceptance_static_nodes())
    rounds = max(1, args.rounds)
    summary = {"rounds": rounds, "business": [], "static": None}
    for round_no in range(1, rounds + 1):
        proc = subprocess.run(
            ["uv", "run", "pytest", business, "-q", "--run-llm", "--run-db"],
            capture_output=True,
            text=True,
            timeout=1800,
        )
        (out_dir / f"business-round{round_no}.log").write_text(
            proc.stdout + proc.stderr, encoding="utf-8"
        )
        tail = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else ""
        summary["business"].append(
            {"round": round_no, "tail": tail, "exit": proc.returncode}
        )
        print(f"round {round_no}/{rounds}: {tail}")
    proc = subprocess.run(
        ["uv", "run", "pytest", *regression.split(), "-q", "--run-db"],
        capture_output=True,
        text=True,
        timeout=1200,
    )
    (out_dir / "static.log").write_text(proc.stdout + proc.stderr, encoding="utf-8")
    tail = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else ""
    summary["static"] = {"tail": tail, "exit": proc.returncode}
    print(f"static (R+P): {tail}")
    (out_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    print(f"验收记录: {out_dir}")
    ok = all(r["exit"] == 0 for r in summary["business"]) and proc.returncode == 0
    return 0 if ok else 1


def _acceptance_static_nodes() -> list[str]:
    from pathlib import Path as _P

    import yaml

    cases = yaml.safe_load(
        (_P(__file__).parent / "config" / "acceptance_cases.yaml").read_text(
            encoding="utf-8"
        )
    )
    return list(cases.get("regression_nodes", [])) + list(
        cases.get("protocol_nodes", [])
    )


def _run_maintenance_cleanup(args, cfg) -> int:
    if args.dry_run and args.apply:
        parser_error = "cleanup 的 --dry-run 与 --apply 互斥"
        print(parser_error, file=sys.stderr)
        return 2
    before = None
    if args.before:
        from datetime import datetime

        try:
            before = datetime.fromisoformat(args.before)
        except ValueError:
            print("--before 需要 YYYY-MM-DD 日期", file=sys.stderr)
            return 2
        if before.tzinfo is None:  # 朴素时间按本地时区解释，避免与 UTC 比较报错
            before = before.astimezone()
    from src.modules.db_deepagent.agent import build_db_agent
    from src.modules.db_deepagent.persistence.archive import QaArchive
    from src.modules.db_deepagent.persistence.checkpoint import build_persistence
    from src.modules.db_deepagent.persistence.maintenance import MaintenanceService
    from src.modules.db_deepagent.services import build_services

    if cfg.persistence.mode == "memory":
        print("memory 持久化模式没有可清理的平台库数据，跳过 cleanup")
        return 0
    # cleanup 只做平台库线程/归档清理，不执行按源检索；占位来源仅为满足
    # build_services 的来源绑定（doc 4 §4.7）
    services = build_services(cfg, source_id=_PLACEHOLDER_SOURCE_ID)
    try:
        # cleanup 需要真实平台库（checkpoints 在 PG）；接通已核对表的池
        pool = _Session._open_platform_pool(cfg)
        try:
            persistence = build_persistence(cfg, platform_pool=pool)
            handle = build_db_agent(
                services,
                session_id="maintenance",
                checkpointer=persistence.checkpointer,
                store=persistence.store,
            )
            report = MaintenanceService(
                cfg, QaArchive(persistence.store), handle.agent
            ).cleanup(apply=args.apply and not args.dry_run, before=before)
        finally:
            pool.close()
    finally:
        services.close()
    print(f"== cleanup ({'apply' if args.apply else 'dry-run'}) ==")
    print(f"拟删/已删线程: {len(report.deleted_threads)}")
    for thread_id in report.deleted_threads:
        print(f"  - {thread_id}")
    print(
        f"保留: {len(report.kept_threads)}  跳过: {len(report.skipped)}  "
        f"结果目录: {len(report.result_dirs)}  归档条目: {len(report.archives)}"
    )
    for item in report.skipped:
        print(f"  - {item}")
    for result_dir in report.result_dirs:
        print(f"  - 结果目录 {result_dir}")
    for kind, key in report.archives:
        print(f"  - 归档 {kind}/{key}")
    if report.errors:
        print(f"失败: {len(report.errors)}")
        for error in report.errors:
            print(f"  - {error}")
        return 1
    return 0


def _run_query_command(args, json_mode: bool) -> int:
    import uuid as _uuid

    from src.modules.db_deepagent.config import load_db_agent_config

    question = args.question
    session_id = args.session_id or ("s_" + _uuid.uuid4().hex[:10])
    source_id = _validate_source_id_arg(getattr(args, "source_id", None))
    # 配置加载（env 加载提示走 stdout）留在重定向内；登记源解析过程不写
    # stdout，放在重定向之外：--json 模式的错误出口保持"stdout 单 JSON
    # 文档"协议（§15.2）
    with (
        contextlib.redirect_stdout(sys.stderr)
        if json_mode
        else contextlib.nullcontext()
    ):
        cfg = load_db_agent_config()
        if args.memory_persistence or cfg.persistence.mode == "memory":
            cfg = cfg.model_copy(deep=True)
            cfg.persistence.mode = "memory"
        if args.fixed_now:
            cfg = cfg.model_copy(deep=True)
            cfg.clock.fixed_now = args.fixed_now
    # 登记源注入：业务库连接与三键运行配置取登记值（doc 4 §4.7）
    cfg = _resolve_registration_and_apply(cfg, source_id, json_mode=json_mode)
    with (
        contextlib.redirect_stdout(sys.stderr)
        if json_mode
        else contextlib.nullcontext()
    ):
        session = _Session(
            cfg,
            session_id=session_id,
            source_id=source_id,
            memory_persistence=args.memory_persistence,
        )
        try:
            result = session.runner.run(
                question,
                session_id,
                thread_id=args.thread_id,
                request_id=args.request_id,
                source_id=source_id,
            )
        finally:
            session.close()
    if json_mode:
        print(result.model_dump_json())
    else:
        _render_result(result)
    if args.show_trace and json_mode:
        print(
            "（--show-trace 在 --json 模式下忽略；轨迹见会话 audit.jsonl）",
            file=sys.stderr,
        )
    return (
        0
        if result.status in ("completed", "partial", "needs_clarification", "no_data")
        else 1
    )


def _run_chat_command(args) -> int:
    import uuid as _uuid

    from src.modules.db_deepagent.config import load_db_agent_config

    session_id = args.session_id or ("s_" + _uuid.uuid4().hex[:10])
    source_id = _validate_source_id_arg(getattr(args, "source_id", None))
    cfg = load_db_agent_config()
    if args.memory_persistence or cfg.persistence.mode == "memory":
        cfg = cfg.model_copy(deep=True)
        cfg.persistence.mode = "memory"
    # 登记源注入：业务库连接与三键运行配置取登记值（doc 4 §4.7）
    cfg = _resolve_registration_and_apply(cfg, source_id)
    print(
        f"交互会话 {session_id}（source_id={source_id}；/rotate /trace "
        "/result <ref> /exit）"
    )
    session = _Session(
        cfg,
        session_id=session_id,
        source_id=source_id,
        memory_persistence=args.memory_persistence,
    )
    last_result = None
    pending_rotate = False  # /rotate 登记：下一轮问题前强制轮换线程（§13.4）
    try:
        while True:
            try:
                line = input("问题> ").strip()
            except EOFError:
                break
            if not line:
                continue
            if line == "/exit":
                break
            if line == "/rotate":
                pending_rotate = True
                print("（已登记：下一轮问题前将强制轮换到新线程并携带证据）")
                continue
            if line == "/trace":
                _print_last_trace(session_id, cfg)
                continue
            if line.startswith("/result "):
                _print_result_ref(session, line.split(None, 1)[1], cfg)
                continue
            last_result = session.runner.run(
                line, session_id, force_rotate=pending_rotate, source_id=source_id
            )
            pending_rotate = False
            _render_result(last_result)
    finally:
        session.close()
    return 0


def _print_last_trace(session_id: str, cfg) -> None:
    path = (
        __import__("pathlib").Path(cfg.workspace.root)
        / "sessions"
        / session_id
        / "audit.jsonl"
    )
    if not path.exists():
        print("（暂无轨迹）")
        return
    lines = path.read_text(encoding="utf-8").splitlines()[-20:]
    import json as _json

    for line in lines:
        try:
            record = _json.loads(line)
            print(
                f"  seq={record.get('seq')} tools={record.get('tools')} "
                f"elapsed={record.get('elapsed_ms')}ms"
            )
        except ValueError:
            continue


def _print_result_ref(session, ref_args: str, cfg) -> None:
    parts = ref_args.split()
    ref = parts[0]
    offset = int(parts[1]) if len(parts) > 1 else 0
    try:
        page = session.services.results.read_rows(
            ref, session_id=session.session_id, offset=offset, limit=20
        )
    except Exception as exc:  # 查看命令按错误展示
        print(f"读取失败: {exc}")
        return
    print(f"ref={ref} 已保存 {page.saved_row_count} 行（从 {page.offset} 起，每页 20）")
    for row in page.rows:
        print("  " + " | ".join(str(v) for v in row))


def _check_assembly() -> tuple[bool, str]:
    """doctor --stage all 的装配后验证（§10.6；不调用模型生成）。

    build_services 需要来源绑定（doc 4 §4.7）：装配验证不执行按源检索，
    使用固定占位来源；真实会话必须传登记 source_id。
    """
    from src.modules.db_deepagent.agent import build_db_agent, stage_expected_tool_names
    from src.modules.db_deepagent.config import load_db_agent_config
    from src.modules.db_deepagent.services import build_services

    cfg = load_db_agent_config()
    services = build_services(cfg, source_id=_PLACEHOLDER_SOURCE_ID)
    try:
        handle = build_db_agent(services, session_id="doctor-assembly")
        expected = stage_expected_tool_names(cfg)
        return (
            True,
            f"装配验证通过：{len(expected)} 件工具，harness={handle.harness_key}",
        )
    except Exception as exc:  # doctor 汇总失败
        return False, f"{type(exc).__name__}: {exc}"
    finally:
        services.close()


def _init_logging(json_mode: bool) -> None:
    """按 §16.2 初始化日志：复用 src/configs/logging.yaml 的处理器。

    此前 CLI 从未接线，nl2sql.db_deepagent.* 的 INFO 日志无处理器不可见。
    --json 模式下 console 处理器须绑定 stderr（保持 stdout 单 JSON 文档）：
    在 redirect 上下文内初始化，ext://sys.stdout 解析到重定向后的流。
    """
    from pathlib import Path as _P

    from src.utils.logger import setup_logging_from_yaml

    path = _P(__file__).resolve().parents[2] / "configs" / "logging.yaml"
    if not path.exists():
        return  # 无配置时保持静默（不阻断 CLI）
    if json_mode:
        with contextlib.redirect_stdout(sys.stderr):
            setup_logging_from_yaml(str(path))
    else:
        setup_logging_from_yaml(str(path))


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    json_mode = _prescan_json_mode(argv)
    _init_logging(json_mode)
    parser = _JsonAwareParser(
        prog="db_deepagent", description="db_deepagent 命令行入口"
    )
    # 类级设置让 add_parser 生成的子解析器同走 JSON 错误出口（单次进程内安全）
    _JsonAwareParser.json_mode = json_mode
    parser.json_mode = json_mode
    parser.add_argument("--config", default=None, help="配置文件路径（默认模块内配置）")
    parser.add_argument(
        "--fixed-now", default=None, help="覆盖 clock.fixed_now（ISO 时间，验收用）"
    )
    parser.add_argument(
        "--memory-persistence",
        action="store_true",
        help="显式选择内存模式（会话不保存）",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_doctor = sub.add_parser("doctor", help="环境／组件检查（§15.3）")
    p_doctor.add_argument(
        "--stage", choices=_DOCTOR_STAGES, default="all", help="检查阶段"
    )
    p_doctor.add_argument("--json", action="store_true", help="结构化 JSON 输出")
    p_doctor.add_argument(
        "--source-id",
        default=None,
        help="只读统计指定源的 Milvus/Neo4j 计数（doc 4 §4.7 可选辅助交付）",
    )

    p_maint = sub.add_parser("maintenance", help="平台库维护动作")
    p_maint.add_argument(
        "action",
        choices=["init-platform", "cleanup"],
        help="初始化平台库 Schema 与表（§13.1）／清理过期线程与结果（§13.5）",
    )
    p_maint.add_argument("--dry-run", action="store_true", help="预览（默认）")
    p_maint.add_argument("--apply", action="store_true", help="执行清理")
    p_maint.add_argument("--before", default=None, help="日期筛选 YYYY-MM-DD（只收紧）")

    p_query = sub.add_parser("query", help="单次问答")
    p_query.add_argument("question", nargs="?", default=None, help="自然语言问题")
    p_query.add_argument("--json", action="store_true", help="输出 DbAgentResult JSON")
    p_query.add_argument("--plain", action="store_true", help="纯文本输出")
    p_query.add_argument("--session-id", default=None)
    p_query.add_argument("--thread-id", default=None)
    p_query.add_argument("--request-id", default=None)
    p_query.add_argument(
        "--source-id",
        required=True,
        help="平台登记 source_id（UUID，必填；不自动选源、不回退全图）",
    )
    p_query.add_argument("--show-trace", action="store_true", help="显示工具调用轨迹")

    p_chat = sub.add_parser("chat", help="交互会话")
    p_chat.add_argument("--session-id", default=None)
    p_chat.add_argument(
        "--source-id",
        required=True,
        help="平台登记 source_id（UUID，必填；不自动选源、不回退全图）",
    )

    p_acc = sub.add_parser("acceptance", help="验收题集运行（§18.4）")
    p_acc.add_argument("run", help="执行验收")
    p_acc.add_argument("--rounds", type=int, default=3, help="业务任务轮数（默认 3）")

    args = parser.parse_args(argv)
    if args.command == "doctor":
        source_id = None
        if getattr(args, "source_id", None):
            source_id = _validate_source_id_arg(args.source_id)
        return run_doctor(args.stage, json_output=json_mode, source_id=source_id)
    if args.command == "maintenance":
        from src.modules.db_deepagent.config import load_db_agent_config

        cfg = load_db_agent_config(args.config)
        if args.action == "init-platform":
            from src.modules.db_deepagent.persistence.platform_pool import init_platform

            tables = init_platform(cfg.platform_db)
            print(f"init-platform 完成: {', '.join(sorted(tables))}")
            return 0
        return _run_maintenance_cleanup(args, cfg)
    if args.command == "acceptance":
        return _run_acceptance(args)
    if args.command == "query":
        if not args.question:
            parser.error("query 需要一个问题参数")
        if args.plain and json_mode:
            print("--json 优先，忽略 --plain", file=sys.stderr)
        return _run_query_command(args, json_mode)
    if args.command == "chat":
        if json_mode:
            _emit_json_error(
                "invalid_argument",
                "交互模式不支持 --json（多轮无单一结果文档）",
                code=2,
            )
        return _run_chat_command(args)
    print(f"命令 {args.command!r} 将在步骤 5 交付（docs/v4/5 §19）")
    return 2


if __name__ == "__main__":
    sys.exit(main())
