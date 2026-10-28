"""受管 Dim 步骤 worker（doc 22.1 §5、6.2 §2.1/§5.1）。

步骤标识由 `dim_config` 更名为 `dim`，产物目录 `dim_config/` 更名为
`dim/`；CLI 功能开关 `--generate-dim-config` 与生成器类名保持不变。
上游就绪判定读取 `json/` 顶层唯一成功采集 marker（doc 6 §4.1）。
生成落 `mkstemp` 唯一临时普通文件，核验通过后 `os.replace` 到最终路径。
"""

import logging
import os
import tempfile
import time
from pathlib import Path

import yaml

from backend.data_sources.json_step_service import JSON_MANIFEST_NAME
from backend.data_sources.step_context import (
    DbStepConfigError,
    StepContext,
    StepOutcome,
)
from backend.workspace_files.backups import backup_directory_before_update
from backend.workspace_files.markers import read_collection_marker
from metaweave.core.clean_targets import (
    resolve_dim_output_dir,
    resolve_json_output_dir,
)
from metaweave.core.dim_value.config_generator import DimTableConfigGenerator

logger = logging.getLogger("backend.data_sources")

# 保留旧常量名供既有引用；步骤白名单统一为 dim（doc 6 §2）
DIM_CONFIG_STEP_NAME = "dim"
JSON_STEP_FOR_DIM_CONFIG = "json"
DIM_CONFIG_ARTIFACT_NAME = "dim_tables.yaml"
JSON_NOT_READY = "工作区的 JSON 产物未就绪；请先执行 JSON 采集"


class DimConfigPreparationError(RuntimeError):
    """Dim 准备阶段故障（本地文件段的非受控异常）；503 载体。"""

    def __init__(self, message: str):
        super().__init__(message)


def _discard_tmp(tmp_path: Path) -> None:
    """清理临时文件；清理自身失败只告警。"""
    try:
        tmp_path.unlink(missing_ok=True)
    except OSError:
        logger.warning("Dim 临时文件清理失败 (%s)", tmp_path.name)


def _verify_dim_config_output(yaml_path: Path) -> "tuple[bool, str]":
    """成功判定（doc 22.1 §5.3）：普通文件 → YAML 对象 → databases 为 mapping。"""
    if yaml_path.is_symlink() or not yaml_path.is_file():
        return False, "Dim 产物缺失或不是普通文件"
    try:
        parsed = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return False, f"Dim 产物 YAML 无法解析（{type(exc).__name__}）"
    if not isinstance(parsed, dict) or not isinstance(parsed.get("databases"), dict):
        return False, "Dim 产物 databases 不是对象"
    return True, ""


def _check_json_products_ready(ctx: StepContext) -> tuple[str, list[Path]]:
    """工作区 JSON 前置（doc 6 §4.1）：唯一成功 marker + 可读画像文件。"""
    json_dir = resolve_json_output_dir(ctx.source_id, ctx.workspace_id)
    marker, error = read_collection_marker(json_dir)
    if marker is None:
        raise DbStepConfigError(error or JSON_NOT_READY)
    if marker.status != "success":
        raise DbStepConfigError(JSON_NOT_READY)
    json_files = []
    for candidate in sorted(json_dir.glob("*.json")):
        # 排除清单：manifest.json 是普通文件但不是画像产物
        if candidate.name == JSON_MANIFEST_NAME:
            continue
        if candidate.is_symlink() or not candidate.is_file():
            logger.warning("跳过非常规 json 文件: %s", candidate)
            continue
        json_files.append(candidate)
    if not json_files:
        raise DbStepConfigError(JSON_NOT_READY)
    return marker.run_id, json_files


def run_dim_config_step(ctx: StepContext) -> StepOutcome:
    """执行受管 Dim 步骤：JSON 前置 → 备份 → 生成（临时文件）→ 核验 → 落盘。"""
    started = time.perf_counter()
    json_run_id, json_files = _check_json_products_ready(ctx)
    json_dir = resolve_json_output_dir(ctx.source_id, ctx.workspace_id)
    dim_dir = resolve_dim_output_dir(ctx.source_id, ctx.workspace_id)

    backup_directory_before_update(dim_dir)
    tmp_path = None
    generator = None
    try:
        tmp_fd, tmp_name = tempfile.mkstemp(
            dir=dim_dir, prefix=".dim_tables.", suffix=".tmp"
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
        logger.error(
            "Dim 生成执行失败 type=%s source_id=%s workspace_id=%s",
            type(exc).__name__,
            ctx.source_id,
            ctx.workspace_id,
        )
        return StepOutcome(
            success=False, message=f"Dim 生成失败（{type(exc).__name__}）"
        )

    try:
        ok, reason = _verify_dim_config_output(tmp_path)
    except Exception as exc:  # noqa: BLE001
        ok, reason = False, f"Dim 生成核验失败（{type(exc).__name__}）"
    # 可读性底线（doc 22.1 §5.3）：json_files 非空而无一可解析 → 失败
    if ok and not generator.last_readable_count:
        ok, reason = False, "所有画像 JSON 均无法解析，无法生成 Dim 配置"
    if ok:
        try:
            os.chmod(tmp_path, 0o644)
            os.replace(tmp_path, dim_dir / DIM_CONFIG_ARTIFACT_NAME)
        except OSError as exc:
            ok, reason = False, f"Dim 产物落盘失败（{type(exc).__name__}）"
    if not ok:
        _discard_tmp(tmp_path)
        return StepOutcome(success=False, message=reason)

    databases = result.get("databases", {})
    total_tables = sum(len(db_cfg.get("tables", {})) for db_cfg in databases.values())
    input_files = [(f"json/{path.name}", json_run_id) for path in json_files]
    return StepOutcome(
        success=True,
        message=(
            f"受管 Dim 配置生成完成（{len(databases)} 个数据库、"
            f"{total_tables} 个维度表，{time.perf_counter() - started:.1f}s）"
        ),
        input_files=input_files,
        output_files=[f"dim/{DIM_CONFIG_ARTIFACT_NAME}"],
    )
