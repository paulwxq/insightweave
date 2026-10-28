"""受管 JSON 步骤 worker（doc 18 §3/§4、6.2 §7.1）。

JSON 只消费当前工作区的 DDL 产物：上游就绪判定改为读取 `ddl/` 顶层
唯一成功采集 marker（doc 6 §4.1），文件、清单与登记库名一致检查保留。
"""

import json
import logging
import time
from pathlib import Path

from backend.data_sources.ddl_step_service import build_managed_ddl_config
from backend.data_sources.step_context import (
    DbStepConfigError,
    StepContext,
    StepOutcome,
)
from backend.profile_loading.ddl_loader import DDLInputError, read_ddl_definitions
from backend.workspace_files.backups import backup_directory_before_update
from backend.workspace_files.markers import read_collection_marker
from metaweave.core.clean_targets import resolve_ddl_output_dir, resolve_json_output_dir
from metaweave.core.metadata.formatter import validate_output_options
from metaweave.core.metadata.generator import (
    MetadataGenerator,
    validate_exclude_tables,
    validate_json_sampling_method,
)
from metaweave.core.metadata.generation_config import MetadataGenerationConfig
from metaweave.core.metadata.json_loader import (
    JsonLoader,
    JsonLoaderError,
    parse_strict_stem,
)
from metaweave.utils.file_utils import atomic_write_json

logger = logging.getLogger("backend.data_sources")

JSON_STEP_NAME = "json"
DDL_STEP_FOR_JSON = "ddl"
JSON_MANIFEST_NAME = "manifest.json"
_DDL_NOT_READY = "工作区的 DDL 产物未就绪；请先执行 DDL 采集"


class ManagedJsonInputError(ValueError):
    """受管 JSON 产物清单/目录/身份校验失败（doc 19.1 §4 只读输入契约）。"""


def read_managed_json_input(source_id, workspace_id, database_name: str) -> list:
    """只读校验受管 JSON 产物并返回对象清单（doc 19.1 §4）。

    校验：清单是普通文件且形状合法（``files`` 非空、无重复、均为
    ``.json`` 文件名）；清单与目录全部画像文件（排除清单自身）双向一致；
    每个文件名为当前库严格三段式；再经 ``JsonLoader.list_objects()``
    做 v3 与身份校验。任何失败抛 ``ManagedJsonInputError``。
    """
    json_dir = resolve_json_output_dir(source_id, workspace_id)
    manifest_path = json_dir / JSON_MANIFEST_NAME
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise ManagedJsonInputError("JSON 采集清单缺失或不是普通文件")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ManagedJsonInputError(
            f"JSON 采集清单不可读（{type(exc).__name__}）"
        ) from None
    files = manifest.get("files") if isinstance(manifest, dict) else None
    if (
        not isinstance(files, list)
        or not files
        or any(
            not isinstance(name, str) or not name.endswith(".json") for name in files
        )
    ):
        raise ManagedJsonInputError("JSON 采集清单形状不合法")
    if len(set(files)) != len(files):
        raise ManagedJsonInputError("JSON 采集清单存在重复条目")

    actual: set = set()
    for path in json_dir.glob("*.json"):
        if path.name == JSON_MANIFEST_NAME:
            continue
        if path.is_symlink() or not path.is_file():
            raise ManagedJsonInputError(f"JSON 产物目录存在非常规文件: {path.name}")
        actual.add(path.name)
    if actual != set(files):
        raise ManagedJsonInputError("JSON 采集清单与目录实际文件不一致")

    schemas: set = set()
    for name in files:
        parsed = parse_strict_stem(name.removesuffix(".json"))
        if parsed is None:
            raise ManagedJsonInputError(f"JSON 文件名不是严格三段式: {name}")
        if parsed[0] != database_name:
            raise ManagedJsonInputError(f"JSON 清单包含不属于当前数据库的文件: {name}")
        schemas.add(parsed[1])

    loader = JsonLoader(json_dir, database_name=database_name)
    objects = []
    try:
        for schema in sorted(schemas):
            objects.extend(loader.list_objects(schema))
    except JsonLoaderError as exc:
        raise ManagedJsonInputError(
            f"JSON 产物未通过 v3/身份校验（{type(exc).__name__}）"
        ) from None
    return [
        {
            "schema": obj.schema_name,
            "object": obj.object_name,
            "object_type": obj.object_type,
        }
        for obj in objects
    ]


