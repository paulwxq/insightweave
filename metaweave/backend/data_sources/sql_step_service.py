"""受管 SQL 样例步骤 worker（doc 25.1、6.2 §7.1）。

上游就绪判定读取 `md/`、`domain/`、`rel/` 顶层唯一成功采集 marker
（doc 6 §4.1）；输入指纹（md/domain/rel 文件 SHA-256）在生成前后复查，
保留既有重跑与编辑冲突检查。产物目录由 `sql-rag/` 更名 `sql/`。
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path

import yaml
from psycopg.conninfo import make_conninfo

from backend.data_sources.ddl_step_service import (
    build_managed_ddl_config,
    build_managed_output_snapshot,
)
from backend.data_sources.step_context import (
    DbStepConfigError,
    StepContext,
    StepOutcome,
)
from backend.workspace_files.backups import backup_directory_before_update
from backend.workspace_files.markers import read_collection_marker
from metaweave.core.clean_targets import (
    resolve_domain_output_dir,
    resolve_md_output_dir,
    resolve_rel_output_dir,
    resolve_sql_output_dir,
)
from metaweave.core.metadata.connector import DatabaseConnector
from metaweave.core.sql_rag.execution import run_sql_rag_generation
from metaweave.core.sql_rag.validator import validate_validation_config_keys
from metaweave.db.postgres_options import build_connect_kwargs
from metaweave.services.llm_config_resolver import resolve_module_llm_config
from metaweave.services.llm_service import LLMService

logger = logging.getLogger("backend.data_sources")
SQL_STEP_NAME = "sql"
UPSTREAM = ("md", "domain", "rel")
NOT_READY = "工作区的 MD、Domain、Rel 产物未就绪；请先执行上游采集"


class SqlPreparationError(RuntimeError):
    """SQL 准备阶段的非受控本地故障；503 载体。"""

    def __init__(self, message: str):
        super().__init__(message)


@dataclass(frozen=True)
class SqlInputSnapshot:
    markers: tuple[tuple[str, object], ...]
    fingerprints: tuple[tuple[str, str], ...]
    md_dir: Path
    domain_path: Path
    rel_dir: Path
    output_dir: Path


def _regular_bytes(path: Path, label: str) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"{label} 缺失或不是普通文件")
    data = path.read_bytes()
    data.decode("utf-8")
    return data


def _fingerprint(path: Path, label: str) -> str:
    return hashlib.sha256(_regular_bytes(path, label)).hexdigest()


def _generation_config(config: dict) -> tuple[dict, dict]:
    node = config.get("sql_rag") or {}
    if not isinstance(node, dict):
        raise ValueError("sql_rag 必须是对象")
    generation = node.get("generation") or {}
    validation = node.get("validation") or {}
    if not isinstance(generation, dict) or not isinstance(validation, dict):
        raise ValueError("sql_rag.generation 与 validation 必须是对象")
    for key, default in (
        ("questions_per_domain", 10),
        ("uncategorized_questions", 3),
        ("max_prompt_chars", 100000),
    ):
        value = generation.get(key, default)
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or value < (1 if key == "max_prompt_chars" else 0)
        ):
            raise ValueError(f"sql_rag.generation.{key} 必须是非负整数")
    for key in ("skip_uncategorized",):
        if key in generation and not isinstance(generation[key], bool):
            raise ValueError(f"sql_rag.generation.{key} 必须是布尔值")
    if "enabled" in validation and not isinstance(validation["enabled"], bool):
        raise ValueError("sql_rag.validation.enabled 必须是布尔值")
    if validation.get("enabled", True):
        validate_validation_config_keys(validation)
        for key, default, minimum in (
            ("sql_validation_max_concurrent", 5, 1),
            ("sql_validation_max_retries", 2, 0),
            ("timeout", 30, 1),
            ("repair_batch_size", 1, 1),
        ):
            value = validation.get(key, default)
            if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
                raise ValueError(
                    f"sql_rag.validation.{key} 必须是大于等于 {minimum} 的整数"
                )
        for key in ("sql_validation_readonly", "enable_sql_repair"):
            if key in validation and not isinstance(validation[key], bool):
                raise ValueError(f"sql_rag.validation.{key} 必须是布尔值")
    return generation, validation


def inspect_managed_sql_inputs(
    source_id: str, workspace_id: str, db_name: str, config: dict
) -> tuple[SqlInputSnapshot | None, list[str], str]:
    """读取三个上游目录的采集 marker 与受管文件指纹（不访问平台库）。

    就绪条件（doc 6 §4.1）：md/domain/rel 目录各自有唯一 success marker，
    且 md 清单、Domain YAML、Rel 报告内容均有效；指纹用于生成期变化复查。
    """
    missing: list[str] = []
    reasons: list[str] = []
    markers: list[tuple[str, object]] = []

    md_dir = resolve_md_output_dir(source_id, workspace_id)
    domain_path = resolve_domain_output_dir(source_id, workspace_id) / "db_domains.yaml"
    rel_dir = resolve_rel_output_dir(source_id, workspace_id)
    rel_path = rel_dir / f"{db_name}.relationships_global.md"
    output_dir = resolve_sql_output_dir(source_id, workspace_id)
    dir_by_step = {"md": md_dir, "domain": domain_path.parent, "rel": rel_dir}
    for step in UPSTREAM:
        marker, error = read_collection_marker(dir_by_step[step])
        if marker is None or marker.status != "success":
            missing.append(step)
            reasons.append(error or f"{step} 目录没有成功采集 marker")
        else:
            markers.append((step, marker.run_id))

    fingerprints: list[tuple[str, str]] = []
    md_names: set[str] = set()
    md_valid = True

    try:
        files = sorted(md_dir.glob("*.md"))
        if not files:
            raise ValueError("MD 目录没有顶层 .md 文件")
        for path in files:
            parts = path.stem.split(".")
            if len(parts) != 3 or parts[0] != db_name or not all(parts):
                raise ValueError(f"MD 文件名不符合 <库>.<schema>.<表>.md：{path.name}")
            md_names.add(path.stem)
            fingerprints.append((f"md/{path.name}", _fingerprint(path, "MD 文件")))
    except (ValueError, OSError, UnicodeError) as exc:
        md_valid = False
        missing.append("md")
        reasons.append(str(exc))

    try:
        raw = _regular_bytes(domain_path, "Domain YAML")
        document = yaml.safe_load(raw)
        if not isinstance(document, dict):
            raise ValueError("Domain YAML 不是对象")
        database = document.get("database")
        if not isinstance(database, dict) or database.get("name") != db_name:
            raise ValueError("Domain YAML 库名与登记源不一致")
        domains = document.get("domains")
        if not isinstance(domains, list) or not domains:
            raise ValueError("Domain YAML 的 domains 不是非空列表")
        generation, _ = _generation_config(config)
        participating = 0
        for domain in domains:
            if (
                not isinstance(domain, dict)
                or not isinstance(domain.get("name"), str)
                or not domain["name"].strip()
            ):
                raise ValueError("Domain name 必须是非空字符串")
            tables = domain.get("tables", [])
            if not isinstance(tables, list) or any(
                not isinstance(table, str) for table in tables
            ):
                raise ValueError(f"Domain {domain['name']} 的 tables 必须是字符串列表")
            count = (
                generation.get("uncategorized_questions", 3)
                if domain["name"] == "_未分类_"
                else generation.get("questions_per_domain", 10)
            )
            if domain["name"] == "_未分类_" and generation.get(
                "skip_uncategorized", False
            ):
                continue
            if not tables or count <= 0:
                continue
            participating += 1
            for table in tables:
                if md_valid and table not in md_names:
                    raise ValueError(f"Domain 表 {table} 没有对应的受管 MD")
        if participating == 0:
            raise ValueError("没有可生成 SQL 的业务域")
        fingerprints.append(("domain/db_domains.yaml", hashlib.sha256(raw).hexdigest()))
    except (ValueError, OSError, UnicodeError, yaml.YAMLError) as exc:
        missing.append("domain")
        reasons.append(str(exc))

    try:
        raw = _regular_bytes(rel_path, "Rel 报告")
        content = raw.decode("utf-8")
        lines = {line.strip() for line in content.splitlines()}
        if "# 表间关系发现报告" not in lines or f"database: {db_name}" not in lines:
            raise ValueError("Rel 报告头或库名不匹配")
        fingerprints.append((f"rel/{rel_path.name}", hashlib.sha256(raw).hexdigest()))
    except (ValueError, OSError, UnicodeError) as exc:
        missing.append("rel")
        reasons.append(str(exc))

    if missing:
        return None, sorted(set(missing)), "；".join(reasons)
    return (
        SqlInputSnapshot(
            tuple(markers),
            tuple(sorted(fingerprints)),
            md_dir,
            domain_path,
            rel_dir,
            output_dir,
        ),
        [],
        "",
    )


def _verify_output(
    output_dir: Path,
    db_name: str,
    generated: int,
    validation: dict | None,
    repair: bool,
) -> tuple[bool, str, int]:
    path = output_dir / f"qs_{db_name}_pair.json"
    if path.is_symlink() or not path.is_file() or path.parent != output_dir:
        return False, "SQL 样例主文件缺失或位置错误", 0
    try:
        pairs = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return False, "SQL 样例主文件无法解析", 0
    if not isinstance(pairs, list) or not pairs:
        return False, "SQL 样例主文件没有可保留的样例", 0
    for pair in pairs:
        if not isinstance(pair, dict) or any(
            not isinstance(pair.get(key), str) or not pair[key].strip()
            for key in ("question", "sql")
        ):
            return False, "SQL 样例包含空问题或空 SQL", 0
    retained = len(pairs)
    if retained > generated or (validation is None and retained != generated):
        return False, "SQL 样例数量与生成统计不一致", retained
    if validation is not None:
        reports = list(output_dir.glob("sql_validation_summary_*.log"))
        if not reports or any(
            report.is_symlink() or not report.is_file() for report in reports
        ):
            return False, "SQL 校验报告缺失或不是普通文件", retained
        if repair:
            backup = path.with_suffix(path.suffix + ".backup")
            if backup.is_symlink() or not backup.is_file():
                return False, "SQL 修复备份缺失或不是普通文件", retained
        if validation.get("total") != generated:
            return False, "SQL 校验数量与生成数量不一致", retained
        if not repair and validation.get("invalid", 0):
            return False, "存在无效 SQL 且未启用修复", retained
        expected = generated - validation["repair_apply_stats"]["deleted"]
        if retained != expected:
            return False, "SQL 最终保留数与校验回写统计不一致", retained
    return True, "", retained


def run_sql_step(ctx: StepContext) -> StepOutcome:
    """执行受管 SQL 步骤：上游 marker/指纹前置 → 备份 → 生成 → 复查 → 核验。"""
    started = time.perf_counter()
    try:
        _generation_config(ctx.main_config)
    except ValueError as exc:
        raise DbStepConfigError(str(exc)) from None
    stage = "SQL 输入与配置准备"
    try:
        source_name = ctx.source.db_name
        input_snapshot, missing, reason = inspect_managed_sql_inputs(
            ctx.source_id, ctx.workspace_id, source_name, ctx.main_config
        )
        if input_snapshot is None:
            raise DbStepConfigError(f"SQL 输入未就绪（{','.join(missing)}）：{reason}")
        _generation, validation_cfg = _generation_config(ctx.main_config)
        password = (
            ctx.decrypt_password() if validation_cfg.get("enabled", True) else None
        )
        managed_config = build_managed_output_snapshot(
            ctx.main_config,
            ctx.source_id,
            ctx.workspace_id,
            source_name,
            ctx.source.selected_schemas,
        )
        if password is not None:
            managed_config = build_managed_ddl_config(
                ctx.main_config, ctx.source, password, ctx.workspace_id
            )
            database = managed_config["database"]
            make_conninfo(
                **build_connect_kwargs(
                    host=database["host"],
                    port=database["port"],
                    dbname=database["database"],
                    user=database["user"],
                    password=database["password"],
                    sslmode=database.get("sslmode"),
                )
            )
        managed_config["sql_rag"] = dict(managed_config.get("sql_rag") or {})
        managed_config["sql_rag"]["generation"] = dict(_generation)
        managed_config["sql_rag"]["validation"] = dict(validation_cfg)
        llm = LLMService(resolve_module_llm_config(managed_config, "sql_rag.llm"))
        if input_snapshot.output_dir != resolve_sql_output_dir(
            ctx.source_id, ctx.workspace_id
        ):
            raise ValueError("受管 SQL 输出目录与工作区不一致")
    except ValueError as exc:
        raise DbStepConfigError(str(exc)) from None
    except DbStepConfigError:
        raise
    except Exception as exc:
        logger.error("SQL %s故障（%s）", stage, type(exc).__name__)
        raise SqlPreparationError("SQL 准备阶段故障") from None

    run_result = None
    connector = None
    ok = False
    reason = ""
    retained = 0
    try:
        if validation_cfg.get("enabled", True):
            connector = DatabaseConnector(managed_config["database"])
            # 建池会异步连接；实际取得连接后再备份，避免连接失败时动旧产物
            with connector.get_connection():
                pass
        backup_directory_before_update(input_snapshot.output_dir)
        run_result = run_sql_rag_generation(
            managed_config,
            domains_path=input_snapshot.domain_path,
            md_dir=input_snapshot.md_dir,
            rel_dir=input_snapshot.rel_dir,
            output_dir=input_snapshot.output_dir,
            llm_service=llm,
            connector=connector,
        )
        if run_result.generation.success:
            ok, reason, retained = _verify_output(
                input_snapshot.output_dir,
                source_name,
                run_result.generation.total_generated,
                run_result.validation,
                validation_cfg.get("enable_sql_repair", False),
            )
        else:
            reason = "所有业务域均未生成 SQL 样例"
    except Exception as exc:
        logger.exception("SQL 样例生成或校验失败")
        reason = f"SQL 样例生成或校验失败（{type(exc).__name__}）"
        if isinstance(exc, (ValueError, OSError)) and str(exc):
            reason += f"：{exc}"
    finally:
        if connector is not None:
            try:
                connector.close()
            except Exception:
                logger.warning("SQL 源库连接关闭失败", exc_info=True)

    # 结束复查：生成期间上游文件被重跑或编辑则本次产物无效（既有冲突检查）
    try:
        latest, missing, changed_reason = inspect_managed_sql_inputs(
            ctx.source_id, ctx.workspace_id, source_name, ctx.main_config
        )
        if (
            latest is None
            or latest.markers != input_snapshot.markers
            or latest.fingerprints != input_snapshot.fingerprints
        ):
            ok = False
            input_reason = (
                f"SQL 上游输入在生成期间发生变化：{changed_reason or ','.join(missing)}"
            )
            reason = f"{input_reason}；{reason}" if reason else input_reason
    except Exception as exc:
        ok = False
        reason = f"SQL 结束复查故障（{type(exc).__name__}）；{reason}"

    if not ok:
        if run_result is not None:
            generation = run_result.generation
            reason += (
                f"；跳过域：{', '.join(generation.skipped_domains) or '无'}"
                f"；生成失败域：{', '.join(generation.failed_domains) or '无'}"
                f"；零产出域：{', '.join(generation.zero_output_domains) or '无'}"
            )
        return StepOutcome(success=False, message=reason)

    generation = run_result.generation
    details = [
        f"生成 {generation.total_generated} 条，保留 {retained} 条，"
        f"删除 {generation.total_generated - retained} 条",
        "SQL 校验已跳过" if run_result.validation is None else "SQL 校验已执行",
        f"跳过域：{', '.join(generation.skipped_domains) or '无'}",
        f"生成失败域：{', '.join(generation.failed_domains) or '无'}",
        f"零产出域：{', '.join(generation.zero_output_domains) or '无'}",
    ]
    input_files = [
        (
            path,
            next(
                run_id
                for step, run_id in input_snapshot.markers
                if path.startswith(step)
            ),
        )
        for path, _ in input_snapshot.fingerprints
    ]
    output_files = [
        f"sql/{path.name}"
        for path in sorted(
            input_snapshot.output_dir.iterdir(), key=lambda item: item.name
        )
        if not path.name.startswith(".") and path.is_file()
    ]
    return StepOutcome(
        success=True,
        message="；".join(details) + f"（{time.perf_counter() - started:.1f}s）",
        input_files=input_files,
        output_files=output_files,
    )
