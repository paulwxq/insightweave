"""受管 Domain 步骤的入口准备链；Web 采集与重跑共用（doc 21.1 §5）。

准备链先做入口校验（步骤 1），再分两段：平台库读取段（源校验、工作区归属、
md 步骤行）与本地文件/配置段（md 目录枚举、domain 目录解析、快照构造、
LLM 预检）。失败分类（§5.2）：

- 依赖或配置类失败 → `DbStepConfigError`（源禁用 → `SourceDisabledError`）；
- 平台库连接或读取异常 → 返回「平台库连接或读取失败（…）」失败结果；
- 本地段非 `ValueError`（含 LLM 预检非受控故障）→ `DomainPreparationError`。

三类都在 `start_step` 之前返回，不写 domain 行（§5.6）。会话 2 固定顺序：
锁源行 → 复查 md → 锁下解析生效描述 →（可选）同事务写数据源描述 →
`start_step(..., db_desc=生效值)` 唯一一次 commit；离开会话后才调用 LLM（§5.4）。
"""

import logging
import time
from pathlib import Path

import yaml
from sqlalchemy import func

from backend.data_sources.ddl_step_service import build_managed_output_snapshot
from backend.data_sources.db_step_service import (
    DbStepConfigError,
    SourceDisabledError,
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
from metaweave.core.clean_targets import (
    resolve_domain_output_dir,
    resolve_md_output_dir,
)
from metaweave.core.metadata.domain_generator import DomainGenerator
from metaweave.core.metadata.execution import (
    load_main_config_snapshot,
    set_operation_step,
)
from metaweave.services.llm_config_resolver import resolve_module_llm_config
from metaweave.services.llm_service import LLMService
from metaweave.utils.file_utils import clean_top_level_files
from metaweave.utils.source_id import normalize_source_id

logger = logging.getLogger("backend.data_sources")

DOMAIN_STEP_NAME = "domain"
MD_STEP_FOR_DOMAIN = "md"
DOMAIN_ARTIFACT_NAME = "db_domains.yaml"
MD_NOT_READY = "同一作业的 MD 产物未就绪；请先执行 MD 或指定已有 job_id"
SOURCE_GONE = "未找到指定 source_id 的数据源"

# 哨兵：区分「未保存过描述」与「显式携带的 None」的语义（doc 21.1 §5）
_UNSET: object = object()


class DomainPreparationError(PlatformUnavailableError):
    """Domain 准备阶段故障（本地文件/配置段与 LLM 预检的非受控异常）。

    子类化 `PlatformUnavailableError`（doc 21.1 §5.2）：`collection_run`
    既有 `except PlatformUnavailableError` 分支直接覆盖——`results` 为空时
    重抛，已有结果时以封装消息计入该步 failed；API 侧由更具体的 handler
    按 MRO 先命中，返回 503 `domain_preparation_error`。
    """


def _failure(started: float, message: str, job_id=None) -> dict:
    result = {
        "success": False,
        "execution_time": time.perf_counter() - started,
        "message": message,
    }
    if job_id is not None:
        result["job_id"] = str(job_id)
    return result


def _strip_or_none(value) -> "str | None":
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _resolve_effective_description(session, job_uuid, source_row, requested):
    """锁下解析生效描述（doc 21.1 §5.3）。

    请求携带（非 `_UNSET`）优先；未携带时先用本作业 domain 行的 `db_desc`，
    为空再回退数据源描述。从库里读出的值先 `strip()`、strip 后为空按 NULL——
    与 `generate_from_context(user_description=…)` 收到的参数逐字节一致。
    """
    if requested is not _UNSET:
        return requested
    domain_row = get_step_row(session, job_uuid, DOMAIN_STEP_NAME)
    stored = _strip_or_none(domain_row.db_desc if domain_row is not None else None)
    if stored is not None:
        return stored
    return _strip_or_none(source_row.description)


def verify_domain_artifact(yaml_path: Path) -> "tuple[bool, str]":
    """成功判定（doc 21.1 §5.4）：普通文件 → YAML 可解析为对象 → domains 非空。

    doc 23.1 §5.2 公开化（原私有 ``_verify_domain_output``）：rel 准备链对
    受管 db_domains.yaml 做同款三查，两服务共用一份口径。
    """
    if yaml_path.is_symlink() or not yaml_path.is_file():
        return False, "Domain 产物缺失或不是普通文件"
    try:
        parsed = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 —— 核验失败按结果返回，不抛出
        return False, f"Domain 产物 YAML 无法解析（{type(exc).__name__}）"
    if (
        not isinstance(parsed, dict)
        or not isinstance(parsed.get("domains"), list)
        or not parsed["domains"]
    ):
        return False, "Domain 产物 domains 为空"
    return True, ""


def run_domain_step(
    config_path,
    source_id,
    job_id=None,
    *,
    domain_description=_UNSET,
    sync_domain_description: bool = False,
) -> dict:
    """准备链 → 会话 2 固定顺序登记 → 清理并生成 → 核验 → 写回状态（doc 21.1 §5）。"""
    started = time.perf_counter()
    set_operation_step(DOMAIN_STEP_NAME)
    if job_id is None:
        # 新作业 Domain 单选没有任何工作区可用（与 md 相同）
        raise DbStepConfigError(MD_NOT_READY)
    if sync_domain_description and domain_description is _UNSET:
        raise DbStepConfigError("同步数据源描述必须同时携带 domain_description")

    # ---- 入口校验段（对齐 md 入口，doc 21.1 §5.2 步骤 1） ----
    try:
        source_uuid = normalize_source_id(source_id)
        job_uuid = normalize_source_id(job_id)
    except ValueError as exc:
        raise DbStepConfigError(str(exc)) from None
    try:
        main_config = load_main_config_snapshot(config_path)
    except ValueError as exc:
        raise DbStepConfigError(str(exc)) from None
    except Exception as exc:
        raise DbStepConfigError(
            f"主配置加载或校验失败（{type(exc).__name__}）"
        ) from None
    try:
        settings = load_platform_settings_from_env()
    except ValueError as exc:
        raise DbStepConfigError(str(exc)) from None

    # ---- 平台库读取段（with 内只放数据库操作，兜底只覆盖这一段） ----
    try:
        with platform_session(settings) as session:
            source = get_db_source(session, source_uuid)
            if source is None:
                raise DbStepConfigError(SOURCE_GONE)
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
            md_step = get_step_row(session, job_uuid, MD_STEP_FOR_DOMAIN)
            if md_step is None or md_step.last_status != STATUS_SUCCESS:
                raise DbStepConfigError(MD_NOT_READY)
    except DbStepConfigError:
        raise
    except ValueError as exc:
        raise DbStepConfigError(str(exc)) from None
    except Exception as exc:
        return _failure(started, f"平台库连接或读取失败（{type(exc).__name__}）")

    # ---- 本地文件与配置段（不依赖数据库连接） ----
    stage = "md 目录枚举"
    try:
        md_dir = resolve_md_output_dir(source_uuid, job_uuid)
        md_files = []
        for candidate in sorted(md_dir.glob("*.md")):
            # 跳过非常规文件而不是整步拒绝（doc 21.1 §5.2 最简口径：只要有普通
            # .md 即可生成）；读取侧另有 O_NOFOLLOW 加固（§13.2），被跳过文件
            # 的内容不会进入 LLM 摘要。
            if candidate.is_symlink() or not candidate.is_file():
                logger.warning("跳过非常规 md 文件: %s", candidate)
                continue
            md_files.append(candidate)
        if not md_files:
            raise DbStepConfigError(MD_NOT_READY)
        stage = "domain 目录解析"
        domain_dir = resolve_domain_output_dir(source_uuid, job_uuid)
        stage = "快照构造"
        snapshot = build_managed_output_snapshot(
            main_config,
            source_uuid,
            job_uuid,
            source.db_name,
            source.selected_schemas,
        )
        stage = "LLM 预检"
        try:
            llm_config = resolve_module_llm_config(snapshot, "domain_generation.llm")
            LLMService(llm_config)
        except ValueError as exc:
            raise DbStepConfigError(str(exc)) from None
    except DbStepConfigError:
        raise
    except ValueError as exc:
        raise DbStepConfigError(str(exc)) from None
    except Exception as exc:
        # 只记阶段与异常类型两个安全字段，不带 exc_info、不写异常原文（§5.2）
        logger.error(
            "Domain 准备阶段故障 stage=%s type=%s", stage, type(exc).__name__
        )
        raise DomainPreparationError(
            f"Domain 准备阶段故障（{stage}：{type(exc).__name__}）"
        ) from None

    # ---- 会话 2：锁下解析描述并登记（start_step 唯一一次 commit） ----
    try:
        with platform_session(settings) as session:
            source_row = lock_db_source_row(session, source_uuid)
            if source_row is None:
                raise DbStepConfigError(SOURCE_GONE)
            md_step = get_step_row(session, job_uuid, MD_STEP_FOR_DOMAIN)
            if md_step is None or md_step.last_status != STATUS_SUCCESS:
                raise DbStepConfigError(MD_NOT_READY)
            effective = _resolve_effective_description(
                session, job_uuid, source_row, domain_description
            )
            if sync_domain_description:
                source_row.description = effective
                source_row.updated_at = func.now()
            start_step(
                session,
                job_uuid,
                source_uuid,
                DOMAIN_STEP_NAME,
                db_desc=effective,
            )
    except DbStepConfigError:
        raise
    except SourceRegistrationMissingError:
        raise DbStepConfigError(SOURCE_GONE) from None
    except StepAlreadyRunningError as exc:
        return _failure(started, str(exc), job_uuid)

    # ---- 生成（离开锁会话之后才调用 LLM；§5.4） ----
    yaml_path = domain_dir / DOMAIN_ARTIFACT_NAME
    try:
        clean_top_level_files(domain_dir)
        generator = DomainGenerator(
            config=snapshot,
            yaml_path=str(yaml_path),
            md_context_dir=str(md_dir),
        )
        generated = generator.generate_from_context(user_description=effective)
        generator.write_to_yaml(generated)
    except Exception as exc:
        message = f"Domain 生成失败（{type(exc).__name__}）"
        try:
            with platform_session(settings) as session:
                finish_step(session, job_uuid, DOMAIN_STEP_NAME, STATUS_FAILED)
        except Exception as state_exc:
            message += (
                f"；作业状态更新失败（{type(state_exc).__name__}），"
                "请查看日志确认作业状态"
            )
        return _failure(started, message, job_uuid)

    # ---- 核验 ----
    try:
        ok, reason = verify_domain_artifact(yaml_path)
    except Exception as exc:  # noqa: BLE001 —— 核验异常同样记 failed
        ok, reason = False, f"Domain 生成核验失败（{type(exc).__name__}）"

    # ---- 写回状态（锁下 finish） ----
    try:
        with platform_session(settings) as session:
            if lock_db_source_row(session, source_uuid) is None:
                raise SourceRegistrationMissingError(
                    f"数据源登记已不存在: {source_uuid}"
                )
            finish_step(
                session,
                job_uuid,
                DOMAIN_STEP_NAME,
                STATUS_SUCCESS if ok else STATUS_FAILED,
            )
    except SourceRegistrationMissingError:
        return _failure(
            started,
            "Domain 产物可能已生成，但数据源登记已不存在；"
            f"job_id={job_uuid}，请查看日志确认作业状态",
            job_uuid,
        )
    except Exception as exc:
        logger.error(
            "作业状态更新失败（%s） source_id=%s job_id=%s 产物目录=%s",
            type(exc).__name__,
            source_uuid,
            job_uuid,
            domain_dir,
        )
        return _failure(
            started,
            "Domain 产物可能已生成，但作业状态更新失败"
            f"（{type(exc).__name__}）；job_id={job_uuid}，"
            "请查看日志确认作业状态与产物",
            job_uuid,
        )

    if not ok:
        return _failure(started, reason, job_uuid)
    return {
        "success": True,
        "execution_time": time.perf_counter() - started,
        "message": "受管 Domain 生成完成",
        "job_id": str(job_uuid),
    }
