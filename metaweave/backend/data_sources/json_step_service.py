"""受管 JSON 步骤的入口准备链；Web 采集与重跑共用（doc 18 §3/§4）。

错误类型沿用 `DbStepConfigError` / `SourceDisabledError`（语义同为"采集
准备链错误"）。JSON 只消费同一 `source_id/job_id` 工作区的 DDL 产物：
准备链核对 DDL 状态、清单与登记库名一致；生成后在同一源行锁事务下复查
DDL 标识再写清单并提交成功状态。
"""

import json
import logging
import time
from pathlib import Path

from backend.data_sources.db_step_service import (
    DbStepConfigError,
    SourceDisabledError,
)
from backend.data_sources.ddl_step_service import build_managed_ddl_config
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
from backend.profile_loading.ddl_loader import DDLInputError, read_ddl_definitions
from backend.publishing.repository import lock_db_source_row
from backend.security.source_password import decrypt_password
from metaweave.core.clean_targets import resolve_json_output_dir
from metaweave.core.metadata.db_step import validate_schema_names
from metaweave.core.metadata.execution import (
    load_main_config_snapshot,
    set_operation_step,
)
from metaweave.core.metadata.formatter import validate_output_options
from metaweave.core.metadata.generator import (
    MetadataGenerator,
    validate_exclude_tables,
    validate_json_sampling_method,
)
from metaweave.core.metadata.json_loader import parse_strict_stem
from metaweave.core.metadata.generation_config import MetadataGenerationConfig
from metaweave.core.metadata.json_loader import JsonLoader, JsonLoaderError
from metaweave.utils.file_utils import atomic_write_json, clean_top_level_files
from metaweave.utils.source_id import normalize_source_id

logger = logging.getLogger("backend.data_sources")

JSON_STEP_NAME = "json"
DDL_STEP_FOR_JSON = "ddl"
JSON_MANIFEST_NAME = "manifest.json"
_DDL_NOT_READY = "同一作业的 DDL 产物未就绪；请先执行 DDL 或指定已有 job_id"


class ManagedJsonInputError(ValueError):
    """受管 JSON 产物清单/目录/身份校验失败（doc 19.1 §4 只读输入契约）。"""


def read_managed_json_input(source_id, job_id, database_name: str) -> list:
    """只读校验受管 JSON 产物并返回对象清单（doc 19.1 §4）。

    校验：清单是普通文件且形状合法（``files`` 非空、无重复、均为
    ``.json`` 文件名）；清单与目录全部画像文件（排除清单自身）双向一致；
    每个文件名为当前库严格三段式；再经 ``JsonLoader.list_objects()``
    做 v3 与身份校验。任何失败抛 ``ManagedJsonInputError``，由调用方
    （受管 MD 准备链）转为受控准备错误，不得报成平台库故障。
    """
    json_dir = resolve_json_output_dir(source_id, job_id)
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
    # 逐项类型检查先于 set()（doc 19.1 §4）：files 含不可哈希元素时
    # set() 会以 TypeError 逃逸，被调用方误归为平台库故障
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

    # 逐条先查类型再比较（doc 19.1 §4：符号链接/非普通文件均拒绝）——
    # 只比名字会让指向工作区外的符号链接被 JsonLoader 跟随读取成合法输入。
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


def _failure(started: float, message: str, job_id=None) -> dict:
    result = {
        "success": False,
        "execution_time": time.perf_counter() - started,
        "message": message,
    }
    if job_id is not None:
        result["job_id"] = str(job_id)
    return result


def verify_json_collection(result, json_dir: Path, ddl_rows, database_name: str):
    """核对受管 JSON 成功条件（doc 18 §4）。

    成功须同时满足：`result.success`、无失败对象、处理对象数大于零、声明
    输出与目录实际 `.json` 集合一致且都在目标目录内为普通文件；再按 DDL
    对象所属 schema 复用 `JsonLoader.list_objects()` 做 v3 与身份校验，并把
    文件名 stem 集合与 DDL 清单行做区分大小写的精确比较。零对象有意失败。
    """
    if not getattr(result, "success", False):
        return False, "JSON 生成未全部成功"
    if result.failed_tables:
        # 源库新增对象而同作业 DDL 缺件时逐表失败在此早退（doc 18 §4）：
        # 条件化给出重跑指引，不误导纯 LLM/采样失败的场景
        return False, (
            f"有 {result.failed_tables} 个对象采集失败；"
            "若源库对象或采集范围有变化，请重跑同一作业 DDL 后再执行 JSON"
        )
    if result.processed_tables <= 0:
        return False, "选中范围内没有可采集对象"
    json_outputs = [
        Path(path) for path in result.output_files if str(path).endswith(".json")
    ]
    # 先做不解析路径的检查：符号链接与非普通文件在 resolve() 之前拒绝，
    # 否则循环符号链接会让 Path.resolve() 抛 RuntimeError（doc 16 同款教训）
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
    # v3 结构与身份校验：复用 doc 17 的公开枚举接口（按 DDL 行的 schema）。
    # `_database_files()` 会静默跳过其他库名前缀的文件，完整性已由上面的
    # 目录全集核对兜住（doc 18 §4）。
    loader = JsonLoader(json_dir, database_name=database_name)
    try:
        for schema in sorted({row["schema_name"] for row in ddl_rows}):
            loader.list_objects(schema)
    except JsonLoaderError as exc:
        # 坏 v3/身份不符按本次失败处理，不向调用方抛裸异常（doc 18 验收 3）
        return False, f"JSON 产物未通过 v3 校验（{type(exc).__name__}）"
    expected_stems = {
        f"{row['db_name']}.{row['schema_name']}.{row['object_name']}"
        for row in ddl_rows
    }
    actual_stems = {path.stem for path in json_dir.glob("*.json")}
    if actual_stems != expected_stems:
        return False, (
            "JSON 对象集合与同作业 DDL 清单不一致；请重跑同一作业 DDL 后再执行 JSON"
        )
    return True, ""


