"""受管 Domain 步骤 worker（doc 21.1 §5、6.2 §7.2）。

Domain 描述生命周期（doc 6.2 §7.2）：
- 首次生成且无 YAML：本次明确输入（临时覆盖优先于版本默认）→ 均未提供
  时用数据源描述兜底；确定的输入为空时允许无补充描述生成
  （`user_description=None`）。
- 已有有效 YAML：先读取 `database.description`，整目录备份后重跑只更新
  domains 和表归属，保留该描述（不回写数据源描述、不写回作业版本）。
- 已有 YAML 损坏：明确报文件无效（blocked），不得当作未生成过而覆盖。
"""

import logging
import time
from pathlib import Path

import yaml

from backend.data_sources.ddl_step_service import build_managed_output_snapshot
from backend.data_sources.step_context import (
    DbStepConfigError,
    StepContext,
    StepOutcome,
)
from backend.data_sources.step_context import SourceDisabledError  # noqa: F401 兼容导入
from backend.workspace_files.backups import backup_directory_before_update
from backend.workspace_files.markers import read_collection_marker
from metaweave.core.clean_targets import (
    resolve_domain_output_dir,
    resolve_md_output_dir,
)
from metaweave.core.metadata.domain_generator import DomainGenerator
from metaweave.services.llm_config_resolver import resolve_module_llm_config
from metaweave.services.llm_service import LLMService

logger = logging.getLogger("backend.data_sources")

DOMAIN_STEP_NAME = "domain"
MD_STEP_FOR_DOMAIN = "md"
DOMAIN_ARTIFACT_NAME = "db_domains.yaml"
MD_NOT_READY = "工作区的 MD 产物未就绪；请先执行 MD 采集"

# 兼容既有导入（collection_run 曾引用 _UNSET 哨兵；新模型由 step_params 携带）
_UNSET: object = object()


class DomainPreparationError(RuntimeError):
    """Domain 准备阶段故障（本地文件/配置段与 LLM 预检的非受控异常）。

    503 `domain_preparation_error` 的载体；不携带异常原文。
    """

    def __init__(self, message: str):
        super().__init__(message)


