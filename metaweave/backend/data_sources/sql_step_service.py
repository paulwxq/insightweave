"""受管 SQL 样例采集：同作业输入核验、生成及状态写回。"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

import yaml
from psycopg.conninfo import make_conninfo

from backend.data_sources.db_step_service import DbStepConfigError, SourceDisabledError
from backend.data_sources.ddl_step_service import (
    build_managed_ddl_config,
    build_managed_output_snapshot,
)
from backend.data_sources.registration_service import PlatformUnavailableError
from backend.data_sources.repository import get_db_source
from backend.jobs.repository import (
    STATUS_FAILED,
    STATUS_SUCCESS,
    SourceRegistrationMissingError,
    StepAlreadyRunningError,
    WorkspaceMissingError,
    WorkspaceSourceMismatchError,
    assert_workspace_owner,
    finish_step,
    get_step_row,
    start_step,
)
from backend.persistence.platform_db import (
    load_platform_settings_from_env,
    platform_session,
)
from backend.publishing.repository import lock_db_source_row
from backend.security.source_password import decrypt_password
from metaweave.core.clean_targets import (
    resolve_domain_output_dir,
    resolve_md_output_dir,
    resolve_rel_output_dir,
    resolve_sql_rag_output_dir,
)
from metaweave.core.metadata.connector import DatabaseConnector
from metaweave.core.metadata.execution import load_main_config_snapshot
from metaweave.core.sql_rag.execution import run_sql_rag_generation
from metaweave.core.sql_rag.validator import validate_validation_config_keys
from metaweave.db.postgres_options import build_connect_kwargs
from metaweave.services.llm_config_resolver import resolve_module_llm_config
from metaweave.services.llm_service import LLMService
from metaweave.utils.source_id import normalize_source_id

logger = logging.getLogger("backend.data_sources")
SQL_STEP_NAME = "sql"
UPSTREAM = ("md", "domain", "rel")
NOT_READY = "同一作业的 MD、Domain、Rel 产物未就绪；请先执行上游步骤或指定已有 job_id"


class SqlPreparationError(PlatformUnavailableError):
    """SQL 准备阶段的非受控本地故障。"""


@dataclass(frozen=True)
class SqlInputSnapshot:
    markers: tuple[tuple[str, object], ...]
    fingerprints: tuple[tuple[str, str], ...]
    md_dir: Path
    domain_path: Path
    rel_dir: Path
    output_dir: Path


def _failure(started: float, message: str, job_id: UUID | None = None) -> dict:
    result = {
        "success": False,
        "execution_time": time.perf_counter() - started,
        "message": message,
    }
    if job_id is not None:
        result["job_id"] = str(job_id)
    return result


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
    session, source_id: UUID, job_id: UUID, db_name: str, config: dict
) -> tuple[SqlInputSnapshot | None, list[str], str]:
    """读取步骤行和三类受管文件；供就绪接口、启动和结束复查共用。"""
    missing: list[str] = []
    reasons: list[str] = []
    markers: list[tuple[str, object]] = []
    for step in UPSTREAM:
        try:
            row = get_step_row(session, job_id, step)
        except Exception as exc:
            raise PlatformUnavailableError("平台库连接或读取失败") from exc
        if row is None or row.last_status != STATUS_SUCCESS:
            missing.append(step)
            reasons.append(f"{step} 步骤尚未成功")
        else:
            markers.append((step, row.updated_at))

    md_dir = resolve_md_output_dir(source_id, job_id)
    domain_path = resolve_domain_output_dir(source_id, job_id) / "db_domains.yaml"
    rel_dir = resolve_rel_output_dir(source_id, job_id)
    rel_path = rel_dir / f"{db_name}.relationships_global.md"
    output_dir = resolve_sql_rag_output_dir(source_id, job_id)
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


def get_sql_readiness(settings, source_id, job_id) -> dict:
    source_uuid = normalize_source_id(source_id)
    job_uuid = normalize_source_id(job_id)
    try:
        config = load_main_config_snapshot("configs/metadata_config.yaml")
        _generation_config(config)
    except ValueError as exc:
        raise DbStepConfigError(str(exc)) from None
    except Exception as exc:
        raise DbStepConfigError(
            f"主配置加载或校验失败（{type(exc).__name__}）"
        ) from None
    try:
        with platform_session(settings) as session:
            source = get_db_source(session, source_uuid)
            if source is None:
                raise DbStepConfigError("未找到指定 source_id 的数据源")
            if not source.is_active:
                raise SourceDisabledError("该数据源已禁用，不能执行作业")
            try:
                assert_workspace_owner(session, job_uuid, source_uuid)
            except (WorkspaceMissingError, WorkspaceSourceMismatchError) as exc:
                raise DbStepConfigError(str(exc)) from None
            try:
                snapshot, missing, message = inspect_managed_sql_inputs(
                    session, source_uuid, job_uuid, source.db_name, config
                )
            except ValueError as exc:
                raise DbStepConfigError(str(exc)) from None
            return {
                "ready": snapshot is not None,
                "missing": missing,
                "message": message,
            }
    except DbStepConfigError:
        raise
    except Exception as exc:
        logger.error("SQL 就绪查询的平台库读取故障（%s）", type(exc).__name__)
        raise PlatformUnavailableError("平台库暂不可用") from None


def _clean_sql_top_level_files(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    children = list(directory.iterdir())
    if any(child.is_symlink() for child in children):
        raise ValueError("SQL-RAG 输出目录含符号链接")
    for child in children:
        if child.is_symlink():
            raise ValueError("SQL-RAG 输出目录含符号链接")
        if child.is_file():
            child.unlink()
        elif not child.is_dir():
            raise ValueError(f"SQL-RAG 输出目录含非常规条目：{child.name}")


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


def run_sql_step(config_path, source_id, job_id=None) -> dict:
    started = time.perf_counter()
    if job_id is None:
        raise DbStepConfigError(NOT_READY)
    try:
        source_uuid = normalize_source_id(source_id)
        job_uuid = normalize_source_id(job_id)
        config = load_main_config_snapshot(config_path)
        settings = load_platform_settings_from_env()
        _generation_config(config)
    except ValueError as exc:
        raise DbStepConfigError(str(exc)) from None
    except Exception as exc:
        raise DbStepConfigError(
            f"主配置加载或校验失败（{type(exc).__name__}）"
        ) from None

    stage = "平台库读取"
    try:
        with platform_session(settings) as session:
            source = get_db_source(session, source_uuid)
            if source is None:
                raise DbStepConfigError("未找到指定 source_id 的数据源")
            if source.db_type != "postgresql":
                raise DbStepConfigError("当前仅支持 PostgreSQL 数据源")
            if not source.username or not source.host or not source.db_name:
                raise DbStepConfigError("数据源登记缺少必要的连接字段")
            if not source.is_active:
                raise SourceDisabledError("该数据源已禁用，不能执行作业")
            try:
                assert_workspace_owner(session, job_uuid, source_uuid)
            except (WorkspaceMissingError, WorkspaceSourceMismatchError) as exc:
                raise DbStepConfigError(str(exc)) from None
            source_name = source.db_name
            stage = "SQL 输入与配置准备"
            input_snapshot, missing, reason = inspect_managed_sql_inputs(
                session, source_uuid, job_uuid, source_name, config
            )
            if input_snapshot is None:
                raise DbStepConfigError(
                    f"SQL 输入未就绪（{','.join(missing)}）：{reason}"
                )
            _generation, validation_cfg = _generation_config(config)
            password = (
                decrypt_password(source.password_ciphertext)
                if validation_cfg.get("enabled", True)
                else None
            )
            managed_config = build_managed_output_snapshot(
                config, source_uuid, job_uuid, source_name, source.selected_schemas
            )
            if password is not None:
                managed_config = build_managed_ddl_config(
                    config, source, password, job_uuid
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
    except (DbStepConfigError, SourceDisabledError):
        raise
    except PlatformUnavailableError as exc:
        logger.error("SQL 上游步骤行读取故障（%s）", type(exc).__name__)
        return _failure(started, "平台库连接或读取失败")
    except ValueError as exc:
        raise DbStepConfigError(str(exc)) from None
    except Exception as exc:
        logger.error("SQL %s故障（%s）", stage, type(exc).__name__)
        if stage != "平台库读取":
            raise SqlPreparationError(
                f"SQL 准备阶段故障（{type(exc).__name__}）"
            ) from None
        return _failure(started, f"平台库连接或读取失败（{type(exc).__name__}）")

    try:
        managed_config["sql_rag"] = dict(managed_config.get("sql_rag") or {})
        managed_config["sql_rag"]["generation"] = dict(_generation)
        managed_config["sql_rag"]["validation"] = dict(validation_cfg)
        llm = LLMService(resolve_module_llm_config(managed_config, "sql_rag.llm"))
        if input_snapshot.output_dir != resolve_sql_rag_output_dir(
            source_uuid, job_uuid
        ):
            raise ValueError("受管 SQL 输出目录与作业工作区不一致")
    except ValueError as exc:
        raise DbStepConfigError(str(exc)) from None
    except Exception as exc:
        logger.error("SQL 准备阶段故障（%s）", type(exc).__name__)
        raise SqlPreparationError("SQL 准备阶段故障") from None

    try:
        with platform_session(settings) as session:
            locked_source = lock_db_source_row(session, source_uuid)
            if locked_source is None:
                raise DbStepConfigError("未找到指定 source_id 的数据源")
            if not locked_source.is_active:
                raise SourceDisabledError("该数据源已禁用，不能执行作业")
            latest, missing, reason = inspect_managed_sql_inputs(
                session, source_uuid, job_uuid, source_name, config
            )
            if (
                latest is None
                or latest.markers != input_snapshot.markers
                or latest.fingerprints != input_snapshot.fingerprints
            ):
                raise DbStepConfigError(
                    f"SQL 上游输入已变化或未就绪：{reason or ','.join(missing)}"
                )
            start_step(session, job_uuid, source_uuid, SQL_STEP_NAME)
    except (DbStepConfigError, SourceDisabledError):
        raise
    except PlatformUnavailableError as exc:
        logger.error("SQL 启动前步骤行复查故障（%s）", type(exc).__name__)
        return _failure(started, "平台库连接或读取失败")
    except SourceRegistrationMissingError:
        raise DbStepConfigError("未找到指定 source_id 的数据源") from None
    except StepAlreadyRunningError as exc:
        return _failure(started, str(exc), job_uuid)
    except Exception as exc:
        logger.error("SQL 步骤启动状态写入失败（%s）", type(exc).__name__)
        return _failure(started, f"作业状态登记失败（{type(exc).__name__}）", job_uuid)

    run_result = None
    connector = None
    ok = False
    reason = ""
    retained = 0
    try:
        if validation_cfg.get("enabled", True):
            connector = DatabaseConnector(managed_config["database"])
            # 建池会异步连接；实际取得连接后再清理，避免连接失败时删除旧产物。
            with connector.get_connection():
                pass
        _clean_sql_top_level_files(input_snapshot.output_dir)
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

    try:
        with platform_session(settings) as session:
            if lock_db_source_row(session, source_uuid) is None:
                raise SourceRegistrationMissingError(
                    f"数据源登记已不存在: {source_uuid}"
                )
            try:
                latest, missing, changed_reason = inspect_managed_sql_inputs(
                    session, source_uuid, job_uuid, source_name, config
                )
            except Exception as exc:
                latest, missing = None, []
                changed_reason = f"结束复查故障（{type(exc).__name__}）"
            if (
                latest is None
                or latest.markers != input_snapshot.markers
                or latest.fingerprints != input_snapshot.fingerprints
            ):
                ok = False
                input_reason = f"SQL 上游输入在生成期间发生变化：{changed_reason or ','.join(missing)}"
                reason = f"{input_reason}；{reason}" if reason else input_reason
            finish_step(
                session,
                job_uuid,
                SQL_STEP_NAME,
                STATUS_SUCCESS if ok else STATUS_FAILED,
            )
    except Exception as exc:
        logger.error("SQL 状态写回失败（%s）", type(exc).__name__)
        return _failure(
            started,
            f"SQL 产物可能已生成，但作业状态更新失败（{type(exc).__name__}）；请查看日志确认作业状态",
            job_uuid,
        )

    if not ok:
        if run_result is not None:
            generation = run_result.generation
            reason += (
                f"；跳过域：{', '.join(generation.skipped_domains) or '无'}"
                f"；生成失败域：{', '.join(generation.failed_domains) or '无'}"
                f"；零产出域：{', '.join(generation.zero_output_domains) or '无'}"
            )
        return _failure(started, reason, job_uuid)
    generation = run_result.generation
    details = [
        f"生成 {generation.total_generated} 条，保留 {retained} 条，删除 {generation.total_generated - retained} 条",
        "SQL 校验已跳过" if run_result.validation is None else "SQL 校验已执行",
        f"跳过域：{', '.join(generation.skipped_domains) or '无'}",
        f"生成失败域：{', '.join(generation.failed_domains) or '无'}",
        f"零产出域：{', '.join(generation.zero_output_domains) or '无'}",
    ]
    return {
        "success": True,
        "execution_time": time.perf_counter() - started,
        "job_id": str(job_uuid),
        "message": "；".join(details),
    }
