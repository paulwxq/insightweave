"""受管 Dim Config 步骤的入口准备链；Web 采集与重跑共用（doc 22.1 §5）。

准备链先做入口校验，再分两段：平台库读取段（源校验、工作区归属、json
步骤行）与本地文件段（json 目录枚举——排除 manifest.json 并跳过非常规
文件、dim_config 目录解析）。失败分类（§5.2）：

- 依赖或配置类失败 → `DbStepConfigError`（源禁用 → `SourceDisabledError`）；
- 平台库连接或读取异常 → 返回「平台库连接或读取失败（…）」失败结果；
- 本地段非 `ValueError` → `DimConfigPreparationError`。

三类都在 `start_step` 之前返回，不写 dim_config 行（§5.5）。生成落
`mkstemp` 唯一临时普通文件，核验通过后 `os.replace` 到最终路径（§5.3）。
与 md/domain 的有意差异：不加载主配置快照（生成器只消费两个路径参数）。
"""

import logging
import os
import tempfile
import time
from pathlib import Path

import yaml

from backend.data_sources.db_step_service import (
    DbStepConfigError,
    SourceDisabledError,
)
from backend.data_sources.json_step_service import JSON_MANIFEST_NAME
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
    resolve_dim_config_output_dir,
    resolve_json_output_dir,
)
from metaweave.core.dim_value.config_generator import DimTableConfigGenerator
from metaweave.core.metadata.execution import set_operation_step
from metaweave.utils.file_utils import clean_top_level_files
from metaweave.utils.source_id import normalize_source_id

logger = logging.getLogger("backend.data_sources")

DIM_CONFIG_STEP_NAME = "dim_config"
JSON_STEP_FOR_DIM_CONFIG = "json"
DIM_CONFIG_ARTIFACT_NAME = "dim_tables.yaml"
JSON_NOT_READY = "同一作业的 JSON 产物未就绪；请先执行 JSON 或指定已有 job_id"
SOURCE_GONE = "未找到指定 source_id 的数据源"