def verify_json_collection(result, json_dir: Path, ddl_rows, database_name: str):
    """核对受管 JSON 成功条件（doc 18 §4，断代改造后沿用）。"""
    if not getattr(result, "success", False):
        return False, "JSON 生成未全部成功"
    if result.failed_tables:
        return False, (
            f"有 {result.failed_tables} 个对象采集失败；"
            "若源库对象或采集范围有变化，请重跑 DDL 后再执行 JSON"
        )
    if result.processed_tables <= 0:
        return False, "选中范围内没有可采集对象"
    json_outputs = [
        Path(path) for path in result.output_files if str(path).endswith(".json")
    ]
    for path in json_outputs:
        if path.is_symlink() or not path.is_file():
            return False, "存在声称成功但未实际落盘的输出文件"
    declared = {path.resolve() for path in json_outputs}
    if len(declared) != result.processed_tables:
        return False, "JSON 输出文件数与成功对象数不一致"
    for path in (Path(item) for item in result.output_files):
        if str(path).endswith(".json") and path.resolve().parent != json_dir:
            return False, "存在不在本次受管 JSON 目录内的输出文件"
    actual = set()
    for path in json_dir.glob("*.json"):
        if path.is_symlink() or not path.is_file():
            return False, "受管 JSON 目录存在非常规文件"
        actual.add(path.resolve())
    if actual != declared:
        return False, "受管 JSON 目录的实际输出文件与声明清单不一致"
    loader = JsonLoader(json_dir, database_name=database_name)
    try:
        for schema in sorted({row["schema_name"] for row in ddl_rows}):
            loader.list_objects(schema)
    except JsonLoaderError as exc:
        return False, f"JSON 产物未通过 v3 校验（{type(exc).__name__}）"
    expected_stems = {
        f"{row['db_name']}.{row['schema_name']}.{row['object_name']}"
        for row in ddl_rows
    }
    actual_stems = {path.stem for path in json_dir.glob("*.json")}
    if actual_stems != expected_stems:
        return False, ("JSON 对象集合与工作区 DDL 清单不一致；请重跑 DDL 后再执行 JSON")
    return True, ""


def _check_ddl_products_ready(ctx: StepContext) -> tuple[str, list]:
    """工作区 DDL 前置（doc 6 §4.1）：唯一成功 marker + 清单 + 库名一致。

    返回 DDL marker 对应的 collection_run_id 与 DDL 行；不满足时抛
    `DbStepConfigError`（编排层映射 blocked）。
    """
    ddl_dir = resolve_ddl_output_dir(ctx.source_id, ctx.workspace_id)
    marker, error = read_collection_marker(ddl_dir)
    if marker is None:
        raise DbStepConfigError(error or _DDL_NOT_READY)
    if marker.status != "success":
        raise DbStepConfigError(_DDL_NOT_READY)
    try:
        rows, _ddl_dir = read_ddl_definitions(ctx.source_id, ctx.workspace_id)
    except (DDLInputError, OSError) as exc:
        raise DbStepConfigError(
            f"工作区的 DDL 产物未就绪（{type(exc).__name__}）"
        ) from None
    if any(row["db_name"] != ctx.source.db_name for row in rows):
        raise DbStepConfigError(
            "工作区的 DDL 库名与登记源当前库名不一致；请重跑 DDL 后再执行 JSON"
        )
    return marker.run_id, rows


def run_json_step(ctx: StepContext) -> StepOutcome:
    """执行受管 JSON 步骤：DDL 前置 → 备份 → 生成 → 核验 → 写清单。"""
    started = time.perf_counter()
    ddl_run_id, ddl_rows = _check_ddl_products_ready(ctx)
    password = ctx.decrypt_password()
    snapshot = build_managed_ddl_config(
        ctx.main_config, ctx.source, password, ctx.workspace_id
    )
    json_dir = resolve_json_output_dir(ctx.source_id, ctx.workspace_id)
    if Path(snapshot["output"]["json_directory"]) != json_dir:
        raise DbStepConfigError("受管 JSON 输出目录与工作区不一致")
    try:
        MetadataGenerationConfig.from_config(snapshot)
        validate_json_sampling_method(snapshot.get("sampling", {}) or {})
        validate_output_options(snapshot.get("output", {}) or {})
        validate_exclude_tables(snapshot.get("database") or {})
    except ValueError as exc:
        raise DbStepConfigError(str(exc)) from None

    backup_directory_before_update(json_dir)
    try:
        result = MetadataGenerator(ctx.config_path, config=snapshot).generate(
            step=JSON_STEP_NAME, max_workers=4
        )
    except Exception as exc:
        logger.error(
            "JSON 采集执行失败 type=%s source_id=%s workspace_id=%s",
            type(exc).__name__,
            ctx.source_id,
            ctx.workspace_id,
        )
        return StepOutcome(
            success=False, message=f"JSON 采集失败（{type(exc).__name__}）"
        )

    try:
        ok, reason = verify_json_collection(
            result, json_dir, ddl_rows, ctx.source.db_name
        )
    except Exception as exc:
        ok, reason = False, f"JSON 采集核验失败（{type(exc).__name__}）"
    if ok:
        try:
            names = sorted({path.name for path in json_dir.glob("*.json")})
            atomic_write_json({"files": names}, json_dir / JSON_MANIFEST_NAME)
        except OSError:
            ok = False
            reason = "JSON 采集清单写入失败"
    if not ok:
        return StepOutcome(success=False, message=reason)
    output_files = [
        f"json/{name}" for name in sorted(path.name for path in json_dir.glob("*.json"))
    ]
    # 输入清单：DDL 产物文件及其采集来源（doc 6.1 §11.1）
    ddl_dir = resolve_ddl_output_dir(ctx.source_id, ctx.workspace_id)
    input_files = [
        (f"ddl/{path.name}", ddl_run_id)
        for path in sorted(ddl_dir.glob("*.sql"), key=lambda item: item.name)
    ]
    return StepOutcome(
        success=True,
        message=f"受管 JSON 采集完成（{result.processed_tables} 个对象，"
        f"{time.perf_counter() - started:.1f}s）",
        input_files=input_files,
        output_files=output_files,
    )