def _strip_or_none(value) -> "str | None":
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def verify_domain_artifact(yaml_path: Path) -> "tuple[bool, str]":
    """成功判定（doc 21.1 §5.4）：普通文件 → YAML 可解析为对象 → domains 非空。

    rel 准备链与发布 loader 共用同一口径。
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


def _load_existing_description(
    yaml_path: Path,
) -> tuple[bool, "str | None", "str | None"]:
    """读取现有 db_domains.yaml 的 database.description。

    返回 (exists, description, error)：文件不存在 → (False, None, None)；
    损坏 → (True, None, 错误消息)。
    """
    if not yaml_path.exists():
        return False, None, None
    if yaml_path.is_symlink() or not yaml_path.is_file():
        return True, None, "现有 db_domains.yaml 不是普通文件"
    try:
        parsed = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return True, None, f"现有 db_domains.yaml 无法解析（{type(exc).__name__}）"
    if not isinstance(parsed, dict) or not isinstance(parsed.get("domains"), list):
        return True, None, "现有 db_domains.yaml 结构无效"
    database = parsed.get("database")
    description = None
    if isinstance(database, dict):
        description = _strip_or_none(database.get("description"))
    return True, description, None


def _resolve_first_generation_description(ctx: StepContext) -> "str | None":
    """首次生成输入解析（doc 6.2 §7.2）：明确输入优先，兜底数据源描述。

    `ctx.step_params` 已由运行请求解析合并（临时覆盖优先于版本默认）；
    空白视为无补充描述，不再向下兜底。
    """
    explicit = None
    params = ctx.step_params or {}
    domain_params = params.get("domain")
    if isinstance(domain_params, dict) and "description" in domain_params:
        explicit = _strip_or_none(domain_params.get("description"))
        if explicit is not None:
            return explicit
        # 显式空白 = 无补充描述首次生成，不落回数据源描述
        return None
    return _strip_or_none(ctx.source.description)


def run_domain_step(ctx: StepContext) -> StepOutcome:
    """执行受管 Domain 步骤：MD 前置 → 描述解析 → 备份 → LLM 生成 → 核验。"""
    started = time.perf_counter()
    md_dir = resolve_md_output_dir(ctx.source_id, ctx.workspace_id)
    md_marker, md_error = read_collection_marker(md_dir)
    if md_marker is None:
        raise DbStepConfigError(md_error or MD_NOT_READY)
    if md_marker.status != "success":
        raise DbStepConfigError(MD_NOT_READY)

    stage = "md 目录枚举"
    try:
        md_files = []
        for candidate in sorted(md_dir.glob("*.md")):
            # 跳过非常规文件（doc 21.1 §5.2 最简口径）；读取侧另有 O_NOFOLLOW 加固
            if candidate.is_symlink() or not candidate.is_file():
                logger.warning("跳过非常规 md 文件: %s", candidate)
                continue
            md_files.append(candidate)
        if not md_files:
            raise DbStepConfigError(MD_NOT_READY)
        stage = "domain 目录解析"
        domain_dir = resolve_domain_output_dir(ctx.source_id, ctx.workspace_id)
        yaml_path = domain_dir / DOMAIN_ARTIFACT_NAME
        stage = "快照构造"
        snapshot = build_managed_output_snapshot(
            ctx.main_config,
            ctx.source_id,
            ctx.workspace_id,
            ctx.source.db_name,
            ctx.source.selected_schemas,
        )
        stage = "描述解析"
        yaml_exists, preserved_description, yaml_error = _load_existing_description(
            yaml_path
        )
        if yaml_error is not None:
            # 已有 YAML 损坏：明确报文件无效，不当作未生成过而覆盖（doc 6.2 §7.2）
            raise DbStepConfigError(f"{yaml_error}；请修复或删除后重试")
        if yaml_exists:
            user_description = preserved_description
        else:
            user_description = _resolve_first_generation_description(ctx)
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
        logger.error("Domain 准备阶段故障 stage=%s type=%s", stage, type(exc).__name__)
        raise DomainPreparationError(
            f"Domain 准备阶段故障（{stage}：{type(exc).__name__}）"
        ) from None

    # 重跑前读取需保留的描述已完成；执行整目录备份（旧 marker 随旧产物移入）
    backup_directory_before_update(domain_dir)
    try:
        generator = DomainGenerator(
            config=snapshot,
            yaml_path=str(yaml_path),
            md_context_dir=str(md_dir),
        )
        generated = generator.generate_from_context(user_description=user_description)
        if yaml_exists:
            # 重跑只更新 domains 与表归属，保留原 database.description
            database_node = generated.setdefault("database", {})
            database_node["description"] = preserved_description
        generator.write_to_yaml(generated)
    except Exception as exc:
        logger.error(
            "Domain 生成执行失败 type=%s source_id=%s workspace_id=%s",
            type(exc).__name__,
            ctx.source_id,
            ctx.workspace_id,
        )
        return StepOutcome(
            success=False, message=f"Domain 生成失败（{type(exc).__name__}）"
        )

    try:
        ok, reason = verify_domain_artifact(yaml_path)
    except Exception as exc:  # noqa: BLE001
        ok, reason = False, f"Domain 生成核验失败（{type(exc).__name__}）"
    if not ok:
        return StepOutcome(success=False, message=reason)
    input_files = [
        (f"md/{path.name}", md_marker.run_id)
        for path in sorted(md_files, key=lambda item: item.name)
    ]
    return StepOutcome(
        success=True,
        message=f"受管 Domain 生成完成（{time.perf_counter() - started:.1f}s）",
        input_files=input_files,
        output_files=[f"domain/{DOMAIN_ARTIFACT_NAME}"],
    )