def _check_ddl_step_ready(session, job_uuid, source_uuid, database_name: str):
    """准备链的 DDL 前置（doc 18 §3.2）：状态、清单、库名一致。

    返回 DDL 步骤行的 `updated_at` 作为锁下复查标识；不满足时抛
    `DbStepConfigError`（统一"DDL 产物未就绪"口径）。
    """
    ddl_step = get_step_row(session, job_uuid, DDL_STEP_FOR_JSON)
    if ddl_step is None or ddl_step.last_status != STATUS_SUCCESS:
        raise DbStepConfigError(_DDL_NOT_READY)
    try:
        rows, _ddl_dir = read_ddl_definitions(source_uuid, job_uuid)
    except (DDLInputError, OSError) as exc:
        # OSError（并发删除/权限/IO 抖动）与解析错误同属"DDL 产物未就绪"，
        # 不得落入"平台库连接或读取失败"前缀被误报为 503（对齐发布侧
        # publish_service 的捕获惯例，doc 18 §3.2）
        raise DbStepConfigError(
            f"同一作业的 DDL 产物未就绪（{type(exc).__name__}）"
        ) from None
    if any(row["db_name"] != database_name for row in rows):
        raise DbStepConfigError(
            "同一作业的 DDL 库名与登记源当前库名不一致；请重跑 DDL 后再执行 JSON"
        )
    return ddl_step.updated_at, rows


def _ddl_still_unchanged(session, job_uuid, marker) -> bool:
    """锁下复查：DDL 仍为 success 且运行标识未变（doc 18 §4）。"""
    ddl_step = get_step_row(session, job_uuid, DDL_STEP_FOR_JSON)
    return (
        ddl_step is not None
        and ddl_step.last_status == STATUS_SUCCESS
        and ddl_step.updated_at == marker
    )