class DimConfigPreparationError(PlatformUnavailableError):
    """Dim Config 准备阶段故障（本地文件段的非受控异常）。

    子类化 `PlatformUnavailableError`（doc 22.1 §5.2，doc 21.1 同款）：
    `collection_run` 既有 `except PlatformUnavailableError` 分支直接覆盖——
    `results` 为空时重抛，已有结果时以封装消息计入该步 failed；API 侧由
    更具体的 handler 按 MRO 先命中，返回 503 `dim_config_preparation_error`。
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


def _discard_tmp(tmp_path: Path) -> None:
    """清理临时文件；清理自身失败只告警——不得越过 finish_step 留在 running。"""
    try:
        tmp_path.unlink(missing_ok=True)
    except OSError:
        logger.warning("Dim Config 临时文件清理失败 (%s)", tmp_path.name)


def _verify_dim_config_output(yaml_path: Path) -> "tuple[bool, str]":
    """成功判定（doc 22.1 §5.3）：普通文件 → YAML 可解析为对象 → databases 为 mapping。

    核验对象是 mkstemp 临时文件；空 mapping 合法（零 dim 表，§13.4）。
    """
    if yaml_path.is_symlink() or not yaml_path.is_file():
        return False, "Dim Config 产物缺失或不是普通文件"
    try:
        parsed = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 —— 核验失败按结果返回，不抛出
        return False, f"Dim Config 产物 YAML 无法解析（{type(exc).__name__}）"
    if not isinstance(parsed, dict) or not isinstance(parsed.get("databases"), dict):
        return False, "Dim Config 产物 databases 不是对象"
    return True, ""


def run_dim_config_step(config_path, source_id, job_id=None) -> dict:
    """准备链 → 会话 2 锁下登记 → 清理并生成（落临时文件）→ 核验 →
    os.replace → 写回状态（doc 22.1 §5）。

    `config_path` 仅保持与其他步骤服务一致的签名；本服务不加载主配置
    （生成器不消费配置，输入/输出路径均由受管 resolver 决定，§5）。
    """
    started = time.perf_counter()
    set_operation_step(DIM_CONFIG_STEP_NAME)
    if job_id is None:
        # 新作业单选 dim_config 没有任何工作区可用（与 md/domain 相同）
        raise DbStepConfigError(JSON_NOT_READY)

    # ---- 入口校验段（不加载主配置，doc 22.1 §5.2） ----
    try:
        source_uuid = normalize_source_id(source_id)
        job_uuid = normalize_source_id(job_id)
    except ValueError as exc:
        raise DbStepConfigError(str(exc)) from None
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
            json_step = get_step_row(session, job_uuid, JSON_STEP_FOR_DIM_CONFIG)
            if json_step is None or json_step.last_status != STATUS_SUCCESS:
                raise DbStepConfigError(JSON_NOT_READY)
    except DbStepConfigError:
        raise
    except ValueError as exc:
        raise DbStepConfigError(str(exc)) from None
    except Exception as exc:
        return _failure(started, f"平台库连接或读取失败（{type(exc).__name__}）")

    # ---- 本地文件段（不依赖数据库连接） ----
    stage = "json 目录枚举"
    try:
        json_dir = resolve_json_output_dir(source_uuid, job_uuid)
        json_files = []
        for candidate in sorted(json_dir.glob("*.json")):
            # 排除清单（审核修订 6）：manifest.json 是普通文件但不是画像产物
            if candidate.name == JSON_MANIFEST_NAME:
                continue
            # 跳过非常规文件而不是整步拒绝（§5.2）：只要有普通画像 .json 即可
            if candidate.is_symlink() or not candidate.is_file():
                logger.warning("跳过非常规 json 文件: %s", candidate)
                continue
            json_files.append(candidate)
        if not json_files:
            raise DbStepConfigError(JSON_NOT_READY)
        stage = "dim_config 目录解析"
        dim_config_dir = resolve_dim_config_output_dir(source_uuid, job_uuid)
    except DbStepConfigError:
        raise
    except ValueError as exc:
        raise DbStepConfigError(str(exc)) from None
    except Exception as exc:
        # 只记阶段与异常类型两个安全字段，不带 exc_info、不写异常原文（§5.2）
        logger.error(
            "Dim Config 准备阶段故障 stage=%s type=%s", stage, type(exc).__name__
        )
        raise DimConfigPreparationError(
            f"Dim Config 准备阶段故障（{stage}：{type(exc).__name__}）"
        ) from None

    # ---- 会话 2：锁下复查 json 并登记（start_step 唯一一次 commit） ----
    try:
        with platform_session(settings) as session:
            if lock_db_source_row(session, source_uuid) is None:
                raise DbStepConfigError(SOURCE_GONE)
            json_step = get_step_row(session, job_uuid, JSON_STEP_FOR_DIM_CONFIG)
            if json_step is None or json_step.last_status != STATUS_SUCCESS:
                raise DbStepConfigError(JSON_NOT_READY)
            start_step(session, job_uuid, source_uuid, DIM_CONFIG_STEP_NAME)
    except DbStepConfigError:
        raise
    except SourceRegistrationMissingError:
        raise DbStepConfigError(SOURCE_GONE) from None
    except StepAlreadyRunningError as exc:
        return _failure(started, str(exc), job_uuid)

    # ---- 清理 → 生成（落 mkstemp 唯一临时普通文件，doc 22.1 §5.3） ----
    tmp_path = None
    try:
        clean_top_level_files(dim_config_dir)
        tmp_fd, tmp_name = tempfile.mkstemp(
            dir=dim_config_dir, prefix=".dim_tables.", suffix=".tmp"
        )
        os.close(tmp_fd)
        tmp_path = Path(tmp_name)
        generator = DimTableConfigGenerator(
            json_dir=json_dir, output_path=tmp_path, json_files=json_files
        )
        result = generator.generate()
    except Exception as exc:
        if tmp_path is not None:
            _discard_tmp(tmp_path)
        message = f"Dim Config 生成失败（{type(exc).__name__}）"
        try:
            with platform_session(settings) as session:
                finish_step(session, job_uuid, DIM_CONFIG_STEP_NAME, STATUS_FAILED)
        except Exception as state_exc:
            message += (
                f"；作业状态更新失败（{type(state_exc).__name__}），"
                "请查看日志确认作业状态"
            )
        return _failure(started, message, job_uuid)

    # ---- 核验临时文件，通过才 os.replace 到最终路径 ----
    try:
        ok, reason = _verify_dim_config_output(tmp_path)
    except Exception as exc:  # noqa: BLE001 —— 核验异常同样记 failed
        ok, reason = False, f"Dim Config 生成核验失败（{type(exc).__name__}）"
    # 可读性底线（doc 22.1 §5.3，审核修订 7）：json_files 非空（准备链保证）
    # 而无一可解析 → 失败；区别于"有画像但无维表"的零 dim 成功
    if ok and not generator.last_readable_count:
        ok, reason = False, "所有画像 JSON 均无法解析，无法生成 Dim Config"
    if ok:
        try:
            # mkstemp 产物为 0600，与其他步骤产物的默认权限对齐（§5.3）
            os.chmod(tmp_path, 0o644)
            os.replace(tmp_path, dim_config_dir / DIM_CONFIG_ARTIFACT_NAME)
        except OSError as exc:
            ok, reason = False, f"Dim Config 产物落盘失败（{type(exc).__name__}）"
    if not ok:
        _discard_tmp(tmp_path)

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
                DIM_CONFIG_STEP_NAME,
                STATUS_SUCCESS if ok else STATUS_FAILED,
            )
    except SourceRegistrationMissingError:
        return _failure(
            started,
            "Dim Config 产物可能已生成，但数据源登记已不存在；"
            f"job_id={job_uuid}，请查看日志确认作业状态",
            job_uuid,
        )
    except Exception as exc:
        logger.error(
            "作业状态更新失败（%s） source_id=%s job_id=%s 产物目录=%s",
            type(exc).__name__,
            source_uuid,
            job_uuid,
            dim_config_dir,
        )
        return _failure(
            started,
            "Dim Config 产物可能已生成，但作业状态更新失败"
            f"（{type(exc).__name__}）；job_id={job_uuid}，"
            "请查看日志确认作业状态与产物",
            job_uuid,
        )

    if not ok:
        return _failure(started, reason, job_uuid)
    databases = result.get("databases", {})
    total_tables = sum(len(db_cfg.get("tables", {})) for db_cfg in databases.values())
    return {
        "success": True,
        "execution_time": time.perf_counter() - started,
        "message": (
            f"受管 Dim Config 生成完成（{len(databases)} 个数据库、"
            f"{total_tables} 个维度表）"
        ),
        "job_id": str(job_uuid),
    }
