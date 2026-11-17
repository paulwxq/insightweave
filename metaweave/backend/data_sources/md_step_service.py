"""受管 MD 步骤 worker（doc 19.1 §3/§4/§5、6.2 §7.1）。

MD 只消费当前工作区已成功的 JSON 产物：不解密源库密码、不连源库；使用
无凭据工作区快照。上游就绪判定读取 `json/` 顶层唯一成功采集 marker
（doc 6 §4.1）。输出核验以同一 `OutputFormatter.generate_markdown()`
对缓存限定 JSON 文档重渲染并逐字节比对（doc 19.1 §5），成功清单记录
JSON 产物来源 `source_collection_run_id`。
"""

import io
import logging
import time
from pathlib import Path

from backend.data_sources.ddl_step_service import build_managed_output_snapshot
from backend.data_sources.json_step_service import (
    JSON_MANIFEST_NAME,
    ManagedJsonInputError,
    read_managed_json_input,
)
from backend.data_sources.step_context import (
    DbStepConfigError,
    StepContext,
    StepOutcome,
)
from backend.workspace_files.backups import backup_directory_before_update
from backend.workspace_files.markers import read_collection_marker
from metaweave.core.clean_targets import resolve_json_output_dir, resolve_md_output_dir
from metaweave.core.metadata.formatter import validate_output_numeric_options
from metaweave.core.metadata.generator import (
    MetadataGenerator,
    table_matches_exclusion,
    validate_exclude_tables,
)
from metaweave.core.metadata.generation_config import MetadataGenerationConfig
from metaweave.core.metadata.json_loader import parse_strict_stem
from metaweave.core.metadata.models import normalize_database_object_types
from metaweave.utils.file_utils import atomic_write_json

logger = logging.getLogger("backend.data_sources")

MD_STEP_NAME = "md"
JSON_STEP_FOR_MD = "json"
MD_MANIFEST_NAME = "manifest.json"
_SCOPE_MISMATCH_TEMPLATE = (
    "现有 JSON 画像与当前采集范围不一致；请按当前范围重跑 DDL、JSON 后再运行 {step}"
)
_JSON_NOT_READY = "工作区的 JSON 产物未就绪；请先执行 JSON 采集"


def _write_mode_bytes(text: str) -> bytes:
    """按写盘同源的规则编码期望文本（doc 19.1 §5）。"""
    buffer = io.BytesIO()
    wrapper = io.TextIOWrapper(buffer, encoding="utf-8", newline=None)
    wrapper.write(text)
    wrapper.flush()
    return buffer.getvalue()


def _precheck_generator_config(snapshot: dict) -> None:
    """清理前覆盖生成器构造期解析（doc 19.1 §4.5，无文件/数据库副作用）。"""
    from metaweave.core.metadata.logical_key_detector import LogicalKeyDetector
    from metaweave.core.metadata.profiler import MetadataProfiler

    sampling = snapshot.get("sampling", {})
    if not isinstance(sampling, dict):
        raise ValueError("sampling 必须是对象")
    if "column_statistics" in sampling and not isinstance(
        sampling["column_statistics"], dict
    ):
        raise ValueError("sampling.column_statistics 必须是对象")
    logical_key = snapshot.get("logical_key_detection", {})
    if not isinstance(logical_key, dict):
        raise ValueError("logical_key_detection 必须是对象")
    try:
        if logical_key.get("enabled", True):
            LogicalKeyDetector(logical_key)
        MetadataProfiler(snapshot)
        MetadataGenerationConfig.from_config(snapshot)
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(f"画像/生成器配置解析失败（{type(exc).__name__}）") from None
    output_config = snapshot.get("output", {}) or {}
    if not isinstance(output_config, dict):
        raise ValueError("output 必须是对象")
    validate_output_numeric_options(output_config)


def check_objects_in_scope(
    objects, snapshot: dict, source, step_label: str = "MD"
) -> None:
    """范围预检（doc 19.1 §4.3）：schema/对象类型/排除规则与生成器同规则。"""
    database = snapshot.get("database") or {}
    allowed_types = set(
        normalize_database_object_types(database.get("include_object_types", ["table"]))
    )
    validate_exclude_tables(database)
    selected = set(source.selected_schemas)
    for obj in objects:
        if obj["schema"] not in selected:
            raise DbStepConfigError(_SCOPE_MISMATCH_TEMPLATE.format(step=step_label))
        if obj["object_type"] not in allowed_types:
            raise DbStepConfigError(_SCOPE_MISMATCH_TEMPLATE.format(step=step_label))
        if table_matches_exclusion(
            obj["schema"], obj["object"], database.get("exclude_tables") or []
        ):
            raise DbStepConfigError(_SCOPE_MISMATCH_TEMPLATE.format(step=step_label))