def run_json_step(config_path, source_id, job_id=None) -> dict:
    """准备链：校验 → 锁下核对 DDL 并登记 running → 清理 → 生成 →
    核验 → 锁下复查 DDL、写清单并提交状态（doc 18 §3/§4）。"""
    started = time.perf_counter()
    set_operation_step(JSON_STEP_NAME)
    if job_id is None:
        # 新作业 JSON 单选：没有任何工作区可用，不伪造 job_id（doc 18 §3.1）
        raise DbStepConfigError(_DDL_NOT_READY)
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

    # ---- 准备链：登记源、DDL 前置、解密、快照与配置预检 ----
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
                # 与 DDL 步骤同款：工作区不存在/归属错误是受控准备错误，
                # 不是平台库故障（doc 18 验收 2）
                raise DbStepConfigError(str(exc)) from None
            ddl_marker, ddl_rows = _check_ddl_step_ready(
                session, job_uuid, source_uuid, source.db_name
            )
            try:
                validate_schema_names(source.selected_schemas)
                password = decrypt_password(source.password_ciphertext)
            except ValueError as exc:
                raise DbStepConfigError(str(exc)) from None
            snapshot = build_managed_ddl_config(main_config, source, password, job_uuid)
            json_dir = resolve_json_output_dir(source_uuid, job_uuid)
            if Path(snapshot["output"]["json_directory"]) != json_dir:
                raise DbStepConfigError("受管 JSON 输出目录与作业工作区不一致")
            # JSON 生成配置预检：非法配置在登记作业与清理之前失败
            # （doc 18 §4；exclude_tables 类型检查见 doc 19.1 §4）
            MetadataGenerationConfig.from_config(snapshot)
            validate_json_sampling_method(snapshot.get("sampling", {}) or {})
            validate_output_options(snapshot.get("output", {}) or {})
            validate_exclude_tables(snapshot.get("database") or {})
    except DbStepConfigError:
        # 含 SourceDisabledError 子类（有独立 API 映射），准备链错误一律放行
        raise
    except ValueError as exc:
        raise DbStepConfigError(str(exc)) from None
    except Exception as exc:
        return _failure(started, f"平台库连接或读取失败（{type(exc).__name__}）")

    # ---- 锁下核对 DDL 并登记 running（同一事务，start_step 的 commit 释放锁）----
    try:
        with platform_session(settings) as session:
            if lock_db_source_row(session, source_uuid) is None:
                raise SourceRegistrationMissingError(source_uuid)
            if not _ddl_still_unchanged(session, job_uuid, ddl_marker):
                raise DbStepConfigError(_DDL_NOT_READY)
            start_step(session, job_uuid, source_uuid, JSON_STEP_NAME)
    except SourceRegistrationMissingError:
        raise DbStepConfigError("未找到指定 source_id 的数据源") from None
    except DbStepConfigError:
        raise
    except StepAlreadyRunningError as exc:
        return _failure(started, str(exc), job_uuid)
    except Exception as exc:
        return _failure(started, f"作业状态登记失败（{type(exc).__name__}）", job_uuid)

    # ---- 清理本次 JSON 目录并生成（受管固定完整集合，doc 18 §4）----
    try:
        clean_top_level_files(json_dir)
        result = MetadataGenerator(config_path, config=snapshot).generate(
            step=JSON_STEP_NAME, max_workers=4
        )
    except Exception as exc:
        message = f"JSON 采集失败（{type(exc).__name__}）"
        try:
            with platform_session(settings) as session:
                finish_step(session, job_uuid, JSON_STEP_NAME, STATUS_FAILED)
        except Exception as state_exc:
            message += (
                f"；作业状态更新失败（{type(state_exc).__name__}），"
                "请查看日志确认作业状态"
            )
        return _failure(started, message, job_uuid)

    # ---- 核验（异常也按失败处理并写回状态，绝不把步骤留在 running）----
    try:
        ok, reason = verify_json_collection(result, json_dir, ddl_rows, source.db_name)
    except Exception as exc:
        ok, reason = False, f"JSON 采集核验失败（{type(exc).__name__}）"

    # ---- 锁下复查 DDL、写清单并提交状态（同一事务，finish 的 commit 释放锁）----
    try:
        with platform_session(settings) as session:
            if lock_db_source_row(session, source_uuid) is None:
                raise SourceRegistrationMissingError(source_uuid)
            ddl_unchanged = _ddl_still_unchanged(session, job_uuid, ddl_marker)
            if ok and ddl_unchanged:
                try:
                    names = sorted({path.name for path in json_dir.glob("*.json")})
                    atomic_write_json({"files": names}, json_dir / JSON_MANIFEST_NAME)
                except OSError:
                    # 写盘失败不污染数据库事务：置失败并在同一事务落定状态，
                    # 不能让 job_info 停在 running 楔死工作区（doc 18 §4/验收 3）
                    ok = False
                    reason = "JSON 采集清单写入失败"
            status = STATUS_SUCCESS if (ok and ddl_unchanged) else STATUS_FAILED
            if ok and not ddl_unchanged:
                reason = (
                    "同一作业的 DDL 已重跑，本次 JSON 产物已失效；"
                    "请重跑 DDL 后再执行 JSON"
                )
            finish_step(session, job_uuid, JSON_STEP_NAME, status)
    except SourceRegistrationMissingError:
        _discard_manifest(json_dir)
        return _failure(
            started,
            "JSON 产物可能已生成，但数据源登记已不存在；"
            f"job_id={job_uuid}，请查看日志确认作业状态",
            job_uuid,
        )
    except Exception as exc:
        # 状态提交失败：删除新清单，避免"有清单但状态未落"的半成品凭据
        _discard_manifest(json_dir)
        logger.error(
            "作业状态更新失败（%s） source_id=%s job_id=%s 产物目录=%s",
            type(exc).__name__,
            source_uuid,
            job_uuid,
            json_dir,
        )
        return _failure(
            started,
            "JSON 产物可能已生成，但作业状态更新失败"
            f"（{type(exc).__name__}）；job_id={job_uuid}，请查看日志确认作业状态与产物",
            job_uuid,
        )

    if not ok or not ddl_unchanged:
        _discard_manifest(json_dir)
        return _failure(started, reason, job_uuid)
    return {
        "success": True,
        "execution_time": time.perf_counter() - started,
        "message": f"受管 JSON 采集完成（{result.processed_tables} 个对象）",
        "job_id": str(job_uuid),
    }


def _discard_manifest(json_dir: Path) -> None:
    """状态未提交成功时移除新清单（doc 18 §4：不做跨资源原子事务）。"""
    manifest = json_dir / JSON_MANIFEST_NAME
    try:
        manifest.unlink(missing_ok=True)
    except OSError:
        logger.warning("JSON 清单删除失败 (%s)", manifest.name)