def verify_md_result(result, md_dir: Path, objects, generator, database_name: str):
    """核对受管 MD 成功条件（doc 19.1 §5）：集合一致 + 逐字节重渲染比对。"""
    if not getattr(result, "success", False):
        return False, "MD 生成未全部成功"
    if result.failed_tables:
        return False, f"有 {result.failed_tables} 个对象生成失败"
    if result.processed_tables <= 0:
        return False, "选中范围内没有可生成对象"
    md_outputs = [
        Path(path) for path in result.output_files if str(path).endswith(".md")
    ]
    for path in md_outputs:
        if path.is_symlink() or not path.is_file():
            return False, "存在声称成功但未实际落盘的输出文件"
    declared = {path.resolve() for path in md_outputs}
    if len(declared) != result.processed_tables:
        return False, "MD 输出文件数与成功对象数不一致"
    for path in declared:
        if path.parent != md_dir:
            return False, "存在不在本次受管 MD 目录内的输出文件"
    actual = set()
    for path in md_dir.glob("*.md"):
        if path.is_symlink() or not path.is_file():
            return False, f"受管 MD 目录存在非常规文件: {path.name}"
        actual.add(path.resolve())
    if actual != declared:
        return False, "受管 MD 目录的实际输出文件与声明清单不一致"

    expected_keys = {(obj["schema"], obj["object"]) for obj in objects}
    mapped: dict = {}
    for path in declared:
        parsed = parse_strict_stem(path.stem)
        if parsed is None or parsed[0] != database_name:
            return False, f"MD 文件名不是当前库严格三段式: {path.name}"
        key = (parsed[1], parsed[2])
        if key in mapped or key not in expected_keys:
            return False, f"MD 声明路径无法与 JSON 对象一对一对应: {path.name}"
        mapped[key] = path
    if len(mapped) != len(expected_keys):
        return False, "MD 声明路径无法与 JSON 对象一对一对应"

    # 重渲染逐字节核验：同一格式化器 + 缓存限定的本轮 JSON 文档
    import pandas as pd

    loader = generator.managed_json_loader
    for (schema, obj_name), path in mapped.items():
        metadata = loader.load_cached_object(schema, obj_name)
        metadata.database = database_name
        sample_data = (
            pd.DataFrame(metadata.sample_records) if metadata.sample_records else None
        )
        metadata.column_profiles = {}
        metadata.candidate_logical_primary_keys = []
        metadata.table_profile = None
        expected_text = generator.formatter.generate_markdown(metadata, sample_data)
        if path.read_bytes() != _write_mode_bytes(expected_text):
            return False, f"MD 内容与同一 JSON 重渲染结果不一致: {path.name}"
    return True, ""


def _check_json_products_ready(ctx: StepContext) -> tuple[str, list]:
    """工作区 JSON 前置（doc 6 §4.1）：唯一成功 marker + 产物校验。

    返回 (json 产物的 collection_run_id, 对象清单)。
    """
    json_dir = resolve_json_output_dir(ctx.source_id, ctx.workspace_id)
    marker, error = read_collection_marker(json_dir)
    if marker is None:
        raise DbStepConfigError(error or _JSON_NOT_READY)
    if marker.status != "success":
        raise DbStepConfigError(_JSON_NOT_READY)
    try:
        objects = read_managed_json_input(
            ctx.source_id, ctx.workspace_id, ctx.source.db_name
        )
    except ManagedJsonInputError as exc:
        raise DbStepConfigError(f"工作区的 JSON 产物未就绪：{exc}") from None
    return marker.run_id, objects


def run_md_step(ctx: StepContext) -> StepOutcome:
    """执行受管 MD 步骤：JSON 前置 → 备份 → 生成 → 核验 → 写清单。"""
    started = time.perf_counter()
    json_run_id, objects = _check_json_products_ready(ctx)
    snapshot = build_managed_output_snapshot(
        ctx.main_config,
        ctx.source_id,
        ctx.workspace_id,
        ctx.source.db_name,
        ctx.source.selected_schemas,
    )
    md_dir = resolve_md_output_dir(ctx.source_id, ctx.workspace_id)
    if Path(snapshot["output"]["markdown_directory"]) != md_dir:
        raise DbStepConfigError("受管 MD 输出目录与工作区不一致")
    check_objects_in_scope(objects, snapshot, ctx.source)
    try:
        _precheck_generator_config(snapshot)
    except ValueError as exc:
        raise DbStepConfigError(str(exc)) from None

    backup_directory_before_update(md_dir)
    generator = None
    try:
        generator = MetadataGenerator(ctx.config_path, config=snapshot)
        result = generator.generate(step=MD_STEP_NAME, max_workers=4)
    except Exception as exc:
        logger.error(
            "MD 生成执行失败 type=%s source_id=%s workspace_id=%s",
            type(exc).__name__,
            ctx.source_id,
            ctx.workspace_id,
        )
        return StepOutcome(
            success=False, message=f"MD 生成失败（{type(exc).__name__}）"
        )

    try:
        ok, reason = verify_md_result(
            result, md_dir, objects, generator, ctx.source.db_name
        )
    except Exception as exc:
        ok, reason = False, f"MD 采集核验失败（{type(exc).__name__}）"
    if ok:
        try:
            names = sorted(path.name for path in md_dir.glob("*.md"))
            atomic_write_json(
                {
                    "files": names,
                    "source_collection_run_id": json_run_id,
                },
                md_dir / MD_MANIFEST_NAME,
            )
        except OSError:
            ok = False
            reason = "MD 采集清单写入失败"
    if not ok:
        return StepOutcome(success=False, message=reason)
    json_dir = resolve_json_output_dir(ctx.source_id, ctx.workspace_id)
    input_files = [
        (
            f"json/{path.name}",
            json_run_id,
        )
        for path in sorted(json_dir.glob("*.json"), key=lambda item: item.name)
        if path.name != JSON_MANIFEST_NAME
    ]
    output_files = [
        f"md/{name}" for name in sorted(p.name for p in md_dir.glob("*.md"))
    ]
    output_files.append(f"md/{MD_MANIFEST_NAME}")
    return StepOutcome(
        success=True,
        message=f"受管 MD 生成完成（{result.processed_tables} 个对象，"
        f"{time.perf_counter() - started:.1f}s）",
        input_files=input_files,
        output_files=output_files,
    )
